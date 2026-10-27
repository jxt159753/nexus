"""Explicit host roots; Core alone authorizes their use in full mode."""

from pathlib import PurePosixPath, PureWindowsPath

from pydantic import Field, StrictStr, model_serializer, field_validator

from nexus.contracts.base import StrictContract


class HostAccess(StrictContract):
    access_root: StrictStr | None = Field(default=None, min_length=1, max_length=240,
        description="仅 full 模式：目标设备上任意本地绝对目录，例如 C:/Users/name/Desktop；path 相对此目录。省略时使用配置的 workspace。")

    @field_validator('access_root')
    @classmethod
    def absolute_root(cls, value):
        if value is not None:
            if any(ord(c) < 32 for c in value) or not (PurePosixPath(value).is_absolute() or PureWindowsPath(value).is_absolute()):
                raise ValueError('absolute host directory required')
        return value

    @model_serializer(mode='wrap')
    def serialized_host_access(self, handler):
        value = handler(self)
        # Old tasks, receipts and approvals retain their canonical identity.
        if self.access_root is None:
            value.pop('access_root', None)
        return value
