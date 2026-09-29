"""Debounced game-screen state detection built from user-taught templates.

The detector is deliberately independent from bot control.  It observes frames,
turns Autoplay screen roles into a small set of game states, and exposes a
thread-safe snapshot.  BotController decides whether the snapshot should only
be displayed or should guard automatic input.
"""
from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Any, Optional

import cv2
import numpy as np

from .config import GameStateConfig


ROLE_TO_STATE = {
    "running": "RUNNING",
    "ready": "READY",
    "revive": "REVIVE",
    "shop": "SHOP",
    "result": "RESULT",
    "danger": "DANGER",
    "no_hearts": "NO_HEARTS",
    "tap": "MENU",
    "item_screen": "MENU",
    "map_select": "MENU",
    "map": "MENU",
    "roll": "MENU",
    "item": "MENU",
}


@dataclass(frozen=True)
class GameStateSnapshot:
    state: str = "UNKNOWN"
    candidate: str = "UNKNOWN"
    screen: Optional[str] = None
    role: Optional[str] = None
    score: float = 0.0
    changed: bool = False
    observed_at: float = 0.0
    state_since: float = 0.0
    transitions: int = 0
    samples: int = 0

    def to_dict(self, now: float, configured: bool) -> dict[str, Any]:
        return {
            "state": self.state,
            "candidate": self.candidate,
            "screen": self.screen,
            "role": self.role,
            "score": round(self.score, 3),
            "age_ms": round(max(0.0, now - self.observed_at) * 1000, 1) if self.observed_at else None,
            "state_ms": round(max(0.0, now - self.state_since) * 1000, 1) if self.state_since else 0.0,
            "transitions": self.transitions,
            "samples": self.samples,
            "configured": configured,
        }


class GameStateDetector:
    """Classify at a bounded rate and debounce state changes across frames."""

    def __init__(self, screens, on_transition=None) -> None:
        self.screens = screens
        self.on_transition = on_transition
        self._lock = threading.Lock()
        self._snapshot = GameStateSnapshot()
        self._candidate_count = 0
        self._last_sample_at = float("-inf")
        self._last_known_at = float("-inf")
        self._last_frame_id: Optional[int] = None

    def reset(self) -> None:
        with self._lock:
            self._snapshot = GameStateSnapshot()
            self._candidate_count = 0
            self._last_sample_at = float("-inf")
            self._last_known_at = float("-inf")
            self._last_frame_id = None

    def configured(self) -> bool:
        return any(e.get("role") == "running" for e in self.screens.entries())

    def snapshot(self) -> GameStateSnapshot:
        with self._lock:
            return self._snapshot

    def status(self, now: float, cfg: GameStateConfig) -> dict[str, Any]:
        data = self.snapshot().to_dict(now, self.configured())
        data.update({"enabled": cfg.enabled, "guard_actions": cfg.guard_actions})
        data["actions_allowed"] = not (cfg.enabled and cfg.guard_actions) or data["state"] == "RUNNING"
        return data

    def observe(self, image: np.ndarray, now: float, cfg: GameStateConfig,
                frame_id: Optional[int] = None, force: bool = False) -> GameStateSnapshot:
        """Observe one BGR frame. Reusing a frame never advances debounce counts."""
        with self._lock:
            if frame_id is not None and frame_id == self._last_frame_id:
                return self._snapshot
            interval = cfg.sample_interval_ms / 1000.0
            if not force and now - self._last_sample_at < interval:
                return self._snapshot
            self._last_sample_at = now
            self._last_frame_id = frame_id

        gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
        role, entry, _tap = self.screens.classify(gray)
        raw_state = ROLE_TO_STATE.get(role, "UNKNOWN")
        score = self.screens.find(gray, entry)[0] if entry is not None else 0.0
        screen = entry.get("name") if entry else None

        callback = None
        with self._lock:
            previous = self._snapshot
            if raw_state != "UNKNOWN":
                self._last_known_at = now

            # Brief missing-template flicker retains the stable state. A real
            # DANGER match is immediate; other states require confirmation.
            within_grace = raw_state == "UNKNOWN" and previous.state != "UNKNOWN" and (
                now - self._last_known_at) * 1000 <= cfg.unknown_grace_ms
            candidate = previous.candidate if within_grace else raw_state
            if within_grace:
                count = self._candidate_count
            elif candidate == previous.candidate:
                count = self._candidate_count + 1
            else:
                count = 1

            required = 1 if candidate in ("DANGER", "SHOP") else cfg.confirm_frames
            state = previous.state
            state_since = previous.state_since
            changed = False
            if not within_grace and candidate != state and count >= required:
                state, state_since, changed = candidate, now, True

            self._candidate_count = count
            self._snapshot = GameStateSnapshot(
                state=state, candidate=candidate, screen=screen, role=role, score=float(score),
                changed=changed, observed_at=now, state_since=state_since,
                transitions=previous.transitions + int(changed), samples=previous.samples + 1,
            )
            if changed and self.on_transition:
                callback = (previous.state, state, screen)
            result = self._snapshot

        if callback:
            self.on_transition(*callback)
        return result
