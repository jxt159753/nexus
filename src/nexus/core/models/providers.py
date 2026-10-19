"""Provider boundary and network-free disabled implementations."""

from dataclasses import dataclass
from typing import Protocol

from nexus.contracts.errors import ErrorCode, ProtocolError


@dataclass(frozen=True)
class ModelLimits:
    max_calls: int = 0
    max_cost_microunits: int = 0
    max_output_tokens: int = 0
    timeout_seconds: float = 0


@dataclass(frozen=True)
class ModelReply:
    content: str
    input_tokens: int
    output_tokens: int
    complete: bool
    model: str = "deepseek-flash"
    finish_reason: str = "stop"
    title: str | None = None
    proposal: dict | None = None
    native_tool_call: dict | None = None


class DecisionModel(Protocol):
    async def decide(self, context: str, budget: ModelLimits) -> object: ...


class ModelProvider(Protocol):
    def estimate(self, request: str) -> int: ...
    async def invoke(self, request: str, limits: ModelLimits | None = None) -> ModelReply: ...


class DisabledProvider:
    def estimate(self, request: str) -> int:
        raise ProtocolError(ErrorCode.MODEL_DISABLED, "Models are disabled")

    async def invoke(self, request: str, limits: ModelLimits | None = None) -> ModelReply:
        raise ProtocolError(ErrorCode.MODEL_DISABLED, "Models are disabled")


class DisabledDecisionModel:
    async def decide(self, context: str, budget: ModelLimits) -> object:
        raise ProtocolError(ErrorCode.MODEL_DISABLED, "Models are disabled")
