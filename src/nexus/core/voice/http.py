"""Owner/CSRF activation binds a device, conversation and outbound consent."""

from uuid import UUID

from fastapi import APIRouter, HTTPException, Request

from nexus.contracts.base import StrictContract
from nexus.contracts.voice import VoiceConsent
from nexus.core.harness.http import body
from nexus.core.security.types import AccessDenied
from nexus.core.storage.database import one
from pydantic import Field, StrictBool, StrictStr


class Announcement(StrictContract):
    text: StrictStr = Field(min_length=1, max_length=500)
    listen: StrictBool = False


class VoiceHTTP:
    def __init__(self, service, chat, guards, config):
        self.service, self.chat, self.guards, self.config = service, chat, guards, config
        self.router = APIRouter(prefix="/api/v1/voice")
        self.router.add_api_route("", self.info, methods=["GET"])
        self.router.add_api_route("/sessions", self.prepare, methods=["POST"])
        self.router.add_api_route("/sessions/{session_id}/stop", self.stop, methods=["POST"])
        self.router.add_api_route("/sessions/{session_id}/interrupt", self.interrupt, methods=["POST"])
        self.router.add_api_route("/sessions/{session_id}/announce", self.announce, methods=["POST"])

    async def info(self, request: Request):
        await self.guards.owner_http(request)
        return {
            "enabled": self.config.voice.enabled,
            "provider": self.config.voice.provider,
            "audio_disclosure": "麦克风音频经 Core 发送到阿里云百炼；转写、当前会话及获准工具结果发送到现有聊天模型。原始录音不保存。",
            "nodes": [str(key) for key, value in self.config.nodes.items() if value.voice.enabled],
            "max_session_seconds": self.config.voice.max_session_seconds,
            "dnd_start_hour": self.config.voice.dnd_start_hour,
            "dnd_end_hour": self.config.voice.dnd_end_hour,
            "proactive_daily_limit": self.config.voice.proactive_daily_limit,
            "voice_id": self.config.voice.voice_id,
            "sessions": [{"voice_session_id": str(s.voice_session_id), "request_id": str(s.request_id),
                          "node_id": str(s.node_id), "conversation_id": str(s.conversation_id),
                          "state": s.state, "expires_at": s.expires_at.isoformat(), "turns": s.turns,
                          "usage": s.calls} for s in self.service.sessions.values() if s.active],
        }

    async def prepare(self, request: Request):
        owner = await self.guards.owner_http(request)
        value = await body(request, VoiceConsent)
        if not value.allow_audio_model or not value.allow_model:
            raise AccessDenied("MODEL_CONSENT_REQUIRED")
        if self.config.voice.provider == "bailian" and (not self.chat.config.enabled or not self.chat.config.chat_enabled):
            raise AccessDenied("MODEL_DISABLED")
        if value.allow_tools != value.allow_work_results:
            raise AccessDenied("MODEL_CONSENT_REQUIRED")
        if value.proactive_listen and not value.proactive_speak:
            raise AccessDenied("POLICY_DENIED")
        if value.proactive_speak and value.trigger != "wake":
            raise AccessDenied("POLICY_DENIED")
        policy = self.config.nodes.get(value.node_id)
        if not policy or not policy.voice.enabled or not {"capture", "playback"} <= policy.voice.features:
            raise AccessDenied("VOICE_DISABLED")
        if value.trigger == "wake" and "wake" not in policy.voice.features:
            raise AccessDenied("POLICY_DENIED")
        history = await self.chat.history(owner, value.conversation_id)
        if history["conversation"]["archived"] or history["active_turn_id"]:
            raise AccessDenied("BUSY")
        async with self.chat.database.transaction(write=False) as conn:
            if not await one(conn, "SELECT 1 FROM identities WHERE identity_id=? AND role='node' AND revoked_at IS NULL",
                             (str(value.node_id),)):
                raise AccessDenied("POLICY_DENIED")
        consent = value.model_dump(mode="json", exclude={"request_id", "node_id", "conversation_id", "trigger"})
        session = await self.service.prepare(owner, value.node_id, value.request_id, value.trigger,
                                             value.conversation_id, consent,
                                             min(self.config.voice.max_session_seconds, policy.voice.max_session_seconds))
        session.owner_principal = owner
        return {"voice_session_id": str(session.voice_session_id), "request_id": str(session.request_id),
                "expires_at": session.expires_at.isoformat(), "node_id": str(session.node_id)}

    async def _session(self, request, session_id):
        await self.guards.owner_http(request)
        await body(request, StrictContract)
        session = self.service.sessions.get(session_id)
        if session is None:
            raise LookupError("voice session not found")
        return session

    async def stop(self, session_id: UUID, request: Request):
        session = await self._session(request, session_id)
        await self.service.stop(session, "user")
        return {"state": session.state, "media_stopped": session.media_stopped.is_set()}

    async def interrupt(self, session_id: UUID, request: Request):
        session = await self._session(request, session_id)
        await self.service.barge_in(session)
        return {"state": session.state}

    async def announce(self, session_id: UUID, request: Request):
        await self.guards.owner_http(request)
        value = await body(request, Announcement)
        if not value.text.strip():
            raise HTTPException(400, detail="INVALID_MESSAGE")
        session = self.service.sessions.get(session_id)
        if session is None:
            raise LookupError("voice session not found")
        return {"started": await self.service.announce(session, value.text, listen=value.listen)}
