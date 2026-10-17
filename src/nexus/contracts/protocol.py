"""Typed JSON envelopes and bounded codecs; no sockets or authorization."""

from typing import Annotated, Generic, Literal, Self, TypeVar
from uuid import UUID

from pydantic import Field, TypeAdapter, ValidationError, model_validator
from pydantic_core import PydanticSerializationError

from nexus.contracts import messages as payloads
from nexus.contracts.base import MAX_MESSAGE_BYTES, StrictContract, UTCDateTime, Version1
from nexus.contracts.errors import ErrorCode, ProtocolError
from nexus.contracts.task import TaskRequest

Payload = TypeVar("Payload")


class Envelope(StrictContract, Generic[Payload]):
    protocol_version: Version1
    message_id: UUID
    correlation_id: UUID | None
    sent_at: UTCDateTime
    payload: Payload

    @model_validator(mode="after")
    def check_message_bytes(self) -> Self:
        if len(self.model_dump_json().encode("utf-8")) > MAX_MESSAGE_BYTES:
            raise ValueError("message exceeds 64 KiB")
        return self


class TaskSubmitEnvelope(Envelope[TaskRequest]):
    type: Literal["task.submit"]


class ClientSyncEnvelope(Envelope[payloads.ClientSync]):
    type: Literal["client.sync"]


class TaskCancelEnvelope(Envelope[payloads.TaskCancel]):
    type: Literal["task.cancel"]


class ApprovalDecideEnvelope(Envelope[payloads.ApprovalDecide]):
    type: Literal["approval.decide"]


class ControlRequestEnvelope(Envelope[payloads.ControlRequest]):
    type: Literal["control.pause", "control.resume", "control.cancel_all"]


class ClientSyncedEnvelope(Envelope[payloads.ClientSynced]):
    type: Literal["client.synced"]


class TaskAcceptedEnvelope(Envelope[payloads.TaskAccepted]):
    type: Literal["task.accepted"]


class TaskUpdatedEnvelope(Envelope[payloads.TaskUpdated]):
    type: Literal["task.updated"]


class ApprovalRequiredEnvelope(Envelope[payloads.ApprovalRequired]):
    type: Literal["approval.required"]


class ControlUpdatedEnvelope(Envelope[payloads.ControlUpdated]):
    type: Literal["control.updated"]


class NodeHelloEnvelope(Envelope[payloads.NodeHello]):
    type: Literal["node.hello"]


class NodeWelcomeEnvelope(Envelope[payloads.NodeWelcome]):
    type: Literal["node.welcome"]


class NodeHeartbeatEnvelope(Envelope[payloads.NodeHeartbeat]):
    type: Literal["node.heartbeat"]


class CommandExecuteEnvelope(Envelope[payloads.CommandExecute]):
    type: Literal["command.execute"]


class CommandReceiptEnvelope(Envelope[payloads.CommandReceipt]):
    type: Literal["command.accepted", "command.started"]


class CommandStatusEnvelope(Envelope[payloads.CommandStatus]):
    type: Literal["command.status"]


class CommandStatusResultEnvelope(Envelope[payloads.CommandStatusResult]):
    type: Literal["command.status_result"]


class CommandCancelEnvelope(Envelope[payloads.CommandCancel]):
    type: Literal["command.cancel"]


class CommandCancelledEnvelope(Envelope[payloads.CommandCancelled]):
    type: Literal["command.cancelled"]


class CommandResultEnvelope(Envelope[payloads.CommandResult]):
    type: Literal["command.result"]


class ResultAckEnvelope(Envelope[payloads.ResultAck]):
    type: Literal["result.ack"]


class ErrorEnvelope(Envelope[payloads.WireError]):
    type: Literal["error"]


Message = Annotated[
    TaskSubmitEnvelope | ClientSyncEnvelope | TaskCancelEnvelope
    | ApprovalDecideEnvelope | ControlRequestEnvelope | ClientSyncedEnvelope
    | TaskAcceptedEnvelope | TaskUpdatedEnvelope | ApprovalRequiredEnvelope
    | ControlUpdatedEnvelope | NodeHelloEnvelope | NodeWelcomeEnvelope
    | NodeHeartbeatEnvelope | CommandExecuteEnvelope | CommandReceiptEnvelope
    | CommandStatusEnvelope | CommandStatusResultEnvelope | CommandCancelEnvelope
    | CommandCancelledEnvelope | CommandResultEnvelope | ResultAckEnvelope | ErrorEnvelope,
    Field(discriminator="type"),
]
MESSAGE_ADAPTER = TypeAdapter(Message)


def parse_message(data: str | bytes) -> Message:
    """Validate a complete UTF-8 frame before handing it to application code."""
    if len(data) > MAX_MESSAGE_BYTES:
        raise ProtocolError(ErrorCode.INVALID_MESSAGE, "Message exceeds 64 KiB")
    try:
        raw = data.encode("utf-8") if isinstance(data, str) else data
    except UnicodeError:
        raise ProtocolError(ErrorCode.INVALID_MESSAGE, "Invalid UTF-8 message") from None
    if len(raw) > MAX_MESSAGE_BYTES:
        raise ProtocolError(ErrorCode.INVALID_MESSAGE, "Message exceeds 64 KiB")
    try:
        return MESSAGE_ADAPTER.validate_json(raw)
    except ValidationError as exc:
        errors = exc.errors(include_url=False, include_input=False)
        if any(
            len(item["loc"]) == 2 and item["loc"][1] == "protocol_version"
            for item in errors
        ):
            raise ProtocolError(
                ErrorCode.PROTOCOL_UNSUPPORTED, "Unsupported protocol version"
            ) from None
        if any(
            item["loc"] == ("task.submit", "payload")
            and item["type"] == "union_tag_invalid"
            for item in errors
        ):
            raise ProtocolError(
                ErrorCode.UNSUPPORTED_TASK, "Unsupported task intent"
            ) from None
        raise ProtocolError(ErrorCode.INVALID_MESSAGE, "Invalid message") from None


def encode_message(message: Message) -> str:
    """Revalidate before encoding, including instances changed by trusted code."""
    try:
        encoded = message.model_dump_json(warnings="error")
    except PydanticSerializationError:
        raise ProtocolError(ErrorCode.INVALID_MESSAGE, "Invalid message") from None
    return parse_message(encoded).model_dump_json()
