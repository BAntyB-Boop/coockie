"""FastAPI application: REST control API, WebSocket streams, static dashboard.

Run:  python -m backend.app            (binds 127.0.0.1:8765, one worker, no reload)
"""
from __future__ import annotations

import argparse
import asyncio
import hmac
import ipaddress
import logging
import os
import re
import sys
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

import cv2
from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import ValidationError
from starlette.concurrency import run_in_threadpool

from . import PROTOCOL_VERSION, __version__
from .bot_controller import BotController
from .capture import CaptureTimeout, DeviceLost, run_adb
from .config import PROJECT_ROOT, AdbConfig, AppConfig, ConfigStore
from .schemas import AdbEndpointRequest, AutoplayStart, DryRunRequest, ScreenEntry, TestInputRequest, pack_preview
from .streaming import ClientChannel, Hub

log = logging.getLogger("cookiebot.app")
FRONTEND_DIR = PROJECT_ROOT / "frontend"
REPLAYS_DIR = PROJECT_ROOT / "replays"
MIN_TOKEN_LEN = 24


@dataclass
class Settings:
    host: str = "127.0.0.1"
    port: int = 8765
    allow_lan: bool = False
    token: Optional[str] = None
    extra_origins: list[str] = field(default_factory=list)
    allowed_hosts: Optional[set[str]] = None  # None -> derived from host/port
    persist: bool = True
    enable_hotkey: bool = True

    def origins(self) -> set[str]:
        o = {f"http://127.0.0.1:{self.port}", f"http://localhost:{self.port}",
             f"https://127.0.0.1:{self.port}", f"https://localhost:{self.port}"}
        return o | {x.rstrip("/") for x in self.extra_origins}

    def hosts(self) -> Optional[set[str]]:
        if self.allowed_hosts is not None:
            return self.allowed_hosts
        if self.allow_lan:
            return None  # token + origin validation protect LAN mode
        return {f"127.0.0.1:{self.port}", f"localhost:{self.port}", "127.0.0.1", "localhost"}


def validate_settings(s: Settings) -> list[str]:
    errors = []
    loopback = s.host in ("localhost",) or _is_loopback(s.host)
    if not loopback and not s.allow_lan:
        errors.append("binding to a non-loopback address requires --allow-lan")
    if s.allow_lan:
        if not s.token or len(s.token) < MIN_TOKEN_LEN:
            errors.append(f"LAN mode requires COOKIEBOT_TOKEN with at least {MIN_TOKEN_LEN} characters")
        if not s.extra_origins:
            errors.append("LAN mode requires --allowed-origin (e.g. https://192.168.1.20:8765)")
    return errors


def _is_loopback(host: str) -> bool:
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def list_adb_devices(adb: AdbConfig) -> dict:
    try:
        res = run_adb(adb, ["devices", "-l"], adb.command_timeout_s + 3, serial=False)
    except (DeviceLost, CaptureTimeout) as exc:
        return {"available": False, "error": str(exc), "devices": []}
    devices = []
    for line in res.stdout.decode(errors="replace").splitlines()[1:]:
        parts = line.split()
        if len(parts) >= 2:
            devices.append({"serial": parts[0], "state": parts[1], "desc": " ".join(parts[2:])[:200]})
    return {"available": True, "error": None, "devices": devices}


def list_monitors() -> list[dict]:
    try:
        import mss  # type: ignore
        with mss.mss() as s:
            return [{"index": i, **m} for i, m in enumerate(s.monitors)]
    except Exception:
        return []


def create_app(settings: Optional[Settings] = None,
               controller_factory: Optional[Callable[[], BotController]] = None) -> FastAPI:
    settings = settings or Settings()
    events_hub = Hub()
    preview_hub = Hub()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        ctrl = controller_factory() if controller_factory else BotController(
            ConfigStore(persist=settings.persist), enable_hotkey=settings.enable_hotkey)
        app.state.controller = ctrl
        ctrl.start_services()
        tasks = [asyncio.create_task(_events_loop(ctrl)), asyncio.create_task(_preview_loop(ctrl))]
        try:
            yield
        finally:
            for t in tasks:
                t.cancel()
            await run_in_threadpool(ctrl.shutdown)

    app = FastAPI(title="Cookie Run Bot", version=__version__, lifespan=lifespan)
    app.state.settings = settings
    app.state.events_hub = events_hub
    app.state.preview_hub = preview_hub

    def ctrl_of(request_or_ws) -> BotController:
        return request_or_ws.app.state.controller

    # ------------------------------------------------------------ security
    def origin_ok(origin: Optional[str]) -> bool:
        if origin is None:
            return not settings.allow_lan
        return origin.rstrip("/") in settings.origins()

    def host_ok(host: Optional[str]) -> bool:
        allowed = settings.hosts()
        return allowed is None or (host or "") in allowed

    def token_ok(value: Optional[str]) -> bool:
        if not settings.token:
            return True
        return bool(value) and hmac.compare_digest(value.encode(), settings.token.encode())

    @app.middleware("http")
    async def security_mw(request: Request, call_next):
        if not host_ok(request.headers.get("host")):
            return JSONResponse({"detail": "host not allowed"}, status_code=400)
        path = request.url.path
        if path.startswith("/api/") and path != "/api/health":
            if request.method not in ("GET", "HEAD", "OPTIONS") or settings.allow_lan:
                origin = request.headers.get("origin")
                if origin is not None and not origin_ok(origin):
                    return JSONResponse({"detail": "origin not allowed"}, status_code=403)
            auth = request.headers.get("authorization", "")
            bearer = auth[7:] if auth.lower().startswith("bearer ") else None
            if not token_ok(bearer):
                return JSONResponse({"detail": "authentication required"}, status_code=401)
        response = await call_next(request)
        if path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        return response

    async def ws_admit(ws: WebSocket) -> bool:
        if not origin_ok(ws.headers.get("origin")) or not host_ok(ws.headers.get("host")):
            await ws.close(code=1008)
            return False
        await ws.accept()
        if settings.token:
            try:
                msg = await asyncio.wait_for(ws.receive_json(), timeout=5.0)
            except Exception:
                await ws.close(code=1008)
                return False
            if not (isinstance(msg, dict) and msg.get("type") == "auth" and token_ok(str(msg.get("token", "")))):
                await ws.send_json({"v": PROTOCOL_VERSION, "type": "auth_failed"})
                await ws.close(code=1008)
                return False
            await ws.send_json({"v": PROTOCOL_VERSION, "type": "auth_ok"})
        return True

    # --------------------------------------------------------------- REST
    @app.get("/api/health")
    async def health():
        return {"ok": True, "version": __version__, "protocol": PROTOCOL_VERSION}

    @app.get("/api/status")
    async def status(request: Request):
        c = ctrl_of(request)
        return {**c.status(), "telemetry": c.telemetry()}

    @app.get("/api/config")
    async def get_config(request: Request):
        return ctrl_of(request).store.get().model_dump(mode="json")

    @app.put("/api/config")
    async def put_config(request: Request):
        try:
            body = await request.json()
            incoming = AppConfig.model_validate(body)
        except ValidationError as exc:
            raise HTTPException(422, detail=[{"loc": e["loc"], "msg": e["msg"]} for e in exc.errors()])
        except ValueError:
            raise HTTPException(400, detail="invalid JSON")
        saved = await run_in_threadpool(ctrl_of(request).update_config, incoming)
        return saved.model_dump(mode="json")

    @app.get("/api/devices")
    async def devices(request: Request):
        c = ctrl_of(request)
        cfg = c.store.get()
        adb = await run_in_threadpool(list_adb_devices, cfg.adb)
        monitors = await run_in_threadpool(list_monitors)
        replays = sorted(str(p) for p in REPLAYS_DIR.glob("*") if p.is_file()) if REPLAYS_DIR.exists() else []
        return {
            "adb": adb, "monitors": monitors, "replays": replays, "platform": sys.platform,
            "capture_backends": ["synthetic", "replay", "mss", "adb"],
            "input_backends": [
                {"name": "dryrun", "available": True},
                {"name": "adb", "available": adb["available"]},
                {"name": "keyboard", "available": sys.platform == "win32",
                 "reason": None if sys.platform == "win32" else "Windows only"},
            ],
        }

    @app.post("/api/devices/adb/connect")
    async def adb_connect(request: Request, body: AdbEndpointRequest):
        cfg = ctrl_of(request).store.get()
        return await run_in_threadpool(_adb_simple, cfg.adb, ["connect", body.endpoint])

    @app.post("/api/devices/adb/pair")
    async def adb_pair(request: Request, body: AdbEndpointRequest):
        if not body.code:
            raise HTTPException(422, detail="pairing code required")
        cfg = ctrl_of(request).store.get()
        return await run_in_threadpool(_adb_simple, cfg.adb, ["pair", body.endpoint, body.code])

    def control_result(c: BotController, ok: bool, reasons: list[str]) -> dict:
        return {"ok": ok, "state": c.state.value, "reasons": reasons}

    @app.post("/api/control/start")
    async def start(request: Request):
        c = ctrl_of(request)
        ok, reasons = await run_in_threadpool(c.start)
        return control_result(c, ok, reasons)

    @app.post("/api/control/pause")
    async def pause(request: Request):
        c = ctrl_of(request)
        ok = await run_in_threadpool(c.pause)
        return control_result(c, ok, [] if ok else ["bot is not running"])

    @app.post("/api/control/stop")
    async def stop(request: Request):
        c = ctrl_of(request)
        await run_in_threadpool(c.stop)
        return control_result(c, True, [])

    @app.post("/api/control/dry-run")
    async def dry_run(request: Request, body: DryRunRequest):
        c = ctrl_of(request)
        ok, reasons = await run_in_threadpool(c.set_dry_run, body.enabled)
        return {**control_result(c, ok, reasons), "dry_run": c.store.get().safety.dry_run}

    @app.post("/api/control/test-input")
    async def test_input(request: Request, body: TestInputRequest):
        c = ctrl_of(request)
        ok, reasons, backend = await run_in_threadpool(c.test_input, body.action)
        return {**control_result(c, ok, reasons), "backend": backend}

    @app.post("/api/calibration/confirm")
    async def calibrate(request: Request):
        c = ctrl_of(request)
        ok, reasons = await run_in_threadpool(c.confirm_calibration)
        return control_result(c, ok, reasons)

    @app.delete("/api/calibration")
    async def clear_calibration(request: Request):
        c = ctrl_of(request)
        await run_in_threadpool(c.clear_calibration)
        return control_result(c, True, [])

    @app.get("/api/profiles")
    async def profiles(request: Request):
        return {"profiles": ctrl_of(request).store.list_profiles()}

    @app.post("/api/profiles/{name}/save")
    async def save_profile(request: Request, name: str):
        try:
            await run_in_threadpool(ctrl_of(request).store.save_profile, name)
        except ValueError as exc:
            raise HTTPException(422, detail=str(exc))
        return {"ok": True, "name": name}

    @app.post("/api/profiles/{name}/load")
    async def load_profile(request: Request, name: str):
        c = ctrl_of(request)
        try:
            cfg = await run_in_threadpool(c.store.read_profile, name)
        except FileNotFoundError:
            raise HTTPException(404, detail="profile not found")
        except (ValueError, ValidationError) as exc:
            raise HTTPException(422, detail=str(exc))
        saved = await run_in_threadpool(c.update_config, cfg)
        return saved.model_dump(mode="json")

    # ---------------------------------------------------------------- Macro
    @app.get("/api/macro/status")
    async def macro_status(request: Request):
        return ctrl_of(request).macro.status()

    @app.post("/api/macro/record/start")
    async def macro_record_start(request: Request):
        c = ctrl_of(request)
        ok = await run_in_threadpool(c.macro.start_recording)
        return {"ok": ok, "status": c.macro.status()}

    @app.post("/api/macro/record/stop")
    async def macro_record_stop(request: Request):
        c = ctrl_of(request)
        macro = await run_in_threadpool(c.macro.stop_recording)
        c.logs.add("info", f"Macro recorded: {len(macro.events)} events, {macro.duration_s}s (Loop 1 ready)")
        return {"ok": True, "macro": macro.to_dict(), "status": c.macro.status()}

    @app.post("/api/macro/play/start")
    async def macro_play_start(request: Request):
        body = {}
        try:
            body = await request.json()
        except Exception:
            pass
        target_loops = int(body.get("target_loops", 1))
        speed = float(body.get("speed", 1.0))
        auto_restart = bool(body.get("auto_restart", True))
        c = ctrl_of(request)
        ok = await run_in_threadpool(c.macro.start_playback, target_loops, speed, auto_restart)
        if not ok:
            return JSONResponse({"ok": False, "error": c.macro.last_error or "failed to start playback"}, status_code=400)
        c.logs.add("info", f"Macro replay started: {target_loops} loops at {speed}x")
        return {"ok": True, "status": c.macro.status()}

    @app.post("/api/macro/play/pause")
    async def macro_play_pause(request: Request):
        c = ctrl_of(request)
        await run_in_threadpool(c.macro.pause_playback)
        return {"ok": True, "status": c.macro.status()}

    @app.post("/api/macro/play/resume")
    async def macro_play_resume(request: Request):
        c = ctrl_of(request)
        await run_in_threadpool(c.macro.resume_playback)
        return {"ok": True, "status": c.macro.status()}

    @app.post("/api/macro/play/stop")
    async def macro_play_stop(request: Request):
        c = ctrl_of(request)
        await run_in_threadpool(c.macro.stop_playback)
        c.logs.add("info", "Macro replay stopped")
        return {"ok": True, "status": c.macro.status()}

    @app.get("/api/macro/list")
    async def macro_list(request: Request):
        c = ctrl_of(request)
        return {"macros": await run_in_threadpool(c.macro.list_saved)}

    @app.post("/api/macro/save/{name}")
    async def macro_save(request: Request, name: str):
        c = ctrl_of(request)
        ok = await run_in_threadpool(c.macro.save_current, name)
        return {"ok": ok, "name": name}

    @app.post("/api/macro/load/{name}")
    async def macro_load(request: Request, name: str):
        c = ctrl_of(request)
        ok = await run_in_threadpool(c.macro.load_macro, name)
        if not ok:
            raise HTTPException(404, detail=c.macro.last_error or "macro not found")
        return {"ok": True, "status": c.macro.status()}

    @app.post("/api/macro/event")
    async def macro_inject_event(request: Request):
        body = await request.json()
        action = str(body.get("action", "jump"))
        kind = str(body.get("kind", "tap"))
        key = body.get("key")
        x = body.get("x")
        y = body.get("y")
        c = ctrl_of(request)
        c.macro.recorder.record_action(action=action, kind=kind, key=key, x=x, y=y)
        return {"ok": True}

    # ------------------------------------------------------------ Autoplay
    @app.get("/api/autoplay/screens")
    async def ap_screens(request: Request):
        return {"screens": await run_in_threadpool(ctrl_of(request).autoplay.screens.entries)}

    @app.post("/api/autoplay/screens")
    async def ap_teach(request: Request, body: ScreenEntry):
        c = ctrl_of(request)
        f = c.latest.peek()
        if f is None:
            raise HTTPException(409, detail="no frame available")
        try:
            entry = await run_in_threadpool(c.autoplay.screens.save, body.model_dump(), f.image)
        except ValueError as exc:
            raise HTTPException(422, detail=str(exc))
        return {"ok": True, "screen": entry}

    @app.delete("/api/autoplay/screens/{name}")
    async def ap_delete(request: Request, name: str):
        if not await run_in_threadpool(ctrl_of(request).autoplay.screens.delete, name):
            raise HTTPException(404, detail="screen not found")
        return {"ok": True}

    @app.get("/api/autoplay/match")
    async def ap_match(request: Request):
        c = ctrl_of(request)
        f = c.latest.peek()
        if f is None:
            raise HTTPException(409, detail="no frame available")
        return {"scores": await run_in_threadpool(c.autoplay.screens.scores, cv2.cvtColor(f.image, cv2.COLOR_BGR2GRAY))}

    @app.post("/api/autoplay/start")
    async def ap_start(request: Request, body: AutoplayStart):
        ap = ctrl_of(request).autoplay
        reasons = await run_in_threadpool(ap.start, body)
        return {"ok": not reasons, "reasons": reasons, "status": ap.status()}

    @app.post("/api/autoplay/stop")
    async def ap_stop(request: Request):
        ap = ctrl_of(request).autoplay
        await run_in_threadpool(ap.stop, "stopped by user")
        return {"ok": True, "status": ap.status()}

    # ---------------------------------------------------------- WebSockets
    async def _events_loop(c: BotController) -> None:
        last_seq = 0
        while True:
            try:
                if len(events_hub):
                    events_hub.broadcast(c.status())
                    events_hub.broadcast(c.telemetry())
                    entries = c.logs.since(last_seq)
                    if entries:
                        last_seq = entries[-1].seq
                        events_hub.broadcast({"v": PROTOCOL_VERSION, "type": "log", "entries": [
                            {"seq": e.seq, "t": e.t, "level": e.level, "msg": e.msg} for e in entries[-100:]]})
                else:
                    last_seq = c.logs.last_seq
            except Exception:
                log.exception("events loop error")
            await asyncio.sleep(0.25)

    async def _preview_loop(c: BotController) -> None:
        while True:
            now = time.monotonic()
            for ch in list(preview_hub.channels):
                entry = c.get_preview(ch.view)
                if entry is None or entry[0] == ch.last_frame_id or now - ch.last_sent_t < ch.min_interval:
                    continue
                fid, meta, jpeg = entry
                ch.last_frame_id = fid
                ch.last_sent_t = now
                ch.offer((meta, pack_preview(fid, jpeg)))
            await asyncio.sleep(1.0 / max(1.0, c.store.get().preview.fps) / 2)

    @app.websocket("/ws/events")
    async def ws_events(ws: WebSocket):
        if not await ws_admit(ws):
            return
        c = ctrl_of(ws)
        ch = ClientChannel(ws.send_json, maxsize=256)
        events_hub.add(ch)
        c.on_clients_changed(len(events_hub))
        pump = asyncio.create_task(ch.pump())
        recent = c.logs.since(max(0, c.logs.last_seq - 50))
        ch.offer({"v": PROTOCOL_VERSION, "type": "hello", "version": __version__})
        ch.offer(c.status())
        ch.offer({"v": PROTOCOL_VERSION, "type": "log", "entries": [
            {"seq": e.seq, "t": e.t, "level": e.level, "msg": e.msg} for e in recent]})
        try:
            while True:
                await ws.receive_text()  # keepalive pings; content ignored
        except (WebSocketDisconnect, RuntimeError):
            pass
        finally:
            pump.cancel()
            events_hub.remove(ch)
            c.on_clients_changed(len(events_hub))

    @app.websocket("/ws/preview")
    async def ws_preview(ws: WebSocket):
        if not await ws_admit(ws):
            return
        c = ctrl_of(ws)

        async def send(item):
            meta, data = item
            await ws.send_json(meta)
            await ws.send_bytes(data)

        ch = ClientChannel(send, maxsize=1)
        preview_hub.add(ch)
        c.want_view(ch.view, +1)
        pump = asyncio.create_task(ch.pump())
        try:
            while True:
                msg = await ws.receive_json()
                if not isinstance(msg, dict):
                    continue
                if msg.get("type") == "view" and msg.get("view") in ("annotated", "raw", "mask"):
                    c.want_view(ch.view, -1)
                    ch.view = msg["view"]
                    ch.last_frame_id = -1
                    c.want_view(ch.view, +1)
                elif msg.get("type") == "fps":
                    try:
                        fps = float(msg.get("fps", 0))
                        ch.min_interval = 1.0 / fps if 0 < fps <= 60 else 0.0
                    except (TypeError, ValueError):
                        pass
        except (WebSocketDisconnect, RuntimeError, ValueError):
            pass
        finally:
            pump.cancel()
            preview_hub.remove(ch)
            c.want_view(ch.view, -1)

    # Static dashboard LAST so /api and /ws routes take precedence.
    app.mount("/", StaticFiles(directory=str(FRONTEND_DIR), html=True), name="static")
    return app


_ADB_OUT_RE = re.compile(r"[^\x20-\x7e]")


def _adb_simple(adb: AdbConfig, args: list[str]) -> dict:
    try:
        res = run_adb(adb, args, 15.0, serial=False)
    except (DeviceLost, CaptureTimeout) as exc:
        return {"ok": False, "output": str(exc)}
    out = _ADB_OUT_RE.sub("", (res.stdout + res.stderr).decode(errors="replace"))[:500]
    ok = res.returncode == 0 and "failed" not in out.lower() and "cannot" not in out.lower()
    return {"ok": ok, "output": out.strip()}


def main(argv: Optional[list[str]] = None) -> int:
    p = argparse.ArgumentParser(description="Cookie Run Bot local server")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8765)
    p.add_argument("--allow-lan", action="store_true", help="allow non-loopback binding (requires token)")
    p.add_argument("--allowed-origin", action="append", default=[], help="extra allowed Origin (repeatable)")
    p.add_argument("--ssl-certfile")
    p.add_argument("--ssl-keyfile")
    p.add_argument("--no-hotkey", action="store_true", help="disable the Windows F12 emergency-stop hotkey")
    a = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    s = Settings(host=a.host, port=a.port, allow_lan=a.allow_lan, token=os.environ.get("COOKIEBOT_TOKEN") or None,
                 extra_origins=a.allowed_origin, enable_hotkey=not a.no_hotkey)
    if not s.allow_lan:
        s.token = None
    errors = validate_settings(s)
    if errors:
        for e in errors:
            print(f"ERROR: {e}", file=sys.stderr)
        return 2
    scheme = "https" if a.ssl_certfile else "http"
    shown = "127.0.0.1" if s.host in ("0.0.0.0", "::") else s.host
    print(f"\n  Cookie Run Bot dashboard: {scheme}://{shown}:{s.port}/\n  Real input starts DISABLED (dry-run).\n")
    if s.allow_lan and scheme == "http":
        print("  WARNING: LAN mode over plain HTTP. Use --ssl-certfile/--ssl-keyfile or an HTTPS reverse proxy;\n"
              "  Service Workers and safe token transport need a secure context.\n")
    import uvicorn
    uvicorn.run(create_app(s), host=s.host, port=s.port, workers=1, reload=False, log_level="info",
                ssl_certfile=a.ssl_certfile, ssl_keyfile=a.ssl_keyfile, ws_max_size=1 << 20)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

