"""Data carried from Core's MCP registry to a single authenticated Node task."""

from typing import Annotated
from uuid import UUID
from urllib.parse import urlsplit

from pydantic import Field, StrictInt, StrictStr, field_serializer, model_validator

from nexus.contracts.base import StrictContract, CommandDigest
from nexus.plugins.config import MCPServer
from nexus.plugins.schema import bounded, schema, checked
from nexus.contracts.extensions import digest_value

ServerName = Annotated[StrictStr, Field(pattern=r'^[a-z][a-z0-9_-]{0,63}$')]


class MCPConfiguration(StrictContract):
    server: ServerName
    expected_revision: StrictInt = Field(ge=0)
    configuration: MCPServer

    @model_validator(mode='after')
    def transport(self):
        validate_server(self.configuration)
        bounded(self.model_dump(mode='json'), 4096)
        return self


def validate_server(server):
    if bool(server.url) == bool(server.process):
        raise ValueError('exactly one MCP transport is required')
    if server.url:
        url = urlsplit(server.url)
        if (url.scheme != 'https' and not (url.scheme == 'http' and url.hostname in {'localhost','127.0.0.1','::1'})) or not url.hostname or url.username or url.password or url.fragment:
            raise ValueError('MCP requires HTTPS or loopback HTTP without URL credentials')
        if len(server.url) > 2048:
            raise ValueError('MCP URL limit')
    if server.process:
        # Path existence is checked on the target Node, never on Core.
        from pathlib import PureWindowsPath, PurePosixPath
        value = str(server.process.executable)
        if not (PureWindowsPath(value).is_absolute() or PurePosixPath(value).is_absolute()) or '\x00' in value:
            raise ValueError('absolute executable path required')
        if any('\x00' in item or len(item) > 2048 for item in server.process.argv):
            raise ValueError('invalid MCP process arguments')


def server_value(server):
    value=server.model_dump(mode='json')
    if value.get('process'):
        from pathlib import PureWindowsPath
        path=value['process']['executable']
        if PureWindowsPath(path).is_absolute(): value['process']['executable']=PureWindowsPath(path).as_posix()
    return value


class MCPBinding(StrictContract):
    registration_id: UUID
    server: ServerName
    revision: StrictInt = Field(ge=1)
    configuration: MCPServer
    configuration_digest: CommandDigest
    catalog_digest: CommandDigest | None = None
    permission_fingerprint: CommandDigest
    cancel_cursor: StrictInt=Field(ge=0)

    @field_serializer('configuration')
    def canonical_configuration(self, value):
        # Commands are signed on Core and checked on Node. Path serialization
        # must not depend on whether this process runs on Linux or Windows.
        return server_value(value)

    @model_validator(mode='after')
    def digest(self):
        validate_server(self.configuration)
        if digest_value(server_value(self.configuration)) != self.configuration_digest:
            raise ValueError('MCP configuration digest mismatch')
        return self


def tool_schema(value):
    """Conservative schema subset; unsupported tools stay visible but disabled."""
    value = dict(value)
    value.pop('$schema', None)
    # FastMCP marks its structuredContent wrapper with this annotation. The
    # actual result/properties/required constraints below still apply.
    if type(value.get('x-fastmcp-wrap-result')) is bool:
        value.pop('x-fastmcp-wrap-result')
    if value.get('type') == 'object':
        value.setdefault('additionalProperties', True)  # JSON Schema's default.
        value['properties'] = {key: tool_schema(child) for key, child in value.get('properties', {}).items()}
        if isinstance(value.get('additionalProperties'), dict):
            value['additionalProperties'] = tool_schema(value['additionalProperties'])
    elif value.get('type') == 'array':
        value['items'] = tool_schema(value.get('items', {}))
    if 'anyOf' in value:
        value['anyOf'] = [tool_schema(child) for child in value['anyOf']]
    schema(value)
    return value


def catalog(tools):
    import re
    if not isinstance(tools, list) or len(tools) > 64:
        raise ValueError('MCP tool count limit exceeded')
    output = []
    for item in tools:
        name = item.get('name')
        if not isinstance(name, str) or not re.fullmatch(r'[A-Za-z0-9_.-]{1,128}', name):
            raise ValueError('invalid MCP tool name')
        entry = {'name': name, 'description': str(item.get('description', ''))[:1000],
                 'inputSchema': item.get('inputSchema'), 'supported': True}
        if 'outputSchema' in item: entry['outputSchema'] = item['outputSchema']
        try:
            entry['inputSchema'] = tool_schema(entry['inputSchema'])
            if entry['inputSchema'].get('type') != 'object': raise ValueError('object input required')
            if 'outputSchema' in entry: entry['outputSchema'] = tool_schema(entry['outputSchema'])
        except (ValueError, TypeError, KeyError):
            entry['supported'] = False
            entry['reason'] = 'unsupported_bounded_schema'
        output.append(entry)
    if len({item['name'] for item in output}) != len(output): raise ValueError('duplicate MCP tool name')
    output.sort(key=lambda item: item['name'])
    bounded(output, 20000)
    return output, digest_value(output)


def check_call(tools, name, arguments):
    entry = next((item for item in tools if item['name'] == name), None)
    if entry is None or not entry['supported']: raise ValueError('MCP tool unavailable')
    checked(entry['inputSchema'], arguments)
    return entry
