"""Observe ordinary durable ChatService turns; never start a second model loop."""

import asyncio
from collections.abc import AsyncIterator
from time import monotonic
from uuid import NAMESPACE_URL, UUID, uuid5

from nexus.contracts.chat import ChatSubmission, ConversationCreate
from nexus.core.cognitive import work_catalog
from nexus.core.security.types import AccessDenied, Principal
from nexus.core.storage.database import one
from nexus.core.voice.service import VoiceSession


PROGRESS_DELAY_SECONDS = 20
PROGRESS_INTERVAL_SECONDS = 30


class ChatVoiceBridge:
    def __init__(self, chat, owner_id: UUID):
        self.chat, self.owner_id = chat, owner_id

    async def _principal(self, session=None) -> Principal:
        if session is not None and session.owner_principal is not None:
            await self.chat.harness.security.validate_principal(session.owner_principal)
            return session.owner_principal
        async with self.chat.database.transaction(write=False) as conn:
            row = await one(conn, "SELECT token_hash FROM identities WHERE identity_id=? "
                            "AND role='owner' AND revoked_at IS NULL", (str(self.owner_id),))
        if row is None:
            raise AccessDenied("AUTH_REQUIRED")
        return Principal(self.owner_id, "owner", row[0])

    async def create_conversation(self, session: VoiceSession) -> UUID:
        conversation = await self.chat.create(await self._principal(session), ConversationCreate(request_id=session.request_id))
        return UUID(conversation["conversation_id"])

    async def respond(self, session: VoiceSession, text: str) -> str:
        return "".join([part async for part in self.stream(session, text)])

    async def stream(self, session: VoiceSession, text: str) -> AsyncIterator[str]:
        principal = await self._principal(session)
        if session.conversation_id is None:
            session.conversation_id = await self.create_conversation(session)
        history = await self.chat.history(principal, session.conversation_id)
        if history.get("active_turn_id"):
            raise AccessDenied("BUSY")
        consent = session.consent
        options = {}
        if consent.get("allow_tools") and consent.get("allow_work_results"):
            from nexus.core.cognitive.node_routing import directory
            async with self.chat.database.transaction(write=False) as conn:
                available = await directory(self.chat, conn, principal)
            if available['nodes']:
                options.update(allow_tools=True, all_nodes=True, client_node_id=session.node_id, workspace_work=True,
                               allow_work_results=True, work_digest=available['work_digest'])
        request = ChatSubmission(
            request_id=uuid5(NAMESPACE_URL, f"voice:{session.voice_session_id}:{session.utterance_id}"),
            expected_revision=history["conversation"]["revision"], text=text[:2000], allow_model=True,
            include_context=consent.get("include_context", True),
            allow_compaction=consent.get("allow_compaction", False), **options,
        )
        submitted = await self.chat.submit(principal, session.conversation_id, request)
        session.chat_turn_id = submitted["turn_id"]
        previous, exchange = "", None
        started, last_progress = monotonic(), None
        seen_progress, spoken_progress = set(), set()
        try:
            async with self.chat.database.transaction() as conn:
                await conn.execute("UPDATE voice_utterances SET turn_id=? WHERE utterance_id=?",
                                   (str(session.chat_turn_id), str(session.utterance_id)))
            async with asyncio.timeout(180):
                while True:
                    turn = await self.chat.get(principal, session.chat_turn_id)
                    surface = turn.get("presentation", {})
                    phase = surface.get("phase", turn["status"])
                    current_exchange = (turn.get("phase"), turn.get("work_round", 0))
                    if current_exchange != exchange:
                        previous, exchange = "", current_exchange
                    terminal = {"completed", "failed", "stopped", "expired", "interrupted", "incomplete", "unknown", "recovering"}
                    if phase == "awaiting_input":
                        question = next((step.get("request_text") for step in turn.get("work_steps", [])
                                         if step.get("status") == "permission" and step.get("request_text")), "")
                        yield (question + "\n" if question else "") + "请在聊天界面核对并确认这一步。"
                        return
                    # Tool-enabled output may still become a call preamble.
                    # Speak it only once the turn has a final result; tool-free
                    # replies and explicit final summaries retain live streaming.
                    stream_answer = not request.allow_tools or turn.get("phase") == "summary"
                    current = ((surface.get("final_text") if phase in terminal else
                                surface.get("live_text") if stream_answer else "") or
                               (turn.get("response_text", "") if phase == "completed" or
                                stream_answer and phase == "generating" else ""))
                    if current.startswith(previous) and len(current) > len(previous):
                        yield current[len(previous):]
                        previous = current
                    elif current and current != previous and phase in terminal:
                        yield "\n" + current
                        previous = current
                    if phase in terminal:
                        session.end_after_reply = phase in {"unknown", "recovering"}
                        if phase != "completed" and not surface.get("final_text"):
                            raise AccessDenied(turn.get("error_code") or "VOICE_TURN_FAILED")
                        return
                    # Only completed, explicitly marked milestones can interrupt a
                    # long wait. Polling and bookkeeping never create model calls.
                    updates = surface.get("process", [])
                    fresh = [item for item in updates if item["id"] not in seen_progress]
                    seen_progress.update(item["id"] for item in updates)
                    now = monotonic()
                    if (now - started >= PROGRESS_DELAY_SECONDS and
                            (last_progress is None or now - last_progress >= PROGRESS_INTERVAL_SECONDS) and
                            any(step.get("status") == "succeeded" for step in turn.get("work_steps", []))):
                        useful = [item["text"].strip() for item in fresh if item.get("substantive")
                                  and not item.get("truncated") and len(item["text"]) <= 240
                                  and item["text"].strip() and item["text"].strip() not in spoken_progress]
                        if useful:
                            update = useful[-1]
                            spoken_progress.add(update)
                            last_progress = now
                            yield update + "\n"
                    await asyncio.sleep(0.06)
        except asyncio.CancelledError:
            await asyncio.shield(self.cancel(session))
            raise
        except TimeoutError:
            await self.cancel(session)
            raise AccessDenied("VOICE_TURN_TIMEOUT") from None

    async def cancel(self, session: VoiceSession) -> None:
        if session.chat_turn_id:
            # Cancellation must still stop an in-flight request after a login
            # expires. The active owner identity is revalidated independently.
            try:
                await self.chat.cancel(await self._principal(), session.chat_turn_id)
            finally:
                active = getattr(self.chat, "active", None)
                if active and active[0] == str(session.chat_turn_id):
                    active[1].cancel()
