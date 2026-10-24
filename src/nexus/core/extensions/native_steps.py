"""Pinned Shell/MCP steps in the existing Skill executor and task journal."""

import json
from uuid import UUID

from nexus.contracts.extensions import canonical, NATIVE_SKILL_PROFILE
from nexus.core.security.modes import effective
from nexus.core.security.types import AccessDenied
from nexus.core.storage.database import one
from nexus.plugins.catalog import RELEASES, definition
from nexus.plugins.contracts import PluginNodeArguments, PluginNodeResult
from nexus.plugins.schema import checked


def native(version):
    return json.loads(version['manifest_json'])['verification_profile']==NATIVE_SKILL_PROFILE


def contract(step):
    if step['plugin_id'] not in {'builtin.shell','builtin.mcp'}: raise ValueError('unsupported native Skill plugin')
    release=RELEASES[step['plugin_id']]
    if release.digest()!=step['plugin_digest']: raise ValueError('native Skill plugin changed')
    try: return definition(step['plugin_id'],step['plugin_tool'])
    except StopIteration: raise ValueError('native Skill tool unavailable') from None


async def dependency(conn,owner,step):
    tool=contract(step)
    row=await one(conn,'SELECT * FROM plugin_versions WHERE owner_id=? AND plugin_id=? AND digest=? AND builtin=1 AND enabled=1 AND approved=1',
        (str(owner),step['plugin_id'],step['plugin_digest']))
    if row is None: raise AccessDenied('PLUGIN_VERSION_UNAVAILABLE')
    return tool


async def context(conn,run):
    if not run['permission_context_id']: return None
    row=await one(conn,'SELECT owner_id,archived,plan_mode FROM conversations WHERE conversation_id=?',(run['permission_context_id'],))
    if row is None or row['owner_id']!=run['owner_id'] or row['archived'] or row['plan_mode']: raise AccessDenied('WORK_STOPPED')
    mode=await effective(conn,run['permission_context_id'])
    if mode['fingerprint']!=run['permission_fingerprint']: raise AccessDenied('PERMISSION_MODE_CHANGED')
    return mode


async def task_mode(conn,task):
    row=await one(conn,'SELECT r.* FROM skill_runs r JOIN skill_run_steps s USING(run_id) WHERE s.task_id=?',(str(task.task_id),))
    if row is None or row['permission_context_id'] is None: return None
    mode=await context(conn,row)
    return mode['effective_mode']


async def arguments(conn,run,step,value,invocation_id):
    await dependency(conn,run['owner_id'],step)
    await context(conn,run)
    preflight(step,value)
    bound=None
    if step['plugin_id']=='builtin.mcp':
        from nexus.core.plugins.mcp import prepare
        bound=await prepare(conn,run['owner_id'],run['node_id'],run['permission_context_id'],invocation_id,step['plugin_tool'],value)
    return PluginNodeArguments(plugin_id=step['plugin_id'],tool=step['plugin_tool'],input=value,
        release_digest=step['plugin_digest'],context_id=UUID(run['permission_context_id']),invocation_id=invocation_id,mcp_binding=bound,max_subcalls=0)


def preflight(step,value):
    checked(contract(step).input_schema,value)
    if step['plugin_id']=='builtin.shell' and (value.get('persistent') or value.get('run_in_background')):
        raise ValueError('native Skill Shell must complete before advancing')
    if step['plugin_id']=='builtin.mcp' and step['plugin_tool']=='mcp_register':
        from nexus.plugins.mcp_contracts import MCPConfiguration
        MCPConfiguration.model_validate_json(canonical(value))


def standin(step,value):
    preflight(step,value)
    data=({'status':'succeeded','exit_code':0,'output':'contract-test-standin','execution_stopped':True}
          if step['plugin_id']=='builtin.shell' else {'tools':[],'catalog_digest':'0'*64,'verification':'contract_test_standin'})
    return PluginNodeResult(release_digest=step['plugin_digest'],tool=step['plugin_tool'],data=data).model_dump(mode='json')


def successful(result):
    value=json.loads(result);data=value['data']
    if value['tool'] in {'pwsh','bash'}:
        return (data.get('status')=='succeeded' and type(data.get('exit_code')) is int and data['exit_code']==0
                and data.get('execution_stopped') is True)
    if data.get('status') not in {None,'succeeded'}: return False
    if data.get('result',{}).get('isError'): return False
    return True
