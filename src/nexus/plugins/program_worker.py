"""Host Python plugin/program carrier with an explicit JSON tool bridge."""

import contextlib
import io
import json
import sys
import types

from nexus.plugins.sdk import PluginContext


class BoundedText(io.TextIOBase):
    def __init__(self): self.text=''
    def write(self,value):
        self.text=(self.text+str(value))[-4096:]
        return len(value)


def main():
    raw=sys.stdin.buffer.readline(65537)
    if not raw or len(raw)>65536: return 2
    payload=json.loads(raw)
    channel=sys.stdout;counter=0
    def call(plugin_id,tool,input):
        nonlocal counter
        counter+=1
        if counter>20: raise RuntimeError('programmatic call limit exceeded')
        channel.write(json.dumps({'rpc':'call','id':counter,'plugin_id':plugin_id,'tool':tool,'input':input},ensure_ascii=False,separators=(',',':'))+'\n');channel.flush()
        line=sys.stdin.buffer.readline(65537)
        if not line or len(line)>65536: raise RuntimeError('tool bridge closed')
        reply=json.loads(line)
        if reply.get('id')!=counter: raise RuntimeError('tool bridge response mismatch')
        if reply.get('error'): raise RuntimeError(reply['error'])
        return reply['result']
    captured=BoundedText()
    try:
        context=PluginContext(payload['workspace'],call=call)
        sdk=types.ModuleType('nexus_plugin');sdk.PluginContext=PluginContext;sys.modules['nexus_plugin']=sdk
        namespace={'__name__':'nexus_plugin_host','input':payload.get('input',{}),'context':context,'call':call,'result':None}
        with contextlib.redirect_stdout(captured),contextlib.redirect_stderr(captured):
            exec(compile(payload['code'],'plugin.py','exec'),namespace)
            if payload.get('tool'): result=namespace['handle'](payload['tool'],payload['input'],context)
            else: result=namespace.get('result')
        if not isinstance(result,dict): raise ValueError('program/plugin must return a JSON object')
        result={'rpc':'result','data':result,'stdout':captured.text}
    except Exception as exc:
        result={'rpc':'result','error':type(exc).__name__+': '+str(exc)[:500],'stdout':captured.text}
    channel.write(json.dumps(result,ensure_ascii=False,separators=(',',':'),allow_nan=False)+'\n');channel.flush()
    return 0


if __name__=='__main__': raise SystemExit(main())
