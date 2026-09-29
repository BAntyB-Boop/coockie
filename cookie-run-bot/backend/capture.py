"""Capture backends and the capture worker thread.

Note: desktop-region capture (MSS) copies whatever is visible on screen in that
rectangle. It does not capture obscured or minimized windows.
"""
from __future__ import annotations

import subprocess
import sys
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

import cv2
import numpy as np

from .config import AdbConfig, CaptureConfig
from .telemetry import Clock, DurationStats, RateMeter


class CaptureError(Exception):
    pass


class CaptureTimeout(CaptureError):
    pass


class CorruptFrame(CaptureError):
    pass


class DeviceLost(CaptureError):
    pass


@dataclass(frozen=True)
class Frame:
    id: int
    ts: float  # monotonic seconds when the frame was acquired
    image: np.ndarray  # BGR uint8, never mutated after creation
    width: int
    height: int
    source: str


class LatestFrame:
    """Single-slot bounded buffer: newer frames overwrite stale unread frames."""

    def __init__(self) -> None:
        self._cond = threading.Condition()
        self._frame: Optional[Frame] = None
        self._consumed_id = 0
        self.dropped = 0

    def put(self, frame: Frame) -> None:
        with self._cond:
            if self._frame is not None and self._frame.id > self._consumed_id:
                self.dropped += 1
            self._frame = frame
            self._cond.notify_all()

    def peek(self) -> Optional[Frame]:
        with self._cond:
            return self._frame

    def wait_newer(self, last_id: int, timeout: float) -> Optional[Frame]:
        with self._cond:
            if self._frame is None or self._frame.id <= last_id:
                self._cond.wait(timeout)
            if self._frame is not None and self._frame.id > last_id:
                self._consumed_id = self._frame.id
                return self._frame
            return None

    def clear(self) -> None:
        with self._cond:
            self._frame = None


class CaptureBackend:
    name = "base"
    is_device = False

    def open(self) -> None:
        pass

    def grab(self) -> np.ndarray:
        raise NotImplementedError

    def close(self) -> None:
        pass


# --------------------------------------------------------------------------- MSS
class MSSCapture(CaptureBackend):
    name = "mss"

    def __init__(self, cfg: CaptureConfig) -> None:
        self.region = cfg.region
        self._sct = None

    def open(self) -> None:
        pass  # mss handle is created lazily inside the capture thread (thread-affine on Windows)

    def grab(self) -> np.ndarray:
        if self._sct is None:
            try:
                import mss  # type: ignore
            except ImportError as exc:
                raise CaptureError("mss is not installed (pip install -r backend/requirements-pc.txt)") from exc
            self._sct = mss.mss()
        r = self.region
        try:
            shot = self._sct.grab({"left": r.left, "top": r.top, "width": r.width, "height": r.height})
        except Exception as exc:
            raise CaptureError(f"MSS grab failed: {exc}") from exc
        img = np.asarray(shot, dtype=np.uint8)
        if img.ndim != 3 or img.shape[2] < 3:
            raise CorruptFrame("unexpected MSS frame shape")
        return cv2.cvtColor(img, cv2.COLOR_BGRA2BGR)

    def close(self) -> None:
        if self._sct is not None:
            try:
                self._sct.close()
            except Exception:
                pass
            self._sct = None


# --------------------------------------------------------------------------- ADB
ADB_LOST_MARKERS = ("device not found", "device offline", "no devices", "unauthorized", "not found",
                    "closed", "device '")


def run_adb(adb: AdbConfig, args: list[str], timeout: float, serial: bool = True) -> subprocess.CompletedProcess:
    """Run adb with an argument array (never through a shell)."""
    cmd = [adb.executable]
    if serial:
        if not adb.serial:
            raise DeviceLost("no ADB device serial configured")
        cmd += ["-s", adb.serial]
    cmd += args
    kwargs: dict = {}
    if sys.platform == "win32":
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        return subprocess.run(cmd, capture_output=True, timeout=timeout, check=False, **kwargs)
    except FileNotFoundError as exc:
        raise DeviceLost(f"ADB executable not found: {adb.executable}") from exc
    except subprocess.TimeoutExpired as exc:  # subprocess.run kills the child on timeout
        raise CaptureTimeout(f"adb {' '.join(args[:3])} timed out after {timeout:.1f}s") from exc


class ADBCapture(CaptureBackend):
    name = "adb"
    is_device = True

    def __init__(self, adb: AdbConfig) -> None:
        self.adb = adb

    def open(self) -> None:
        if not self.adb.serial:
            raise DeviceLost("select an ADB device serial first")
        res = run_adb(self.adb, ["get-state"], self.adb.command_timeout_s)
        state = res.stdout.decode(errors="replace").strip()
        if res.returncode != 0 or state != "device":
            err = res.stderr.decode(errors="replace").strip()
            raise DeviceLost(f"ADB device {self.adb.serial} not ready: {state or err or 'unknown'}")

    def grab(self) -> np.ndarray:
        res = run_adb(self.adb, ["exec-out", "screencap", "-p"], self.adb.capture_timeout_s)
        if res.returncode != 0:
            err = res.stderr.decode(errors="replace").strip().lower()
            if any(m in err for m in ADB_LOST_MARKERS):
                raise DeviceLost(f"ADB device lost: {err[:200]}")
            raise CaptureError(f"screencap failed ({res.returncode}): {err[:200]}")
        data = res.stdout
        if len(data) < 64 or not data.startswith(b"\x89PNG"):
            raise CorruptFrame(f"screencap returned {len(data)} bytes without PNG signature")
        img = cv2.imdecode(np.frombuffer(data, dtype=np.uint8), cv2.IMREAD_COLOR)
        if img is None:
            raise CorruptFrame("could not decode screencap PNG")
        return img


# ------------------------------------------------------------------------ Replay
IMAGE_EXT = {".png", ".jpg", ".jpeg", ".bmp"}


class ReplayCapture(CaptureBackend):
    name = "replay"

    def __init__(self, path: str, loop: bool = True) -> None:
        self.path = path
        self.loop = loop
        self._cap: Optional[cv2.VideoCapture] = None
        self._still: Optional[np.ndarray] = None

    def open(self) -> None:
        p = Path(self.path).expanduser()
        if not self.path or not p.is_file():
            raise DeviceLost(f"replay file not found: {self.path or '(empty path)'}")
        if p.suffix.lower() in IMAGE_EXT:
            img = cv2.imread(str(p), cv2.IMREAD_COLOR)
            if img is None:
                raise CorruptFrame("could not decode replay image")
            self._still = img
            return
        cap = cv2.VideoCapture(str(p))
        if not cap.isOpened():
            raise DeviceLost(f"cannot open replay video: {self.path}")
        self._cap = cap

    def grab(self) -> np.ndarray:
        if self._still is not None:
            return self._still.copy()
        if self._cap is None:
            raise DeviceLost("replay not open")
        ok, img = self._cap.read()
        if not ok:
            if not self.loop:
                raise DeviceLost("replay finished")
            self._cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
            ok, img = self._cap.read()
            if not ok:
                raise CorruptFrame("replay video returned no frames")
        return img

    def close(self) -> None:
        if self._cap is not None:
            self._cap.release()
            self._cap = None


# --------------------------------------------------------------------- Synthetic
class SyntheticCapture(CaptureBackend):
    """Deterministic synthetic runner scene for demos and tests (not real game imagery)."""

    name = "synthetic"

    def __init__(self, width: int = 960, height: int = 540, speed: float = 420.0,
                 clock: Optional[Clock] = None) -> None:
        self.w, self.h, self.speed = width, height, speed
        self.clock = clock or Clock()
        self.t0 = self.clock.now()
        self.ground = int(height * 0.82)
        # (spawn time s, kind) repeating pattern
        self.pattern = [(0.8, "low"), (2.2, "high"), (3.4, "low"), (4.6, "high"), (5.4, "low")]
        self.period = 6.5

    def render(self, t: float) -> np.ndarray:
        w, h, g = self.w, self.h, self.ground
        img = np.zeros((h, w, 3), np.uint8)
        grad = np.linspace(70, 120, g, dtype=np.uint8)
        img[:g, :, 0] = grad[:, None] + 60
        img[:g, :, 1] = grad[:, None] + 20
        img[:g, :, 2] = grad[:, None]
        img[g:, :] = (40, 90, 60)
        cv2.line(img, (0, g), (w, g), (30, 60, 40), 3)
        # runner
        rx = int(w * 0.18)
        cv2.rectangle(img, (rx - 18, g - 70), (rx + 18, g), (60, 150, 230), -1)
        cv2.circle(img, (rx, g - 82), 16, (80, 170, 240), -1)
        cycle = t % self.period
        for spawn, kind in self.pattern:
            dt = cycle - spawn
            x = int(w + 40 - dt * self.speed)
            if x < -80 or x > w + 80:
                continue
            if kind == "low":
                cv2.rectangle(img, (x, g - int(h * 0.14)), (x + 46, g), (30, 30, 200), -1)
            else:
                cv2.rectangle(img, (x, 0), (x + 60, int(h * 0.5)), (40, 40, 40), -1)
        return img

    def grab(self) -> np.ndarray:
        return self.render(self.clock.now() - self.t0)


def build_capture(cfg: CaptureConfig, adb: AdbConfig) -> CaptureBackend:
    if cfg.backend == "mss":
        return MSSCapture(cfg)
    if cfg.backend == "adb":
        return ADBCapture(adb)
    if cfg.backend == "replay":
        return ReplayCapture(cfg.replay_path, cfg.replay_loop)
    return SyntheticCapture()


# ------------------------------------------------------------------------ Worker
class CaptureWorker:
    """Owns one capture backend in a dedicated thread and publishes into LatestFrame."""

    MAX_SOFT_ERRORS = 3

    def __init__(self, factory: Callable[[], CaptureBackend], latest: LatestFrame, fps: float,
                 clock: Clock, on_event: Callable[[str, str], None]) -> None:
        self.factory = factory
        self.latest = latest
        self.interval = 1.0 / max(1.0, fps)
        self.clock = clock
        self.on_event = on_event  # (kind, message) kind in {"ok","error","lost","info"}
        self.capture_ms = DurationStats()
        self.rate = RateMeter(clock=clock)
        self.errors = 0
        self.last_error: Optional[str] = None
        self.connected = False
        self.backend_name = "?"
        self.is_device = False
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._next_id = 0
        self._id_lock = threading.Lock()

    def set_start_id(self, start: int) -> None:
        with self._id_lock:
            self._next_id = max(self._next_id, start)

    @property
    def last_id(self) -> int:
        with self._id_lock:
            return self._next_id

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="capture", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout)

    def _run(self) -> None:
        backend: Optional[CaptureBackend] = None
        soft = 0
        backoff = 0.5
        while not self._stop.is_set():
            if backend is None:
                try:
                    backend = self.factory()
                    self.backend_name = backend.name
                    self.is_device = backend.is_device
                    backend.open()
                    soft = 0
                    backoff = 0.5
                    self.on_event("info", f"capture backend '{backend.name}' opened")
                except Exception as exc:
                    self._fail(backend, exc, lost=True)
                    backend = None
                    self._stop.wait(backoff)
                    backoff = min(5.0, backoff * 2)
                    continue
            t0 = self.clock.now()
            try:
                img = backend.grab()
                if img is None or img.ndim != 3 or img.shape[0] < 16 or img.shape[1] < 16:
                    raise CorruptFrame("empty or undersized frame")
                if not img.flags["C_CONTIGUOUS"]:
                    img = np.ascontiguousarray(img)
                t1 = self.clock.now()
                with self._id_lock:
                    self._next_id += 1
                    fid = self._next_id
                self.latest.put(Frame(fid, t1, img, img.shape[1], img.shape[0], backend.name))
                self.capture_ms.add((t1 - t0) * 1000.0)
                self.rate.tick()
                soft = 0
                if not self.connected:
                    self.connected = True
                    self.on_event("ok", "capture delivering frames")
            except DeviceLost as exc:
                self._fail(backend, exc, lost=True)
                backend.close()
                backend = None
                self._stop.wait(backoff)
                continue
            except Exception as exc:  # timeouts, corrupt frames, transient errors
                soft += 1
                self.errors += 1
                self.last_error = str(exc)
                self.on_event("error", f"capture error ({soft}/{self.MAX_SOFT_ERRORS}): {exc}")
                if soft >= self.MAX_SOFT_ERRORS:
                    self._fail(backend, exc, lost=True)
                    backend.close()
                    backend = None
                    self._stop.wait(backoff)
                    continue
            elapsed = self.clock.now() - t0
            self._stop.wait(max(0.0, self.interval - elapsed))
        if backend is not None:
            backend.close()
        self.connected = False

    def _fail(self, backend: Optional[CaptureBackend], exc: Exception, lost: bool) -> None:
        self.errors += 1
        self.last_error = str(exc)
        was = self.connected
        self.connected = False
        if lost and (was or self.errors <= 1 or self.errors % 10 == 0):
            self.on_event("lost", f"capture unavailable: {exc}")


