"""Android's bounded read-only evidence; no platform or Core imports."""

from typing import Literal, Self

from pydantic import Field, StrictBool, StrictInt, StrictStr, model_validator

from nexus.contracts.base import MAX_RESULT_BYTES, CommandDigest, StrictContract, UTCDateTime, Version1


class AndroidDeviceStatusArguments(StrictContract):
    pass


class AndroidNotificationDigestArguments(StrictContract):
    limit: StrictInt = Field(default=10, ge=1, le=20)


class AndroidBattery(StrictContract):
    level_percent: StrictInt | None = Field(ge=0, le=100)
    charging: StrictBool
    plugged: Literal["none", "ac", "usb", "wireless", "dock", "other"]


class AndroidNetwork(StrictContract):
    connected: StrictBool
    transport: Literal["wifi", "cellular", "ethernet", "vpn", "other", "none"]
    metered: StrictBool | None

    @model_validator(mode="after")
    def consistent(self) -> Self:
        if self.connected == (self.transport == "none"):
            raise ValueError("network connection and transport disagree")
        return self


class AndroidScreen(StrictContract):
    interactive: StrictBool
    locked: StrictBool


class AndroidStorage(StrictContract):
    free_bytes: StrictInt = Field(ge=0)
    total_bytes: StrictInt = Field(gt=0)

    @model_validator(mode="after")
    def consistent(self) -> Self:
        if self.free_bytes > self.total_bytes:
            raise ValueError("free storage exceeds total")
        return self


class AndroidDeviceStatusResult(StrictContract):
    android_api: StrictInt = Field(ge=26, le=99)
    app_version: StrictStr = Field(min_length=1, max_length=50)
    battery: AndroidBattery
    network: AndroidNetwork
    screen: AndroidScreen
    storage: AndroidStorage


class AndroidNotificationEntry(StrictContract):
    key: CommandDigest
    package_name: StrictStr = Field(min_length=1, max_length=200, pattern=r"^[A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z0-9_]+)+$")
    title: StrictStr = Field(max_length=200)
    text: StrictStr = Field(max_length=500)
    posted_at: UTCDateTime
    ongoing: StrictBool
    redacted: StrictBool


class AndroidNotificationDigestResult(StrictContract):
    entries: list[AndroidNotificationEntry] = Field(max_length=20)
    truncated: StrictBool

    @model_validator(mode="after")
    def bounded_unique(self) -> Self:
        if len({entry.key for entry in self.entries}) != len(self.entries):
            raise ValueError("duplicate notification")
        if len(self.model_dump_json().encode("utf-8")) > MAX_RESULT_BYTES:
            raise ValueError("notification result exceeds byte budget")
        return self


class AndroidDeviceStatusCall(StrictContract):
    tool: Literal["android.device_status"]
    tool_version: Version1
    arguments: AndroidDeviceStatusArguments


class AndroidNotificationDigestCall(StrictContract):
    tool: Literal["android.notification_digest"]
    tool_version: Version1
    arguments: AndroidNotificationDigestArguments
