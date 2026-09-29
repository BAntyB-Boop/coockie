"""Autoplay: classify the current frame against user-taught screens, tap through menus, run the vision bot in-level.

Safety: `danger` screens stop the session (never tapped); nothing here ever buys anything; real taps need the ADB
backend, honour dry-run and the emergency-stop file. No locks in Autoplay (the controller calls `stop` under its lock).
"""
from __future__ import annotations

import json
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import cv2
import numpy as np

from .capture import run_adb
from .config import DATA_DIR, NormPoint, atomic_write_json
from .geometry import frame_to_viewport_norm, to_android_display
from .schemas import ROLES, AutoplayStart

PRIORITY = ROLES[:10]  # map/roll/item are sub-elements, never classified
ACTION_COOLDOWN_S, ROLL_WAIT_S, STUCK_S, MAX_SWIPES, RUN_GRACE_S, MARGIN = 1.0, 2.0, 60.0, 10, 1.0, 24


class Screens:
    """Taught entries (`screens.json` + `<name>.png` crops) and the template matcher."""

    def __init__(self, dir: Path) -> None:
        self.dir = Path(dir)  # not created until the first save
        self._lock = threading.Lock()  # guards the cache only, never calls out
        self._es: Optional[list[dict]] = None
        self._tpl: dict[str, np.ndarray] = {}

    def entries(self) -> list[dict]:
        with self._lock:
            if self._es is None:
                p = self.dir / "screens.json"
                es = json.loads(p.read_text(encoding="utf-8")) if p.exists() else []
                tpl = {}
                for e in es:
                    f = self.dir / f"{e['name']}.png"
                    t = cv2.imdecode(np.frombuffer(f.read_bytes(), np.uint8), cv2.IMREAD_GRAYSCALE) if f.exists() else None
                    if t is not None:
                        tpl[e["name"]] = t
                self._tpl, self._es = tpl, [e for e in es if e["name"] in tpl]  # swap together: find() reads unlocked
            return list(self._es)

    def get(self, name: Optional[str]) -> Optional[dict]:
        return next((e for e in self.entries() if e["name"] == name), None)

    def save(self, entry: dict, img: np.ndarray) -> dict:
        h, w = img.shape[:2]
        x, y, bw, bh = entry["box"]
        if x + bw > w or y + bh > h:
            raise ValueError(f"box outside the {w}x{h} frame")
        if entry["tap"] and not (0 <= entry["tap"][0] < w and 0 <= entry["tap"][1] < h):
            raise ValueError(f"tap outside the {w}x{h} frame")
        crop = img[y:y + bh, x:x + bw]
        if crop.std() < 2:
            raise ValueError("box has no detail to match (flat colour)")
        # names are case-insensitive: <name>.png is one file on Windows, so a differing-case entry is replaced
        es = [e for e in self.entries() if e["name"].lower() != entry["name"].lower()] + [entry]
        self.dir.mkdir(parents=True, exist_ok=True)
        (self.dir / f"{entry['name']}.png").write_bytes(cv2.imencode(".png", crop)[1].tobytes())
        atomic_write_json(self.dir / "screens.json", es)  # type: ignore[arg-type]
        with self._lock:
            self._es = None
        return entry

    def delete(self, name: str) -> bool:
        es = self.entries()
        if not any(e["name"] == name for e in es):
            return False
        rest = [e for e in es if e["name"] != name]
        atomic_write_json(self.dir / "screens.json", rest)  # type: ignore[arg-type]
        if not any(e["name"].lower() == name.lower() for e in rest):  # png may be shared by a differing-case name
            (self.dir / f"{name}.png").unlink(missing_ok=True)
        with self._lock:
            self._es = None
        return True

    def find(self, gray: np.ndarray, e: dict) -> tuple[float, tuple[int, int]]:
        """Best (score, top-left) of the entry's template; map = whole frame, others = box +- MARGIN."""
        t = self._tpl.get(e["name"])
        if t is None:  # deleted mid-session
            return 0.0, (0, 0)
        H, W = gray.shape
        x, y, w, h = e["box"]
        x0, y0, x1, y1 = (0, 0, W, H) if e["role"] == "map" else (
            max(0, x - MARGIN), max(0, y - MARGIN), min(W, x + w + MARGIN), min(H, y + h + MARGIN))
        if x1 - x0 < t.shape[1] or y1 - y0 < t.shape[0]:
            return 0.0, (0, 0)
        _, s, _, (lx, ly) = cv2.minMaxLoc(cv2.matchTemplate(gray[y0:y1, x0:x1], t, cv2.TM_CCOEFF_NORMED))
        return (float(s) if np.isfinite(s) else 0.0), (x0 + lx, y0 + ly)

    def hit(self, gray: np.ndarray, e: Optional[dict]) -> Optional[tuple[int, int]]:
        """Tap point (follows the found template) if the entry matches, else None."""
        if e is None:  # entry deleted mid-session
            return None
        s, (mx, my) = self.find(gray, e)
        if s < e["threshold"]:
            return None
        x, y, w, h = e["box"]
        return (mx + e["tap"][0] - x, my + e["tap"][1] - y) if e["tap"] else (mx + w // 2, my + h // 2)

    def classify(self, gray: np.ndarray) -> tuple[Optional[str], Optional[dict], Optional[tuple[int, int]]]:
        es = self.entries()
        for role in PRIORITY:
            for e in es:
                if e["role"] == role and (tap := self.hit(gray, e)) is not None:
                    return role, e, tap
        return None, None, None

    def scores(self, gray: np.ndarray) -> dict[str, float]:
        return {e["name"]: round(self.find(gray, e)[0], 3) for e in self.entries()}


class ControllerIO:
    """Frame px -> viewport norm -> display px, sent with `adb shell input`. Dry-run only logs."""

    def __init__(self, ctrl) -> None:
        self.c = ctrl

    def _send(self, name: str, pts: list[tuple[int, int]], *tail: str) -> None:
        c = self.c
        cfg = c.store.get()
        if c._estop_active():
            raise RuntimeError("emergency stop file present")
        if cfg.safety.dry_run:
            c.logs.add("info", f"[autoplay dry-run] {name} " + " ".join([f"{x},{y}" for x, y in pts] + list(tail)))
            return
        ctx = c.input_context()
        if ctx is None or cfg.input.backend != "adb":
            raise RuntimeError("autoplay real input needs the ADB backend and a frame")
        dw, dh = c.real.display_size(ctx)
        args: list[str] = []
        for x, y in pts:
            if ctx.capture_is_device:  # frame == display: skip the viewport clamp (taught taps may lie outside it)
                args += [str(min(dw - 1, max(0, round(x * dw / ctx.frame_w)))),
                         str(min(dh - 1, max(0, round(y * dh / ctx.frame_h))))]
                continue
            nx, ny = frame_to_viewport_norm(x, y, cfg.viewport, ctx.frame_w, ctx.frame_h)
            args += map(str, to_android_display(NormPoint(x=nx, y=ny), cfg.viewport, ctx.frame_w, ctx.frame_h,
                                                dw, dh, False))
        r = run_adb(cfg.adb, ["shell", "input", name, *args, *tail], cfg.adb.command_timeout_s)
        if r.returncode:
            raise RuntimeError(f"adb input {name} failed: {r.stderr.decode(errors='replace')[:200]}")

    def tap(self, x: int, y: int) -> None:
        self._send("tap", [(x, y)])

    def swipe(self, x1: int, y1: int, x2: int, y2: int) -> None:
        self._send("swipe", [(x1, y1), (x2, y2)], "300")

    def back(self) -> None:
        self._send("keyevent", [], "4")

    def vision(self, on: bool) -> list[str]:
        if on:
            return self.c.start()[1]
        self.c.pause("autoplay: menu screen")
        return []

    def alert(self, msg: str, img: np.ndarray) -> None:
        d = DATA_DIR / "stuck"
        d.mkdir(parents=True, exist_ok=True)
        (d / (datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ") + ".png")).write_bytes(
            cv2.imencode(".png", img)[1].tobytes())
        self.c.logs.add("warning", f"autoplay alert: {msg}")

    def log(self, msg: str) -> None:
        self.c.logs.add("info", msg)


class Autoplay:
    def __init__(self, ctrl, io=None, root: Optional[Path] = None) -> None:
        self.c = ctrl
        self.io = io or ControllerIO(ctrl)
        self.screens = Screens(root or DATA_DIR / "screens")
        self.thread: Optional[threading.Thread] = None
        self.active = self.vision_on = self.alert = False
        self.screen, self.message, self.map = "", "idle", None
        self.loops_done = self.loops = self.rolls = 0
        self.wait_until = 0.0
        self._claim, self._starting = threading.Lock(), False  # guards check-and-claim only, never calls out

    def status(self) -> dict:
        return {"active": self.active, "screen": self.screen, "loops_done": self.loops_done, "loops": self.loops,
                "rolls": self.rolls, "map": self.map, "message": self.message, "alert": self.alert,
                "wait_s": round(max(0.0, self.wait_until - time.monotonic()), 1) if self.active else 0.0}

    # ------------------------------------------------------------ session
    def start(self, p: AutoplayStart) -> list[str]:
        with self._claim:
            if self.active or self._starting or (self.thread and self.thread.is_alive()):
                return ["autoplay already active"]
            self._starting = True
        try:
            return self._start(p)
        finally:
            self._starting = False

    def _start(self, p: AutoplayStart) -> list[str]:
        c, sc = self.c, self.screens
        es = sc.entries()
        r = [f"teach a '{role}' screen first" for role in ("running", "result") if not any(e["role"] == role for e in es)]
        if p.map is not None and (sc.get(p.map) or {}).get("role") != "map":
            r.append(f"map '{p.map}' is not a taught map screen")
        r += [f"item '{n}' is not a taught item" for n in p.items if (sc.get(n) or {}).get("role") != "item"]
        if p.items and p.roll_cap > 0 and not any(e["role"] == "roll" for e in es):
            r.append("teach a 'roll' screen (or set roll cap 0)")
        if c.macro.player.is_playing or c.macro.recorder.is_recording:
            r.append("stop the macro first")
        if r:
            return r
        prof = (sc.get(p.map) or {}).get("profile")
        if prof:
            try:
                c.update_config(c.store.read_profile(prof))
            except (OSError, ValueError) as exc:  # missing file / invalid profile
                return [f"profile '{prof}': {exc}"]
        r = c.validate_start(full=True)
        cfg = c.store.get()
        if not r and not cfg.safety.dry_run and cfg.input.backend != "adb":
            r = ["autoplay real input needs the ADB backend (or enable dry-run)"]
        if r:
            return r
        self.io.vision(False)  # a bot already RUNNING/READY must not tap menus: autoplay owns vision from here
        self.configure(p)
        self.thread = threading.Thread(target=self._run, name="autoplay", daemon=True)
        self.thread.start()
        return []

    def configure(self, p: AutoplayStart) -> None:
        self.items, self.roll_cap, self.swipe = p.items, p.roll_cap, p.swipe
        self.heart_wait_min, self.unknown_back_s = p.heart_wait_min, p.unknown_back_s
        self.map, self.loops, self.loops_done, self.rolls, self.swipes = p.map, p.loops, 0, 0, 0
        self.counted, self.backed, self.unknown_since, self.last_running_seen = False, False, None, float("-inf")
        self.wait_until, self.screen, self.message, self.alert, self.active = 0.0, "", "started", False, True

    def stop(self, reason: str, alert: bool = False) -> None:
        if not self.active:
            return
        self.active, self.message, self.alert = False, reason, alert
        try:
            self.io.log(f"autoplay stopped: {reason}")
            self.vision_on = False
            self.io.vision(False)  # always: also pauses a bot the user started mid-session (no-op if not running)
        except Exception:  # ponytail: stop must never raise into controller.stop/pause
            pass

    def _run(self) -> None:
        last = 0
        while self.active:
            f = self.c.latest.wait_newer(last, 0.5)
            if f:
                last = f.id
                self.step(f.image, time.monotonic())
            time.sleep(0.25)

    # --------------------------------------------------------------- step
    def step(self, img: np.ndarray, now: float) -> None:
        if not self.active or now < self.wait_until:
            return
        try:
            self._step(img, now)
        except Exception as exc:  # adb failure, disk, vision start...
            self.stop(str(exc) or type(exc).__name__, True)

    def _halt(self, msg: str, img: np.ndarray) -> None:
        self.stop(msg, True)
        self.io.alert(msg, img)

    def _vision(self, on: bool, img: np.ndarray) -> None:
        if on and self.vision_on:  # off is always re-asserted: pauses a bot the user started mid-session
            return
        if not on:
            self.vision_on = False
            self.io.vision(False)
            return
        r = self.io.vision(True)
        if r:
            return self._halt("vision start blocked: " + "; ".join(r), img)
        self.vision_on = True
        if not self.active:  # stopped while starting
            self.vision_on = False
            self.io.vision(False)

    def _step(self, img: np.ndarray, now: float) -> None:
        io, sc = self.io, self.screens
        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        role, e, tap = sc.classify(gray)
        if role is None:
            if self.vision_on and now - self.last_running_seen < RUN_GRACE_S:
                return  # marker flicker mid-run
            self._vision(False, img)
            if self.unknown_since is None:
                self.unknown_since = now
            t = now - self.unknown_since
            if t >= STUCK_S:
                self._halt("stuck: unknown screen 60s", img)
            elif t >= self.unknown_back_s and not self.backed:
                io.back()
                self.backed = True
            return
        self.unknown_since, self.backed, self.screen = None, False, e["name"]
        if role != "running":
            self._vision(False, img)
        if role in ("danger", "shop"):
            self._halt(f"{role} screen {e['name']}", img)
        elif role == "no_hearts":
            io.tap(*tap)
            self.wait_until = now + self.heart_wait_min * 60
            self.message = f"out of hearts, waiting {self.heart_wait_min:g} min"
        elif role in ("tap", "ready", "revive"):
            io.tap(*tap)
            self.wait_until = now + ACTION_COOLDOWN_S
            if role == "revive":
                self.message = f"revive choice {e['name']} tapped"
        elif role == "result":
            if not self.counted:
                self.loops_done, self.counted = self.loops_done + 1, True
            if self.loops_done >= self.loops:
                return self.stop(f"done {self.loops_done}/{self.loops}")
            io.tap(*tap)
            self.wait_until = now + ACTION_COOLDOWN_S
        elif role == "item_screen":
            if self.items and self.rolls < self.roll_cap and not any(sc.hit(gray, sc.get(n)) for n in self.items):
                rt = sc.hit(gray, next(x for x in sc.entries() if x["role"] == "roll"))
                if rt is None:
                    return self._halt("roll button not found", img)
                io.tap(*rt)
                self.rolls += 1
                self.wait_until = now + ROLL_WAIT_S
            else:
                io.tap(*tap)  # START
                self.wait_until = now + ACTION_COOLDOWN_S
        elif role == "map_select":
            if self.map is None:
                return self._halt("map select screen but no map chosen", img)
            mt = sc.hit(gray, sc.get(self.map))
            if mt:
                io.tap(*mt)
                self.swipes = 0
            elif self.swipes < MAX_SWIPES:
                h, w = img.shape[:2]
                io.swipe(*(self.swipe or (int(.8 * w), h // 2, int(.2 * w), h // 2)))
                self.swipes += 1
            else:
                return self._halt("map not found after 10 swipes", img)
            self.wait_until = now + ACTION_COOLDOWN_S
        elif role == "running":
            self.last_running_seen, self.counted, self.rolls, self.swipes = now, False, 0, 0
            self._vision(True, img)
