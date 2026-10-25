"""Versioned plugin releases are data until an approved executor receives them."""

from nexus.contracts.host_access import HostAccess
from typing import Any, Annotated, Literal
from uuid import UUID

from pydantic import Field, StrictBool, StrictInt, StrictStr, model_validator, model_serializer

from nexus.contracts.base import StrictContract, CommandDigest
from nexus.contracts.extensions import digest_value
from nexus.plugins.schema import bounded, schema

PluginId=Annotated[StrictStr,Field(pattern=r'^(builtin|user)\.[a-z][a-z0-9_]{1,47}$')]
ToolId=Annotated[StrictStr,Field(pattern=r'^[a-z][a-z0-9_]{0,63}$')]


class PluginTool(StrictContract):
    name: ToolId
    description: StrictStr=Field(min_length=1,max_length=1000)
    input_schema: dict[str,Any]
    output_schema: dict[str,Any]=Field(default_factory=lambda:{'type':'object','additionalProperties':True})
    effect: Literal['read','execute','manage','destructive','network']='execute'

    @model_validator(mode='after')
    def contracts(self):
        schema(self.input_schema); schema(self.output_schema)
        if self.input_schema.get('type')!='object': raise ValueError('tool input must be an object')
        if self.output_schema.get('type')!='object': raise ValueError('tool output must be an object')
        return self


class PluginCase(StrictContract):
    tool: ToolId
    input: dict[str,Any]
    expected: dict[str,Any]
    files: dict[StrictStr,StrictStr]=Field(default_factory=dict,max_length=8)


class PluginManifest(StrictContract):
    schema_version: Literal[1]=1
    plugin_id: PluginId
    version: Annotated[StrictStr,Field(pattern=r'^\d{1,4}\.\d{1,4}\.\d{1,4}$')]
    name: StrictStr=Field(min_length=1,max_length=100)
    description: StrictStr=Field(min_length=1,max_length=2000)
    license: StrictStr=Field(min_length=1,max_length=100)
    runtime: Literal['core','node','python']='python'
    tools: list[PluginTool]=Field(min_length=1,max_length=32)
    dependencies: dict[StrictStr,StrictStr]=Field(default_factory=dict,max_length=16)

    @model_validator(mode='after')
    def identity(self):
        if len({tool.name for tool in self.tools})!=len(self.tools): raise ValueError('duplicate tool name')
        if self.plugin_id.startswith('user.') and self.runtime!='python': raise ValueError('user plugins cannot run in Core')
        return self


class PluginRelease(StrictContract):
    manifest: PluginManifest
    source: StrictStr=Field(default='',max_length=20000)
    tests: list[PluginCase]=Field(default_factory=list,max_length=12)

    @model_validator(mode='after')
    def size(self):
        bounded(self.model_dump(mode='json'),24576)
        if self.manifest.plugin_id.startswith('user.') and (not self.source or len(self.source.encode())>20000 or '\x00' in self.source):
            raise ValueError('bounded plugin Python source required')
        names={tool.name for tool in self.manifest.tools}
        if any(case.tool not in names for case in self.tests): raise ValueError('test references unknown tool')
        return self

    def digest(self): return digest_value(self.model_dump(mode='json'))


class PluginInspectArguments(StrictContract):
    plugin_id: PluginId | None=None


class PluginExecuteArguments(HostAccess):
    plugin_id: PluginId
    tool: ToolId
    input: dict[str,Any]=Field(default_factory=dict)

    @model_validator(mode='after')
    def size(self):
        bounded(self.input,8192)
        return self


class PluginNodeArguments(PluginExecuteArguments):
    root_id: Literal['workspace']='workspace'
    release_digest: CommandDigest
    context_id: UUID
    invocation_id: UUID
    release: PluginRelease | None=None
    capability_digests: dict[PluginId,CommandDigest]=Field(default_factory=dict,max_length=64)
    max_subcalls: StrictInt=Field(default=8,ge=0,le=20)
    mcp_binding: 'MCPBinding | None'=None

    @model_serializer(mode='wrap')
    def serialized(self,handler):
        value=handler(self)
        # Preserve the canonical identity of pre-MCP commands/receipts.
        if self.mcp_binding is None: value.pop('mcp_binding',None)
        if self.access_root is None: value.pop('access_root',None)
        return value


class PluginNodeResult(StrictContract):
    root_id: Literal['workspace']='workspace'
    release_digest: CommandDigest
    tool: ToolId
    data: dict[str,Any]
    verification: Literal['plugin_contract_checked']='plugin_contract_checked'

    @model_validator(mode='after')
    def size(self):
        bounded(self.data,24576)
        return self


class PluginNodeCall(StrictContract):
    tool: Literal['plugin.call']
    tool_version: Literal[1]
    arguments: PluginNodeArguments


class PluginInspectProposal(StrictContract):
    intent: Literal['plugin_inspect']
    arguments: PluginInspectArguments


class PluginExecuteProposal(StrictContract):
    intent: Literal['plugin_execute']
    arguments: PluginExecuteArguments


class PluginInstallArguments(StrictContract):
    release: PluginRelease


class PluginInstallProposal(StrictContract):
    intent: Literal['plugin_install']
    arguments: PluginInstallArguments


from nexus.plugins.mcp_contracts import MCPBinding
PluginNodeArguments.model_rebuild()
