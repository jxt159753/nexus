"""Explicit plugin runtime policy. Models cannot configure host executables."""

from pathlib import Path
from pydantic import Field, StrictBool, StrictInt, StrictStr
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


class PluginNodeConfig(StrictContract):
    enabled: StrictBool=False
    allow_host_python: StrictBool=False
    pwsh: Path | None=None
    bash: Path | None=None
    git: Path | None=None
    lsp: dict[StrictStr,ProcessServer]=Field(default_factory=dict,max_length=16)
    mcp: dict[StrictStr,MCPServer]=Field(default_factory=dict,max_length=16)
    max_jobs: StrictInt=Field(default=8,ge=1,le=16)
    max_job_seconds: StrictInt=Field(default=600,ge=1,le=1800)

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
        if not self.mcp: features['builtin.mcp']=[]
        if not any(target.kind in {'edge','chrome'} for target in targets.values()): features['builtin.browser']=[]
        if self.allow_host_python: features['user.python']=['handle']
        return features
