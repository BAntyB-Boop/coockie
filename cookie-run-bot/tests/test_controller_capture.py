import os
import stat
import sys
import threading

import numpy as np
import pytest

from backend.bot_controller import BotController, State, active_controllers
from backend.capture import (ADBCapture, CaptureBackend, CaptureTimeout, CorruptFrame, DeviceLost,
                             ReplayCapture, SyntheticCapture)
from backend.config import AdbConfig, AppConfig
from backend.input_handler import ADBInput, InputBackend, InputError
from conftest import wait_until


class FakeReal(InputBackend):
    name = "adb"
    is_real = True

    def __init__(self, fail=False):
        self.calls = []
        self.fail = fail
        self.released = 0

    def capabilities(self):
        return {**super().capabilities(), "double_jump": True, "timed_hold": True, "uses_coordinates": True}

    def display_size(self, ctx):
        return 960, 540

    def tap(self, action, ctx, tap_ms):
        if self.fail:
            raise InputError("simulated adb timeout")
        self.calls.append(action)

    def timed_hold(self, action, ctx, ms):
        self.calls.append(action + ":hold")

    def release_all(self):
        self.released += 1


class FlakyCapture(CaptureBackend):
    """Delivers N synthetic frames then reports the device as lost; recovers afterwards."""
    name = "flaky"

    def __init__(self, state):
        self.state = state
        self.syn = SyntheticCapture()

    def grab(self):
        self.state["n"] += 1
        if self.state["lose"]:
            raise DeviceLost("device offline")
        return self.syn.grab()


@pytest.fixture
def make_ctrl(tmp_store, tmp_path):
    made = []

    def factory(real=None, capture=None, **cfg_updates):
        cfg = tmp_store.get()
        if cfg_updates:
            cfg = AppConfig.model_validate({**cfg.model_dump(), **cfg_updates})
            tmp_store.set(cfg)
        real = real or FakeReal()
        c = BotController(tmp_store, capture_factory=capture or (lambda c: SyntheticCapture()),
                          input_factory=lambda c: real, estop_file=tmp_path / "EMERGENCY_STOP")
        made.append(c)
        c.start_services()
        assert wait_until(lambda: c.latest.peek() is not None)
        return c, real

    yield factory
    for c in made:
        c.shutdown()


def test_single_controller_per_process(make_ctrl, tmp_store):
    c, _ = make_ctrl()
    assert active_controllers() == 1
    with pytest.raises(RuntimeError):
        BotController(tmp_store)
    assert [t.name for t in threading.enumerate()].count("processing") == 1


def test_dry_run_prevents_real_input(make_ctrl):
    c, real = make_ctrl(input={"backend": "adb"})
    ok, reasons = c.start()
    assert ok, reasons
    assert wait_until(lambda: c.state == State.RUNNING)
    assert wait_until(lambda: len(c.dry.events) >= 2, timeout=8)
    assert real.calls == []
    c.stop()


def test_real_input_requires_calibration_then_works(make_ctrl):
    c, real = make_ctrl(input={"backend": "adb"})
    ok, reasons = c.set_dry_run(False)
    assert not ok and any("calibration" in r for r in reasons)
    ok, reasons = c.confirm_calibration()
    assert ok, reasons
    ok, reasons = c.set_dry_run(False)
    assert ok, reasons
    ok, _ = c.start()
    assert ok
    assert wait_until(lambda: len(real.calls) >= 1, timeout=8)
    c.pause()
    n = len(real.calls)
    assert c.state == State.PAUSED and c.input.depth == 0
    import time
    time.sleep(1.5)
    assert len(real.calls) == n  # paused: no further input
    c.stop()
    assert c.state == State.STOPPED and real.released >= 1


def test_input_failure_enters_error_and_suppresses(make_ctrl):
    c, real = make_ctrl(real=FakeReal(fail=True), input={"backend": "adb"})
    assert c.confirm_calibration()[0]
    assert c.set_dry_run(False)[0]
    assert c.start()[0]
    assert wait_until(lambda: c.state == State.ERROR, timeout=8)
    assert "input failure" in (c.last_error or "")
    assert c.input.depth == 0


def test_device_loss_errors_and_never_auto_resumes(make_ctrl):
    st = {"n": 0, "lose": False}
    c, _ = make_ctrl(capture=lambda cfg: FlakyCapture(st))
    assert c.start()[0]
    assert wait_until(lambda: c.state == State.RUNNING)
    st["lose"] = True
    assert wait_until(lambda: c.state == State.ERROR, timeout=5)
    st["lose"] = False
    assert wait_until(lambda: c._capture.connected, timeout=6)
    import time
    time.sleep(0.3)
    assert c.state == State.ERROR  # reconnection does not resume


def test_resolution_change_pauses_real_input(make_ctrl):
    c, real = make_ctrl(input={"backend": "adb"})
    assert c.confirm_calibration()[0] and c.set_dry_run(False)[0]
    c.state = State.RUNNING
    from backend.capture import Frame
    img = np.zeros((600, 800, 3), np.uint8)
    c._frame_dims = (960, 540)
    c.process_frame(Frame(10**6, c.clock.now(), img, 800, 600, "test"))
    assert c.state == State.PAUSED
    cal = c.store.get().safety.calibration
    assert not cal.matches(cal.model_copy(update={"frame_w": 800, "frame_h": 600}))


def test_estop_file_stops_and_blocks_start(make_ctrl, tmp_path):
    c, _ = make_ctrl()
    assert c.start()[0]
    (tmp_path / "EMERGENCY_STOP").write_text("stop")
    assert wait_until(lambda: c.state == State.STOPPED, timeout=3)
    ok, reasons = c.start()
    assert not ok and any("emergency" in r for r in reasons)


def test_disconnect_policy(make_ctrl):
    c, _ = make_ctrl()
    assert c.start()[0]
    c.on_clients_changed(1)
    c.on_clients_changed(0)
    assert c.state == State.PAUSED
    cfg = c.store.get()
    c.store.set(cfg.model_copy(update={"safety": cfg.safety.model_copy(update={"disconnect_policy": "continue_background"})}))
    assert c.start()[0]
    c.on_clients_changed(1)
    c.on_clients_changed(0)
    assert c.state in (State.READY, State.RUNNING)


# ----------------------------------------------------------- capture backends
def _fake_adb(tmp_path, body):
    p = tmp_path / "fake_adb"
    p.write_text("#!/bin/sh\n" + body + "\n")
    p.chmod(p.stat().st_mode | stat.S_IEXEC)
    return str(p)


@pytest.mark.skipif(sys.platform == "win32", reason="uses a POSIX shell script as fake adb")
def test_adb_capture_corrupt_timeout_and_lost(tmp_path):
    corrupt = ADBCapture(AdbConfig(executable=_fake_adb(tmp_path, "echo garbage"), serial="emulator-5554"))
    with pytest.raises(CorruptFrame):
        corrupt.grab()
    slow = ADBCapture(AdbConfig(executable=_fake_adb(tmp_path, "sleep 5"), serial="x", capture_timeout_s=0.5))
    with pytest.raises(CaptureTimeout):
        slow.grab()
    lost = ADBCapture(AdbConfig(executable=_fake_adb(tmp_path, "echo \"error: device 'x' not found\" >&2; exit 1"),
                                serial="x"))
    with pytest.raises(DeviceLost):
        lost.grab()
    with pytest.raises(DeviceLost):
        ADBCapture(AdbConfig(executable=str(tmp_path / "missing"), serial="x")).grab()


@pytest.mark.skipif(sys.platform == "win32", reason="uses a POSIX shell script as fake adb")
def test_adb_input_timeout_and_args(tmp_path):
    log = tmp_path / "args.txt"
    ok = ADBInput(AdbConfig(executable=_fake_adb(tmp_path, f'echo "$@" >> {log}'), serial="dev1"))
    from backend.input_handler import InputContext
    ctx = InputContext(AppConfig(), 1000, 500, True)
    ok.tap("jump", ctx, 40)
    assert log.read_text().strip() == "-s dev1 shell input tap 120 425"
    slow = ADBInput(AdbConfig(executable=_fake_adb(tmp_path, "sleep 5"), serial="d", command_timeout_s=0.3))
    with pytest.raises(InputError):
        slow.tap("jump", ctx, 40)


def test_replay_capture_video_and_missing(tmp_path):
    import cv2
    path = tmp_path / "clip.avi"
    vw = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*"MJPG"), 30, (320, 240))
    if not vw.isOpened():
        pytest.skip("MJPG writer unavailable")
    syn = SyntheticCapture(320, 240)
    for i in range(10):
        vw.write(syn.render(i / 30))
    vw.release()
    r = ReplayCapture(str(path), loop=True)
    r.open()
    frames = [r.grab() for _ in range(25)]  # loops past end
    assert all(f.shape == (240, 320, 3) for f in frames)
    r.close()
    with pytest.raises(DeviceLost):
        ReplayCapture(str(tmp_path / "nope.mp4")).open()
    _ = os
