"""Bounded phone screenshots and gestures tied to the observed screen."""
from typing import Literal, Self
from pydantic import Field, StrictInt, StrictStr, model_validator
from nexus.contracts.base import CommandDigest, StrictContract, Version1
from nexus.contracts.android_control import digest
from nexus.contracts.images import ScreenImage


class AndroidScreenObserveArguments(StrictContract):
    # Physical display pixels; omitted means the complete display.
    region: list[StrictInt] | None = Field(default=None, min_length=4, max_length=4)

    @model_validator(mode="after")
    def bounded(self) -> Self:
        if self.region is not None:
            l,t,r,b=self.region
            if not 0 <= l < r <= 8192 or not 0 <= t < b <= 8192:
                raise ValueError("invalid screen crop")
        return self


class AndroidScreenActArguments(StrictContract):
    observation_digest: CommandDigest
    action: Literal["tap", "long_press", "swipe"]
    # Coordinates refer to the returned image, including when it is cropped.
    x: StrictInt = Field(ge=0, le=1599)
    y: StrictInt = Field(ge=0, le=1599)
    end_x: StrictInt | None = Field(default=None, ge=0, le=1599)
    end_y: StrictInt | None = Field(default=None, ge=0, le=1599)

    @model_validator(mode="after")
    def consistent(self) -> Self:
        if self.action == "swipe":
            if self.end_x is None or self.end_y is None or (self.x,self.y)==(self.end_x,self.end_y):
                raise ValueError("swipe requires a distinct endpoint")
        elif self.end_x is not None or self.end_y is not None:
            raise ValueError("only swipe takes an endpoint")
        return self


class AndroidScreenState(StrictContract):
    package_name: StrictStr = Field(min_length=1, max_length=200)
    window_id: StrictInt
    width: StrictInt = Field(ge=1, le=8192)
    height: StrictInt = Field(ge=1, le=8192)
    rotation: StrictInt = Field(ge=0, le=3)
    region: list[StrictInt] = Field(min_length=4, max_length=4)

    @model_validator(mode="after")
    def bounded(self) -> Self:
        l,t,r,b=self.region
        if not 0 <= l < r <= self.width or not 0 <= t < b <= self.height or self.width*self.height>16_000_000:
            raise ValueError("invalid screen geometry")
        return self


class AndroidScreenResult(StrictContract):
    state: AndroidScreenState
    image: ScreenImage
    observation_digest: CommandDigest
    action: Literal["tap", "long_press", "swipe"] | None = None
    previous_digest: CommandDigest | None = None
    verification: Literal["screen_observed", "gesture_completed"]

    @model_validator(mode="after")
    def evidence(self) -> Self:
        l,t,r,b=self.state.region
        if self.image.width>r-l or self.image.height>b-t:
            raise ValueError("image exceeds screen region")
        if digest({"state":self.state.model_dump(mode="json"),"image_sha256":self.image.sha256})!=self.observation_digest:
            raise ValueError("screen digest mismatch")
        if (self.action is None)!=(self.verification=="screen_observed") or (self.previous_digest is None)!=(self.action is None):
            raise ValueError("gesture evidence mismatch")
        return self


class AndroidScreenObserveCall(StrictContract):
    tool: Literal["android.screen_observe"]
    tool_version: Version1
    arguments: AndroidScreenObserveArguments


class AndroidScreenActCall(StrictContract):
    tool: Literal["android.screen_act"]
    tool_version: Version1
    arguments: AndroidScreenActArguments


def validate_screen_result(command, result):
    if not isinstance(result, AndroidScreenResult):
        return
    if command.tool == "android.screen_act":
        if result.action != command.arguments.action or result.previous_digest != command.arguments.observation_digest:
            raise ValueError("gesture does not match command")
    else:
        expected=command.arguments.region or [0,0,result.state.width,result.state.height]
        if result.action is not None or result.state.region != expected:
            raise ValueError("screenshot does not match request")
