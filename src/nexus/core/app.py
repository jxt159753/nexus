"""Single Core: authenticated transport, durable Harness and owner API."""

import asyncio
import mimetypes
import os
import sqlite3
from contextlib import asynccontextmanager
from importlib.resources import files

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import SecretStr

from nexus.core.config import CoreConfig
from nexus.core.harness.http import HarnessHTTP
from nexus.core.harness.service import Harness
from nexus.core.link.nodes import NodeLink
from nexus.core.link.clients import ClientLink
from nexus.core.memory.http import RecordsHTTP
from nexus.core.models.providers import DisabledDecisionModel, DisabledProvider
from nexus.core.models.deepseek import DeepSeekProvider
from nexus.core.models.chat import DeepSeekChatProvider
from nexus.core.models.web import DeepSeekWebProvider
from nexus.core.cognitive.language import LanguageService
from nexus.core.cognitive.http import LanguageHTTP
from nexus.core.cognitive.chat import ChatService
from nexus.core.cognitive.chat_http import ChatHTTP
from nexus.core.cognitive.attachment_http import AttachmentHTTP
from nexus.core.extensions.http import ExtensionHTTP
from nexus.core.extensions.service import ExtensionService
from nexus.core.extensions.skills import SkillService
from nexus.core.extensions.tools import ToolService
from nexus.core.link.sandbox import SandboxLink
from nexus.core.models.extensions import DeepSeekExtensionProvider
from nexus.core.security.http import SecurityHTTP, TransportPolicy
from nexus.core.security.service import SecurityService
from nexus.core.security.types import AccessDenied
from nexus.core.security.modes import PermissionHTTP
from nexus.core.plugins.http import PluginHTTP
from nexus.core.storage.database import Database
from nexus.core.storage.tasks import Conflict
from nexus.core.voice.link import VoiceLink
from nexus.core.voice.providers import FakeAsrProvider, FakeTtsProvider
from nexus.core.voice.service import VoiceService
from nexus.locking import DataDirectoryLock


def create_app(config: CoreConfig) -> FastAPI:
    config = CoreConfig.model_validate_json(config.model_dump_json())
    database = Database(config.data_dir.resolve() / "core.sqlite3")
    guards = SecurityHTTP(SecurityService(database), TransportPolicy(config.origins, config.local_http))
    link = NodeLink(guards, config)
    harness = Harness(link)
    clients = ClientLink(harness, guards)
    sandbox_link = SandboxLink(guards, config, harness.operation)
    provider = None
    chat_provider = None
    extension_provider = None
    if config.models.enabled:
        key = os.environ.get("DEEPSEEK_API_KEY", "")
        if not key or len(key) > 256 or any(c.isspace() for c in key):
            raise ValueError("DEEPSEEK_API_KEY must be configured securely")
        provider = DeepSeekProvider(SecretStr(key), config.models)
        if config.models.chat_enabled:
            chat_provider = DeepSeekChatProvider(SecretStr(key), config.models)
        extension_provider = DeepSeekExtensionProvider(SecretStr(key), config.models.timeout_seconds)
    language = LanguageService(harness, provider)
    chat = ChatService(harness, chat_provider, language.owner)
    if config.models.enabled and config.models.chat_enabled and config.models.web_enabled:
        chat.work.web.provider = DeepSeekWebProvider(SecretStr(key))
    extensions = ExtensionService(harness, extension_provider)
    tools = ToolService(extensions, sandbox_link, config.extensions)
    extensions.sandbox = tools
    skills = SkillService(harness, extensions, tools)
    extensions.skills = skills
    chat.skills = skills
    from nexus.core.plugins.testing import PluginTester
    chat.plugins.tester = PluginTester(chat.plugins, sandbox_link, tools)
    voice_asr = FakeAsrProvider()
    voice_tts = FakeTtsProvider()
    if config.voice.enabled and config.voice.provider == "bailian":
        from nexus.core.voice.bailian import BailianAsrProvider, BailianTtsProvider
        voice_asr = BailianAsrProvider(config.voice)
        voice_tts = BailianTtsProvider(config.voice)
    voice_responder = None
    voice_conversation = None
    voice_streamer = voice_canceller = None
    if chat_provider is not None:
        from nexus.core.voice.chat_bridge import ChatVoiceBridge
        bridge = ChatVoiceBridge(chat, config.owner_id)
        voice_responder, voice_conversation = bridge.respond, bridge.create_conversation
        voice_streamer, voice_canceller = bridge.stream, bridge.cancel
    elif config.voice.provider == "fake":
        async def voice_echo(session, text):
            return f"收到：{text}"
        voice_responder = voice_echo
    voice = VoiceService(
        database, config.voice, config.owner_id,
        asr=voice_asr, tts=voice_tts,
        responder=voice_responder, conversation_factory=voice_conversation,
        streamer=voice_streamer, canceller=voice_canceller,
    )
    voice_link = VoiceLink(guards, config, voice)
    voice.validate_owner = guards.service.validate_principal

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        database.path.parent.mkdir(parents=True, exist_ok=True)
        lock = DataDirectoryLock(database.path.parent / "core.lock")
        scheduler = None
        interpreter = None
        chatter = None
        generator = None
        skill_runner = None
        plugin_scheduler = None
        plugin_tester = None
        voice_watcher = None
        try:
            await database.initialize()
            await harness.executions.resume_verification()
            await language.recover()
            await chat.recover()
            await voice.recover()
            if config.voice.enabled:
                voice_watcher = asyncio.create_task(voice.watch())
            from nexus.core.plugins.lifecycle import recover as recover_plugins
            await recover_plugins(chat.plugins)
            await extensions.recover()
            await tools.recover()
            await chat.plugins.seed()
            await chat.plugins.tester.recover()
            plugin_tester = asyncio.create_task(chat.plugins.tester.run())
            from nexus.core.plugins.schedule import run as run_plugin_schedule
            plugin_scheduler = asyncio.create_task(run_plugin_schedule(chat.plugins))
            scheduler = asyncio.create_task(harness.run())
            interpreter = asyncio.create_task(language.run())
            chatter = asyncio.create_task(chat.run())
            generator = asyncio.create_task(extensions.run())
            skill_runner = asyncio.create_task(skills.run())
            yield
        finally:
            try:
                for task in (voice_watcher, plugin_tester, plugin_scheduler, skill_runner, generator, chatter, interpreter, scheduler):
                    if task is not None:
                        task.cancel()
                await asyncio.gather(*(task for task in (voice_watcher, plugin_tester, plugin_scheduler, skill_runner, generator, chatter, interpreter, scheduler) if task is not None), return_exceptions=True)
                await chat.work.web.close()
                await voice_link.close()
                await link.close()
                await sandbox_link.close()
                await clients.close()
            finally:
                lock.close()

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.include_router(guards.router)
    app.include_router(link.router)
    app.include_router(HarnessHTTP(harness, guards).router)
    app.include_router(clients.router)
    app.include_router(RecordsHTTP(guards).router)
    app.include_router(LanguageHTTP(language, guards).router)
    app.include_router(ChatHTTP(chat, guards).router)
    app.include_router(PermissionHTTP(chat, guards).router)
    app.include_router(PluginHTTP(chat.plugins, guards).router)
    app.include_router(voice_link.router)
    from nexus.core.voice.http import VoiceHTTP
    app.include_router(VoiceHTTP(voice, chat, guards, config).router)
    app.include_router(AttachmentHTTP(chat, guards).router)
    app.include_router(ExtensionHTTP(extensions, guards).router)
    app.include_router(sandbox_link.router)
    app.state.node_link = link
    app.state.harness = harness
    app.state.client_link = clients
    app.state.model_provider = provider or DisabledProvider()
    app.state.language = language
    app.state.chat = chat
    app.state.plugins = chat.plugins
    app.state.extensions = extensions
    app.state.skills = skills
    app.state.sandbox_link = sandbox_link
    app.state.voice = voice
    app.state.voice_link = voice_link
    app.state.decision_model = DisabledDecisionModel()
    web = files("nexus.web")
    # Python's mimetypes does not know .mjs on every platform; browsers refuse
    # module scripts served as text/plain.
    mimetypes.add_type("text/javascript", ".mjs")
    app.mount("/static", StaticFiles(directory=str(web)), name="static")

    @app.get("/")
    async def index():
        return FileResponse(str(web.joinpath("index.html")))

    @app.get("/app")
    async def app_client():
        return FileResponse(str(web.joinpath("app", "app.html")))

    @app.middleware("http")
    async def response_policy(request: Request, call_next):
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = "default-src 'self'; script-src 'self' 'sha256-UaO1DKdOZTH3g72w3U/IhBu3y5cxME0JyJYosMMMerQ=' 'sha256-jH4JDM+p01kuu6ou/A4MgTq6B9+X7dUZGHEdH52L4GY='; style-src 'self'; img-src 'self' data:; connect-src 'self'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'; object-src 'none'"
        return response

    @app.exception_handler(AccessDenied)
    async def access_error(request: Request, exc: AccessDenied):
        return JSONResponse({"detail": exc.code}, status_code=401 if exc.code == "AUTH_REQUIRED" else 503 if exc.code == "BUSY" else 403, headers={"Cache-Control": "no-store"})

    @app.exception_handler(Conflict)
    async def conflict_error(request: Request, exc: Conflict):
        return JSONResponse({"detail": "BUSY" if str(exc) == "BUSY" else "IDEMPOTENCY_CONFLICT"}, status_code=429 if str(exc) == "BUSY" else 409)

    @app.exception_handler(LookupError)
    async def missing_error(request: Request, exc: LookupError):
        return JSONResponse({"detail": "NOT_FOUND"}, status_code=404)

    @app.exception_handler(sqlite3.Error)
    async def storage_error(request: Request, exc: sqlite3.Error):
        harness.failed = True
        harness.wake.set()
        await link.close()
        return JSONResponse({"detail": "STORAGE_UNAVAILABLE"}, status_code=503)
    return app
