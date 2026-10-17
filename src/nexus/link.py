"""Bounded wire helpers shared by the two transport endpoints."""

import json
import time
from collections import deque
from datetime import UTC, datetime
from uuid import UUID, uuid4

from nexus.contracts.base import StrictContract
from nexus.contracts.errors import ErrorCode, ProtocolError
from nexus.contracts.protocol import Message, encode_message, parse_message


def wire_message(kind: str, payload: StrictContract, correlation_id: UUID | None = None) -> str:
    value = dict(protocol_version=1, type=kind, message_id=str(uuid4()),
                 correlation_id=None if correlation_id is None else str(correlation_id),
                 sent_at=datetime.now(UTC).isoformat(), payload=payload.model_dump(mode="json"))
    return encode_message(parse_message(json.dumps(value, ensure_ascii=False)))


class MessageRate:
    def __init__(self, maximum: int = 60, window: float = 10):
        self.maximum, self.window = maximum, window
        self.times: deque[float] = deque()

    def check(self) -> None:
        now = time.monotonic()
        while self.times and self.times[0] <= now - self.window:
            self.times.popleft()
        if len(self.times) >= self.maximum:
            raise ProtocolError(ErrorCode.BUSY, "Message rate exceeded")
        self.times.append(now)


def session_matches(message: Message, session_id: UUID) -> None:
    if getattr(message.payload, "session_id", None) != session_id:
        raise ProtocolError(ErrorCode.FORBIDDEN, "Connection session mismatch")
