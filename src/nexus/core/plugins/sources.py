"""Immutable, digest-checked public GitHub packages; no arbitrary fetch URLs."""

import asyncio
import hashlib
import re
from urllib.request import Request,build_opener,ProxyHandler,HTTPRedirectHandler
from nexus.plugins.contracts import PluginRelease


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self,*args,**kwargs): return None


def fetch(repository,commit,path,sha256):
    if not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+',repository) or not re.fullmatch(r'[a-f0-9]{40}',commit):
        raise ValueError('GitHub source requires repository and immutable commit SHA')
    if not re.fullmatch(r'[A-Za-z0-9_./-]+\.json',path) or any(part in {'','..','.'} for part in path.split('/')):
        raise ValueError('invalid release JSON path')
    if not re.fullmatch(r'[a-f0-9]{64}',sha256): raise ValueError('package SHA-256 is required')
    url=f'https://raw.githubusercontent.com/{repository}/{commit}/{path}'
    with build_opener(ProxyHandler({}),NoRedirect()).open(Request(url,headers={'Accept':'application/json'}),timeout=8) as response:
        raw=response.read(32769)
    if len(raw)>32768 or hashlib.sha256(raw).hexdigest()!=sha256: raise ValueError('plugin package digest mismatch')
    return PluginRelease.model_validate_json(raw)


async def github(repository,commit,path,sha256):
    return await asyncio.to_thread(fetch,repository,commit,path,sha256)
