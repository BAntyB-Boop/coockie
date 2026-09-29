"""Telemetry primitives: monotonic clock, rate meters, duration stats, bounded logs."""
from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import Callable, Optional


class Clock:
    """Monotonic clock abstraction (seconds). Replace with FakeClock in tests."""

    def now(self) -> float:
        return time.monotonic()

    def sleep(self, seconds: float) -> None:
        if seconds > 0:
            time.sleep(seconds)


class FakeClock(Clock):
    def __init__(self, start: float = 1000.0) -> None:
        self.t = start
        self._lock = threading.Lock()

    def now(self) -> float:
        with self._lock:
            return self.t

    def advance(self, seconds: float) -> None:
        with self._lock:
            self.t += seconds

    def sleep(self, seconds: float) -> None:
        self.advance(max(0.0, seconds))


def percentile(values: list[float], pct: float) -> Optional[float]:
    if not values:
        return None
    s = sorted(values)
    if len(s) == 1:
        return s[0]
    k = (len(s) - 1) * (pct / 100.0)
    lo = int(k)
    hi = min(lo + 1, len(s) - 1)
    return s[lo] + (s[hi] - s[lo]) * (k - lo)


class RateMeter:
    """Events per second over a sliding window (monotonic timestamps)."""

    def __init__(self, window_s: float = 2.0, clock: Optional[Clock] = None) -> None:
        self.window_s = window_s
        self.clock = clock or Clock()
        self._ts: deque[float] = deque(maxlen=4096)
        self._lock = threading.Lock()

    def tick(self) -> None:
        with self._lock:
            self._ts.append(self.clock.now())

    def rate(self) -> float:
        now = self.clock.now()
        with self._lock:
            while self._ts and now - self._ts[0] > self.window_s:
                self._ts.popleft()
            n = len(self._ts)
            if n < 2:
                return 0.0
            span = self._ts[-1] - self._ts[0]
            return (n - 1) / span if span > 0 else 0.0


class DurationStats:
    def __init__(self, maxlen: int = 300) -> None:
        self._v: deque[float] = deque(maxlen=maxlen)
        self._lock = threading.Lock()

    def add(self, ms: float) -> None:
        with self._lock:
            self._v.append(ms)

    def snapshot(self) -> dict:
        with self._lock:
            vals = list(self._v)
        return {
            "last": round(vals[-1], 2) if vals else None,
            "p50": _r(percentile(vals, 50)),
            "p95": _r(percentile(vals, 95)),
            "n": len(vals),
        }


def _r(v: Optional[float]) -> Optional[float]:
    return None if v is None else round(v, 2)


@dataclass
class LogEntry:
    seq: int
    t: float  # wall clock, display only
    level: str
    msg: str


class LogBuffer:
    def __init__(self, maxlen: int = 500, sink: Optional[Callable[[str, str], None]] = None) -> None:
        self._d: deque[LogEntry] = deque(maxlen=maxlen)
        self._seq = 0
        self._lock = threading.Lock()
        self._sink = sink

    def add(self, level: str, msg: str) -> None:
        with self._lock:
            self._seq += 1
            self._d.append(LogEntry(self._seq, time.time(), level, msg[:500]))
        if self._sink:
            self._sink(level, msg)

    def since(self, seq: int) -> list[LogEntry]:
        with self._lock:
            return [e for e in self._d if e.seq > seq]

    @property
    def last_seq(self) -> int:
        with self._lock:
            return self._seq
