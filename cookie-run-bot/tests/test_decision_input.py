import numpy as np

from backend.config import AppConfig
from backend.decision_engine import DecisionEngine, new_command
from backend.geometry import PxRect
from backend.input_handler import DryRunInput, InputBackend, InputContext, InputError, InputWorker
from backend.telemetry import FakeClock
from backend.vision_engine import RoiResult, VisionResult

CAPS_ALL = DryRunInput().capabilities()
CAPS_ADB = {"double_jump": True, "timed_hold": True, "independent_hold_release": False}


def rr(name, confirmed, onset):
    return RoiResult(name, PxRect(0, 0, 10, 10), confirmed, confirmed, onset, 0.1, 0.5, [], 0, 0)


def vr(fid, ts, jump=(False, 0), slide=(False, 0)):
    return VisionResult(fid, ts, 100, 100, rr("jump", *jump), rr("slide", *slide), 1.0)


def test_cooldown_and_duplicate_suppression():
    cfg = AppConfig()
    d = DecisionEngine()
    clock = FakeClock()
    t = clock.now()
    out = d.evaluate(vr(1, t, jump=(True, 1)), t, True, cfg, CAPS_ALL)
    assert [c.action for c in out.commands] == ["jump"]
    out = d.evaluate(vr(2, t + 0.01, jump=(True, 1)), t + 0.01, True, cfg, CAPS_ALL)
    assert not out.commands and out.suppressed["jump"] == "duplicate"
    # new obstacle but within cooldown
    out = d.evaluate(vr(3, t + 0.1, jump=(True, 2)), t + 0.1, True, cfg, CAPS_ALL)
    assert out.suppressed["jump"] == "cooldown"
    # after cooldown, a new onset triggers again
    t2 = t + cfg.decision.jump_cooldown_ms / 1000 + 0.01
    out = d.evaluate(vr(4, t2, jump=(True, 3)), t2, True, cfg, CAPS_ALL)
    assert [c.action for c in out.commands] == ["jump"]


def test_not_running_and_stale_frames_suppressed():
    cfg = AppConfig()
    d = DecisionEngine()
    out = d.evaluate(vr(1, 10.0, jump=(True, 1)), 10.0, False, cfg, CAPS_ALL)
    assert not out.commands and out.suppressed["jump"] == "not_running"
    stale_now = 10.0 + cfg.decision.max_frame_age_ms / 1000 + 0.001
    d.reset()
    out = d.evaluate(vr(1, 10.0, jump=(True, 1)), stale_now, True, cfg, CAPS_ALL)
    assert not out.commands
    assert out.suppressed["jump"] in ("stale_frame", "detection_expired")


def test_detection_expiration():
    cfg = AppConfig.model_validate({"decision": {"detection_ttl_ms": 50, "max_frame_age_ms": 500}})
    d = DecisionEngine()
    out = d.evaluate(vr(1, 10.0, jump=(True, 1)), 10.2, True, cfg, CAPS_ALL)
    assert not out.commands and out.suppressed["jump"] == "detection_expired"


def test_arbitration_modes():
    for mode, expected in (("prefer_jump", ["jump"]), ("prefer_slide", ["slide"]), ("suppress_both", [])):
        cfg = AppConfig.model_validate({"decision": {"arbitration": mode}})
        out = DecisionEngine().evaluate(vr(1, 5.0, (True, 1), (True, 1)), 5.0, True, cfg, CAPS_ALL)
        assert [c.action for c in out.commands] == expected


def test_hold_and_double_jump_respect_capabilities():
    cfg = AppConfig.model_validate({"decision": {"slide_hold": True, "double_jump": True}})
    out = DecisionEngine().evaluate(vr(1, 5.0, slide=(True, 1)), 5.0, True, cfg, CAPS_ADB)
    assert out.commands[0].kind == "timed_hold"  # ADB cannot hold/release independently
    out = DecisionEngine().evaluate(vr(1, 5.0, slide=(True, 1)), 5.0, True, cfg, CAPS_ALL)
    assert out.commands[0].kind == "hold"
    out = DecisionEngine().evaluate(vr(1, 5.0, slide=(True, 1)), 5.0, True, cfg, {"tap": True})
    assert out.commands[0].kind == "tap" and out.notes
    out = DecisionEngine().evaluate(vr(1, 5.0, jump=(True, 1)), 5.0, True, cfg, CAPS_ADB)
    assert out.commands[0].repeat_after_ms == cfg.decision.double_jump_delay_ms


# ------------------------------------------------------------------ worker
class Recorder(InputBackend):
    name = "fake"
    is_real = True

    def __init__(self, fail=False):
        self.calls = []
        self.fail = fail

    def capabilities(self):
        return {**super().capabilities(), "double_jump": True, "independent_hold_release": True}

    def tap(self, action, ctx, tap_ms):
        if self.fail:
            raise InputError("adb input timed out after 2.0s")
        self.calls.append(("tap", action))

    def hold_start(self, action, ctx):
        self.calls.append(("down", action))

    def hold_end(self, action, ctx):
        self.calls.append(("up", action))


def make_worker(clock, backend, allowed=lambda c: None, errors=None):
    ctx = InputContext(AppConfig(), 100, 100, False)
    errors = errors if errors is not None else []
    return InputWorker(clock, 4, lambda c: backend, allowed, lambda: ctx, errors.append, lambda *a: None)


def cmd(clock, action="jump", kind="tap", age_ms=100, **kw):
    now = clock.now()
    return new_command(action=action, kind=kind, created=now, deadline=now + age_ms / 1000, frame_id=1,
                       obstacle_id=1, **kw)


def test_expired_commands_dropped():
    clock = FakeClock()
    b = Recorder()
    w = make_worker(clock, b)
    w.submit(cmd(clock))
    clock.advance(0.2)
    w.process_once()
    assert b.calls == [] and w.expired == 1


def test_queue_bounded_and_clear():
    clock = FakeClock()
    b = Recorder()
    w = make_worker(clock, b)
    for _ in range(10):
        w.submit(cmd(clock))
    assert w.q.qsize() == 4 and w.dropped == 6
    assert w.clear() == 4
    w.process_once()
    assert b.calls == []


def test_hold_release_scheduled_and_released_on_stop():
    clock = FakeClock()
    b = Recorder()
    w = make_worker(clock, b)
    w.submit(cmd(clock, "slide", "hold", hold_ms=300))
    w.process_once()
    assert b.calls == [("down", "slide")]
    clock.advance(0.31)
    w.process_once()
    assert b.calls[-1] == ("up", "slide")
    w.submit(cmd(clock, "slide", "hold", hold_ms=300))
    w.process_once()
    w.clear_and_release()  # Stop/Pause path
    assert b.calls[-1] == ("up", "slide")


def test_double_jump_follow_up_and_cancel():
    clock = FakeClock()
    b = Recorder()
    w = make_worker(clock, b)
    w.submit(cmd(clock, repeat_after_ms=120))
    w.process_once()
    clock.advance(0.13)
    w.process_once()
    assert b.calls == [("tap", "jump"), ("tap", "jump")]
    w.submit(cmd(clock, repeat_after_ms=120))
    w.process_once()
    w.clear()
    clock.advance(0.2)
    w.process_once()
    assert b.calls.count(("tap", "jump")) == 3


def test_input_timeout_reports_error_and_gate_rejects():
    clock = FakeClock()
    errors = []
    w = make_worker(clock, Recorder(fail=True), errors=errors)
    w.submit(cmd(clock))
    w.process_once()
    assert errors and "timed out" in str(errors[0])
    b = Recorder()
    w2 = make_worker(clock, b, allowed=lambda c: "state PAUSED")
    w2.submit(cmd(clock))
    w2.process_once()
    assert b.calls == [] and w2.rejected == 1


def test_synthetic_vision_detects_obstacles():
    from backend.capture import SyntheticCapture
    from backend.vision_engine import VisionEngine
    cfg = AppConfig()
    cap = SyntheticCapture()
    v = VisionEngine()
    jumps = slides = 0
    for i in range(int(6.5 * 30)):
        r = v.process(i, i / 30, cap.render(i / 30), cfg)
        jumps += r.jump.confirmed
        slides += r.slide.confirmed
    assert jumps > 0 and slides > 0
    # empty frame -> no detections
    v.reset()
    blank = np.full((540, 960, 3), 90, np.uint8)
    assert not any(v.process(i, 0, blank, cfg).jump.confirmed for i in range(5))
