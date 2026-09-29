"""Action timing, separated from obstacle detection.

Uses calibrated trigger zones (the ROIs): an obstacle confirmed inside a ROI is
the trigger. Time-to-contact is intentionally NOT implemented because the
baseline vision does not track objects or estimate velocity.
"""
from __future__ import annotations

import itertools
from dataclasses import dataclass, field
from typing import Optional

from .config import AppConfig
from .vision_engine import VisionResult

ACTIONS = ("jump", "slide")
_cmd_ids = itertools.count(1)


@dataclass
class Command:
    id: int
    action: str               # "jump" | "slide"
    kind: str                 # "tap" | "hold" | "timed_hold"
    created: float            # monotonic seconds
    deadline: float           # monotonic seconds; expired commands are dropped
    frame_id: int
    obstacle_id: int
    tap_ms: int = 40
    hold_ms: int = 0
    repeat_after_ms: Optional[int] = None  # double jump
    test: bool = False

    def to_dict(self) -> dict:
        return {"id": self.id, "action": self.action, "kind": self.kind, "frame_id": self.frame_id,
                "obstacle_id": self.obstacle_id, "hold_ms": self.hold_ms,
                "double": self.repeat_after_ms is not None, "test": self.test}


def new_command(**kw) -> Command:
    return Command(id=next(_cmd_ids), **kw)


@dataclass
class DecisionOutcome:
    frame_id: int
    frame_age_ms: float
    detected: list[str] = field(default_factory=list)
    commands: list[Command] = field(default_factory=list)
    suppressed: dict[str, str] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)


class DecisionEngine:
    def __init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        self.last_action_t: dict[str, float] = {}
        self.last_obstacle: dict[str, int] = {}
        self.active: dict[str, tuple[int, float, int]] = {}  # action -> (onset_id, frame_ts, frame_id)

    def evaluate(self, result: VisionResult, now: float, running: bool, cfg: AppConfig,
                 caps: dict) -> DecisionOutcome:
        d = cfg.decision
        age_ms = (now - result.frame_ts) * 1000.0
        out = DecisionOutcome(result.frame_id, age_ms)

        for name, rr in (("jump", result.jump), ("slide", result.slide)):
            if rr.confirmed:
                self.active[name] = (rr.onset_id, result.frame_ts, result.frame_id)
            else:
                self.active.pop(name, None)
        ttl = d.detection_ttl_ms / 1000.0
        for name in list(self.active):
            if now - self.active[name][1] > ttl:
                self.active.pop(name)
                out.suppressed[name] = "detection_expired"
        out.detected = [a for a in ACTIONS if a in self.active]
        if not out.detected:
            return out
        if not running:
            for a in out.detected:
                out.suppressed[a] = "not_running"
            return out
        if age_ms > d.max_frame_age_ms:
            for a in out.detected:
                out.suppressed[a] = "stale_frame"
            return out

        candidates = list(out.detected)
        if len(candidates) == 2:
            if d.arbitration == "prefer_jump":
                candidates = ["jump"]
                out.suppressed["slide"] = "arbitration"
            elif d.arbitration == "prefer_slide":
                candidates = ["slide"]
                out.suppressed["jump"] = "arbitration"
            else:
                out.suppressed["jump"] = out.suppressed["slide"] = "arbitration"
                return out

        for a in candidates:
            onset, _, fid = self.active[a]
            if self.last_obstacle.get(a) == onset:
                out.suppressed[a] = "duplicate"
                continue
            cooldown = (d.jump_cooldown_ms if a == "jump" else d.slide_cooldown_ms) / 1000.0
            last = self.last_action_t.get(a)
            if last is not None and now - last < cooldown:
                out.suppressed[a] = "cooldown"
                continue
            cmd = self._build(a, onset, fid, now, cfg, caps, out)
            out.commands.append(cmd)
            self.last_action_t[a] = now
            self.last_obstacle[a] = onset
        return out

    @staticmethod
    def _build(action: str, onset: int, fid: int, now: float, cfg: AppConfig, caps: dict,
               out: DecisionOutcome) -> Command:
        d = cfg.decision
        kind, hold, repeat = "tap", 0, None
        if action == "jump" and d.double_jump:
            if caps.get("double_jump"):
                repeat = d.double_jump_delay_ms
            else:
                out.notes.append("double jump unsupported by input backend")
        if action == "slide" and d.slide_hold:
            if caps.get("independent_hold_release"):
                kind, hold = "hold", d.slide_hold_ms
            elif caps.get("timed_hold"):
                kind, hold = "timed_hold", d.slide_hold_ms
            else:
                out.notes.append("slide hold unsupported by input backend")
        return new_command(action=action, kind=kind, created=now, deadline=now + d.max_command_age_ms / 1000.0,
                           frame_id=fid, obstacle_id=onset, tap_ms=d.tap_ms, hold_ms=hold,
                           repeat_after_ms=repeat)
