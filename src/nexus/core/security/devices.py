"""Owner-authorized device enrollment, durable profiles and native possession proof."""
import hashlib
import json
import secrets
from datetime import timedelta
from uuid import UUID, uuid4

from fastapi import APIRouter, Request
from pydantic import Field, SecretStr, StrictStr

from nexus.contracts.base import StrictContract
from nexus.core.cognitive import device_commands as commands
from nexus.core.cognitive.node_routing import NodeName, names
from nexus.core.config import TrustedNode, VoiceNodePolicy
from nexus.core.harness.http import body
from nexus.core.security.service import has_grant, token_hash
from nexus.core.security.types import AccessDenied
from nexus.core.storage.database import audit, one, timestamp, utc_now
from nexus.core.storage.tasks import Conflict, canonical

TOOLS = {"status": ("android.device_status", "android:device"),
         "receive_files": ("filesystem.receive", "workspace"),
         "notifications": ("android.notification_digest", "android:notifications")}


class DeviceClient(StrictContract):
    client_id: UUID
    node_id: UUID
    proof: SecretStr
    node_token: SecretStr
    name: StrictStr = Field(min_length=1, max_length=40)


class DeviceRegistry:
    def __init__(self, chat):
        self.chat = chat
        self.link = chat.harness.link
        self.database = chat.database
        self.config = self.link.config

    async def reload(self):
        async with self.database.transaction(write=False) as conn:
            rows = await conn.execute_fetchall("SELECT node_id,policy_json FROM managed_node_profiles WHERE policy_json IS NOT NULL")
        for row in rows:
            identifier = UUID(row["node_id"])
            self.config.nodes[identifier] = TrustedNode.model_validate_json(row["policy_json"])
            if connection := self.link.connections.get(identifier):
                connection.scope = self.link.trusted_scope(connection.hello, self.config.nodes[identifier])

    async def verify(self, conn, principal, client_id, proof):
        await self.chat.owner(conn, principal)
        row = await one(conn, "SELECT * FROM device_clients WHERE client_id=? AND owner_id=?", (str(client_id), str(principal.identity_id)))
        if row is None or not secrets.compare_digest(row["proof_hash"], token_hash(SecretStr(proof or ""))):
            raise AccessDenied("AUTH_REQUIRED")
        return row

    async def prepare(self, principal, value):
        proof, credential = token_hash(value.proof), token_hash(value.node_token)
        if secrets.compare_digest(proof, credential):
            raise AccessDenied("POLICY_DENIED")
        label = NodeName(name=value.name).name
        async with self.database.transaction() as conn:
            await self.chat.owner(conn, principal)
            old = await one(conn, "SELECT * FROM device_clients WHERE client_id=? OR node_id=?", (str(value.client_id), str(value.node_id)))
            if old:
                if (old["client_id"] != str(value.client_id) or old["owner_id"] != str(principal.identity_id)
                        or old["node_id"] != str(value.node_id) or not secrets.compare_digest(old["proof_hash"], proof)
                        or not secrets.compare_digest(old["node_token_hash"], credential)):
                    raise Conflict("IDEMPOTENCY_CONFLICT")
                return {"client_id": str(value.client_id)}
            identity = await one(conn, "SELECT * FROM identities WHERE identity_id=?", (str(value.node_id),))
            if identity and (identity["role"] != "node" or identity["revoked_at"] or not secrets.compare_digest(identity["token_hash"], credential)
                             or value.node_id not in self.config.nodes):
                raise AccessDenied("POLICY_DENIED")
            if (await one(conn, "SELECT count(*) FROM device_clients"))[0] >= 64:
                raise AccessDenied("BUSY")
            await conn.execute("INSERT INTO device_clients VALUES(?,?,?,?,?,?,?)", (str(value.client_id), str(principal.identity_id), str(value.node_id), proof, credential, label, timestamp(utc_now())))
            await audit(conn, str(principal.identity_id), None, "device.client_bound", utc_now())
        return {"client_id": str(value.client_id)}

    async def status(self, principal, client_id, proof):
        async with self.database.transaction(write=False) as conn:
            client = await self.verify(conn, principal, client_id, proof)
            identifier = UUID(client["node_id"])
            identity = await one(conn, "SELECT revoked_at FROM identities WHERE identity_id=? AND role='node'", (str(identifier),))
            policy = self.config.nodes.get(identifier)
            if not identity or not policy:
                return {"registered": False, "revoked": False}
            if identity["revoked_at"]:
                return {"registered": False, "revoked": True}
            labels = await names(conn, self.config)
            profile = await one(conn, "SELECT * FROM managed_node_profiles WHERE node_id=?", (str(identifier),))
            # Static permission is intersected with grants; the native app never
            # treats an advertised or configured capability as an owner grant.
            enabled = await self.permissions(conn, identifier, policy)
            return {"registered": True, "revoked": False, "node_id": str(identifier), "name": labels[str(identifier)],
                    "description": profile["description"] if profile else "", "revision": profile["revision"] if profile else 0,
                    "permissions": enabled, "online": (await self.link.node_status(identifier))["online"]}

    async def permissions(self, conn, identifier, policy):
        result = {"voice": policy.voice.enabled}
        for key, (tool, root) in TOOLS.items():
            owner = await has_grant(conn, self.config.owner_id, identifier, tool, 1, root)
            node = await has_grant(conn, identifier, identifier, tool, 1, root)
            result[key] = owner and node and f"{tool}@1" in policy.tools and root in policy.roots
        return result

    async def command(self, principal, conversation_id, value, verified_client_id):
        command = commands.parse(value.text)
        if command is None:
            return None
        from nexus.core.cognitive.chat import ACTIVE, TURN_SELECT
        async with self.database.transaction() as conn:
            conversation = await self.chat.conversation(conn, principal, conversation_id)
            encoded = canonical(value.model_dump(mode="json"))
            old = await one(conn, TURN_SELECT + " WHERE t.owner_id=? AND t.request_id=?", (str(principal.identity_id), str(value.request_id)))
            if old:
                if old["conversation_id"] != str(conversation_id) or old["request_json"] != encoded:
                    raise Conflict("IDEMPOTENCY_CONFLICT")
                return await self.chat.snapshot(conn, old)
            if conversation["archived"] or conversation["revision"] != value.expected_revision:
                raise Conflict("IDEMPOTENCY_CONFLICT")
            if self.chat.failed or self.chat.harness.failed or await one(conn, f"SELECT 1 FROM chat_turns WHERE conversation_id=? AND {ACTIVE}", (str(conversation_id),)):
                raise Conflict("BUSY")
            if (await one(conn, "SELECT paused FROM system_state"))[0]:
                raise AccessDenied("SYSTEM_PAUSED")
            recent = (await one(conn, "SELECT count(*) FROM chat_turns WHERE owner_id=? AND created_at>?", (str(principal.identity_id), timestamp(utc_now() - timedelta(minutes=1)))))[0]
            count = (await one(conn, "SELECT count(*) FROM chat_turns WHERE conversation_id=?", (str(conversation_id),)))[0]
            if recent >= 10 or count >= 200:
                raise Conflict("BUSY")
            labels = await names(conn, self.config)
            if (not command.register and command.target not in commands.CURRENT
                    and not any(label.casefold() == command.target.casefold() or key == command.target for key, label in labels.items())):
                return None
            client = None
            identifier = None
            error = command.error
            if not error:
                if command.target in commands.CURRENT:
                    if verified_client_id is not None and value.device_client_id == verified_client_id:
                        client = await one(conn, "SELECT * FROM device_clients WHERE client_id=? AND owner_id=?", (str(verified_client_id), str(principal.identity_id)))
                    if client is None:
                        error = "当前会话没有经过本机设备验证。请在新版 Android App 登录后发送这句话；网页不能代替设备注册。"
                    else:
                        identifier = UUID(client["node_id"])
                else:
                    matches = [key for key, label in labels.items() if label.casefold() == command.target.casefold() or str(key) == command.target]
                    if len(matches) != 1:
                        # Other ordinary rename instructions remain normal chat.
                        return None
                    identifier = UUID(matches[0])
            policy = self.config.nodes.get(identifier)
            identity = await one(conn, "SELECT * FROM identities WHERE identity_id=?", (str(identifier),)) if identifier else None
            if not error and identity and (identity["revoked_at"] or identity["role"] != "node"):
                error = "该设备身份已撤销，不能通过会话重新启用。"
            if not error and not policy and not command.register:
                error = "此设备尚未注册。请先说“将此设备注册为节点”。"
            if not error and command.permissions and policy and not any(tool.startswith("android.") for tool in policy.tools):
                # A managed Android profile can legitimately have every tool disabled.
                managed = await one(conn, "SELECT 1 FROM device_clients WHERE node_id=?", (str(identifier),))
                if not managed:
                    error = "这次只能配置 Android 节点的设备状态、收件箱、语音和通知摘要；电脑节点可修改名称和备注。"
            label = command.name or labels.get(str(identifier)) or (client["suggested_name"] if client else "设备")
            if not error and any(key != str(identifier) and other.casefold() == label.casefold() for key, other in labels.items()):
                if command.name:
                    error = "已有同名设备，请指定一个不同的名称。本次未修改配置。"
                else:
                    base = label[:32]
                    index = 2
                    while any(other.casefold() == f"{base} {index}".casefold() for other in labels.values()):
                        index += 1
                    label = f"{base} {index}"
            now = utc_now()
            result = {"changed": False}
            if error:
                response = error + "\n\n" + commands.HELP
            else:
                created = identity is None
                if created:
                    if not command.register or client is None or identifier in self.config.nodes:
                        raise AccessDenied("POLICY_DENIED")
                    if len(self.config.nodes) >= 16:
                        raise AccessDenied("BUSY")
                    await conn.execute("INSERT INTO identities VALUES(?,'node',?,NULL,?)", (str(identifier), client["node_token_hash"], timestamp(now)))
                    policy = TrustedNode()
                enabled = await self.permissions(conn, identifier, policy)
                patch = {"status": True} if created else {}
                patch.update(command.permissions)
                enabled.update(patch)
                if patch:
                    tools, roots = set(policy.tools), set(policy.roots)
                    for key, allow in patch.items():
                        if key not in TOOLS:
                            continue
                        tool, root = TOOLS[key]
                        if allow:
                            tools.add(f"{tool}@1"); roots.add(root)
                            await conn.execute("INSERT OR IGNORE INTO grants(grant_id,identity_id,tool,tool_version,root_id) VALUES(?,?,?,1,?)", (str(uuid4()), str(identifier), tool, root))
                            await conn.execute("INSERT OR IGNORE INTO device_owner_grants VALUES(?,?,?,1,?)", (str(principal.identity_id), str(identifier), tool, root))
                        else:
                            tools.discard(f"{tool}@1")
                            await conn.execute("DELETE FROM grants WHERE identity_id=? AND tool=? AND tool_version=1 AND root_id=?", (str(identifier), tool, root))
                            await conn.execute("DELETE FROM device_owner_grants WHERE node_id=? AND tool=? AND tool_version=1 AND root_id=?", (str(identifier), tool, root))
                    voice = policy.voice
                    if "voice" in patch:
                        voice = VoiceNodePolicy(enabled=patch["voice"], features=frozenset({"capture", "playback", "streaming_asr", "streaming_tts"}) if patch["voice"] else frozenset())
                    policy = policy.model_copy(update={"tools": frozenset(tools), "roots": frozenset(roots), "voice": voice, "resource_version": policy.resource_version + 1})
                previous = await one(conn, "SELECT * FROM managed_node_profiles WHERE node_id=?", (str(identifier),))
                description = command.description if command.description is not None else previous["description"] if previous else ""
                # Name-only edits never replace a static non-Android capability policy.
                policy_json = policy.model_dump_json() if patch or (previous and previous["policy_json"]) else None
                await conn.execute("INSERT INTO managed_node_profiles(node_id,description,policy_json) VALUES(?,?,?) ON CONFLICT(node_id) DO UPDATE SET description=excluded.description,policy_json=excluded.policy_json,revision=revision+1", (str(identifier), description, policy_json))
                await conn.execute("INSERT INTO node_names(node_id,name) VALUES(?,?) ON CONFLICT(node_id) DO UPDATE SET name=excluded.name", (str(identifier), label))
                if patch:
                    await conn.execute("UPDATE policy_state SET version=version+1")
                result = {"changed": True, "node_id": str(identifier), "name": label, "description": description, "permissions": enabled, "registration": command.register}
                response = f"已{'注册' if created else '更新'}设备“{label}”。"
                if command.description is not None:
                    response += f"备注：{description or '已清空'}。"
                if patch or command.register:
                    response += "\n已允许：" + ("、".join(commands.LABELS[k] for k, v in enabled.items() if v) or "无") + "。"
                    response += "\n未开启：" + ("、".join(commands.LABELS[k] for k, v in enabled.items() if not v) or "无") + "。"
                if command.register:
                    response += "\n配置已保存，App 将自动启动设备连接；连接状态以设备页的实时状态为准。"
                if enabled.get("voice") and (command.register or "voice" in patch):
                    response += "语音仍在你点击开麦并同意音频外发后开始。"
                if enabled.get("notifications") and (command.register or "notifications" in patch):
                    response += "通知摘要还需要在设备页选择应用并授予系统通知读取权限。"
                await audit(conn, str(principal.identity_id), None, "device.enrolled" if created else "device.configured", now, subject_id=str(identifier))
            turn_id = str(uuid4())
            context = canonical([{"role": "user", "content": value.text}])
            await conn.execute("INSERT INTO chat_turns(turn_id,conversation_id,owner_id,request_id,request_json,text,context_json,context_digest,context_count,context_truncated,status,response_text,created_at,expires_at) VALUES(?,?,?,?,?,?,?,?,0,0,'completed',?,?,?)",
                               (turn_id, str(conversation_id), str(principal.identity_id), str(value.request_id), encoded, value.text, context, hashlib.sha256(context.encode()).hexdigest(), response, timestamp(now), timestamp(now + timedelta(minutes=5))))
            await conn.execute("INSERT INTO device_chat_changes VALUES(?,?,?)", (turn_id, str(identifier) if result["changed"] else None, canonical(result)))
            await conn.execute("UPDATE conversations SET revision=revision+1,title=CASE WHEN auto_title=1 AND revision=1 THEN ? ELSE title END WHERE conversation_id=?", (value.text[:40], str(conversation_id)))
        await self.reload()
        async with self.database.transaction(write=False) as conn:
            return await self.chat.snapshot(conn, await one(conn, TURN_SELECT + " WHERE t.turn_id=?", (turn_id,)))


class DeviceHTTP:
    def __init__(self, registry, guards):
        self.registry, self.guards = registry, guards
        self.router = APIRouter(prefix="/api/v1/device-clients")
        self.router.add_api_route("", self.prepare, methods=["POST"])
        self.router.add_api_route("/{client_id}", self.status, methods=["GET"])

    async def prepare(self, request: Request):
        return await self.registry.prepare(await self.guards.owner_http(request), await body(request, DeviceClient, max_bytes=2048))

    async def status(self, client_id: UUID, request: Request):
        return await self.registry.status(await self.guards.owner_http(request), client_id, request.headers.get("X-Nexus-Device-Proof"))
