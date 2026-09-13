from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from src.health import snapshot
from src.hub import Hub
from src.orchestrator import Orchestrator
from src.settings import load_settings

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("cohost")

STATIC = Path(__file__).resolve().parent / "web" / "static"


class ListenBody(BaseModel):
    enabled: bool


class PttBody(BaseModel):
    down: bool


class MuteBody(BaseModel):
    mute: bool = Field(...)


def create_app() -> FastAPI:
    @asynccontextmanager
    async def life(app: FastAPI):
        settings = load_settings()
        hub = Hub()
        orch = Orchestrator(settings, hub)
        app.state.settings = settings
        app.state.hub = hub
        app.state.orch = orch
        await orch.start(asyncio.get_running_loop())
        yield
        await orch.shutdown()

    app = FastAPI(title="Local Twitch Cohost", lifespan=life)
    app.mount("/static", StaticFiles(directory=STATIC), name="static")

    @app.get("/")
    async def index():
        return FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-cache"})

    @app.get("/overlay")
    async def overlay():
        return FileResponse(STATIC / "overlay.html", headers={"Cache-Control": "no-cache"})

    @app.get("/api/health")
    async def health():
        data = await snapshot(app.state.settings, app.state.orch)
        data["status"] = app.state.hub.status
        return data

    @app.get("/api/devices")
    async def devices():
        from src.audio.devices import list_devices

        try:
            return list_devices()
        except Exception as e:
            return JSONResponse({"error": str(e)}, status_code=500)

    @app.post("/api/listen")
    async def listen(body: ListenBody):
        await app.state.orch.set_listen(body.enabled)
        return {"ok": True, "listen": body.enabled}

    @app.post("/api/ptt")
    async def ptt(body: PttBody):
        await app.state.orch.set_ptt(body.down)
        return {"ok": True, "ptt": body.down}

    @app.post("/api/tts/mute")
    async def mute(body: MuteBody):
        await app.state.orch.set_mute(body.mute)
        return {"ok": True, "mute": body.mute}

    @app.post("/api/interrupt")
    async def interrupt():
        await app.state.orch.interrupt()
        return {"ok": True}

    @app.post("/api/persona/reload")
    async def persona_reload():
        app.state.orch.reload_persona()
        return {"ok": True, "name": app.state.orch.persona.get("name")}

    @app.get("/api/memory")
    async def memory():
        return app.state.orch.memory.snapshot()

    @app.post("/api/memory/clear")
    async def memory_clear():
        app.state.orch.clear_memory()
        return {"ok": True}

    @app.post("/api/twitch/connect")
    async def twitch_connect():
        try:
            await app.state.orch.twitch_connect()
            return {"ok": True}
        except Exception as e:
            return JSONResponse({"ok": False, "error": str(e)}, status_code=400)

    @app.post("/api/twitch/disconnect")
    async def twitch_disconnect():
        await app.state.orch.twitch_disconnect()
        return {"ok": True}

    @app.websocket("/ws")
    async def ws(ws: WebSocket):
        await ws.accept()
        hub: Hub = app.state.hub
        await hub.register(ws)
        await ws.send_json({"type": "status", "data": dict(hub.status)})
        try:
            while True:
                await ws.receive_text()
        except WebSocketDisconnect:
            pass
        finally:
            await hub.unregister(ws)

    return app


app = create_app()
