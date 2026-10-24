"""Bounded goal plans and evidence references; never executable instructions."""

from typing import Literal, Self
from uuid import UUID

from pydantic import Field, JsonValue, StrictBool, StrictInt, StrictStr, model_validator

from nexus.contracts.base import StrictContract
from nexus.contracts.extensions import canonical


class Assertion(StrictContract):
    path: StrictStr = Field(max_length=240, pattern=r"^(|/.*)$")
    equals: JsonValue


class Criterion(StrictContract):
    id: StrictStr = Field(pattern=r"^[a-zA-Z0-9_-]{1,40}$")
    description: StrictStr = Field(min_length=1, max_length=300)
    category: Literal["artifact", "test", "effect"]
    tool: StrictStr = Field(min_length=1, max_length=64)
    arguments: list[Assertion] = Field(default_factory=list, max_length=8)
    assertions: list[Assertion] = Field(min_length=1, max_length=8)


class GoalStep(StrictContract):
    id: StrictStr = Field(pattern=r"^[a-zA-Z0-9_-]{1,40}$")
    instruction: StrictStr = Field(min_length=1, max_length=400)
    depends_on: list[StrictStr] = Field(default_factory=list, max_length=16)
    criteria: list[StrictStr] = Field(min_length=1, max_length=12)


class GoalPlan(StrictContract):
    expected_revision: StrictInt = Field(ge=0)
    constraints: list[StrictStr] = Field(default_factory=list, max_length=8)
    criteria: list[Criterion] = Field(min_length=1, max_length=12)
    steps: list[GoalStep] = Field(min_length=1, max_length=16)
    reason: StrictStr = Field(default="", max_length=500)

    @model_validator(mode="after")
    def valid_graph(self) -> Self:
        criteria = {item.id for item in self.criteria}
        steps = {item.id: item for item in self.steps}
        if len(criteria) != len(self.criteria) or len(steps) != len(self.steps):
            raise ValueError("duplicate plan identifiers")
        if sum(len(item) for item in self.constraints) > 800 or len(canonical(self.model_dump(mode="json")).encode()) > 7000:
            raise ValueError("goal plan exceeds context limit")
        seen: set[str] = set()
        visiting: set[str] = set()
        def visit(identifier: str) -> None:
            if identifier in visiting or identifier not in steps:
                raise ValueError("goal dependencies must be an acyclic graph")
            if identifier in seen:
                return
            visiting.add(identifier)
            step = steps[identifier]
            if not set(step.criteria) <= criteria or len(set(step.depends_on)) != len(step.depends_on):
                raise ValueError("unknown criterion or duplicate dependency")
            for parent in step.depends_on:
                visit(parent)
            visiting.remove(identifier)
            seen.add(identifier)
        for identifier in steps:
            visit(identifier)
        if set().union(*(set(step.criteria) for step in self.steps)) != criteria:
            raise ValueError("every criterion must belong to a plan step")
        return self


class EvidenceBinding(StrictContract):
    criterion_id: StrictStr = Field(max_length=40)
    task_id: UUID


class GoalCheckpoint(StrictContract):
    expected_revision: StrictInt = Field(ge=1)
    step_id: StrictStr = Field(max_length=40)
    status: Literal["succeeded", "failed", "blocked"]
    evidence: list[EvidenceBinding] = Field(default_factory=list, max_length=12)
    note: StrictStr = Field(default="", max_length=1000)
    remaining: list[StrictStr] = Field(default_factory=list, max_length=8)

    @model_validator(mode="after")
    def bounded(self) -> Self:
        if sum(len(item) for item in self.remaining) > 1000:
            raise ValueError("remaining work exceeds limit")
        return self


class GoalPolicy(StrictContract):
    expected_revision: StrictInt = Field(ge=0)
    resume_after_restart: StrictBool
    priority: StrictInt = Field(default=0, ge=0, le=9)

