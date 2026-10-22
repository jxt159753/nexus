"""Bounded node-tool workflows and programmatic invocation under one task."""

import json
import codecs
import sys
import time
from uuid import uuid4

from nexus.contracts.extensions import canonical
from nexus.node.process import ExecutionStopped
from nexus.plugins.catalog import RELEASES,definition
from nexus.plugins.schema import checked,bounded


def nested(runtime,plugin_id,name,value,context,deadline,cancel,records):
    release=RELEASES.get(plugin_id)
    allowed=getattr(runtime.invocation,'capabilities',{})
    if release is None or release.manifest.runtime!='node' or plugin_id=='builtin.workflow' or allowed.get(plugin_id)!=release.digest():
        raise PermissionError('programmatic tool is outside the approved invocation catalog')
    if len(records)>=getattr(runtime.invocation,'max_subcalls',0): raise ValueError('programmatic operation limit exceeded')
    contract=definition(plugin_id,name)
    checked(contract.input_schema,value)
    identifier=uuid4().hex
    with runtime.db() as conn:
        if conn.execute('SELECT count(*) FROM subcalls').fetchone()[0]>=10000: raise ValueError('programmatic journal full')
        conn.execute('INSERT INTO subcalls VALUES(?,?,?,?,?,NULL)',(identifier,context,plugin_id,name,canonical(value)))
    result=runtime.call(plugin_id,name,value,context,deadline,cancel,release)
    checked(contract.output_schema,result)
    with runtime.db() as conn: conn.execute('UPDATE subcalls SET result_json=? WHERE call_id=?',(canonical(result),identifier))
    records.append({'call_id':identifier,'plugin_id':plugin_id,'tool':name,'result':result})
    if result.get('status') in {'failed','unknown','timed_out'} or result.get('exit_code',0) not in (None,0):
        raise ValueError('nested tool did not complete successfully')
    return result


def program(runtime,code,input,context,deadline,cancel,*,tool=None):
    argv=([sys.executable,'--plugin-program'] if getattr(sys,'frozen',False) else [sys.executable,'-B','-m','nexus.plugins.program_worker'])
    with runtime.lock: process=runtime.start(context,'program',argv,runtime.cwd(context))
    records=[];pending='';answer=None;decoder=codecs.getincrementaldecoder('utf-8')()
    try:
        process.send(canonical({'code':code,'input':input,'workspace':str(runtime.cwd(context)),'tool':tool})+'\n')
        while process.process.poll() is None or answer is None:
            if cancel.is_set(): raise ExecutionStopped('cancelled')
            if time.monotonic()>=deadline: raise ExecutionStopped('timeout')
            piece=process.take_bytes(8192)
            pending+=decoder.decode(piece['bytes'])
            if len(pending.encode())>32768 or piece['dropped']: raise ValueError('program bridge output limit exceeded')
            while '\n' in pending:
                line,pending=pending.split('\n',1)
                message=json.loads(line)
                if message.get('rpc')=='call':
                    try:
                        result=nested(runtime,message['plugin_id'],message['tool'],message['input'],context,deadline,cancel,records)
                        reply={'id':message['id'],'result':result}
                    except (ValueError,PermissionError,KeyError,StopIteration) as exc:
                        reply={'id':message['id'],'error':str(exc)[:500]}
                    process.send(canonical(reply)+'\n')
                elif message.get('rpc')=='result':
                    answer=message
                else: raise ValueError('invalid program bridge message')
            if answer is not None: break
            if piece['exit_code'] is not None and not piece['truncated']: raise ValueError('program exited without a result')
            time.sleep(.01)
        if answer.get('error'): raise ValueError(answer['error'])
        result=bounded(answer['data'],20000)
        return {'result':result,'stdout':answer.get('stdout',''),'subcalls':[{key:row[key] for key in ('call_id','plugin_id','tool')} for row in records],'verification':'program_returned_object'}
    finally:
        with runtime.lock:
            process.stop();runtime.processes.pop(process.identifier,None)
            with runtime.db() as conn: conn.execute("UPDATE processes SET state='completed',result='{}' WHERE id=?",(process.identifier,))


def execute(runtime,name,value,context,deadline,cancel):
    if name=='run_code': return program(runtime,value['code'],value.get('input',{}),context,deadline,cancel)
    records=[]
    for step in value['steps']:
        nested(runtime,step['plugin_id'],step['tool'],step['input'],context,deadline,cancel,records)
    return bounded({'steps':records,'verification':'ordered_tool_results'},24576)
