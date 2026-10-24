"""Real Edge CDP evidence in a disposable headless browser profile."""

import http.server
import os
from pathlib import Path
import socket
import threading
import time
from uuid import uuid4

import httpx2
import pytest
from nexus.node.config import NodeConfig
from nexus.node.tools.interaction_config import BrowserTarget
from nexus.plugins.config import PluginNodeConfig
from nexus.plugins.node_runtime import PluginRuntime,HostProcess


@pytest.mark.skipif(os.name!='nt',reason='real Windows Edge')
def test_native_browser_observe_act_extract_navigate_and_image(tmp_path):
    executable=Path('C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe')
    if not executable.is_file(): pytest.skip('Edge not installed')
    class Page(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            raw=b'<html><title>Plugin fixture</title><input id="name"><button id="save" onclick="document.getElementById(\'output\').innerText=document.getElementById(\'name\').value">Save</button><div id="output">ready</div></html>'
            self.send_response(200);self.send_header('Content-Type','text/html; charset=utf-8');self.end_headers();self.wfile.write(raw)
        def log_message(self,*args): pass
    server=http.server.ThreadingHTTPServer(('127.0.0.1',0),Page)
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    origin=f'http://127.0.0.1:{server.server_port}'
    with socket.socket() as port:
        port.bind(('127.0.0.1',0));debug=port.getsockname()[1]
    browser=HostProcess([str(executable),'--headless=new',f'--remote-debugging-port={debug}',f'--user-data-dir={tmp_path / "edge"}',
        '--no-first-run','--no-default-browser-check','--disable-gpu','--disable-extensions','--disable-background-networking',origin],tmp_path,'Local\\NEXUS-Plugin-Test-'+uuid4().hex)
    runtime=None
    try:
        with httpx2.Client(trust_env=False,timeout=.5) as client:
            deadline=time.monotonic()+15
            while True:
                try:
                    pages=client.get(f'http://127.0.0.1:{debug}/json/list').json()
                    if any(page.get('url','').startswith(origin) for page in pages): break
                except (httpx2.HTTPError,ValueError): pass
                if time.monotonic()>deadline: raise AssertionError('owned Edge did not start')
                time.sleep(.1)
        runtime=PluginRuntime(NodeConfig(node_id=uuid4(),data_dir=tmp_path,roots={'workspace':tmp_path},plugins=PluginNodeConfig(enabled=True),
            interaction_targets={'edge':BrowserTarget(kind='edge',endpoint=f'http://127.0.0.1:{debug}',allowed_origins=[origin])}))
        context=str(uuid4())
        def call(name,**value): return runtime.call('builtin.browser',name,{'target_id':'edge',**value},context,time.monotonic()+10,threading.Event())
        assert call('browser_tabs')['tabs']
        assert call('browser_observe')['title']=='Plugin fixture'
        call('browser_act',selector='#name',action='fill',value='actual browser evidence')
        call('browser_act',selector='#save',action='click')
        assert call('browser_extract',selector='#output')['text']=='actual browser evidence'
        image=call('browser_screenshot')
        assert image['image']['data_base64'] and image['viewport_cropped']
        assert call('browser_navigate',url=origin+'/second')['url'].endswith('/second')
    finally:
        if runtime: runtime.close()
        browser.stop();server.shutdown();server.server_close();thread.join(timeout=2)
