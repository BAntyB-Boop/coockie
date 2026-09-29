"""Macro record & replay engine for Cookie Run automation.

Records user input timing in the first round (jump, slide, restart click),
saves it as Loop 1, and replays it automatically for N loops.
"""
from __future__ import annotations

import json
import logging
import sys
import threading
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

log = logging.getLogger("cookiebot.macro")

# ---------------------------------------------------------------------------
# Virtual key mappings (Windows)
# ---------------------------------------------------------------------------
VK_NAMES: dict[str, int] = {
    "lbutton": 0x01, "rbutton": 0x02,
    "backspace": 0x08, "tab": 0x09, "enter": 0x0D, "return": 0x0D,
    "shift": 0x10, "ctrl": 0x11, "alt": 0x12, "pause": 0x13, "capslock": 0x14,
    "esc": 0x1B, "escape": 0x1B, "space": 0x20, "pageup": 0x21, "pagedown": 0x22,
    "end": 0x23, "home": 0x24, "left": 0x25, "up": 0x26, "right": 0x27, "down": 0x28,
    "insert": 0x2D, "delete": 0x2E,
    "f1": 0x70, "f2": 0x71, "f3": 0x72, "f4": 0x73, "f5": 0x74, "f6": 0x75,
    "f7": 0x76, "f8": 0x77, "f9": 0x78, "f10": 0x79, "f11": 0x7A, "f12": 0x7B,
}


def key_to_vk(key: str) -> Optional[int]:
    """Convert key name or character to Windows Virtual Key code."""
    k = key.lower().strip()
    if k in VK_NAMES:
        return VK_NAMES[k]
    if len(k) == 1:
        if "a" <= k <= "z":
            return ord(k.upper())
        if "0" <= k <= "9":
            return ord(k)
    return None


def vk_to_key(vk: int) -> str:
    """Reverse map VK code to human-readable string."""
    for name, code in VK_NAMES.items():
        if code == vk:
            return name
    if 0x41 <= vk <= 0x5A:
        return chr(vk).lower()
    if 0x30 <= vk <= 0x39:
        return chr(vk)
    return f"vk_{hex(vk)}"


# ---------------------------------------------------------------------------
# Native Windows Input Helpers (ctypes fallback if pydirectinput not present)
# ---------------------------------------------------------------------------
def _native_key_down(vk: int) -> None:
    if sys.platform != "win32":
        return
    import ctypes
    u = ctypes.windll.user32
    scan = u.MapVirtualKeyW(vk, 0)
    u.keybd_event(vk, scan, 0, 0)


def _native_key_up(vk: int) -> None:
    if sys.platform != "win32":
        return
    import ctypes
    u = ctypes.windll.user32
    scan = u.MapVirtualKeyW(vk, 0)
    u.keybd_event(vk, scan, 2, 0)  # 2 = KEYEVENTF_KEYUP


def _native_mouse_click(x: int, y: int) -> None:
    if sys.platform != "win32":
        return
    import ctypes
    u = ctypes.windll.user32
    u.SetCursorPos(x, y)
    u.mouse_event(0x0002, 0, 0, 0, 0)  # MOUSEEVENTF_LEFTDOWN
    time.sleep(0.04)
    u.mouse_event(0x0004, 0, 0, 0, 0)  # MOUSEEVENTF_LEFTUP


def _get_cursor_pos() -> tuple[int, int]:
    if sys.platform != "win32":
        return (0, 0)
    import ctypes
    import ctypes.wintypes
    u = ctypes.windll.user32
    pt = ctypes.wintypes.POINT()
    u.GetCursorPos(ctypes.byref(pt))
    return (pt.x, pt.y)


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------
@dataclass
class MacroEvent:
    t: float                          # seconds from start of round (e.g. 1.25)
    action: str                       # "jump", "slide", "restart", "click", "key"
    kind: str                         # "down", "up", "tap", "click"
    key: Optional[str] = None         # "space", "down", "d", "f", etc.
    x: Optional[int] = None           # mouse screen X (if click)
    y: Optional[int] = None           # mouse screen Y (if click)
    duration_s: Optional[float] = None  # for combined tap/hold

    def to_dict(self) -> dict:
        d = asdict(self)
        return {k: v for k, v in d.items() if v is not None}

    @classmethod
    def from_dict(cls, d: dict) -> "MacroEvent":
        return cls(
            t=float(d["t"]),
            action=str(d.get("action", "key")),
            kind=str(d.get("kind", "down")),
            key=d.get("key"),
            x=d.get("x"),
            y=d.get("y"),
            duration_s=d.get("duration_s"),
        )


@dataclass
class MacroData:
    name: str = "loop_1"
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    duration_s: float = 0.0
    events: list[MacroEvent] = field(default_factory=list)
    restart_index: Optional[int] = None

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "created_at": self.created_at,
            "duration_s": round(self.duration_s, 3),
            "events": [e.to_dict() for e in self.events],
            "restart_index": self.restart_index,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "MacroData":
        events = [MacroEvent.from_dict(e) for e in d.get("events", [])]
        return cls(
            name=d.get("name", "loop_1"),
            created_at=d.get("created_at", ""),
            duration_s=float(d.get("duration_s", 0.0)),
            events=events,
            restart_index=d.get("restart_index"),
        )

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.to_dict(), indent=2), encoding="utf-8")

    @classmethod
    def load(cls, path: Path) -> "MacroData":
        data = json.loads(path.read_text(encoding="utf-8"))
        return cls.from_dict(data)


# ---------------------------------------------------------------------------
# Recorder
# ---------------------------------------------------------------------------
class MacroRecorder:
    def __init__(self, on_event: Optional[Callable[[MacroEvent], None]] = None) -> None:
        self.is_recording = False
        self.start_t: float = 0.0
        self.events: list[MacroEvent] = []
        self.on_event = on_event
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._lock = threading.Lock()
        self.jump_key: str = "space"
        self.slide_key: str = "down"
        self.restart_key: str = "enter"

    def configure_keys(self, jump_key: str, slide_key: str, restart_key: str = "enter") -> None:
        self.jump_key = jump_key
        self.slide_key = slide_key
        self.restart_key = restart_key

    def start(self, name: str = "loop_1") -> None:
        with self._lock:
            if self.is_recording:
                return
            self.events = []
            self.start_t = time.perf_counter()
            self.is_recording = True
            self._stop_event.clear()
            self._thread = threading.Thread(target=self._poll_loop, name="macro-recorder", daemon=True)
            self._thread.start()
            log.info("Macro recording started (T=0.0s)")

    def stop(self) -> MacroData:
        with self._lock:
            if not self.is_recording:
                return MacroData(events=list(self.events), duration_s=0.0)
            self.is_recording = False
            self._stop_event.set()
            duration = max(0.0, time.perf_counter() - self.start_t)

        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=1.0)

        # Detect restart index (if any mouse click or restart key at the end of recording)
        restart_idx = None
        for i in reversed(range(len(self.events))):
            if self.events[i].action in ("restart", "click"):
                restart_idx = i
                break

        macro = MacroData(
            name="loop_1",
            duration_s=round(duration, 3),
            events=list(self.events),
            restart_index=restart_idx,
        )
        log.info(f"Macro recording finished: {len(macro.events)} events, duration {macro.duration_s}s")
        return macro

    def record_action(self, action: str, kind: str, key: Optional[str] = None,
                      x: Optional[int] = None, y: Optional[int] = None) -> None:
        """Record an explicit action (e.g. from web UI or keyboard poll)."""
        if not self.is_recording:
            return
        t = round(max(0.0, time.perf_counter() - self.start_t), 3)
        evt = MacroEvent(t=t, action=action, kind=kind, key=key, x=x, y=y)
        with self._lock:
            self.events.append(evt)
        if self.on_event:
            try:
                self.on_event(evt)
            except Exception:
                pass

    def _poll_loop(self) -> None:
        if sys.platform != "win32":
            while not self._stop_event.wait(0.05):
                pass
            return

        import ctypes
        user32 = ctypes.windll.user32

        # Build list of VK codes to monitor
        key_actions = {
            self.jump_key: "jump",
            self.slide_key: "slide",
            self.restart_key: "restart",
        }
        # Also include common keys if not already mapped
        for k in ("space", "down", "d", "f", "enter", "r"):
            if k not in key_actions:
                key_actions[k] = k

        monitored_vks: dict[int, tuple[str, str]] = {}  # vk -> (action, key_name)
        for k, act in key_actions.items():
            vk = key_to_vk(k)
            if vk is not None:
                monitored_vks[vk] = (act, k)

        key_states: dict[int, bool] = {vk: False for vk in monitored_vks}
        mouse_down = False
        vk_lbutton = 0x01

        while not self._stop_event.is_set():
            # Check monitored keyboard keys
            for vk, (act, kname) in monitored_vks.items():
                is_down = bool(user32.GetAsyncKeyState(vk) & 0x8000)
                prev = key_states[vk]
                if is_down and not prev:
                    key_states[vk] = True
                    self.record_action(action=act, kind="down", key=kname)
                elif not is_down and prev:
                    key_states[vk] = False
                    self.record_action(action=act, kind="up", key=kname)

            # Check left mouse click (especially useful for clicking Restart in game)
            is_m_down = bool(user32.GetAsyncKeyState(vk_lbutton) & 0x8000)
            if is_m_down and not mouse_down:
                mouse_down = True
                mx, my = _get_cursor_pos()
                self.record_action(action="restart", kind="click", x=mx, y=my)
            elif not is_m_down and mouse_down:
                mouse_down = False

            # High precision 5ms sleep
            self._stop_event.wait(0.005)


# ---------------------------------------------------------------------------
# Player
# ---------------------------------------------------------------------------
class MacroPlayer:
    def __init__(self, input_dispatch: Optional[Callable[[MacroEvent], None]] = None) -> None:
        self.is_playing = False
        self.is_paused = False
        self.current_loop = 0
        self.target_loops = 1  # 0 = infinite
        self.speed = 1.0
        self.auto_restart = True
        self.macro: Optional[MacroData] = None
        self.input_dispatch = input_dispatch
        self.loop_start_t: float = 0.0
        self.elapsed_in_loop: float = 0.0
        self.total_elapsed: float = 0.0
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._pause_event = threading.Event()
        self._held_keys: set[str] = set()

    def start(self, macro: MacroData, target_loops: int = 1, speed: float = 1.0,
              auto_restart: bool = True) -> None:
        self.stop()
        self.macro = macro
        self.target_loops = target_loops
        self.speed = max(0.1, min(10.0, speed))
        self.auto_restart = auto_restart
        self.current_loop = 1
        self.is_playing = True
        self.is_paused = False
        self._stop_event.clear()
        self._pause_event.clear()
        self._thread = threading.Thread(target=self._play_loop, name="macro-player", daemon=True)
        self._thread.start()
        log.info(f"Macro replay started for {target_loops} loops (speed={speed}x)")

    def pause(self) -> None:
        if self.is_playing and not self.is_paused:
            self.is_paused = True
            self._release_held()
            log.info("Macro replay paused")

    def resume(self) -> None:
        if self.is_playing and self.is_paused:
            self.is_paused = False
            log.info("Macro replay resumed")

    def stop(self) -> None:
        if self.is_playing:
            self.is_playing = False
            self.is_paused = False
            self._stop_event.set()
            self._release_held()
            if self._thread and self._thread.is_alive():
                self._thread.join(timeout=1.0)
            log.info(f"Macro replay stopped at loop {self.current_loop}")

    def _release_held(self) -> None:
        for k in list(self._held_keys):
            vk = key_to_vk(k)
            if vk is not None:
                _native_key_up(vk)
        self._held_keys.clear()

    def _execute_event(self, evt: MacroEvent) -> None:
        # Custom dispatcher first if provided
        if self.input_dispatch:
            try:
                self.input_dispatch(evt)
                return
            except Exception as exc:
                log.warning(f"Custom dispatch failed, falling back to native: {exc}")

        # Native Windows execution
        if evt.action in ("restart", "click") or evt.kind == "click":
            if evt.x is not None and evt.y is not None:
                _native_mouse_click(evt.x, evt.y)
            elif evt.key:
                vk = key_to_vk(evt.key)
                if vk is not None:
                    _native_key_down(vk)
                    time.sleep(0.05)
                    _native_key_up(vk)
            return

        # Keyboard actions (jump, slide, or raw key)
        k = evt.key or ("space" if evt.action == "jump" else ("down" if evt.action == "slide" else evt.action))
        vk = key_to_vk(k)
        if vk is None:
            return

        if evt.kind == "down":
            _native_key_down(vk)
            self._held_keys.add(k)
        elif evt.kind == "up":
            _native_key_up(vk)
            self._held_keys.discard(k)
        elif evt.kind == "tap":
            _native_key_down(vk)
            time.sleep(evt.duration_s or 0.04)
            _native_key_up(vk)

    def _play_loop(self) -> None:
        assert self.macro is not None
        macro = self.macro
        speed = self.speed
        total_start = time.perf_counter()

        while self.is_playing:
            log.info(f"--- Starting Macro Loop {self.current_loop} of {self.target_loops or 'inf'} ---")
            loop_start = time.perf_counter()
            self.loop_start_t = loop_start

            for evt in macro.events:
                if not self.is_playing:
                    break

                while self.is_paused and self.is_playing:
                    time.sleep(0.05)

                target_elapsed = evt.t / speed
                # High-precision wait
                while self.is_playing:
                    now = time.perf_counter()
                    self.elapsed_in_loop = now - loop_start
                    self.total_elapsed = now - total_start
                    remaining = target_elapsed - self.elapsed_in_loop
                    if remaining <= 0:
                        break
                    if remaining > 0.005:
                        time.sleep(remaining - 0.003)
                    else:
                        time.sleep(0.0005)

                if not self.is_playing:
                    break

                self._execute_event(evt)

            # Wait for remainder of loop duration
            loop_duration = macro.duration_s / speed
            while self.is_playing:
                now = time.perf_counter()
                self.elapsed_in_loop = now - loop_start
                self.total_elapsed = now - total_start
                remaining = loop_duration - self.elapsed_in_loop
                if remaining <= 0:
                    break
                if remaining > 0.005:
                    time.sleep(remaining - 0.003)
                else:
                    time.sleep(0.0005)

            self._release_held()

            if not self.is_playing:
                break

            # Check if target loops reached
            if self.target_loops > 0 and self.current_loop >= self.target_loops:
                log.info(f"Macro replay completed all {self.target_loops} loops successfully!")
                self.is_playing = False
                break

            self.current_loop += 1

        self._release_held()


# ---------------------------------------------------------------------------
# Manager (Orchestrator)
# ---------------------------------------------------------------------------
class MacroManager:
    def __init__(self, macros_dir: Optional[Path] = None,
                 input_dispatch: Optional[Callable[[MacroEvent], None]] = None) -> None:
        self.macros_dir = macros_dir or (Path(__file__).parent.parent / "data" / "macros")
        self.macros_dir.mkdir(parents=True, exist_ok=True)
        self.recorder = MacroRecorder()
        self.player = MacroPlayer(input_dispatch=input_dispatch)
        self.current_macro: Optional[MacroData] = None
        self.last_error: Optional[str] = None
        self._hotkey_thread: Optional[threading.Thread] = None
        self._shutdown = threading.Event()
        self._load_default()

    def _load_default(self) -> None:
        default_file = self.macros_dir / "loop_1.json"
        if default_file.exists():
            try:
                self.current_macro = MacroData.load(default_file)
                log.info(f"Loaded existing macro '{self.current_macro.name}' ({len(self.current_macro.events)} events)")
            except Exception as exc:
                log.warning(f"Could not load default macro: {exc}")

    def start_hotkeys(self) -> None:
        if sys.platform != "win32":
            return
        if self._hotkey_thread and self._hotkey_thread.is_alive():
            return
        self._hotkey_thread = threading.Thread(target=self._hotkey_loop, name="macro-hotkeys", daemon=True)
        self._hotkey_thread.start()

    def shutdown(self) -> None:
        self._shutdown.set()
        self.recorder.stop()
        self.player.stop()

    def start_recording(self, name: str = "loop_1") -> bool:
        if self.player.is_playing:
            self.last_error = "cannot record while playback is running"
            return False
        self.player.stop()
        self.recorder.start(name)
        self.last_error = None
        return True

    def stop_recording(self, save_name: str = "loop_1") -> MacroData:
        macro = self.recorder.stop()
        macro.name = save_name
        self.current_macro = macro
        # Save as loop_1.json and {save_name}.json
        macro.save(self.macros_dir / "loop_1.json")
        if save_name != "loop_1":
            macro.save(self.macros_dir / f"{save_name}.json")
        log.info(f"Macro saved as '{save_name}' to {self.macros_dir}")
        return macro

    def start_playback(self, target_loops: int = 1, speed: float = 1.0,
                       auto_restart: bool = True) -> bool:
        if self.recorder.is_recording:
            self.last_error = "cannot play while recording is active"
            return False
        if not self.current_macro or not self.current_macro.events:
            self.last_error = "no macro recorded or loaded yet"
            return False
        self.last_error = None
        self.player.start(self.current_macro, target_loops=target_loops, speed=speed,
                          auto_restart=auto_restart)
        return True

    def pause_playback(self) -> None:
        self.player.pause()

    def resume_playback(self) -> None:
        self.player.resume()

    def stop_playback(self) -> None:
        self.player.stop()

    def list_saved(self) -> list[dict]:
        res = []
        for p in sorted(self.macros_dir.glob("*.json")):
            try:
                m = MacroData.load(p)
                res.append({
                    "name": p.stem,
                    "duration_s": m.duration_s,
                    "event_count": len(m.events),
                    "created_at": m.created_at,
                })
            except Exception:
                pass
        return res

    def save_current(self, name: str) -> bool:
        if not self.current_macro:
            self.last_error = "no active macro to save"
            return False
        self.current_macro.name = name
        self.current_macro.save(self.macros_dir / f"{name}.json")
        return True

    def load_macro(self, name: str) -> bool:
        p = self.macros_dir / f"{name}.json"
        if not p.exists():
            self.last_error = f"macro '{name}' not found"
            return False
        try:
            self.current_macro = MacroData.load(p)
            self.last_error = None
            return True
        except Exception as exc:
            self.last_error = str(exc)
            return False

    def _hotkey_loop(self) -> None:
        """Global hotkey listener: F9 = Record toggle, F10 = Play toggle, F12 = E-Stop."""
        import ctypes
        user32 = ctypes.windll.user32
        vk_f9 = 0x78   # F9: Record/Stop
        vk_f10 = 0x79  # F10: Play/Stop
        vk_f12 = 0x7B  # F12: Emergency stop

        f9_down = False
        f10_down = False

        while not self._shutdown.is_set():
            # F12 Emergency Stop
            if user32.GetAsyncKeyState(vk_f12) & 0x8000:
                if self.recorder.is_recording:
                    self.stop_recording()
                if self.player.is_playing:
                    self.stop_playback()
                self._shutdown.wait(0.5)

            # F9 Record Toggle
            f9 = bool(user32.GetAsyncKeyState(vk_f9) & 0x8000)
            if f9 and not f9_down:
                f9_down = True
                if self.recorder.is_recording:
                    self.stop_recording()
                else:
                    self.start_recording()
            elif not f9 and f9_down:
                f9_down = False

            # F10 Play Toggle
            f10 = bool(user32.GetAsyncKeyState(vk_f10) & 0x8000)
            if f10 and not f10_down:
                f10_down = True
                if self.player.is_playing:
                    self.stop_playback()
                else:
                    self.start_playback()
            elif not f10 and f10_down:
                f10_down = False

            self._shutdown.wait(0.04)

    def status(self) -> dict:
        state = "IDLE"
        if self.recorder.is_recording:
            state = "RECORDING"
        elif self.player.is_playing:
            state = "PAUSED" if self.player.is_paused else "PLAYING"

        duration = self.current_macro.duration_s if self.current_macro else 0.0
        elapsed = 0.0
        if self.recorder.is_recording:
            elapsed = max(0.0, time.perf_counter() - self.recorder.start_t)
        elif self.player.is_playing:
            elapsed = self.player.elapsed_in_loop

        events_summary = []
        if self.current_macro:
            for e in self.current_macro.events[:50]:
                events_summary.append(e.to_dict())

        return {
            "state": state,
            "name": self.current_macro.name if self.current_macro else None,
            "duration_s": round(duration, 2),
            "elapsed_s": round(elapsed, 2),
            "current_loop": self.player.current_loop if self.player.is_playing else 0,
            "target_loops": self.player.target_loops,
            "event_count": len(self.current_macro.events) if self.current_macro else 0,
            "events_sample": events_summary,
            "last_error": self.last_error,
            "hotkeys": {"record": "F9", "play": "F10", "estop": "F12"},
        }
