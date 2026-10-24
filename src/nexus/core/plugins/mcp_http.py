"""Owner/CSRF-protected MCP registration, discovery and atomic reload."""

from uuid import UUID
from fastapi import APIRouter, Request, HTTPException
from pydantic import Field, StrictInt

from nexus.contracts.base import StrictContract
from nexus.contracts.extensions import canonical
from nexus.core.harness.http import body
from nexus.core.plugins import mcp
from nexus.core.security.types import AccessDenied
from nexus.core.storage.database import utc_now
from nexus.core.storage.tasks import REQUEST, load_task, Conflict
from nexus.plugins.contracts import PluginNodeArguments
from nexus.plugins.mcp_contracts import MCPConfiguration, ServerName


class Operation(StrictContract):
    request_id: UUID
    conversation_id: UUID
    node_id: UUID
    expected_revision: StrictInt | None=Field(default=None,ge=1)


class Register(MCPConfiguration):
    request_id: UUID
    conversation_id: UUID
    node_id: UUID


class MCPHTTP:
    def __init__(self,service,guards):
        self.service,self.guards=service,guards
        self.router=APIRouter(prefix='/api/v1/mcp')
        self.router.add_api_route('',self.page,methods=['GET'])
        self.router.add_api_route('',self.register,methods=['POST'])
        self.router.add_api_route('/{server}/tools',self.tools,methods=['GET'])
        self.router.add_api_route('/{server}/discover',self.discover,methods=['POST'])
        self.router.add_api_route('/{server}/reload',self.reload,methods=['POST'])
        self.router.add_api_route('/{server}',self.remove,methods=['DELETE'])

    async def page(self,request:Request,node_id:UUID):
        principal=await self.guards.owner_http(request)
        await mcp.poll(self.service)
        async with self.service.database.transaction(write=False) as conn:
            await self.service.chat.owner(conn,principal)
            self.service.chat.harness.scope(node_id)
            return {'servers':await mcp.rows(conn,principal.identity_id,node_id)}

    async def tools(self,server:ServerName,request:Request,node_id:UUID):
        principal=await self.guards.owner_http(request)
        await mcp.poll(self.service)
        async with self.service.database.transaction(write=False) as conn:
            await self.service.chat.owner(conn,principal)
            return mcp.public(await mcp.active(conn,principal.identity_id,node_id,server))

    async def submit(self,principal,value,name,input):
        self.service.require_enabled()
        chat=self.service.chat
        async with chat.harness.operation,self.service.database.transaction() as conn:
            conversation=await chat.conversation(conn,principal,value.conversation_id)
            if conversation['archived'] or conversation['plan_mode']: raise AccessDenied('PLAN_MODE_READ_ONLY')
            scope=chat.harness.scope(value.node_id)
            if 'plugin.call@1' not in scope.tools or not scope.plugin_digest: raise AccessDenied('PLUGIN_TOOL_UNAVAILABLE')
            try: bound=await mcp.prepare(conn,principal.identity_id,value.node_id,value.conversation_id,value.request_id,name,input)
            except Conflict: raise
            except ValueError: raise HTTPException(400,detail='MCP_CONFIGURATION_INVALID') from None
            row,_=await self.service.resolve(conn,principal.identity_id,'builtin.mcp')
            request=REQUEST.validate_json(canonical({'request_id':str(value.request_id),'project_id':conversation['project_id'],'intent':'execute_plugin',
                'arguments':PluginNodeArguments(plugin_id='builtin.mcp',tool=name,input=input,release_digest=row['digest'],context_id=value.conversation_id,
                    invocation_id=value.request_id,mcp_binding=bound,max_subcalls=0).model_dump(mode='json')}))
        async with chat.harness.operation:
            task=await chat.harness.submit(principal,value.node_id,request)
        return {'operation_id':value.request_id,'task':task,'approval_required':task.status=='awaiting_approval'}

    async def register(self,request:Request):
        principal=await self.guards.owner_http(request)
        value=await body(request,Register,max_bytes=8192)
        return await self.submit(principal,value,'mcp_register',{'server':value.server,'expected_revision':value.expected_revision,'configuration':value.configuration.model_dump(mode='json')})

    async def discover(self,server:ServerName,request:Request):
        principal=await self.guards.owner_http(request)
        value=await body(request,Operation)
        return await self.submit(principal,value,'mcp_discover',{'server':server})

    async def reload(self,server:ServerName,request:Request):
        principal=await self.guards.owner_http(request)
        value=await body(request,Operation)
        if value.expected_revision is None: raise HTTPException(400,detail='EXPECTED_REVISION_REQUIRED')
        return await self.submit(principal,value,'mcp_reload',{'server':server,'expected_revision':value.expected_revision})

    async def remove(self,server:ServerName,request:Request):
        principal=await self.guards.owner_http(request)
        value=await body(request,Operation)
        if value.expected_revision is None: raise HTTPException(400,detail='EXPECTED_REVISION_REQUIRED')
        self.service.require_enabled()
        async with self.service.chat.harness.operation,self.service.database.transaction() as conn:
            conversation=await self.service.chat.conversation(conn,principal,value.conversation_id)
            if conversation['archived'] or conversation['plan_mode']: raise AccessDenied('PLAN_MODE_READ_ONLY')
            result=await mcp.remove(conn,principal.identity_id,value.node_id,server,value.expected_revision,value.request_id,value.conversation_id)
            from nexus.core.storage.control import cancel_task
            for identifier in result.pop('cancel_task_ids',[]):
                await cancel_task(conn,await load_task(conn,UUID(identifier)),principal.identity_id,utc_now())
            return result
