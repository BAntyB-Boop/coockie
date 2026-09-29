import asyncio
import json
import threading

import pytest
from fastapi.testclient import TestClient

from backend.app import Settings, create_app
from backend.bot_controller import BotController
from backend.capture import SyntheticCapture
from backend.schemas import unpack_preview
from backend.streaming import ClientChannel, Hub
from conftest import wait_until

TOKEN = "t" * 32


def make_client(tmp_store, tmp_path, **settings_kw):
    settings = Settings(persist=False, enable_hotkey=False, allowed_hosts={"testserver"}, **settings_kw)
    app = create_app(settings, controller_factory=lambda: BotController(
        tmp_store, capture_factory=lambda c: SyntheticCapture(), estop_file=tmp_path / "ESTOP"))
    return TestClient(app)


def test_status_config_and_validation(tmp_store, tmp_path):
    with make_client(tmp_store, tmp_path) as client:
        assert client.get("/api/health").json()["ok"]
        st = client.get("/api/status").json()
        assert st["state"] == "STOPPED" and st["dry_run"] is True
        assert client.get("/api/status").headers["cache-control"] == "no-store"
        cfg = client.get("/api/config").json()
        cfg["rois"]["jump_roi"]["x"] = 0.99  # out of bounds with w
        assert client.put("/api/config", json=cfg).status_code == 422
        cfg = client.get("/api/config").json()
        cfg["safety"]["dry_run"] = False  # cannot disable dry-run via PUT
        cfg["rois"]["jump_roi"]["x"] = 0.4
        saved = client.put("/api/config", json=cfg).json()
        assert saved["safety"]["dry_run"] is True and saved["rois"]["jump_roi"]["x"] == 0.4
        # dry-run cannot be disabled with the dry-run input backend
        r = client.post("/api/control/dry-run", json={"enabled": False}).json()
        assert r["ok"] is False and r["dry_run"] is True
        assert client.get("/").status_code == 200  # static mount after API routes
        assert "Cookie Run" in client.get("/").text


def test_start_pause_stop_flow(tmp_store, tmp_path):
    with make_client(tmp_store, tmp_path) as client:
        c = client.app.state.controller
        assert wait_until(lambda: client.get("/api/status").json()["can_start"])
        assert client.post("/api/control/start").json()["ok"]
        assert wait_until(lambda: c.state.value == "RUNNING")
        assert client.post("/api/control/pause").json()["state"] == "PAUSED"
        assert client.post("/api/control/stop").json()["state"] == "STOPPED"
        r = client.post("/api/control/test-input", json={"action": "jump"}).json()
        assert r["ok"] and r["backend"] == "dryrun"
        assert wait_until(lambda: ("tap", "jump") in c.dry.events)


def test_multiple_ws_clients_single_loop_and_preview_protocol(tmp_store, tmp_path):
    with make_client(tmp_store, tmp_path) as client:
        with client.websocket_connect("/ws/events") as a, client.websocket_connect("/ws/events") as b, \
                client.websocket_connect("/ws/events") as c3:
            for ws in (a, b, c3):
                assert ws.receive_json()["type"] == "hello"
            assert [t.name for t in threading.enumerate()].count("processing") == 1
            assert client.app.state.controller.event_clients == 3
            with client.websocket_connect("/ws/preview") as p:
                meta = p.receive_json()
                assert meta["type"] == "preview_meta" and meta["v"] == 1
                version, fid, jpeg = unpack_preview(p.receive_bytes())
                assert version == 1 and fid == meta["frame_id"] and jpeg[:2] == b"\xff\xd8"
                p.send_json({"type": "view", "view": "mask"})
                for _ in range(6):
                    m = p.receive_json()
                    p.receive_bytes()
                    if m["view"] == "mask":
                        break
                assert m["view"] == "mask"
        # clients gone: controller count updated, bot not auto-started
        assert wait_until(lambda: client.app.state.controller.event_clients == 0)
        assert client.app.state.controller.state.value == "STOPPED"


def test_origin_and_host_validation(tmp_store, tmp_path):
    with make_client(tmp_store, tmp_path) as client:
        r = client.post("/api/control/stop", headers={"Origin": "https://evil.example"})
        assert r.status_code == 403
        r = client.post("/api/control/stop", headers={"Origin": "http://127.0.0.1:8765"})
        assert r.status_code == 200
        assert client.get("/api/status", headers={"Host": "evil.example"}).status_code == 400
        with pytest.raises(Exception):
            with client.websocket_connect("/ws/events", headers={"Origin": "https://evil.example"}) as ws:
                ws.receive_json()


def test_lan_auth_required_http_and_ws(tmp_store, tmp_path):
    origin = "https://192.168.1.5:8765"
    with make_client(tmp_store, tmp_path, allow_lan=True, token=TOKEN, extra_origins=[origin]) as client:
        assert client.get("/api/status").status_code == 401
        assert client.get("/api/status", headers={"Authorization": "Bearer wrong"}).status_code == 401
        ok = client.get("/api/status", headers={"Authorization": f"Bearer {TOKEN}"})
        assert ok.status_code == 200
        assert client.get("/api/health").status_code == 200
        with client.websocket_connect("/ws/events", headers={"Origin": origin}) as ws:
            ws.send_json({"type": "auth", "token": "nope"})
            assert ws.receive_json()["type"] == "auth_failed"
        with client.websocket_connect("/ws/events", headers={"Origin": origin}) as ws:
            ws.send_json({"type": "auth", "token": TOKEN})
            assert ws.receive_json()["type"] == "auth_ok"
            assert ws.receive_json()["type"] == "hello"
        logs = json.dumps([e.msg for e in client.app.state.controller.logs.since(0)])
        assert TOKEN not in logs


def test_slow_preview_client_does_not_block():
    async def scenario():
        hub = Hub()
        gate = asyncio.Event()
        fast_got = []

        async def slow_send(item):
            await gate.wait()

        async def fast_send(item):
            fast_got.append(item)

        slow, fast = ClientChannel(slow_send, 1), ClientChannel(fast_send, 1)
        hub.add(slow)
        hub.add(fast)
        tasks = [asyncio.create_task(slow.pump()), asyncio.create_task(fast.pump())]
        for i in range(200):
            hub.broadcast(i)  # never awaits
            await asyncio.sleep(0)
        await asyncio.sleep(0.01)
        assert slow.queue.qsize() <= 1 and slow.dropped >= 190
        assert fast_got[-1] == 199
        for t in tasks:
            t.cancel()

    asyncio.run(scenario())
