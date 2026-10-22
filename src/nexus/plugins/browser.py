"""Browser plugin over dedicated configured CDP targets; fixed parameterized JS."""

import asyncio
import base64
import json
import sys
from urllib.parse import urlsplit

import httpx2
from websockets.asyncio.client import connect

from nexus.node.tools.browser import CDP,ACTION
from nexus.node.tools.interaction_config import origin,BrowserTarget
from nexus.media.images import normalize_image

OBSERVE="""()=>({url:location.href,title:document.title.slice(0,200),text:(document.body?.innerText||'').slice(0,4000),controls:[...document.querySelectorAll('a,button,input,textarea,select,[role=button]')].slice(0,30).map(e=>({tag:e.tagName,id:e.id,type:e.type,text:(e.innerText||e.getAttribute('aria-label')||'').slice(0,120),name:e.getAttribute('name'),disabled:!!e.disabled}))})"""
EXTRACT="""(selector)=>{const a=selector?document.querySelectorAll(selector):[document.body];if(a.length!==1)throw Error('unique selector required');const text=a[0]?.innerText||'';return {url:location.href,title:document.title.slice(0,200),text:text.slice(0,8192),truncated:text.length>8192}}"""


async def operate(template,name,value):
    async with httpx2.AsyncClient(trust_env=False,timeout=2) as client:
        async with client.stream('GET',template.endpoint.rstrip('/')+'/json/list') as response:
            response.raise_for_status();raw=bytearray()
            async for block in response.aiter_bytes():
                if len(raw)+len(block)>65536: raise ValueError('browser target list limit')
                raw.extend(block)
    targets=json.loads(raw)
    if not isinstance(targets,list) or len(targets)>32: raise ValueError('browser page count limit')
    eligible=[]
    for item in targets:
        try:
            if item.get('type')=='page' and origin(item.get('url','')) in template.allowed_origins: eligible.append(item)
        except ValueError: pass
    if name=='browser_tabs': return {'tabs':[{'page_id':item['id'],'title':item.get('title','')[:200],'url':item['url']} for item in eligible]}
    if value.get('page_id'): eligible=[item for item in eligible if item['id']==value['page_id']]
    if len(eligible)!=1: raise ValueError('select exactly one configured browser page with page_id')
    identifier=eligible[0]['id']
    if not isinstance(identifier,str) or not identifier or len(identifier)>128 or any(c not in 'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_' for c in identifier): raise ValueError('invalid page identifier')
    port=urlsplit(template.endpoint).port
    async with connect(f'ws://127.0.0.1:{port}/devtools/page/{identifier}',proxy=None,open_timeout=2,close_timeout=.5,max_size=262144) as socket:
        cdp=CDP(socket)
        current=await cdp.evaluate('location.href')
        if origin(current) not in template.allowed_origins: raise PermissionError('browser origin changed')
        if name=='browser_navigate':
            if origin(value['url']) not in template.allowed_origins: raise PermissionError('navigation origin not configured')
            result=await cdp.call('Page.navigate',url=value['url'])
            if result.get('errorText'): raise ValueError('navigation failed')
            await asyncio.sleep(.1)
        if name=='browser_act':
            if value['action']=='press':
                if value.get('value') not in {'Enter','Tab','Escape','Backspace','ArrowDown','ArrowUp'}: raise ValueError('unsupported named key')
                result=await cdp.evaluate('(s=>{const e=document.querySelectorAll(s);if(e.length!==1||e[0].matches("input[type=password],input[type=file]"))throw Error("target unavailable");e[0].focus();return true})('+json.dumps(value['selector'])+')')
                await cdp.call('Input.dispatchKeyEvent',type='keyDown',key=value['value'])
                await cdp.call('Input.dispatchKeyEvent',type='keyUp',key=value['value'])
            else:
                result=await cdp.evaluate('('+ACTION+')('+','.join(json.dumps(item,ensure_ascii=False) for item in (value['selector'],value['action'],value.get('value'),template.allowed_origins))+')')
            if result is not True: raise ValueError('browser action did not return evidence')
            await asyncio.sleep(.1)
        if name=='browser_extract':
            result=await cdp.evaluate('('+EXTRACT+')('+json.dumps(value.get('selector'))+')')
        elif name=='browser_screenshot':
            view=(await cdp.call('Page.getLayoutMetrics')).get('cssLayoutViewport',{})
            shot=await cdp.call('Page.captureScreenshot',format='jpeg',quality=30,captureBeyondViewport=False,
                clip={'x':view.get('pageX',0),'y':view.get('pageY',0),'width':min(1024,view.get('clientWidth',1024)),
                      'height':min(768,view.get('clientHeight',768)),'scale':1})
            result=normalize_image(base64.b64decode(shot['data'],validate=True)).model_dump(mode='json')
            result.update(path='browser/'+identifier,url=current,viewport_cropped=True)
        else: result=await cdp.evaluate('('+OBSERVE+')()')
        after=await cdp.evaluate('location.href')
        if origin(after) not in template.allowed_origins: raise PermissionError('browser left configured origins')
        result['verification']='browser_observation' if name in {'browser_observe','browser_extract','browser_screenshot'} else 'browser_action_returned'
        return result


def execute(runtime,name,value,context,deadline,cancel):
    target=runtime.config.interaction_targets.get(value['target_id'])
    if not isinstance(target,BrowserTarget): raise ValueError('browser target is not configured')
    argv=([sys.executable,'--plugin-browser'] if getattr(sys,'frozen',False) else [sys.executable,'-B','-m','nexus.plugins.browser'])
    return runtime.json_process(argv,{'template':target.model_dump(mode='json'),'name':name,'input':value},context,deadline,cancel)


def main():
    try:
        raw=sys.stdin.buffer.readline(65537)
        if not raw or len(raw)>65536: return 2
        value=json.loads(raw)
        result=asyncio.run(operate(BrowserTarget.model_validate(value['template']),value['name'],value['input']))
    except Exception as exc: result={'worker_error':str(exc)[:500]}
    sys.stdout.write(json.dumps(result,ensure_ascii=False,separators=(',',':')))
    return 0


if __name__=='__main__': raise SystemExit(main())
