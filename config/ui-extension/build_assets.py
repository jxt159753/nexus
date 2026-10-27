"""Build the trusted sandbox HTML and mirror shared runtime assets to Windows."""
import base64
import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
WEB = ROOT / 'src/nexus/web/app'
WINDOWS = ROOT / 'apps/windows/ui'


def build():
    bootstrap = Path(__file__).with_name('extension-sandbox.js').read_text(encoding='utf-8')
    digest = base64.b64encode(hashlib.sha256(bootstrap.encode()).digest()).decode()
    policy = f"default-src 'none'; script-src 'sha256-{digest}'; worker-src blob:; connect-src 'none'; base-uri 'none'; form-action 'none'"
    html = f'<!doctype html><meta charset="utf-8"><meta http-equiv="Content-Security-Policy" content="{policy}"><title>NEXUS 插件隔离环境</title><script>{bootstrap}</script>\n'
    (WEB / 'extension-sandbox.html').write_text(html, encoding='utf-8', newline='\n')
    for name in ('extension-sandbox.html', 'extension-runtime.js', 'ui-extensions.js', 'gesture-input.js', 'gesture-worker.js'):
        if (WEB / name).exists():
            (WINDOWS / name).write_bytes((WEB / name).read_bytes())
    lock = json.loads(Path(__file__).with_name('mediapipe.lock.json').read_text(encoding='utf-8'))
    for name, digest in lock['files'].items():
        data = (WEB / 'vendor/mediapipe' / name).read_bytes()
        if hashlib.sha256(data).hexdigest() != digest:
            raise ValueError(f'MediaPipe asset digest mismatch: {name}')
        target = WINDOWS / 'vendor/mediapipe' / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)


if __name__ == '__main__':
    build()
