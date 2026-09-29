import numpy as np

from backend.bot_controller import BotController, State
from backend.capture import Frame, SyntheticCapture
from backend.config import GameStateConfig
from backend.game_state import GameStateDetector, GameStateSnapshot


class FakeScreens:
    def __init__(self, observations, configured=True):
        self.observations = list(observations)
        self.last = (None, None, None, 0.0)
        self.has_running = configured

    def entries(self):
        return [{"name": "run", "role": "running"}] if self.has_running else []

    def classify(self, _gray):
        if self.observations:
            role, name, score = self.observations.pop(0)
            entry = {"name": name, "role": role, "threshold": 0.8} if role else None
            self.last = (role, entry, None, score)
        return self.last[:3]

    def find(self, _gray, _entry):
        return self.last[3], (0, 0)


def test_detector_confirms_states_and_tolerates_marker_flicker():
    screens = FakeScreens([
        ("running", "run", .91), ("running", "run", .93),
        (None, None, 0), (None, None, 0), (None, None, 0),
    ])
    cfg = GameStateConfig(sample_interval_ms=50, confirm_frames=2, unknown_grace_ms=500)
    detector = GameStateDetector(screens)
    image = np.zeros((40, 60, 3), np.uint8)

    assert detector.observe(image, 0.00, cfg, 1).state == "UNKNOWN"
    running = detector.observe(image, 0.10, cfg, 2)
    assert running.state == "RUNNING" and running.changed and running.score == .93
    assert detector.observe(image, 0.20, cfg, 3).state == "RUNNING"
    assert detector.observe(image, 0.70, cfg, 4).state == "RUNNING"
    unknown = detector.observe(image, 0.80, cfg, 5)
    assert unknown.state == "UNKNOWN" and unknown.changed


def test_danger_transition_is_immediate():
    screens = FakeScreens([("danger", "crystal_shop", .88)])
    cfg = GameStateConfig(confirm_frames=5)
    got = []
    detector = GameStateDetector(screens, lambda old, new, name: got.append((old, new, name)))
    snap = detector.observe(np.zeros((10, 10, 3), np.uint8), 1.0, cfg, 1)
    assert snap.state == "DANGER" and snap.changed
    assert got == [("UNKNOWN", "DANGER", "crystal_shop")]


def test_reusing_frame_does_not_advance_confirmation():
    screens = FakeScreens([("running", "run", .9), ("running", "run", .9)])
    cfg = GameStateConfig(confirm_frames=2)
    detector = GameStateDetector(screens)
    image = np.zeros((10, 10, 3), np.uint8)
    assert detector.observe(image, 0.0, cfg, 7).state == "UNKNOWN"
    assert detector.observe(image, 1.0, cfg, 7).state == "UNKNOWN"
    assert detector.observe(image, 1.1, cfg, 8).state == "RUNNING"


class StubGameState:
    def __init__(self, snapshots):
        self.snapshots = iter(snapshots)
        self.current = GameStateSnapshot()

    def observe(self, *_args, **_kwargs):
        self.current = next(self.snapshots)
        return self.current

    def snapshot(self):
        return self.current

    def reset(self):
        pass

    def configured(self):
        return True

    def status(self, now, cfg):
        data = self.current.to_dict(now, True)
        data.update(enabled=cfg.enabled, guard_actions=cfg.guard_actions,
                    actions_allowed=self.current.state == "RUNNING")
        return data


def test_controller_waits_for_running_when_guard_enabled(tmp_store, tmp_path):
    cfg = tmp_store.get()
    tmp_store.set(cfg.model_copy(update={"game_state": cfg.game_state.model_copy(update={"guard_actions": True})}))
    controller = BotController(tmp_store, capture_factory=lambda _c: SyntheticCapture(),
                               estop_file=tmp_path / "ESTOP")
    controller.game_state = StubGameState([
        GameStateSnapshot(state="MENU", candidate="MENU", observed_at=1.0),
        GameStateSnapshot(state="RUNNING", candidate="RUNNING", observed_at=1.1, changed=True),
    ])
    image = np.zeros((720, 1280, 3), np.uint8)
    now = controller.clock.now()
    controller.state = State.READY
    try:
        controller.process_frame(Frame(1, now, image, 1280, 720, "test"))
        assert controller.state == State.READY and "now MENU" in controller.state_reason
        controller.process_frame(Frame(2, controller.clock.now(), image, 1280, 720, "test"))
        assert controller.state == State.RUNNING
    finally:
        controller.shutdown()


def test_guard_requires_a_taught_running_screen(tmp_store, tmp_path):
    cfg = tmp_store.get()
    tmp_store.set(cfg.model_copy(update={"game_state": cfg.game_state.model_copy(update={"guard_actions": True})}))
    controller = BotController(tmp_store, capture_factory=lambda _c: SyntheticCapture(),
                               estop_file=tmp_path / "ESTOP")
    try:
        assert "game-state guard needs a taught 'running' screen" in controller.validate_start(False)
    finally:
        controller.shutdown()
