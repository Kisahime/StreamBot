from __future__ import annotations

import asyncio
import logging
import os
import secrets
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.middleware.trustedhost import TrustedHostMiddleware

from src.audio.cuda_libs import ensure_cuda_dlls
from src.health import snapshot
from src.hub import Hub
from src.logsetup import setup_logging
from src.orchestrator import Orchestrator
from src.security import BoothGuard, origin_is_local, trusted_hosts
from src.settings import ROOT, Settings, assert_bind_allowed, load_settings

log = logging.getLogger("cohost")

STATIC = Path(__file__).resolve().parent / "web" / "static"


class ListenBody(BaseModel):
    enabled: bool


class PttBody(BaseModel):
    down: bool


class MuteBody(BaseModel):
    mute: bool = Field(...)


class StreamerBody(BaseModel):
    display_name: str = ""
    twitch_login: str = ""


class SayBody(BaseModel):
    text: str = ""
    post_chat: bool = True


class PretendBody(BaseModel):
    text: str = ""


class PersonaBody(BaseModel):
    text: str = ""


class MoodBody(BaseModel):
    mood: str = "normal"


class VadBody(BaseModel):
    silence_ms: int = Field(default=800, ge=200, le=3000)
    energy_threshold: float = Field(default=0.012, ge=0.001, le=0.2)
    max_utterance_seconds: float = Field(default=20, ge=3, le=60)


class DeviceBody(BaseModel):
    mic_device: int | None = None
    speaker_device: int | None = None
    mic_device_name: str | None = None
    speaker_device_name: str | None = None


class FlagBody(BaseModel):
    enabled: bool


class LanguageBody(BaseModel):
    language: str = "en"


class VoiceBody(BaseModel):
    voice: str = ""


class ShoutoutBody(BaseModel):
    user: str = ""


class MarkerBody(BaseModel):
    note: str = ""


class ForgetBody(BaseModel):
    login: str = ""


class NoteBody(BaseModel):
    login: str = ""
    text: str = ""
    op: str = "add"
    pinned: bool | None = None


class IdBody(BaseModel):
    id: str = ""


class ProfileBody(BaseModel):
    name: str = ""


class RecapBody(BaseModel):
    vibe: bool = False


class DebugLogBody(BaseModel):
    enabled: bool = True


def _booth_port(prepared: tuple[Settings, Hub, Orchestrator] | None) -> int:
    """Port for the Host allow list.

    A prepared app (tests) uses that settings object. The process app reads
    ``server.port`` from config without loading ``.env``.
    """
    if prepared is not None:
        return int(prepared[0].port)
    try:
        import yaml

        with (ROOT / "config.yaml").open(encoding="utf-8") as handle:
            data = yaml.safe_load(handle) or {}
        return int((data.get("server") or {}).get("port") or 8080)
    except Exception:
        return 8080


def create_app(prepared: tuple[Settings, Hub, Orchestrator] | None = None) -> FastAPI:
    @asynccontextmanager
    async def life(app: FastAPI):
        app.state.control_token = secrets.token_urlsafe(32)
        app.state.control_key = os.getenv("COHOST_CONTROL_KEY", "").strip()
        if prepared is not None:
            settings, hub, orch = prepared
            app.state.settings = settings
            app.state.hub = hub
            app.state.orch = orch
            yield
            return
        ensure_cuda_dlls()
        settings = load_settings()
        setup_logging(
            [settings.twitch_token, settings.twitch_event_token, settings.event_token()],
            level=settings.log_level,
            debug_modules=settings.log_debug_modules,
        )
        bind = os.getenv("COHOST_BIND_HOST", settings.host)
        assert_bind_allowed(bind, settings.allow_remote)
        if settings.missing_secrets() and os.getenv("COHOST_SKIP_SECRET_CHECK") != "1":
            raise RuntimeError(" ".join(settings.secret_hints()))
        hub = Hub()
        orch = Orchestrator(settings, hub)
        app.state.settings = settings
        app.state.hub = hub
        app.state.orch = orch
        await orch.start(asyncio.get_running_loop())
        yield
        await orch.shutdown()

    app = FastAPI(title="Local Twitch Cohost", lifespan=life)
    app.add_middleware(BoothGuard)
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=trusted_hosts(_booth_port(prepared)), www_redirect=False)
    app.mount("/static", StaticFiles(directory=STATIC), name="static")

    @app.get("/")
    async def index():
        return FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-cache"})

    @app.get("/overlay")
    async def overlay():
        return FileResponse(STATIC / "overlay.html", headers={"Cache-Control": "no-cache"})

    @app.get("/api/session")
    async def session():
        return {"token": app.state.control_token}

    @app.get("/api/transcript")
    async def transcript():
        memory = app.state.orch.memory
        path = memory.transcript_path()
        filename = f"{memory.session_id}.jsonl"
        if not path.exists():
            return Response(
                b"",
                media_type="application/x-ndjson",
                headers={"Content-Disposition": f'attachment; filename="{filename}"'},
            )
        return FileResponse(path, filename=filename, media_type="application/x-ndjson")

    @app.get("/api/health")
    async def health():
        data = await snapshot(app.state.settings, app.state.orch)
        data["status"] = app.state.hub.status
        return data

    @app.get("/api/devices")
    async def devices(refresh: int = 0):
        from src.health import load_devices

        try:
            return await load_devices(refresh=bool(refresh))
        except Exception as e:
            return JSONResponse({"error": str(e)}, status_code=500)

    @app.get("/api/queue")
    async def queue():
        return app.state.orch.queue_snapshot()

    @app.post("/api/queue/drop")
    async def queue_drop(body: IdBody):
        ok = await app.state.orch.drop_queued(body.id)
        if not ok:
            return JSONResponse({"ok": False, "error": "not in queue"}, status_code=404)
        return {"ok": True, "queue": app.state.orch.queue_snapshot()}

    @app.post("/api/queue/answer")
    async def queue_answer(body: IdBody):
        ok = await app.state.orch.answer_queued(body.id)
        if not ok:
            return JSONResponse({"ok": False, "error": "not in queue"}, status_code=404)
        return {"ok": True, "queue": app.state.orch.queue_snapshot()}

    @app.post("/api/queue/replay")
    async def queue_replay():
        ok = await app.state.orch.replay_last()
        if not ok:
            return JSONResponse({"ok": False, "error": "nothing to replay"}, status_code=400)
        return {"ok": True}

    @app.post("/api/recap")
    async def recap(body: RecapBody):
        result = await app.state.orch.make_recap(vibe=body.vibe)
        return {"ok": True, **result}

    @app.post("/api/profile")
    async def profile(body: ProfileBody):
        try:
            name = await app.state.orch.apply_profile(body.name)
        except ValueError as e:
            return JSONResponse({"ok": False, "error": str(e)}, status_code=400)
        return {"ok": True, "profile": name}

    @app.post("/api/listen")
    async def listen(body: ListenBody):
        ok = await app.state.orch.set_listen(body.enabled)
        if body.enabled and not ok:
            return JSONResponse({"ok": False, "error": "models are not ready"}, status_code=400)
        return {"ok": True, "listen": body.enabled}

    @app.post("/api/ptt")
    async def ptt(body: PttBody):
        await app.state.orch.set_ptt(body.down)
        return {"ok": True, "ptt": body.down}

    @app.post("/api/tts/mute")
    async def mute(body: MuteBody):
        await app.state.orch.set_mute(body.mute)
        return {"ok": True, "mute": body.mute}

    @app.post("/api/chat/post_voice")
    async def post_voice(body: FlagBody):
        app.state.orch.set_post_voice(body.enabled)
        await app.state.hub.patch_status(post_voice=bool(body.enabled))
        return {"ok": True, "enabled": body.enabled}

    @app.post("/api/interrupt")
    async def interrupt():
        await app.state.orch.interrupt()
        return {"ok": True}

    @app.post("/api/say")
    async def say(body: SayBody):
        await app.state.orch.say_line(body.text, post_chat=body.post_chat)
        return {"ok": True}

    @app.post("/api/pretend")
    async def pretend(body: PretendBody):
        await app.state.orch.pretend(body.text)
        return {"ok": True}

    @app.get("/api/persona")
    async def persona_get():
        path = app.state.orch.persona_path
        text = path.read_text(encoding="utf-8") if path.exists() else ""
        return {"text": text, "name": app.state.orch.persona.get("name")}

    @app.post("/api/persona")
    async def persona_save(body: PersonaBody):
        try:
            name = app.state.orch.save_persona_text(body.text)
        except Exception as e:
            return JSONResponse({"ok": False, "error": str(e)}, status_code=400)
        return {"ok": True, "name": name}

    @app.post("/api/persona/reload")
    async def persona_reload():
        app.state.orch.reload_persona()
        return {"ok": True, "name": app.state.orch.persona.get("name")}

    @app.post("/api/logging/debug")
    async def debug_logging(body: DebugLogBody):
        if body.enabled:
            await app.state.orch.enable_debug_log()
            return {"ok": True, "debug": True, "seconds": 600}
        app.state.orch.restore_log_level()
        app.state.orch._debug_log_active = False
        return {"ok": True, "debug": False}

    @app.post("/api/mood")
    async def mood(body: MoodBody):
        try:
            value = await app.state.orch.set_mood(body.mood)
        except ValueError as e:
            return JSONResponse({"ok": False, "error": str(e)}, status_code=400)
        return {"ok": True, "mood": value}

    @app.post("/api/audio/vad")
    async def vad(body: VadBody):
        await app.state.orch.set_vad(body.silence_ms, body.energy_threshold, body.max_utterance_seconds)
        return {"ok": True}

    @app.post("/api/audio/devices")
    async def audio_devices(body: DeviceBody):
        try:
            await app.state.orch.set_devices(
                body.mic_device,
                body.speaker_device,
                mic_name=body.mic_device_name,
                speaker_name=body.speaker_device_name,
            )
        except Exception as e:
            return JSONResponse({"ok": False, "error": str(e)}, status_code=400)
        return {"ok": True}

    @app.post("/api/audio/duck")
    async def duck(body: FlagBody):
        await asyncio.to_thread(app.state.orch.set_duck, body.enabled)
        await app.state.hub.patch_status(duck_enabled=bool(body.enabled), duck=app.state.orch.duck.mode)
        return {"ok": True, "mode": app.state.orch.duck.mode, "note": app.state.orch.duck.note}

    @app.post("/api/stt/language")
    async def language(body: LanguageBody):
        try:
            await app.state.orch.set_language(body.language)
        except ValueError as e:
            return JSONResponse({"ok": False, "error": str(e)}, status_code=400)
        return {"ok": True, "language": app.state.settings.whisper_language}

    @app.get("/api/tts/voices")
    async def voices():
        return {"voices": app.state.settings.tts_voices, "current": app.state.settings.tts_voice}

    @app.post("/api/tts/voice")
    async def voice(body: VoiceBody):
        try:
            await app.state.orch.set_voice(body.voice)
        except Exception as e:
            return JSONResponse({"ok": False, "error": str(e)}, status_code=400)
        return {"ok": True, "voice": app.state.settings.tts_voice, "ready": app.state.orch.tts.ready}

    @app.get("/api/streamer")
    async def streamer_get():
        ident = app.state.orch.identity
        return {
            "display_name": ident.display_name,
            "twitch_login": ident.twitch_login or app.state.settings.twitch_channel,
            "spoken_name": ident.spoken_name(),
        }

    @app.post("/api/streamer")
    async def streamer_set(body: StreamerBody):
        ident = app.state.orch.set_identity(body.display_name, body.twitch_login)
        return {
            "ok": True,
            "display_name": ident.display_name,
            "twitch_login": ident.twitch_login,
            "spoken_name": ident.spoken_name(),
        }

    @app.get("/api/memory")
    async def memory():
        return app.state.orch.memory.snapshot()

    @app.post("/api/memory/clear")
    async def memory_clear():
        app.state.orch.clear_memory()
        return {"ok": True}

    @app.post("/api/memory/forget")
    async def memory_forget(body: ForgetBody):
        app.state.orch.forget_viewer(body.login)
        return {"ok": True}

    @app.post("/api/memory/note")
    async def memory_note(body: NoteBody):
        snap = await app.state.orch.edit_note(body.login, body.text, body.op, body.pinned)
        return {"ok": True, "memory": snap}

    @app.post("/api/marker")
    async def marker(body: MarkerBody):
        result = await app.state.orch.mark_moment(body.note)
        return {"ok": True, **result}

    @app.post("/api/clip")
    async def clip():
        try:
            result = await app.state.orch.create_clip()
        except Exception as e:
            return JSONResponse({"ok": False, "error": str(e)}, status_code=400)
        return {"ok": True, **result}

    @app.post("/api/twitch/connect")
    async def twitch_connect():
        try:
            await app.state.orch.twitch_connect()
            return {"ok": True}
        except Exception as e:
            return JSONResponse({"ok": False, "error": str(e)}, status_code=400)

    @app.post("/api/twitch/irc")
    async def twitch_irc(body: FlagBody):
        try:
            if body.enabled:
                await app.state.orch.connect_irc()
            else:
                await app.state.orch.disconnect_irc()
            return {"ok": True}
        except Exception as e:
            return JSONResponse({"ok": False, "error": str(e)}, status_code=400)

    @app.post("/api/twitch/events")
    async def twitch_events(body: FlagBody):
        try:
            if body.enabled:
                await app.state.orch.connect_events()
            else:
                await app.state.orch.disconnect_events()
            return {"ok": True}
        except Exception as e:
            return JSONResponse({"ok": False, "error": str(e)}, status_code=400)

    @app.post("/api/twitch/disconnect")
    async def twitch_disconnect():
        await app.state.orch.twitch_disconnect()
        return {"ok": True}

    @app.post("/api/twitch/shoutout")
    async def twitch_shoutout(body: ShoutoutBody):
        try:
            await app.state.orch.shoutout(body.user or None)
            return {"ok": True}
        except Exception as e:
            return JSONResponse({"ok": False, "error": str(e)}, status_code=400)

    @app.websocket("/ws")
    async def ws(ws: WebSocket):
        origin = ws.headers.get("origin")
        if origin and not origin_is_local(origin):
            await ws.close(code=1008)
            return
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
