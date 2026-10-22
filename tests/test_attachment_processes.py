"""Opaque uploads persist across a real independent Core restart, without Node."""

import base64
import json
import os
import socket
from uuid import uuid4

import httpx2
import pytest

from test_link_processes import spawn,stop,wait_for


@pytest.mark.skipif(os.name!="nt",reason="Windows process launcher")
def test_real_http_upload_download_idempotency_and_restart_without_models(world,tmp_path):
    with socket.socket() as reserved:
        reserved.bind(("127.0.0.1",0)); port=reserved.getsockname()[1]
    origin=f"http://127.0.0.1:{port}"
    config=tmp_path/"core.toml"
    config.write_text(f'''data_dir={json.dumps(str(world.db.path.parent))}
owner_id="{world.owner_id}"
host="127.0.0.1"
port={port}
local_http=true
origins=["{origin}"]
''',encoding="utf-8")
    process=None
    with (tmp_path/"core.log").open("w+b") as log,httpx2.Client(base_url=origin,timeout=3,trust_env=False) as client:
        try:
            process=spawn("nexus.core",config,log)
            wait_for(lambda:client.get("/api/v1/session").status_code==401)
            response=client.post("/api/v1/session",json={"token":world.owner_token.get_secret_value()},headers={"origin":origin})
            assert response.status_code==200
            auth={"origin":origin,"x-csrf-token":response.json()["csrf_token"]}
            conv=client.post("/api/v1/chat/conversations",json={"request_id":str(uuid4())},headers=auth).json()["conversation"]
            base=f"/api/v1/chat/conversations/{conv['conversation_id']}/attachments"
            metadata={"request_id":str(uuid4()),"filename":"opaque.docx"}
            raw=b"PK\x03\x04\x00\xffopaque-container"*150000
            assert len(raw)>2*1024*1024
            response=client.post(base,params=metadata,content=raw,headers=auth)
            assert response.status_code==200,response.text
            item=response.json()["attachment"]
            assert item["preview_kind"]=="unparsed"
            assert client.get(item["download_url"]).content==raw
            assert client.post(base,params=metadata,content=raw,headers=auth).json()["attachment"]==item
            assert client.post(base,params=metadata,content=b"changed",headers=auth).status_code==409
            second=client.post(base,params={"encoding":"base64"},json={"request_id":str(uuid4()),"filename":"text.txt","data_base64":base64.b64encode(b"a text attachment").decode()},headers=auth)
            assert second.status_code==200
            stop(process); process=spawn("nexus.core",config,log)
            wait_for(lambda:client.get(base).status_code==200)
            assert len(client.get(base).json()["attachments"])==2
            assert client.get(item["download_url"]).content==raw
            encoded=client.get(item["download_url"],params={"encoding":"base64"}).json()
            assert base64.b64decode(encoded["data_base64"])==raw
            assert client.get("/api/v1/models/usage").json()["calls"]==0
            assert client.get("/api/v1/nodes").json()["nodes"]==[]
        finally:
            stop(process)
