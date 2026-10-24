"""A dedicated Edge/Chrome CDP connection; fixed scripts and configured actions."""

import asyncio
import hashlib
import json
from urllib.parse import urlsplit

import httpx2
from websockets.asyncio.client import connect

from nexus.contracts.interaction import InteractionObservation, InteractionResult
from nexus.contracts.extensions import canonical
from nexus.node.tools.interaction_config import origin


SNAPSHOT = """(selectors)=>{const e=document.body;return {url:location.href,title:document.title.slice(0,200),text:(e?e.innerText:'').slice(0,2000),truncated:(e?e.innerText.length:0)>2000,controls:selectors.map(selector=>{const all=document.querySelectorAll(selector),n=all[0];return {selector,count:all.length,tag:n?.tagName,type:n?.type,disabled:!!n?.disabled,text:(n?.innerText||'').slice(0,120),value:n&&!n.matches('input[type=password],input[type=file]')?String(n.value||'').slice(0,256):null};})}}"""
# selector/value are JSON values in a fixed function, never executable source.
ACTION = """(selector,kind,value,allowed)=>{if(!allowed.includes(location.origin))throw Error('origin changed');const all=document.querySelectorAll(selector);if(all.length!==1)throw Error('unique target required');const e=all[0];if(!e.isConnected||e.disabled||e.getClientRects().length===0)throw Error('target unavailable');if(e.matches('input[type=password],input[type=file]'))throw Error('sensitive target');if(kind==='click'){e.click();return true;}if(kind==='fill' && e.matches('input,textarea')){const proto=e instanceof HTMLTextAreaElement?HTMLTextAreaElement.prototype:HTMLInputElement.prototype;Object.getOwnPropertyDescriptor(proto,'value').set.call(e,value);e.dispatchEvent(new Event('input',{bubbles:true}));e.dispatchEvent(new Event('change',{bubbles:true}));return true;}throw Error('unsupported target');}"""


class CDP:
    def __init__(self, socket):
        self.socket=socket
        self.counter=0

    async def call(self, method, **params):
        self.counter+=1
        identifier=self.counter
        await self.socket.send(canonical({"id":identifier,"method":method,"params":params}))
        async with asyncio.timeout(2):
            for _ in range(200):
                message=json.loads(await self.socket.recv())
                if message.get("id")==identifier:
                    if "error" in message:
                        raise ValueError("browser protocol operation failed")
                    return message.get("result",{})
        raise ValueError("browser event limit")

    async def evaluate(self, expression):
        value=await self.call("Runtime.evaluate",expression=expression,returnByValue=True,timeout=1000)
        if "exceptionDetails" in value:
            raise ValueError("browser script rejected target")
        return value["result"].get("value")


async def snapshot(cdp, template):
    selectors=list(dict.fromkeys(item.selector for item in template.actions.values() if item.selector))
    value=await cdp.evaluate("("+SNAPSHOT+")("+json.dumps(selectors)+")")
    if not isinstance(value,dict) or origin(value["url"]) not in template.allowed_origins:
        raise PermissionError("browser outside configured origins")
    body=canonical(value)
    if len(body.encode())>18000:
        raise ValueError("browser observation exceeds byte limit")
    return InteractionObservation(kind=template.kind,state_json=body,state_sha256=hashlib.sha256(body.encode()).hexdigest(),
        actions=[{"action_id":key,"kind":item.kind,"description":item.description} for key,item in template.actions.items()])


async def operate(args, template):
    if template.digest()!=args.template_digest:
        raise PermissionError("interaction policy changed")
    async with httpx2.AsyncClient(trust_env=False,timeout=2) as client:
        async with client.stream("GET",template.endpoint.rstrip("/")+"/json/list") as response:
            response.raise_for_status()
            raw=bytearray()
            async for part in response.aiter_bytes():
                if len(raw)+len(part)>65536:
                    raise ValueError("browser target list too large")
                raw.extend(part)
        targets=json.loads(raw)
        if not isinstance(targets,list) or len(targets)>32:
            raise ValueError("browser target count limit")
    eligible=[]
    for item in targets[:32]:
        try:
            if item.get("type")=="page" and origin(item.get("url","")) in template.allowed_origins:
                eligible.append(item)
        except ValueError:
            continue
    if len(eligible)!=1:
        raise ValueError("dedicated browser must have exactly one configured page")
    target=eligible[0]
    # Never trust an arbitrary endpoint returned in /json/list.
    identifier=target["id"]
    if not isinstance(identifier,str) or not identifier or len(identifier)>128 or any(c not in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_" for c in identifier):
        raise ValueError("invalid browser target")
    parsed=urlsplit(template.endpoint)
    socket_url=f"ws://127.0.0.1:{parsed.port}/devtools/page/{identifier}"
    async with connect(socket_url,proxy=None,open_timeout=2,close_timeout=.5,max_size=65536) as socket:
        cdp=CDP(socket)
        before=await snapshot(cdp,template)
        action_id=getattr(args,"action_id",None)
        if action_id is not None:
            if before.state_sha256!=args.expected_state_sha256:
                raise PermissionError("browser state changed; observe again")
            action=template.actions.get(action_id)
            if action is None or (action.kind=="fill")!=(args.value is not None):
                raise PermissionError("unconfigured browser action or value")
            if action.kind=="navigate":
                response=await cdp.call("Page.navigate",url=action.url)
                if response.get("errorText"):
                    raise ValueError("navigation failed")
            else:
                # Run the fixed function in the main document; no arbitrary model JS.
                result=await cdp.evaluate("("+ACTION+")("+",".join(json.dumps(v,ensure_ascii=False) for v in (action.selector,action.kind,args.value,template.allowed_origins))+")")
                if result is not True:
                    raise ValueError("action did not return evidence")
            await asyncio.sleep(.1)
        after=await snapshot(cdp,template) if action_id else before
    return InteractionResult(root_id=args.root_id,target_id=args.target_id,template_digest=template.digest(),observation=after,
        action_id=action_id,previous_state_sha256=before.state_sha256 if action_id else None,
        verification="configured_action_returned" if action_id else "configured_target_observed")


def browser_operation(args, template):
    return asyncio.run(operate(args,template))
