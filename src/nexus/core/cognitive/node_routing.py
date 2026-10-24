"""Owner-visible names and bounded cross-device catalogs; no extra model loop."""

import json
from collections import Counter
from uuid import UUID, uuid4

from pydantic import Field, StrictStr, field_validator
from nexus.contracts.base import StrictContract
from nexus.contracts.extensions import canonical, digest_value
from nexus.core.security.types import AccessDenied
from nexus.core.storage.tasks import Conflict
from nexus.core.storage.database import one, utc_now


class NodeName(StrictContract):
    name: StrictStr = Field(min_length=1, max_length=40)

    @field_validator("name")
    @classmethod
    def readable(cls, value):
        value = " ".join(value.split())
        if not value or any(ord(c) < 32 for c in value):
            raise ValueError("请输入设备名称")
        return value


async def names(conn, config):
    saved = {row[0]: row[1] for row in await conn.execute_fetchall("SELECT node_id,name FROM node_names")}
    kinds = {str(node): ("手机" if any(t.startswith("android.") for t in policy.tools) else "电脑")
             for node, policy in config.nodes.items()}
    totals, indexes = Counter(kinds.values()), Counter()
    result = {}
    used = {value.casefold() for value in saved.values()}
    for node, kind in sorted(kinds.items()):
        indexes[kind] += 1
        if node in saved:
            result[node] = saved[node]
            continue
        label = "我的"+kind if totals[kind] == 1 else f"{kind} {indexes[kind]}"
        suffix = 1
        while label.casefold() in used:
            suffix += 1
            label = f"{kind} {indexes[kind]} ({suffix})"
        result[node] = label
        used.add(label.casefold())
    return result


async def directory(chat, conn, principal):
    from nexus.core.cognitive import work_catalog
    await chat.owner(conn, principal)
    config = chat.harness.link.config
    if len(config.nodes) > 16:
        raise AccessDenied("MODEL_INPUT_LIMIT")
    labels = await names(conn, config)
    entries, devices, shared = [], [], set()
    for index, node_id in enumerate(sorted(config.nodes, key=str)):
        if not await one(conn, "SELECT 1 FROM identities WHERE identity_id=? AND role='node' AND revoked_at IS NULL", (str(node_id),)):
            continue
        current = await work_catalog.catalog(chat, conn, principal, node_id)
        state = await chat.harness.link.node_status(node_id)
        device = {"node_id": str(node_id), "name": labels[str(node_id)], "online": state['online']}
        devices.append(device)
        scope = chat.harness.scope(node_id)
        plugin_ready = 'plugin.call@1' in scope.tools & scope.local_tools and 'workspace' in scope.roots & scope.local_roots
        if plugin_ready:
            for identity in (str(principal.identity_id),str(node_id)):
                if not await one(conn,"SELECT 1 FROM grants WHERE identity_id=? AND tool='plugin.call' AND tool_version=1 AND root_id='workspace'",(identity,)):
                    plugin_ready = False
        versions = [item for item in current['catalog'] if 'version_id' in item]
        # Keep pinned plugin versions for execution; these are not function schemas.
        for item in current['catalog']:
            if item.get('plugin_id') and item['plugin_id'] not in shared:
                entries.append(item)
                shared.add(item['plugin_id'])
        for function in work_catalog.functions(current['catalog']):
            entry = dict(function['function'])
            original = entry['name']
            if original == 'plugin_execute' and not plugin_ready:
                continue
            if original == 'skill_create' and not ({'system.info@1','filesystem.list@1','plugin.call@1'} & scope.tools & scope.local_tools):
                # Current Skill profiles cannot execute Android-only tools.
                continue
            # These run entirely in Core and have no device-specific inputs.
            common = original in {'get_current_time','web_search','web_fetch','plugin_install'}
            if common and original in shared:
                continue
            if common:
                shared.add(original)
            entry.update(name=f"device{index}__{original}", route_node_id=str(node_id), route_intent=original, route_label=device['name'])
            entry['description'] = ("Core 共用能力。" if common else f"设备：{device['name']}。")+entry['description']
            if original in {'run_skill', 'run_tool', 'inspect_capability'}:
                entry['route_versions'] = versions
            entries.append(entry)
    # Online observations do not invalidate an already previewed permission scope.
    return {"catalog": entries, "nodes": devices,
            "work_digest": digest_value({"catalog": entries})}


async def prepare(work, conn, principal, conversation_id, value):
    from nexus.core.cognitive import work_catalog
    current = await directory(work.chat, conn, principal)
    if current['work_digest'] != value.work_digest:
        raise Conflict("IDEMPOTENCY_CONFLICT")
    if not current['nodes']:
        raise AccessDenied("POLICY_DENIED")
    parent = None
    for device in current['nodes']:
        node_id = UUID(device['node_id'])
        local = await work_catalog.catalog(work.chat, conn, principal, node_id)
        scoped = value.model_copy(update={"all_nodes": False, "node_id": node_id,
                                         "request_id": uuid4(), "work_digest": local['work_digest']})
        _, grant_id = await work.prepare(conn, principal, conversation_id, scoped)
        if parent is None:
            parent = grant_id
        else:
            await conn.execute("UPDATE chat_work_grants SET parent_grant_id=? WHERE grant_id=?", (parent, grant_id))
    if value.client_node_id is not None:
        for entry in current['catalog']:
            if 'route_node_id' in entry:
                entry['client_device'] = entry['route_node_id'] == str(value.client_node_id)
    return current['catalog'], parent


async def validate_turn(chat, conn, row, now):
    from nexus.core.security.work import valid_grant
    grants = await conn.execute_fetchall("SELECT grant_id,node_id FROM chat_work_grants WHERE grant_id=? OR parent_grant_id=?",
                                         (row['work_grant_id'], row['work_grant_id']))
    if not grants:
        raise AccessDenied("WORK_PERMISSION_EXPIRED")
    for grant in grants:
        await valid_grant(conn, grant['grant_id'], now, owner_id=row['owner_id'],
                          scope=chat.harness.scope(UUID(grant['node_id'])))


async def bind_step(conn, row, proposal, now):
    if proposal['intent'] != 'on_node':
        if json.loads(row['request_json']).get('all_nodes'):
            raise AccessDenied("POLICY_DENIED")
        return proposal, None
    if not json.loads(row['request_json']).get('all_nodes'):
        raise AccessDenied("POLICY_DENIED")
    node_id = proposal['arguments']['node_id']
    grant = await one(conn, "SELECT grant_id FROM chat_work_grants WHERE node_id=? AND (grant_id=? OR parent_grant_id=?)",
                      (node_id, row['work_grant_id'], row['work_grant_id']))
    if grant is None:
        raise AccessDenied("POLICY_DENIED")
    return proposal['arguments']['action'], grant['grant_id']
