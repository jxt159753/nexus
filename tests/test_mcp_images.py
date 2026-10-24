import base64
import io
import json
import random
from types import SimpleNamespace

import pytest

from nexus.plugins.mcp_images import MAX_MESSAGE_BYTES, extract
from nexus.plugins.protocols import receive


def image_bytes():
    from PIL import Image
    output=io.BytesIO()
    Image.frombytes('RGB',(256,256),random.Random(9).randbytes(256*256*3)).save(output,format='PNG')
    return output.getvalue()


def test_image_uses_artifact_bytes_without_expanding_result_envelope(tmp_path):
    raw=image_bytes()
    value={'result':{'content':[{'type':'text','text':'screen'},
        {'type':'image','mimeType':'image/png','data':base64.b64encode(raw).decode()}]}}
    wire=json.dumps({'id':3,'result':value}).encode()+b'\n'
    assert len(wire)>32768
    with pytest.raises(ValueError,match='message limit'):
        receive(SimpleNamespace(stdout=io.BytesIO(wire)),3,False)
    result=receive(SimpleNamespace(stdout=io.BytesIO(wire)),3,False,MAX_MESSAGE_BYTES)
    extract(result,tmp_path)
    assert (tmp_path/'mcp-image-1.png').read_bytes()==raw
    assert result['files'][0]['size_bytes']==len(raw)
    assert len(json.dumps(result))<2048
    assert result['result']['content'][0]['text']=='screen'


@pytest.mark.parametrize('kind',['invalid-base64','oversized'])
def test_bad_image_never_enters_artifact_envelope(tmp_path,kind):
    data='bad base64!' if kind=='invalid-base64' else base64.b64encode(b'x'*(4*1024*1024+1)).decode()
    with pytest.raises(ValueError):
        extract({'result':{'content':[{'type':'image','mimeType':'image/png','data':data}]}},tmp_path)
    assert not list(tmp_path.iterdir())
