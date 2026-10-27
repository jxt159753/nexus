"""Explicit plugin runtime policy. Models cannot configure host executables."""

from pathlib import Path
from typing import Literal
from pydantic import Field, StrictBool, StrictInt, StrictStr, model_validator
from nexus.contracts.base import StrictContract


class PluginConfig(StrictContract):
    enabled: StrictBool=False
    max_versions: StrictInt=Field(default=256,ge=1,le=1000)
    max_total_bytes: StrictInt=Field(default=8*1024*1024,ge=32768,le=32*1024*1024)


class ProcessServer(StrictContract):
    executable: Path
    argv: list[StrictStr]=Field(default_factory=list,max_length=32)


class MCPServer(StrictContract):
    url: StrictStr | None=None
    process: ProcessServer | None=None


class AndroidEmulatorConfig(StrictContract):
    """Owner-selected local runtime; tool arguments cannot select other devices."""
    sdk_dir: Path
    system_image: Path
    emulator_version: StrictStr
    image_revision: StrictStr
    port: StrictInt=Field(default=5588,ge=5554,le=5680)
    adb_port: StrictInt=Field(default=5088,ge=1024,le=65535)
    memory_mb: StrictInt=Field(default=4096,ge=2048,le=8192)

    @model_validator(mode='after')
    def paths(self):
        if not self.sdk_dir.is_absolute() or not self.system_image.is_absolute():
            raise ValueError('emulator paths must be absolute owner configuration')
        if self.port % 2 or self.adb_port in {self.port,self.port+1}:
            raise ValueError('emulator needs an even console port and separate ADB port')
        return self


class MuMuConfig(StrictContract):
    backend: Literal['mumu']
    install_dir: Path
    version: StrictStr
    instance: StrictInt=Field(ge=0,le=999)
    created_timestamp: StrictInt=Field(gt=0)
    adb_port: StrictInt=Field(default=5088,ge=1024,le=65535)

    @model_validator(mode='after')
    def paths(self):
        if not self.install_dir.is_absolute():
            raise ValueError('MuMu install_dir must be absolute owner configuration')
        return self


class PluginNodeConfig(StrictContract):
    enabled: StrictBool=False
    allow_host_python: StrictBool=False
    allow_managed_mcp: StrictBool=False
    pwsh: Path | None=None
    bash: Path | None=None
    git: Path | None=None
    lsp: dict[StrictStr,ProcessServer]=Field(default_factory=dict,max_length=16)
    mcp: dict[StrictStr,MCPServer]=Field(default_factory=dict,max_length=16)
    max_jobs: StrictInt=Field(default=8,ge=1,le=16)
    max_job_seconds: StrictInt=Field(default=600,ge=1,le=1800)
    android_emulator: AndroidEmulatorConfig | MuMuConfig | None=None

    def digest(self):
        from nexus.contracts.extensions import digest_value
        from nexus.plugins.catalog import RELEASES
        return digest_value({'policy':self.model_dump(mode='json'),'bundled':{key:value.digest() for key,value in RELEASES.items()}}) if self.enabled else None

    def features(self,targets):
        from nexus.plugins.catalog import RELEASES
        if not self.enabled: return {}
        features={key:[tool.name for tool in release.manifest.tools] for key,release in RELEASES.items() if release.manifest.runtime=='node'}
        features['builtin.shell']=[name for name in ('pwsh','bash') if getattr(self,name) and getattr(self,name).is_file()]
        if not features['builtin.shell']: features['builtin.terminal']=[]
        features['builtin.workspace']=[name for name in features['builtin.workspace'] if (name!='lsp' or self.lsp) and (name!='create_worktree' or self.git and self.git.is_file())]
        if not self.mcp and not self.allow_managed_mcp: features['builtin.mcp']=[]
        elif not self.allow_managed_mcp:
            features['builtin.mcp']=['list_mcp_resources','list_mcp_resource_templates','read_mcp_resource']
        if not any(target.kind in {'edge','chrome'} for target in targets.values()): features['builtin.browser']=[]
        if self.allow_host_python: features['user.python']=['handle']
        if self.android_emulator is None: features['builtin.android_emulator']=[]
        return features
