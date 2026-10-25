"""Deliver existing owner uploads through the original Task/Harness authority."""

import hashlib
import json
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, HTTPException, Request
from pydantic import SecretStr, TypeAdapter

from nexus.contracts.base import StrictContract
from nexus.contracts.file_transfer import FileReceiveArguments, receiving_path
from nexus.contracts.messages import CommandExecute
from nexus.contracts.task import FileReceiveRequest
from nexus.core.cognitive.attachment_http import TransferResponse
from nexus.core.cognitive.node_routing import names
from nexus.core.harness.http import body
from nexus.core.security.types import AccessDenied
from nexus.core.storage.database import audit, one, utc_now
from nexus.core.storage.tasks import task_record


class SendFile(StrictContract):
    request_id: UUID
    node_id: UUID


class FileTransferHTTP:
    def __init__(self, chat, guards, harness):
        self.chat, self.guards, self.harness = chat, guards, harness
        self.attachments = chat.attachments
        self.router = APIRouter()
        base = '/api/v1/chat/conversations/{conversation_id}/attachments/{attachment_id}/transfers'
        self.router.add_api_route(base, self.send, methods=['POST'])
        self.router.add_api_route(base, self.history, methods=['GET'])
        self.router.add_api_route('/api/v1/file-transfers/targets', self.targets, methods=['GET'])
        self.router.add_api_route('/api/v1/file-transfers/{attempt_id}/content', self.content, methods=['GET'])
        files = '/api/v1/chat/conversations/{conversation_id}/files/{source_kind}/{source_id}'
        self.router.add_api_route(files+'/preview', self.preview, methods=['GET'])
        self.router.add_api_route(files+'/attachment', self.import_file, methods=['POST'])

    async def preview(self, conversation_id: UUID, source_kind: Literal['attachment','deliverable'], source_id: UUID, request: Request):
        from nexus.core.cognitive import chat_files
        from nexus.core.cognitive.attachment_media import inspect
        principal = await self.guards.owner_http(request)
        async with self.attachments.parsing:
            async with self.chat.database.transaction(write=False) as conn:
                await self.chat.conversation(conn,principal,conversation_id)
                row = await chat_files.source(conn,principal.identity_id,conversation_id,source_kind,source_id,content=source_kind=='deliverable')
            if source_kind=='attachment':
                value=json.loads(row['preview_json'])
            else:
                if hashlib.sha256(row['content']).hexdigest()!=row['sha256']:
                    raise ValueError('File source integrity mismatch')
                _, value = await inspect(row['content'])
        return {'file':chat_files.card(row,source_kind,source_id),'preview':value}

    async def import_file(self, conversation_id: UUID, source_kind: Literal['attachment','deliverable'], source_id: UUID, request: Request):
        from nexus.core.cognitive.chat_files import materialize
        principal = await self.guards.owner_http(request)
        await body(request,StrictContract)
        value=await materialize(self.chat,principal,conversation_id,source_kind,source_id)
        # Never return a database row containing raw bytes.
        row=await self.attachments.get(principal,conversation_id,UUID(str(value['attachment_id'])))
        return {'attachment':self.attachments.public(row)}

    async def targets(self, request: Request):
        principal = await self.guards.owner_http(request)
        result = []
        async with self.chat.database.transaction(write=False) as conn:
            labels = await names(conn, self.harness.link.config)
            for node_id, policy in self.harness.link.config.nodes.items():
                granted = 'filesystem.receive@1' in policy.tools and 'workspace' in policy.roots
                for identity in (principal.identity_id,node_id):
                    from nexus.core.security.service import has_grant
                    active = await one(conn, "SELECT 1 FROM identities WHERE identity_id=? AND revoked_at IS NULL", (str(identity),))
                    granted = granted and bool(active) and await has_grant(conn, identity, node_id, 'filesystem.receive', 1, 'workspace')
                connection = self.harness.link.connections.get(node_id)
                online = bool(connection and connection.ready)
                supported = bool(connection and 'filesystem.receive@1' in connection.scope.tools & connection.scope.local_tools
                                 and 'workspace' in connection.scope.roots & connection.scope.local_roots)
                reason = 'ready' if granted and online and supported else 'not_authorized' if not granted else 'offline' if not online else 'upgrade_required'
                result.append({'node_id':str(node_id),'label':labels[str(node_id)],'online':online,'available':reason=='ready','reason':reason})
        return {'nodes':result,'max_file_bytes':16*1024*1024,'destination':'NEXUS-Inbox','overwrite':False}

    async def send(self, conversation_id: UUID, attachment_id: UUID, request: Request):
        principal = await self.guards.owner_http(request)
        value = await body(request,SendFile)
        source = await self.attachments.get(principal,conversation_id,attachment_id)
        submitted = FileReceiveRequest(request_id=value.request_id,intent='receive_file',arguments=FileReceiveArguments(
            root_id='workspace',conversation_id=conversation_id,attachment_id=attachment_id,
            path=receiving_path(value.request_id,source['filename']),sha256=source['sha256'],size_bytes=source['size_bytes']))
        task = await self.harness.submit(principal,value.node_id,submitted)
        return {'task':task}

    async def history(self, conversation_id: UUID, attachment_id: UUID, request: Request):
        principal = await self.guards.owner_http(request)
        async with self.chat.database.transaction(write=False) as conn:
            await self.attachments.owned(conn,principal,conversation_id,attachment_id)
            rows = await conn.execute_fetchall("SELECT * FROM tasks WHERE owner_id=? AND json_extract(action_json,'$.tool')='filesystem.receive' AND json_extract(action_json,'$.arguments.attachment_id')=? ORDER BY rowid DESC LIMIT 20",(str(principal.identity_id),str(attachment_id)))
            return {'tasks':[task_record(row) for row in rows]}

    async def content(self, attempt_id: UUID, request: Request):
        self.guards._transport(request,origin=False)
        authorization = request.headers.get('authorization','')
        if not authorization.startswith('Bearer '):
            raise HTTPException(401,detail='AUTH_REQUIRED')
        principal = await self.guards.service.authenticate_token(SecretStr(authorization[7:]),'node')
        async with self.chat.database.transaction(write=False) as conn:
            row = await one(conn,'SELECT a.command_json,t.owner_id FROM task_attempts a JOIN tasks t USING(task_id) WHERE a.attempt_id=? AND t.node_id=?',(str(attempt_id),str(principal.identity_id)))
            if row is None:
                raise AccessDenied()
            command = TypeAdapter(CommandExecute).validate_json(row['command_json'])
            owner_id = row['owner_id']
        if command.tool != 'filesystem.receive' or not await self.harness.executions.may_send(principal,self.harness.scope(principal.identity_id),command):
            raise AccessDenied()
        if self.attachments.incoming.locked():
            raise HTTPException(429,detail='UPLOAD_BUSY')
        await self.attachments.incoming.acquire()
        try:
            async with self.chat.database.transaction() as conn:
                # Recheck the source binding and reserve a bounded download before any bytes leave.
                args = command.arguments
                source = await one(conn,'SELECT content,sha256,size_bytes FROM chat_attachments WHERE attachment_id=? AND conversation_id=? AND owner_id=? AND deleted_at IS NULL',(str(args.attachment_id),str(args.conversation_id),owner_id))
                count = await one(conn,"SELECT count(*) FROM audit_events WHERE task_id=? AND kind='file.download_started'",(str(command.task_id),))
                if source is None or count[0] >= 3 or source['sha256'] != args.sha256 or source['size_bytes'] != args.size_bytes:
                    raise AccessDenied()
                raw = source['content']
                if len(raw) != args.size_bytes or hashlib.sha256(raw).hexdigest() != args.sha256:
                    raise ValueError('File integrity check failed')
                await audit(conn,str(principal.identity_id),str(command.task_id),'file.download_started',utc_now())
            return TransferResponse(raw,media_type='application/octet-stream',release=self.attachments.incoming.release,
                headers={'Cache-Control':'no-store','X-Content-Type-Options':'nosniff','X-Content-SHA256':args.sha256})
        except BaseException:
            self.attachments.incoming.release()
            raise
