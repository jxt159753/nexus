"""Configured UI targets and observed-state-bound actions; no selectors/code input."""

from pydantic import Field, StrictStr, model_validator

from nexus.contracts.base import CommandDigest, StrictContract
from nexus.contracts.basic import CommandId
from typing import Literal


class ObserveArguments(StrictContract):
    root_id: Literal["workspace"]
    target_id: CommandId
    template_digest: CommandDigest


class InteractArguments(ObserveArguments):
    action_id: CommandId
    expected_state_sha256: CommandDigest
    value: StrictStr | None = Field(default=None, max_length=2000)

    @model_validator(mode="after")
    def bounded(self):
        if self.value is not None and ("\0" in self.value or len(self.value.encode())>6000):
            raise ValueError("bounded UI value required")
        return self


class WorkObserveArguments(StrictContract):
    root_id: Literal["workspace"]
    target_id: CommandId


class WorkInteractArguments(WorkObserveArguments):
    action_id: CommandId
    expected_state_sha256: CommandDigest
    value: StrictStr | None = Field(default=None, max_length=2000)

    @model_validator(mode="after")
    def bounded(self):
        if self.value is not None and ("\0" in self.value or len(self.value.encode())>6000):
            raise ValueError("bounded UI value required")
        return self


class ActionDescription(StrictContract):
    action_id: CommandId
    description: StrictStr = Field(min_length=1, max_length=200)
    kind: Literal["navigate","click","fill","focus","invoke","set_value"]


class InteractionObservation(StrictContract):
    kind: Literal["edge","chrome","windows_app"]
    state_json: StrictStr = Field(max_length=18000)
    state_sha256: CommandDigest
    actions: list[ActionDescription] = Field(max_length=16)


class InteractionResult(StrictContract):
    root_id: Literal["workspace"]
    target_id: CommandId
    template_digest: CommandDigest
    observation: InteractionObservation
    action_id: CommandId | None = None
    previous_state_sha256: CommandDigest | None = None
    verification: Literal["configured_target_observed","configured_action_returned"]
