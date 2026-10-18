"""No network clients, keys or fallback. Every model entry fails closed."""

from dataclasses import dataclass
from typing import Protocol

from nexus.contracts.errors import ErrorCode, ProtocolError


@dataclass(frozen=True)
class ModelLimits:
    max_calls: int = 0
    max_cost_microunits: int = 0
    max_output_tokens: int = 0
    timeout_seconds: float = 0


class DecisionModel(Protocol):
    async def decide(self, context: str, budget: ModelLimits) -> object: ...


class ModelProvider(Protocol):
    def estimate(self, request: str) -> object: ...
    async def invoke(self, request: str, limits: ModelLimits) -> object: ...


class DisabledProvider:
    def estimate(self, request: str) -> object:
        raise ProtocolError(ErrorCode.MODEL_DISABLED, "Models are disabled")

    async def invoke(self, request: str, limits: ModelLimits) -> object:
        raise ProtocolError(ErrorCode.MODEL_DISABLED, "Models are disabled")


class DisabledDecisionModel:
    async def decide(self, context: str, budget: ModelLimits) -> object:
        raise ProtocolError(ErrorCode.MODEL_DISABLED, "Models are disabled")
