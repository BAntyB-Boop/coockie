"""Tests for Macro record & loop replay functionality."""
import time
from pathlib import Path

from backend.macro import MacroData, MacroEvent, MacroManager, MacroPlayer, MacroRecorder


def test_macro_event_serialization():
    evt = MacroEvent(t=1.523, action="jump", kind="down", key="space")
    d = evt.to_dict()
    assert d["t"] == 1.523
    assert d["action"] == "jump"
    assert d["kind"] == "down"
    assert d["key"] == "space"

    restored = MacroEvent.from_dict(d)
    assert restored.t == 1.523
    assert restored.action == "jump"
    assert restored.kind == "down"
    assert restored.key == "space"


def test_macro_data_save_load(tmp_path: Path):
    events = [
        MacroEvent(t=0.5, action="jump", kind="down", key="space"),
        MacroEvent(t=0.6, action="jump", kind="up", key="space"),
        MacroEvent(t=1.2, action="slide", kind="down", key="down"),
        MacroEvent(t=1.8, action="slide", kind="up", key="down"),
        MacroEvent(t=15.0, action="restart", kind="click", x=500, y=800),
    ]
    macro = MacroData(name="test_run", duration_s=18.5, events=events, restart_index=4)
    file_path = tmp_path / "test_run.json"
    macro.save(file_path)

    assert file_path.exists()
    loaded = MacroData.load(file_path)
    assert loaded.name == "test_run"
    assert loaded.duration_s == 18.5
    assert len(loaded.events) == 5
    assert loaded.events[4].action == "restart"
    assert loaded.events[4].x == 500
    assert loaded.events[4].y == 800
    assert loaded.restart_index == 4


def test_macro_recorder_lifecycle():
    rec = MacroRecorder()
    rec.start()
    assert rec.is_recording

    # Simulate recorded actions
    rec.record_action("jump", "down", key="space")
    time.sleep(0.05)
    rec.record_action("jump", "up", key="space")
    time.sleep(0.05)
    rec.record_action("restart", "click", x=640, y=720)

    data = rec.stop()
    assert not rec.is_recording
    assert len(data.events) == 3
    assert data.events[0].action == "jump"
    assert data.events[0].kind == "down"
    assert data.events[2].action == "restart"
    assert data.events[2].x == 640
    assert data.duration_s > 0.09
    assert data.restart_index == 2


def test_macro_player_playback():
    dispatched = []

    def mock_dispatch(evt: MacroEvent):
        dispatched.append(evt)

    events = [
        MacroEvent(t=0.02, action="jump", kind="down", key="space"),
        MacroEvent(t=0.05, action="jump", kind="up", key="space"),
    ]
    macro = MacroData(name="quick_test", duration_s=0.08, events=events)
    player = MacroPlayer(input_dispatch=mock_dispatch)

    player.start(macro, target_loops=2, speed=1.0)
    time.sleep(0.25)
    player.stop()

    assert not player.is_playing
    # Played 2 loops: each loop dispatched 2 events = 4 events total
    assert len(dispatched) >= 2
    assert dispatched[0].action == "jump"
    assert dispatched[0].kind == "down"


def test_macro_manager_integration(tmp_path: Path):
    manager = MacroManager(macros_dir=tmp_path)
    assert manager.status()["state"] == "IDLE"

    # Start recording
    assert manager.start_recording("stage_1")
    assert manager.status()["state"] == "RECORDING"

    manager.recorder.record_action("jump", "down", key="space")
    time.sleep(0.02)
    manager.recorder.record_action("jump", "up", key="space")
    time.sleep(0.02)
    manager.recorder.record_action("restart", "click", x=900, y=500)

    # Stop recording & save
    macro = manager.stop_recording("stage_1")
    assert manager.status()["state"] == "IDLE"
    assert len(macro.events) == 3

    # Check that file was saved
    saved_list = manager.list_saved()
    assert any(m["name"] == "stage_1" for m in saved_list)

    # Replay
    dispatched = []
    manager.player.input_dispatch = lambda e: dispatched.append(e)
    assert manager.start_playback(target_loops=1, speed=2.0)
    assert manager.status()["state"] == "PLAYING"

    time.sleep(0.1)
    manager.stop_playback()
    assert manager.status()["state"] == "IDLE"
