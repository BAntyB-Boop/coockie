import numpy as np
import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from backend.app import Settings, create_app
from backend.autoplay import Autoplay, Screens
from backend.bot_controller import BotController, State
from backend.capture import SyntheticCapture
from backend.schemas import AutoplayStart, ScreenEntry
from conftest import wait_until

W, H, PW, PH = 1280, 720, 60, 40


def patch(seed):
    return np.random.RandomState(seed).randint(0, 256, (PH, PW, 3), np.uint8)


def frame(*items):  # items: (seed, x, y)
    img = np.zeros((H, W, 3), np.uint8)
    for seed, x, y in items:
        img[y:y + PH, x:x + PW] = patch(seed)
    return img


class FakeIO:
    def __init__(self):
        self.calls, self.alerts, self.vis = [], [], []

    def tap(self, x, y): self.calls.append(("tap", x, y))
    def swipe(self, *a): self.calls.append(("swipe", *a))
    def back(self): self.calls.append(("back",))
    def alert(self, msg, img): self.alerts.append(msg)
    def log(self, msg): pass

    def vision(self, on):
        self.vis.append(on)
        return []

    def taps(self): return [c for c in self.calls if c[0] == "tap"]


@pytest.fixture
def ap(tmp_path):
    return Autoplay(None, io=FakeIO(), root=tmp_path / "screens")


def teach(ap, name, role, seed, x, y, tap="default", **kw):
    e = {"name": name, "role": role, "box": [x, y, PW, PH], "threshold": 0.8, "profile": None,
         "tap": [x + 5, y + 5] if tap == "default" else tap, **kw}
    ap.screens.save(ScreenEntry(**e).model_dump(), frame((seed, x, y)))


def run(ap, **kw):
    ap.configure(AutoplayStart(**{"loops": 5, **kw}))


def test_danger_beats_tap_and_never_taps(ap):
    teach(ap, "play", "tap", 1, 100, 100)
    teach(ap, "shop", "danger", 2, 400, 100, tap=None)
    run(ap)
    ap.step(frame((1, 100, 100), (2, 400, 100)), 0)
    assert not ap.active and ap.alert and "danger screen shop" in ap.message
    assert ap.io.taps() == [] and len(ap.io.alerts) == 1


def test_loop_count_and_stop_without_last_tap(ap):
    teach(ap, "res", "result", 3, 100, 100)
    teach(ap, "run", "running", 4, 600, 50, tap=None)
    run(ap, loops=2)
    res, running = frame((3, 100, 100)), frame((4, 600, 50))
    ap.step(res, 0)
    assert ap.loops_done == 1 and ap.io.taps() == [("tap", 105, 105)]
    assert ap.io.vis == [False]  # menu screen pauses the bot even if autoplay never started it
    ap.step(res, 0.5)  # cooldown
    ap.step(res, 2.0)  # same result screen: not recounted, tapped again
    assert ap.loops_done == 1 and len(ap.io.taps()) == 2
    ap.step(running, 3.0)
    assert ap.io.vis[-1] is True
    ap.step(res, 6.0)
    assert not ap.active and not ap.alert and ap.message == "done 2/2" and len(ap.io.taps()) == 2
    assert ap.io.vis[-1] is False


def test_roll_until_cap_then_start(ap):
    teach(ap, "items", "item_screen", 5, 100, 100)
    teach(ap, "roll", "roll", 6, 100, 300)
    teach(ap, "sword", "item", 7, 500, 300, tap=None)
    run(ap, items=["sword"], roll_cap=2)
    scr = frame((5, 100, 100), (6, 100, 300))
    for t in (0, 3, 6):
        ap.step(scr, t)
    assert ap.io.taps() == [("tap", 105, 305), ("tap", 105, 305), ("tap", 105, 105)] and ap.rolls == 2


def test_start_immediately_with_wanted_item(ap):
    teach(ap, "items", "item_screen", 5, 100, 100)
    teach(ap, "roll", "roll", 6, 100, 300)
    teach(ap, "sword", "item", 7, 500, 300, tap=None)
    run(ap, items=["sword"])
    ap.step(frame((5, 100, 100), (6, 100, 300), (7, 500, 300)), 0)
    assert ap.io.taps() == [("tap", 105, 105)] and ap.rolls == 0


def test_map_swipe_then_tap_and_give_up(ap):
    teach(ap, "sel", "map_select", 8, 100, 100)
    teach(ap, "m1", "map", 9, 100, 300, tap=None)
    run(ap, map="m1")
    ap.step(frame((8, 100, 100)), 0)
    assert ap.io.calls == [("swipe", 1024, 360, 256, 360)]
    ap.step(frame((8, 100, 100), (9, 700, 400)), 2)  # map found elsewhere: tap follows it (box centre)
    assert ap.io.calls[-1] == ("tap", 730, 420) and ap.swipes == 0
    for i in range(10):
        ap.step(frame((8, 100, 100)), 10 + 2 * i)
    assert ap.swipes == 10 and ap.active
    ap.step(frame((8, 100, 100)), 40)
    assert not ap.active and ap.alert and "map not found" in ap.message


def test_map_select_without_map_stops(ap):
    teach(ap, "sel", "map_select", 8, 100, 100)
    run(ap)
    ap.step(frame((8, 100, 100)), 0)
    assert not ap.active and ap.alert and ap.io.calls == []


def test_unknown_back_once_then_stuck(ap):
    teach(ap, "play", "tap", 1, 100, 100)
    run(ap)
    blank = frame()
    ap.step(blank, 0)
    ap.step(blank, 4.9)
    assert ap.io.calls == []
    ap.step(blank, 5.0)
    ap.step(blank, 20.0)
    assert ap.io.calls == [("back",)] and ap.active
    ap.step(blank, 60.0)
    assert not ap.active and ap.alert and "stuck" in ap.message


def test_no_hearts_waits(ap):
    teach(ap, "hearts", "no_hearts", 10, 100, 100)
    teach(ap, "play", "tap", 1, 400, 100)
    run(ap, heart_wait_min=12)
    ap.step(frame((10, 100, 100)), 0)
    ap.step(frame((1, 400, 100)), 100)  # still waiting
    assert ap.io.taps() == [("tap", 105, 105)]
    ap.step(frame((1, 400, 100)), 721)
    assert len(ap.io.taps()) == 2


def test_running_flicker_grace(ap):
    teach(ap, "run", "running", 4, 600, 50, tap=None)
    run(ap)
    ap.step(frame((4, 600, 50)), 0)
    ap.step(frame(), 0.5)  # marker flicker: vision stays on
    assert ap.io.vis == [True]
    ap.step(frame(), 1.5)
    assert ap.io.vis[0] is True and ap.io.vis[-1] is False


def test_entry_validation(ap):
    ok = {"name": "a", "role": "tap", "box": [10, 10, 20, 20], "tap": [12, 12]}
    ScreenEntry(**ok)
    for bad in ({"name": "../x"}, {"box": [10, 10, 4, 20]}, {"tap": None}, {"role": "nope"},
                {"profile": "p"}, {"threshold": 0.2}):
        with pytest.raises(ValidationError):
            ScreenEntry(**{**ok, **bad})
    with pytest.raises(ValueError):  # box outside frame
        ap.screens.save({**ScreenEntry(**ok).model_dump(), "box": [W - 5, 0, 20, 20]}, frame())
    with pytest.raises(ValueError):  # flat crop
        ap.screens.save(ScreenEntry(**ok).model_dump(), frame())
    assert Screens(ap.screens.dir).entries() == []


def test_api_teach_list_delete_start(tmp_store, tmp_path):
    app = create_app(Settings(persist=False, enable_hotkey=False, allowed_hosts={"testserver"}),
                     controller_factory=lambda: BotController(
                         tmp_store, capture_factory=lambda c: SyntheticCapture(), estop_file=tmp_path / "ESTOP"))
    with TestClient(app) as client:
        c = client.app.state.controller
        c.autoplay.screens = Screens(tmp_path / "screens")
        assert wait_until(lambda: c.latest.peek() is not None)
        body = {"name": "play", "role": "tap", "box": [100, 420, 80, 40], "tap": [110, 430]}
        assert client.post("/api/autoplay/screens", json={**body, "role": "nope"}).status_code == 422
        assert client.post("/api/autoplay/screens", json={**body, "tap": None}).status_code == 422
        assert client.post("/api/autoplay/screens", json={**body, "box": [900, 500, 80, 80]}).status_code == 422
        assert client.get("/api/autoplay/screens").json() == {"screens": []}
        r = client.post("/api/autoplay/screens", json=body).json()
        assert r["ok"] and r["screen"]["name"] == "play"
        assert [s["name"] for s in client.get("/api/autoplay/screens").json()["screens"]] == ["play"]
        assert "play" in client.get("/api/autoplay/match").json()["scores"]
        assert client.get("/api/status").json()["autoplay"]["active"] is False
        st = client.post("/api/autoplay/start", json={"loops": 3}).json()  # no running/result taught
        assert st["ok"] is False and len(st["reasons"]) == 2
        assert client.post("/api/autoplay/stop").json()["ok"]
        assert client.delete("/api/autoplay/screens/play").json()["ok"]
        assert client.delete("/api/autoplay/screens/play").status_code == 404
        assert client.post("/api/autoplay/start", json={"loops": 0}).status_code == 422


def test_names_case_insensitive_and_newline(ap):
    teach(ap, "shop", "danger", 2, 400, 100, tap=None)
    teach(ap, "Shop", "tap", 8, 400, 100)  # same png on Windows: replaces, never leaves two entries
    assert [e["name"] for e in ap.screens.entries()] == ["Shop"]
    assert ap.screens.delete("Shop") and ap.screens.entries() == []
    for bad in ("a\n", "a\r"):
        with pytest.raises(ValidationError):
            ScreenEntry(name=bad, role="tap", box=[10, 10, 20, 20], tap=[12, 12])


def test_missing_entry_mid_session_is_no_match(ap):
    teach(ap, "run", "running", 4, 600, 50, tap=None)
    gray = np.zeros((H, W), np.uint8)
    assert ap.screens.hit(gray, None) is None
    assert ap.screens.find(gray, {**ap.screens.get("run"), "name": "gone"}) == (0.0, (0, 0))


def test_second_start_while_starting_is_refused(ap):
    ap._starting = True
    assert ap.start(AutoplayStart(loops=1)) == ["autoplay already active"]


def test_controller_stop_ends_autoplay_and_keeps_pause_reason(tmp_store, tmp_path):
    c = BotController(tmp_store, capture_factory=lambda c: SyntheticCapture(), estop_file=tmp_path / "ESTOP")
    c.autoplay.configure(AutoplayStart(loops=1))  # active while the controller is STOPPED (menu taps)
    c.stop("emergency stop hotkey (F12)")
    assert not c.autoplay.active
    c.autoplay.configure(AutoplayStart(loops=1))
    c.autoplay.vision_on = True
    c.state = State.RUNNING
    try:
        assert c.pause("paused by user") is True and c.state_reason == "paused by user" and not c.autoplay.active
    finally:
        c.shutdown()
