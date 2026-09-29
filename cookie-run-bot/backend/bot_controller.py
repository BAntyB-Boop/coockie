"""The single bot controller: Capture -> Latest Frame -> Vision -> Decision -> Input.

Preview encoding and telemetry run on their own threads/tasks and never block
the control loop. Only one controller may exist per process.
"""
from __future__ import annotations

import logging
import sys
import threading
import time
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Callable, Optional

from . import PROTOCOL_VERSION
from .autoplay import Autoplay
from .capture import CaptureBackend, CaptureWorker, LatestFrame, build_capture
from .config import (ESTOP_FILE, AppConfig, CalibrationSignature, ConfigStore, merge_client_config)
from .decision_engine import Command, DecisionEngine, new_command
from .geometry import roi_pixel_problems
from .input_handler import (DryRunInput, InputBackend, InputContext, InputWorker, build_input)
from .macro import MacroEvent, MacroManager
from .telemetry import Clock, DurationStats, LogBuffer, RateMeter
from .vision_engine import VisionEngine, VisionResult, encode_jpeg, render_view

log = logging.getLogger("cookiebot")
VIEWS = ("annotated", "raw", "mask")


class State(str, Enum):
    STOPPED = "STOPPED"
    READY = "READY"
    RUNNING = "RUNNING"
    PAUSED = "PAUSED"
    ERROR = "ERROR"


_GUARD = threading.Lock()
_ACTIVE = 0


def active_controllers() -> int:
    return _ACTIVE


class BotController:
    def __init__(self, store: ConfigStore, clock: Optional[Clock] = None,
                 capture_factory: Optional[Callable[[AppConfig], CaptureBackend]] = None,
                 input_factory: Optional[Callable[[AppConfig], InputBackend]] = None,
                 estop_file: Optional[Path] = ESTOP_FILE, enable_hotkey: bool = False) -> None:
        global _ACTIVE
        with _GUARD:
            if _ACTIVE:
                raise RuntimeError("only one BotController may exist per server process")
            _ACTIVE += 1
        self._released = False
        self.store = store
        self.clock = clock or Clock()
        self.capture_factory = capture_factory or (lambda c: build_capture(c.capture, c.adb))
        self.input_factory = input_factory or build_input
        self.estop_file = estop_file
        self.enable_hotkey = enable_hotkey
        self.logs = LogBuffer(sink=lambda lvl, m: log.log(getattr(logging, lvl.upper(), logging.INFO), m))
        self.latest = LatestFrame()
        self.vision = VisionEngine()
        self.decision = DecisionEngine()
        self.dry = DryRunInput()
        self.real: InputBackend = self.input_factory(store.get())
        self._lock = threading.RLock()
        self.state = State.STOPPED
        self.state_reason = "stopped"
        self.last_error: Optional[str] = None
        self.process_rate = RateMeter(clock=self.clock)
        self.preview_rate = RateMeter(clock=self.clock)
        self.vision_ms = DurationStats()
        self.frame_age_ms = DurationStats()
        self.last_detected: dict = {}
        self.last_executed: dict = {}
        self.last_suppressed: dict = {}
        self.decision_notes: list[str] = []
        self._pair: Optional[tuple] = None  # (Frame, VisionResult)
        self._frame_dims: Optional[tuple[int, int]] = None
        self._previews: dict[str, tuple[int, dict, bytes]] = {}
        self._views_wanted: dict[str, int] = {}
        self.event_clients = 0
        self._shutdown = threading.Event()
        self._threads: list[threading.Thread] = []
        self._capture: Optional[CaptureWorker] = None
        self._estop_checked = 0.0
        self._estop_present = False
        self.input = self._make_input_worker()
        self.macro = MacroManager(input_dispatch=self._macro_dispatch)
        self.autoplay = Autoplay(self)
        _init_cfg = store.get()
        self.macro.recorder.configure_keys(_init_cfg.input.jump_key, _init_cfg.input.slide_key)

    # ------------------------------------------------------------ lifecycle
    def _make_input_worker(self) -> InputWorker:
        return InputWorker(self.clock, self.store.get().input.queue_size, self._select_backend,
                           self._may_execute, self.input_context, self._on_input_error, self._on_executed)

    def start_services(self) -> None:
        self._start_capture()
        self.input.start()
        self.macro.start_hotkeys()
        for name, fn in (("processing", self._process_loop), ("preview", self._preview_loop)):
            t = threading.Thread(target=fn, name=name, daemon=True)
            t.start()
            self._threads.append(t)
        if self.enable_hotkey and sys.platform == "win32":
            t = threading.Thread(target=self._hotkey_loop, name="estop-hotkey", daemon=True)
            t.start()
            self._threads.append(t)
        self.logs.add("info", "services started (real input disabled: dry-run)")

    def _start_capture(self) -> None:
        cfg = self.store.get()
        start_id = self._capture.last_id if self._capture else 0
        fps = cfg.capture.replay_fps if cfg.capture.backend == "replay" else cfg.capture.target_fps
        self._capture = CaptureWorker(lambda: self.capture_factory(self.store.get()), self.latest, fps,
                                      self.clock, self._on_capture_event)
        self._capture.set_start_id(start_id)
        self._capture.start()

    def _restart_capture(self) -> None:
        if self._capture:
            self._capture.stop()
        self.latest.clear()
        self._start_capture()

    def shutdown(self) -> None:
        global _ACTIVE
        self.stop("server shutdown")
        self._shutdown.set()
        self.macro.shutdown()
        if self._capture:
            self._capture.stop()
        self.input.stop()
        for t in self._threads:
            t.join(5.0)
        try:
            self.real.close()
        except Exception:
            pass
        with _GUARD:
            if not self._released:
                _ACTIVE -= 1
                self._released = True

    # --------------------------------------------------------------- state
    def _set_state(self, st: State, reason: str) -> None:
        if self.state != st or self.state_reason != reason:
            self.logs.add("warning" if st == State.ERROR else "info", f"state {self.state.value} -> {st.value}: {reason}")
        self.state = st
        self.state_reason = reason

    def _macro_dispatch(self, evt: MacroEvent) -> None:
        cfg = self.store.get()
        if cfg.safety.dry_run or cfg.input.backend == "dryrun":
            self.logs.add("info", f"[Macro DryRun] {evt.action} {evt.kind} (t={evt.t}s)")
            return

        ctx = self.input_context()
        if evt.action in ("jump", "slide"):
            if cfg.input.backend == "adb" and ctx:
                if evt.kind in ("down", "tap"):
                    self.real.tap(evt.action, ctx, cfg.decision.tap_ms)
                elif evt.kind == "timed_hold":
                    self.real.timed_hold(evt.action, ctx, int((evt.duration_s or 0.4) * 1000))
                return
            elif cfg.input.backend == "keyboard" and ctx:
                if evt.kind == "down":
                    self.real.hold_start(evt.action, ctx)
                elif evt.kind == "up":
                    self.real.hold_end(evt.action, ctx)
                elif evt.kind == "tap":
                    self.real.tap(evt.action, ctx, cfg.decision.tap_ms)
                return

        if evt.action in ("restart", "click") or evt.kind == "click":
            if evt.x is not None and evt.y is not None:
                from .macro import _native_mouse_click
                _native_mouse_click(evt.x, evt.y)
                self.logs.add("info", f"[Macro] Restart clicked at ({evt.x}, {evt.y})")

    def _halt_input(self) -> None:
        self.input.clear_and_release()
        try:
            self.real.release_all()
        except Exception:
            pass
        self.dry.release_all()

    def start(self) -> tuple[bool, list[str]]:
        with self._lock:
            if self.state in (State.RUNNING, State.READY):
                return True, []
        reasons = self.validate_start(full=True)
        with self._lock:
            if reasons:
                self.logs.add("warning", "start blocked: " + "; ".join(reasons))
                return False, reasons
            self.decision.reset()
            self.vision.reset()
            self.last_error = None
            self._set_state(State.READY, "waiting for a fresh frame")
            return True, []

    def pause(self, reason: str = "paused by user") -> bool:
        with self._lock:  # own state first: autoplay.stop's nested pause is then a no-op and keeps this reason
            ok = self.state in (State.RUNNING, State.READY)
            if ok:
                self._halt_input()
                self._set_state(State.PAUSED, reason)
        if not reason.startswith("autoplay"):
            self.autoplay.stop(reason)
        return ok

    def stop(self, reason: str = "stopped by user") -> None:
        with self._lock:
            self._halt_input()
            self.decision.reset()
            self.vision.reset()
            self._set_state(State.STOPPED, reason)
        if not reason.startswith("autoplay"):
            self.autoplay.stop(reason)

    def _error(self, reason: str) -> None:
        with self._lock:
            self.last_error = reason
            self._halt_input()
            if self.state in (State.RUNNING, State.READY, State.PAUSED):
                self._set_state(State.ERROR, reason)
            else:
                self.logs.add("warning", reason)
        if not reason.startswith("autoplay"):
            self.autoplay.stop(reason)

    # ---------------------------------------------------------- validation
    def input_context(self) -> Optional[InputContext]:
        f = self.latest.peek()
        if f is None:
            return None
        return InputContext(self.store.get(), f.width, f.height,
                            bool(self._capture and self._capture.is_device))

    def _estop_active(self) -> bool:
        if self.estop_file is None:
            return False
        now = self.clock.now()
        if now - self._estop_checked > 0.1:
            self._estop_checked = now
            self._estop_present = self.estop_file.exists()
        return self._estop_present

    def current_signature(self, query: bool) -> Optional[CalibrationSignature]:
        ctx = self.input_context()
        if ctx is None:
            return None
        cfg = ctx.cfg
        dw = dh = None
        if cfg.input.backend == "adb":
            cached = getattr(self.real, "_display", None)
            if ctx.capture_is_device:
                dw, dh = ctx.frame_w, ctx.frame_h
            elif query:
                dw, dh = self.real.display_size(ctx)
            elif cached:
                dw, dh = cached
            else:
                return None
        return CalibrationSignature(capture_backend=cfg.capture.backend, frame_w=ctx.frame_w,
                                    frame_h=ctx.frame_h, input_backend=cfg.input.backend,
                                    display_w=dw, display_h=dh)

    def calibration_problems(self, query: bool) -> list[str]:
        cfg = self.store.get()
        cal = cfg.safety.calibration
        if cal is None:
            return ["calibration required: confirm viewport, ROIs and touch/key mapping"]
        try:
            cur = self.current_signature(query)
        except Exception as exc:
            return [f"cannot verify calibration: {exc}"]
        if cur is None:
            return ["cannot verify calibration yet (no frame or unknown display size)"]
        if not cal.matches(cur):
            return [f"calibration mismatch: calibrated {cal.frame_w}x{cal.frame_h} "
                    f"({cal.capture_backend}->{cal.input_backend}, display {cal.display_w}x{cal.display_h}), "
                    f"now {cur.frame_w}x{cur.frame_h} ({cur.capture_backend}->{cur.input_backend}, "
                    f"display {cur.display_w}x{cur.display_h}); recalibrate"]
        return []

    def validate_start(self, full: bool) -> list[str]:
        cfg = self.store.get()
        reasons: list[str] = []
        if self._estop_active():
            reasons.append(f"emergency stop file present: delete {self.estop_file}")
        f = self.latest.peek()
        if f is None or not (self._capture and self._capture.connected):
            reasons.append("capture is not delivering frames"
                           + (f": {self._capture.last_error}" if self._capture and self._capture.last_error else ""))
        elif (self.clock.now() - f.ts) * 1000 > cfg.safety.stale_capture_ms:
            reasons.append("latest frame is stale")
        if f is not None:
            reasons += roi_pixel_problems("jump", cfg.rois.jump_roi, cfg.viewport, f.width, f.height)
            reasons += roi_pixel_problems("slide", cfg.rois.slide_roi, cfg.viewport, f.width, f.height)
        if not cfg.safety.dry_run:
            if cfg.input.backend == "dryrun":
                reasons.append("real input needs an ADB or keyboard input backend (or enable dry-run)")
            elif full:
                reasons += self.real.check_ready(self.input_context())
            reasons += self.calibration_problems(query=full)
        return reasons

    # ----------------------------------------------------------- settings
    def update_config(self, incoming: AppConfig) -> AppConfig:
        with self._lock:
            cur = self.store.get()
            merged = merge_client_config(cur, incoming)
            capture_changed = (merged.capture != cur.capture) or (
                merged.adb != cur.adb and merged.capture.backend == "adb")
            input_changed = merged.input.backend != cur.input.backend or merged.adb != cur.adb
            queue_changed = merged.input.queue_size != cur.input.queue_size
            if (capture_changed or input_changed or queue_changed) and self.state in (State.RUNNING, State.READY):
                self.pause("capture/input settings changed")
            if cur.safety.calibration and merged.safety.calibration is None:
                self.logs.add("warning", "calibration cleared: coordinate-affecting settings changed")
            saved = self.store.set(merged)
            if input_changed:
                try:
                    self.real.close()
                except Exception:
                    pass
                self.real = self.input_factory(saved)
            if queue_changed:
                self.input.stop()
                self.input = self._make_input_worker()
                self.input.start()
        if capture_changed:
            self._restart_capture()
        self.macro.recorder.configure_keys(saved.input.jump_key, saved.input.slide_key)
        self.logs.add("info", "configuration saved")
        return saved

    def set_dry_run(self, enabled: bool) -> tuple[bool, list[str]]:
        cfg = self.store.get()
        if enabled:
            with self._lock:
                self.input.clear_and_release()
                self.real.release_all()
                self.store.set(cfg.model_copy(update={"safety": cfg.safety.model_copy(update={"dry_run": True})}))
            self.logs.add("info", "dry-run enabled: real input blocked")
            return True, []
        reasons: list[str] = []
        if cfg.input.backend == "dryrun":
            reasons.append("select an ADB or keyboard input backend first")
        else:
            reasons += self.real.check_ready(self.input_context())
            reasons += self.calibration_problems(query=True)
        if self._estop_active():
            reasons.append("emergency stop file present")
        if reasons:
            return False, reasons
        with self._lock:
            cfg = self.store.get()
            self.store.set(cfg.model_copy(update={"safety": cfg.safety.model_copy(update={"dry_run": False})}))
        self.logs.add("warning", "dry-run disabled: REAL INPUT will be sent while RUNNING")
        return True, []

    def confirm_calibration(self) -> tuple[bool, list[str]]:
        with self._lock:
            if self.state in (State.RUNNING, State.READY):
                return False, ["pause or stop before calibrating"]
        cfg = self.store.get()
        if cfg.input.backend != "dryrun":
            problems = self.real.check_ready(self.input_context())
            if problems:
                return False, problems
        try:
            sig = self.current_signature(query=True)
        except Exception as exc:
            return False, [str(exc)]
        if sig is None:
            return False, ["no frame available to calibrate against"]
        f = self.latest.peek()
        assert f is not None
        problems = roi_pixel_problems("jump", cfg.rois.jump_roi, cfg.viewport, f.width, f.height)
        problems += roi_pixel_problems("slide", cfg.rois.slide_roi, cfg.viewport, f.width, f.height)
        if problems:
            return False, problems
        sig = sig.model_copy(update={"confirmed_at": datetime.now(timezone.utc).isoformat(timespec="seconds")})
        with self._lock:
            cfg = self.store.get()
            self.store.set(cfg.model_copy(update={"safety": cfg.safety.model_copy(update={"calibration": sig})}))
        self.logs.add("info", f"calibration confirmed for {sig.frame_w}x{sig.frame_h} "
                              f"{sig.capture_backend}->{sig.input_backend}")
        return True, []

    def clear_calibration(self) -> None:
        with self._lock:
            cfg = self.store.get()
            self.store.set(cfg.model_copy(update={"safety": cfg.safety.model_copy(
                update={"calibration": None, "dry_run": True})}))
            self._halt_input()
        self.logs.add("info", "calibration cleared; dry-run enabled")

    def test_input(self, action: str) -> tuple[bool, list[str], str]:
        with self._lock:
            if self.state in (State.RUNNING, State.READY):
                return False, ["pause or stop the bot before testing input"], ""
        cfg = self.store.get()
        f = self.latest.peek()
        if f is None:
            return False, ["no frame available (needed for coordinate mapping)"], ""
        real = not cfg.safety.dry_run
        if real:
            problems = self.real.check_ready(self.input_context())
            if problems:
                return False, problems, ""
        now = self.clock.now()
        cmd = new_command(action=action, kind="tap", created=now, deadline=now + 1.0, frame_id=f.id,
                          obstacle_id=-1, tap_ms=cfg.decision.tap_ms, test=True)
        self.input.submit(cmd)
        name = self.real.name if real else "dryrun"
        self.logs.add("info", f"user test input: {action} via {name}")
        return True, [], name

    def on_clients_changed(self, count: int) -> None:
        prev, self.event_clients = self.event_clients, count
        policy = self.store.get().safety.disconnect_policy
        if count == 0 and prev > 0 and policy == "pause":
            if self.pause("dashboard disconnected (policy: pause)"):
                self.logs.add("warning", "all dashboards disconnected; input paused")

    # --------------------------------------------------- input callbacks
    def _select_backend(self, cmd: Command) -> InputBackend:
        return self.dry if self.store.get().safety.dry_run else self.real

    def _may_execute(self, cmd: Command) -> Optional[str]:
        if self._estop_active():
            return "emergency stop"
        if cmd.test:
            return "bot active" if self.state in (State.RUNNING, State.READY) else None
        if self.state != State.RUNNING:
            return f"state {self.state.value}"
        f = self.latest.peek()
        if f is None or (self.clock.now() - f.ts) * 1000 > self.store.get().safety.stale_capture_ms:
            return "capture stale"
        return None

    def _on_input_error(self, exc: Exception) -> None:
        self._error(f"input failure: {exc}")

    def _on_executed(self, cmd: Command, backend: InputBackend, ms: float) -> None:
        self.last_executed = {**cmd.to_dict(), "backend": backend.name, "real": backend.is_real,
                              "dispatch_ms": round(ms, 2), "t": time.time()}

    def _on_capture_event(self, kind: str, msg: str) -> None:
        if kind == "lost":
            self._error(msg)
        elif kind == "error":
            self.last_error = msg
            self.logs.add("warning", msg)
        else:
            self.logs.add("info", msg + (" (input stays suspended until you Start again)"
                                         if self.state == State.ERROR else ""))

    # --------------------------------------------------------- main loop
    def _check_safety(self, now: float) -> None:
        if self._estop_active() and (self.state in (State.RUNNING, State.READY, State.PAUSED) or self.autoplay.active):
            self.stop("emergency stop file detected")
        if self.state in (State.RUNNING, State.READY):
            f = self.latest.peek()
            stale = self.store.get().safety.stale_capture_ms / 1000.0
            if f is None or now - f.ts > stale:
                self._error("stale capture: no fresh frames")

    def process_frame(self, frame) -> Optional[VisionResult]:
        """One pipeline step (also called directly by tests)."""
        cfg = self.store.get()
        dims = (frame.width, frame.height)
        if self._frame_dims and dims != self._frame_dims:
            self.logs.add("warning", f"frame size changed {self._frame_dims} -> {dims}")
            if not cfg.safety.dry_run and self.state in (State.RUNNING, State.READY):
                self.pause("resolution/orientation changed: recalibrate before real input")
        self._frame_dims = dims
        result = self.vision.process(frame.id, frame.ts, frame.image, cfg)
        self.vision_ms.add(result.duration_ms)
        self._pair = (frame, result)
        with self._lock:
            if self.state == State.READY and (self.clock.now() - frame.ts) * 1000 <= cfg.decision.max_frame_age_ms:
                self._set_state(State.RUNNING, "running" + (" (dry-run)" if cfg.safety.dry_run else " (REAL INPUT)"))
            running = self.state == State.RUNNING
        caps = (self.dry if cfg.safety.dry_run else self.real).capabilities()
        out = self.decision.evaluate(result, self.clock.now(), running, cfg, caps)
        self.frame_age_ms.add(out.frame_age_ms)
        if out.detected:
            self.last_detected = {"actions": out.detected, "frame_id": frame.id, "t": time.time()}
        if out.suppressed:
            self.last_suppressed = {**out.suppressed, "frame_id": frame.id}
        if out.notes:
            self.decision_notes = out.notes
        for cmd in out.commands:
            self.input.submit(cmd)
        self.process_rate.tick()
        return result

    def _process_loop(self) -> None:
        last_id = 0
        while not self._shutdown.is_set():
            frame = self.latest.wait_newer(last_id, timeout=0.1)
            now = self.clock.now()
            try:
                self._check_safety(now)
                if frame is None:
                    continue
                last_id = frame.id
                self.process_frame(frame)
            except Exception as exc:
                log.exception("processing error")
                self._error(f"processing error: {exc}")
                self._shutdown.wait(0.2)

    # ------------------------------------------------------------ preview
    def want_view(self, view: str, delta: int) -> None:
        if view not in VIEWS:
            return
        with self._lock:
            self._views_wanted[view] = max(0, self._views_wanted.get(view, 0) + delta)

    def get_preview(self, view: str) -> Optional[tuple[int, dict, bytes]]:
        return self._previews.get(view)

    def _preview_loop(self) -> None:
        last_fid = -1
        while not self._shutdown.is_set():
            cfg = self.store.get()
            interval = 1.0 / cfg.preview.fps
            t0 = time.monotonic()
            with self._lock:
                views = [v for v, n in self._views_wanted.items() if n > 0]
            pair = self._pair
            if views and pair is not None and pair[0].id != last_fid:
                frame, result = pair
                last_fid = frame.id
                try:
                    for v in views:
                        img = render_view(v, frame.image, result, cfg, cfg.preview.max_width)
                        jpeg = encode_jpeg(img, cfg.preview.jpeg_quality)
                        meta = {"v": PROTOCOL_VERSION, "type": "preview_meta", "frame_id": frame.id, "view": v,
                                "width": img.shape[1], "height": img.shape[0], "src_width": frame.width,
                                "src_height": frame.height, "bytes": len(jpeg),
                                "jump": result.jump.to_dict(), "slide": result.slide.to_dict()}
                        self._previews[v] = (frame.id, meta, jpeg)
                    self.preview_rate.tick()
                except Exception as exc:
                    self.logs.add("warning", f"preview error: {exc}")
            self._shutdown.wait(max(0.005, interval - (time.monotonic() - t0)))

    def _hotkey_loop(self) -> None:  # pragma: no cover - windows only
        import ctypes
        user32 = ctypes.windll.user32  # type: ignore[attr-defined]
        vk_f12 = 0x7B
        self.logs.add("info", "global emergency stop hotkey: F12")
        while not self._shutdown.is_set():
            if user32.GetAsyncKeyState(vk_f12) & 0x8000:
                if self.state in (State.RUNNING, State.READY, State.PAUSED) or self.autoplay.active:
                    self.stop("emergency stop hotkey (F12)")
                self._shutdown.wait(0.5)
            self._shutdown.wait(0.03)

    # ---------------------------------------------------------- snapshots
    def status(self) -> dict:
        cfg = self.store.get()
        f = self.latest.peek()
        blockers = self.validate_start(full=False)
        cap = self._capture
        return {
            "v": PROTOCOL_VERSION, "type": "status", "state": self.state.value, "reason": self.state_reason,
            "dry_run": cfg.safety.dry_run, "disconnect_policy": cfg.safety.disconnect_policy,
            "can_start": not blockers and self.state not in (State.RUNNING, State.READY),
            "start_blockers": blockers,
            "calibrated": cfg.safety.calibration is not None,
            "calibration": cfg.safety.calibration.model_dump() if cfg.safety.calibration else None,
            "calibration_problems": self.calibration_problems(query=False) if cfg.safety.calibration else [],
            "capture": {"backend": cfg.capture.backend, "connected": bool(cap and cap.connected),
                        "last_error": cap.last_error if cap else None,
                        "frame": {"id": f.id, "w": f.width, "h": f.height,
                                  "age_ms": round((self.clock.now() - f.ts) * 1000, 1)} if f else None,
                        "is_device": bool(cap and cap.is_device)},
            "input": {"backend": cfg.input.backend, "capabilities": self.real.capabilities(),
                      "dryrun_capabilities": self.dry.capabilities()},
            "estop": self._estop_active(), "estop_file": str(self.estop_file) if self.estop_file else None,
            "clients": self.event_clients, "last_error": self.last_error,
            "macro": self.macro.status(), "autoplay": self.autoplay.status(),
        }

    def telemetry(self) -> dict:
        cap = self._capture
        return {
            "v": PROTOCOL_VERSION, "type": "telemetry",
            "capture_fps": round(cap.rate.rate(), 1) if cap else 0.0,
            "processing_fps": round(self.process_rate.rate(), 1),
            "preview_fps": round(self.preview_rate.rate(), 1),
            "capture_ms": cap.capture_ms.snapshot() if cap else None,
            "vision_ms": self.vision_ms.snapshot(),
            "dispatch_ms": self.input.dispatch_ms.snapshot(),
            "frame_age_ms": self.frame_age_ms.snapshot(),
            "input_queue": self.input.depth, "dropped_frames": self.latest.dropped,
            "expired_commands": self.input.expired, "dropped_commands": self.input.dropped,
            "rejected_commands": self.input.rejected, "executed_commands": self.input.executed,
            "capture_errors": cap.errors if cap else 0,
            "last_detected": self.last_detected, "last_executed": self.last_executed,
            "last_suppressed": self.last_suppressed, "notes": self.decision_notes,
            "last_error": self.last_error,
        }
