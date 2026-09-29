"""Input backends and the single bounded input worker."""
from __future__ import annotations

import heapq
import itertools
import queue
import re
import sys
import threading
from dataclasses import dataclass
from typing import Callable, Optional

from .capture import CaptureTimeout, DeviceLost, run_adb
from .config import AdbConfig, AppConfig
from .decision_engine import Command
from .geometry import to_android_display
from .telemetry import Clock, DurationStats


class InputError(Exception):
    pass


@dataclass
class InputContext:
    cfg: AppConfig
    frame_w: int
    frame_h: int
    capture_is_device: bool


class InputBackend:
    name = "base"
    is_real = False

    def capabilities(self) -> dict:
        return {"name": self.name, "real": self.is_real, "tap": True, "double_jump": False,
                "timed_hold": False, "independent_hold_release": False, "uses_coordinates": False,
                "notes": []}

    def check_ready(self, ctx: Optional[InputContext]) -> list[str]:
        return []

    def display_size(self, ctx: Optional[InputContext]) -> tuple[Optional[int], Optional[int]]:
        return None, None

    def tap(self, action: str, ctx: InputContext, tap_ms: int) -> None:
        raise NotImplementedError

    def hold_start(self, action: str, ctx: InputContext) -> None:
        raise InputError(f"{self.name} does not support independent hold/release")

    def hold_end(self, action: str, ctx: Optional[InputContext]) -> None:
        raise InputError(f"{self.name} does not support independent hold/release")

    def timed_hold(self, action: str, ctx: InputContext, ms: int) -> None:
        raise InputError(f"{self.name} does not support timed hold")

    def release_all(self) -> None:
        pass

    def close(self) -> None:
        self.release_all()


class DryRunInput(InputBackend):
    """Records actions without touching any device."""

    name = "dryrun"

    def __init__(self) -> None:
        self.events: list[tuple[str, str]] = []
        self.held: set[str] = set()
        self._lock = threading.Lock()

    def capabilities(self) -> dict:
        c = super().capabilities()
        c.update(double_jump=True, timed_hold=True, independent_hold_release=True,
                 notes=["simulated: no input is sent"])
        return c

    def _rec(self, kind: str, action: str) -> None:
        with self._lock:
            self.events.append((kind, action))
            if len(self.events) > 1000:
                del self.events[:500]

    def tap(self, action: str, ctx: InputContext, tap_ms: int) -> None:
        self._rec("tap", action)

    def hold_start(self, action: str, ctx: InputContext) -> None:
        self.held.add(action)
        self._rec("down", action)

    def hold_end(self, action: str, ctx: Optional[InputContext]) -> None:
        self.held.discard(action)
        self._rec("up", action)

    def timed_hold(self, action: str, ctx: InputContext, ms: int) -> None:
        self._rec("timed_hold", action)

    def release_all(self) -> None:
        for a in list(self.held):
            self.hold_end(a, None)


_WM_RE = re.compile(r"(Physical|Override) size:\s*(\d+)x(\d+)")
_ORIENT_RE = re.compile(r"SurfaceOrientation:\s*(\d)")


class ADBInput(InputBackend):
    """``adb shell input`` taps. Holds are fixed-duration swipe gestures only.

    ADB cannot hold and later release independently. Killing the local adb
    process does not cancel a gesture that Android already started.
    """

    name = "adb"
    is_real = True

    def __init__(self, adb: AdbConfig) -> None:
        self.adb = adb
        self._display: Optional[tuple[int, int]] = None

    def capabilities(self) -> dict:
        c = super().capabilities()
        c.update(double_jump=True, timed_hold=True, independent_hold_release=False, uses_coordinates=True,
                 notes=["hold = fixed-duration swipe gesture (blocks the input worker for its duration)",
                        "timing shown is adb command completion, not game response"])
        return c

    def _run(self, args: list[str], timeout: float) -> None:
        try:
            res = run_adb(self.adb, args, timeout)
        except (CaptureTimeout, DeviceLost) as exc:
            raise InputError(str(exc)) from exc
        if res.returncode != 0:
            raise InputError(f"adb input failed: {res.stderr.decode(errors='replace').strip()[:200]}")

    def query_display(self) -> tuple[int, int]:
        try:
            res = run_adb(self.adb, ["shell", "wm", "size"], self.adb.command_timeout_s)
        except (CaptureTimeout, DeviceLost) as exc:
            raise InputError(str(exc)) from exc
        sizes = {m.group(1): (int(m.group(2)), int(m.group(3)))
                 for m in _WM_RE.finditer(res.stdout.decode(errors="replace"))}
        if not sizes:
            raise InputError("could not read display size (adb shell wm size)")
        w, h = sizes.get("Override", sizes.get("Physical"))
        try:
            ores = run_adb(self.adb, ["shell", "dumpsys", "input"], self.adb.command_timeout_s * 2)
            m = _ORIENT_RE.search(ores.stdout.decode(errors="replace"))
            if m and m.group(1) in ("1", "3"):
                w, h = h, w
        except (CaptureTimeout, DeviceLost):
            pass
        return w, h

    def display_size(self, ctx: Optional[InputContext]) -> tuple[Optional[int], Optional[int]]:
        if ctx is not None and ctx.capture_is_device:
            return ctx.frame_w, ctx.frame_h  # screencap is in current display orientation
        if self._display is None:
            self._display = self.query_display()
        return self._display

    def check_ready(self, ctx: Optional[InputContext]) -> list[str]:
        if not self.adb.serial:
            return ["ADB input: no device serial selected"]
        try:
            res = run_adb(self.adb, ["get-state"], self.adb.command_timeout_s)
        except (CaptureTimeout, DeviceLost) as exc:
            return [f"ADB input: {exc}"]
        if res.stdout.decode(errors="replace").strip() != "device":
            return [f"ADB input: device {self.adb.serial} is not in 'device' state"]
        self._display = None
        try:
            self.display_size(ctx)
        except InputError as exc:
            return [f"ADB input: {exc}"]
        return []

    def _point(self, action: str, ctx: InputContext) -> tuple[int, int]:
        cfg = ctx.cfg
        pt = cfg.input.jump_point if action == "jump" else cfg.input.slide_point
        dw, dh = self.display_size(ctx)
        assert dw and dh
        return to_android_display(pt, cfg.viewport, ctx.frame_w, ctx.frame_h, dw, dh, ctx.capture_is_device)

    def tap(self, action: str, ctx: InputContext, tap_ms: int) -> None:
        x, y = self._point(action, ctx)
        self._run(["shell", "input", "tap", str(x), str(y)], self.adb.command_timeout_s)

    def timed_hold(self, action: str, ctx: InputContext, ms: int) -> None:
        x, y = self._point(action, ctx)
        self._run(["shell", "input", "swipe", str(x), str(y), str(x), str(y), str(int(ms))],
                  self.adb.command_timeout_s + ms / 1000.0)


class WindowsKeyboardInput(InputBackend):
    """Keyboard input via PyDirectInput (Windows only; imported lazily)."""

    name = "keyboard"
    is_real = True

    def __init__(self) -> None:
        self._pdi = None
        self._import_error: Optional[str] = None
        self.held: set[str] = set()
        if sys.platform != "win32":
            self._import_error = "keyboard backend requires Windows"
            return
        try:
            import pydirectinput  # type: ignore
            pydirectinput.PAUSE = 0
            pydirectinput.FAILSAFE = False
            self._pdi = pydirectinput
        except Exception as exc:  # pragma: no cover - windows only
            self._import_error = f"pydirectinput unavailable: {exc}"

    def capabilities(self) -> dict:
        c = super().capabilities()
        c.update(double_jump=True, independent_hold_release=True, timed_hold=False,
                 notes=["the emulator window must have keyboard focus"])
        if self._import_error:
            c.update(tap=False, double_jump=False, independent_hold_release=False,
                     notes=[self._import_error])
        return c

    def check_ready(self, ctx: Optional[InputContext]) -> list[str]:
        return [f"Keyboard input: {self._import_error}"] if self._import_error else []

    def _key(self, action: str, ctx: Optional[InputContext]) -> str:
        if ctx is None:
            raise InputError("missing context")
        return ctx.cfg.input.jump_key if action == "jump" else ctx.cfg.input.slide_key

    def tap(self, action: str, ctx: InputContext, tap_ms: int) -> None:  # pragma: no cover - windows only
        if not self._pdi:
            raise InputError(self._import_error or "keyboard unavailable")
        key = self._key(action, ctx)
        self._pdi.keyDown(key)
        try:
            threading.Event().wait(tap_ms / 1000.0)
        finally:
            self._pdi.keyUp(key)

    def hold_start(self, action: str, ctx: InputContext) -> None:  # pragma: no cover - windows only
        if not self._pdi:
            raise InputError(self._import_error or "keyboard unavailable")
        key = self._key(action, ctx)
        self._pdi.keyDown(key)
        self.held.add(key)

    def hold_end(self, action: str, ctx: Optional[InputContext]) -> None:  # pragma: no cover
        if ctx is None:
            self.release_all()
            return
        key = self._key(action, ctx)
        if self._pdi and key in self.held:
            self._pdi.keyUp(key)
        self.held.discard(key)

    def release_all(self) -> None:
        if self._pdi:
            for key in list(self.held):
                try:
                    self._pdi.keyUp(key)
                except Exception:
                    pass
        self.held.clear()


def build_input(cfg: AppConfig) -> InputBackend:
    if cfg.input.backend == "adb":
        return ADBInput(cfg.adb)
    if cfg.input.backend == "keyboard":
        return WindowsKeyboardInput()
    return DryRunInput()


# ---------------------------------------------------------------------- worker
class InputWorker:
    """One worker thread, one bounded queue. Expired commands are dropped."""

    def __init__(self, clock: Clock, maxsize: int,
                 select_backend: Callable[[Command], InputBackend],
                 may_execute: Callable[[Command], Optional[str]],
                 get_context: Callable[[], Optional[InputContext]],
                 on_error: Callable[[Exception], None],
                 on_executed: Callable[[Command, InputBackend, float], None]) -> None:
        self.clock = clock
        self.q: "queue.Queue[Command]" = queue.Queue(maxsize=maxsize)
        self.select_backend = select_backend
        self.may_execute = may_execute
        self.get_context = get_context
        self.on_error = on_error
        self.on_executed = on_executed
        self.dispatch_ms = DurationStats()
        self.expired = 0
        self.dropped = 0
        self.rejected = 0
        self.executed = 0
        self._sched: list[tuple[float, int, str, object]] = []  # (due, seq, kind, payload)
        self._seq = itertools.count()
        self._lock = threading.Lock()
        self._held: dict[str, InputBackend] = {}
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._generation = 0

    @property
    def depth(self) -> int:
        return self.q.qsize() + len(self._sched)

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="input", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout)
        self.clear_and_release()

    def submit(self, cmd: Command) -> bool:
        try:
            self.q.put_nowait(cmd)
            return True
        except queue.Full:
            try:
                self.q.get_nowait()
                self.dropped += 1
            except queue.Empty:
                pass
            try:
                self.q.put_nowait(cmd)
                return True
            except queue.Full:
                self.dropped += 1
                return False

    def clear(self) -> int:
        n = 0
        with self._lock:
            self._generation += 1
            while True:
                try:
                    self.q.get_nowait()
                    n += 1
                except queue.Empty:
                    break
            self._sched = [s for s in self._sched if s[2] == "release"]
        return n

    def release_held(self) -> None:
        with self._lock:
            held = dict(self._held)
            self._held.clear()
            self._sched = [s for s in self._sched if s[2] != "release"]
        ctx = self.get_context()
        for action, backend in held.items():
            try:
                backend.hold_end(action, ctx)
            except Exception:
                pass
            try:
                backend.release_all()
            except Exception:
                pass

    def clear_and_release(self) -> int:
        n = self.clear()
        self.release_held()
        return n

    def _schedule(self, due: float, kind: str, payload: object) -> None:
        with self._lock:
            heapq.heappush(self._sched, (due, next(self._seq), kind, payload))

    def _pop_due(self, now: float) -> Optional[tuple[str, object]]:
        with self._lock:
            if self._sched and self._sched[0][0] <= now:
                _, _, kind, payload = heapq.heappop(self._sched)
                return kind, payload
            return None

    def _next_wait(self, now: float) -> float:
        with self._lock:
            if self._sched:
                return max(0.0, min(0.05, self._sched[0][0] - now))
        return 0.05

    def process_once(self, wait: float = 0.0) -> None:
        """Handle due scheduled items, then at most one queued command (used by tests too)."""
        now = self.clock.now()
        while True:
            item = self._pop_due(now)
            if item is None:
                break
            kind, payload = item
            if kind == "release":
                action = str(payload)
                with self._lock:
                    backend = self._held.pop(action, None)
                if backend is not None:
                    try:
                        backend.hold_end(action, self.get_context())
                    except Exception as exc:
                        self._fail(exc)
            elif kind == "command":
                assert isinstance(payload, Command)
                self._execute(payload)
        try:
            cmd = self.q.get(timeout=wait) if wait > 0 else self.q.get_nowait()
        except queue.Empty:
            return
        self._execute(cmd)

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.process_once(self._next_wait(self.clock.now()))
            except Exception as exc:  # never let the worker die silently
                self._fail(exc)

    def _fail(self, exc: Exception) -> None:
        self.clear_and_release()
        self.on_error(exc)

    def _execute(self, cmd: Command) -> None:
        now = self.clock.now()
        if now > cmd.deadline:
            self.expired += 1
            return
        reason = self.may_execute(cmd)
        if reason:
            self.rejected += 1
            return
        ctx = self.get_context()
        if ctx is None:
            self.rejected += 1
            return
        backend = self.select_backend(cmd)
        gen = self._generation
        t0 = self.clock.now()
        try:
            if cmd.kind == "hold":
                backend.hold_start(cmd.action, ctx)
                with self._lock:
                    self._held[cmd.action] = backend
                self._schedule(t0 + cmd.hold_ms / 1000.0, "release", cmd.action)
            elif cmd.kind == "timed_hold":
                backend.timed_hold(cmd.action, ctx, cmd.hold_ms)
            else:
                backend.tap(cmd.action, ctx, cmd.tap_ms)
        except Exception as exc:
            self._fail(exc if isinstance(exc, InputError) else InputError(str(exc)))
            return
        dt = (self.clock.now() - t0) * 1000.0
        self.dispatch_ms.add(dt)
        self.executed += 1
        self.on_executed(cmd, backend, dt)
        if cmd.repeat_after_ms is not None and gen == self._generation:
            follow = Command(id=cmd.id, action=cmd.action, kind="tap", created=cmd.created,
                             deadline=self.clock.now() + cmd.repeat_after_ms / 1000.0 + (cmd.deadline - cmd.created),
                             frame_id=cmd.frame_id, obstacle_id=cmd.obstacle_id, tap_ms=cmd.tap_ms,
                             repeat_after_ms=None, test=cmd.test)
            self._schedule(self.clock.now() + cmd.repeat_after_ms / 1000.0, "command", follow)
