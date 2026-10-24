"""The trusted Node parent uploads bytes; host workers never receive its token."""

import hashlib
import json
import ssl
from urllib.parse import urlsplit,urlunsplit
from urllib.request import Request,build_opener,ProxyHandler,HTTPSHandler,HTTPRedirectHandler


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self,*args,**kwargs): return None


def uploader(config,token):
    endpoint=urlsplit(config.core_url)
    context=ssl.create_default_context(cafile=str(config.ca_file) if config.ca_file else None)
    opener=build_opener(ProxyHandler({}),NoRedirect(),HTTPSHandler(context=context))
    base=urlunsplit(('https' if endpoint.scheme=='wss' else 'http',endpoint.netloc,'','',''))
    def upload(invocation,index,content):
        digest=hashlib.sha256(content).hexdigest()
        request=Request(f'{base}/api/v1/plugins/transfers/{invocation}/{index}',data=content,method='POST',
            headers={'Content-Type':'application/octet-stream','X-Content-SHA256':digest,'Authorization':'Bearer '+token.get_secret_value()})
        with opener.open(request,timeout=4) as response: raw=response.read(8193)
        if len(raw)>8192: raise ValueError('artifact response limit')
        value=json.loads(raw)
        if value['sha256']!=digest or value['size_bytes']!=len(content): raise ValueError('artifact readback mismatch')
        return value
    return upload
