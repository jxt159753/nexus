"""Real Core/Windows Node file observation, restart and optional wall-clock soak.

NEXUS_AUTONOMY_SOAK_SECONDS=7200 runs the same assertions for two real hours.
No model provider, production endpoint or external notification is used.
"""

import asyncio
import hashlib
import json
import os
import socket
import time
from uuid import uuid4

import httpx2
import pytest

from test_link_processes import spawn, stop, wait_for


@pytest.mark.skipif(os.name != 'nt', reason='real Windows executor')
def test_autonomy_process_restart_and_sensor_soak(world, tmp_path):
    duration = int(os.environ.get('NEXUS_AUTONOMY_SOAK_SECONDS', '100'))
    assert 90 <= duration <= 10800
    async def grants():
        for identity in (world.owner_id, world.node_id):
            await world.security.set_grant(identity, 'filesystem.read', root_id='workspace')
    asyncio.run(grants())
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        port = sock.getsockname()[1]
    origin = f'http://127.0.0.1:{port}'
    root = tmp_path / 'workspace'; root.mkdir()
    observed = root / 'observed.txt'; observed.write_text('initial', encoding='utf-8')
    node_dir = tmp_path / 'node'; node_dir.mkdir()
    config = tmp_path / 'core.toml'
    config.write_text(f'''data_dir={json.dumps(str(world.db.path.parent))}
owner_id="{world.owner_id}"
host="127.0.0.1"
port={port}
local_http=true
origins=["{origin}"]
[plugins]
enabled=true
[nodes."{world.node_id}"]
tools=["filesystem.read@1"]
roots=["workspace"]
''', encoding='utf-8')
    node_config = tmp_path / 'node.toml'
    node_config.write_text(f'''node_id="{world.node_id}"
data_dir={json.dumps(str(node_dir))}
core_url="ws://127.0.0.1:{port}/ws/v1/node"
local_http=true
allowed_tools=["filesystem.read@1"]
[roots]
workspace={json.dumps(str(root))}
''', encoding='utf-8')
    core = node = None
    evidence = {'duration_requested': duration, 'core_pids': [], 'node_pids': [], 'samples': []}
    with (tmp_path / 'process.log').open('w+b') as log, httpx2.Client(base_url=origin, trust_env=False, timeout=4) as browser:
        def login():
            result = browser.post('/api/v1/session', json={'token': world.owner_token.get_secret_value()}, headers={'origin': origin})
            assert result.status_code == 200, result.text
            return {'origin': origin, 'x-csrf-token': result.json()['csrf_token']}
        def online():
            return browser.get('/api/v1/nodes').json()['nodes'][0]['online']
        def state():
            return browser.get('/api/v1/autonomy/watches').json()['watches'][0]
        try:
            core = spawn('nexus.core', config, log); evidence['core_pids'].append(core.pid)
            wait_for(lambda: browser.get('/api/v1/session').status_code == 401)
            auth = login()
            node = spawn('nexus.node', node_config, log, token=world.node_token.get_secret_value()); evidence['node_pids'].append(node.pid)
            wait_for(online)
            conv = browser.post('/api/v1/chat/conversations', json={'request_id': str(uuid4())}, headers=auth).json()['conversation']
            request = {'request_id': str(uuid4()), 'conversation_id': conv['conversation_id'], 'kind': 'file', 'node_id': str(world.node_id),
                       'target': 'observed.txt', 'interval_seconds': 30, 'max_polls': 500, 'duration_minutes': 240}
            created = browser.post('/api/v1/autonomy/watches', json=request, headers=auth)
            assert created.status_code == 200, created.text
            watch = created.json()
            assert browser.post('/api/v1/autonomy/watches', json=request, headers=auth).json()['watch_id'] == watch['watch_id']
            wait_for(lambda: state()['polls'] >= 1, timeout=20)
            assert browser.get('/api/v1/autonomy/events').json()['events'] == []
            started = time.monotonic(); evidence['started_at'] = time.time()
            observed.write_text('after initial observation', encoding='utf-8')
            restarted_core = restarted_node = False
            login_window = 0
            while time.monotonic() - started < duration:
                elapsed = time.monotonic() - started
                if int(elapsed // 1800) != login_window:
                    auth = login(); login_window = int(elapsed // 1800)
                if elapsed >= 35 and not restarted_core:
                    wait_for(lambda: len(browser.get('/api/v1/autonomy/events').json()['events']) >= 1)
                    stop(core); core = None
                    core = spawn('nexus.core', config, log); evidence['core_pids'].append(core.pid)
                    wait_for(lambda: browser.get('/api/v1/session').status_code in (200, 401))
                    auth = login(); wait_for(online)
                    assert state()['watch_id'] == watch['watch_id']
                    observed.write_text('after Core restart', encoding='utf-8')
                    restarted_core = True
                if elapsed >= 70 and not restarted_node:
                    stop(node); node = None
                    node = spawn('nexus.node', node_config, log, token=world.node_token.get_secret_value()); evidence['node_pids'].append(node.pid)
                    wait_for(online)
                    observed.write_text('after Node restart', encoding='utf-8')
                    restarted_node = True
                health = browser.get('/api/v1/autonomy/health').json()
                assert health['status'] == 'ok', health
                assert not state()['paused']
                assert core.poll() is None and node.poll() is None
                evidence['samples'].append({'elapsed': round(elapsed, 2), 'polls': state()['polls'], 'health': health['status']})
                (tmp_path / 'soak-evidence.json').write_text(json.dumps(evidence, ensure_ascii=False, indent=2), encoding='utf-8')
                time.sleep(min(15, max(.1, duration - elapsed)))
            observed.write_text('final effect after sustained run', encoding='utf-8')
            digest = hashlib.sha256(observed.read_bytes()).hexdigest()
            final = wait_for(lambda: next((e for e in browser.get('/api/v1/autonomy/events').json()['events'] if json.loads(e['value_json']).get('sha256') == digest), None), timeout=45)
            events = browser.get('/api/v1/autonomy/events').json()['events']
            assert len({e['source_key'] for e in events}) == len(events)
            assert len(events) >= 3
            assert all(json.loads(e['value_json'])['kind'] == 'file' for e in events)
            assert browser.get('/api/v1/models/usage').json()['calls'] == 0
            response = browser.request('DELETE', '/api/v1/autonomy/watches/' + watch['watch_id'], json={'expected_revision': state()['revision']}, headers=auth)
            assert response.status_code == 200, response.text
            evidence.update(elapsed_seconds=round(time.monotonic()-started, 2), events=events, final_event_id=final['event_id'], model_calls=0, result='passed')
        finally:
            stop(node); stop(core)
            (tmp_path / 'soak-evidence.json').write_text(json.dumps(evidence, ensure_ascii=False, indent=2), encoding='utf-8')
        log.seek(0)
        output = log.read().decode('utf-8', errors='replace')
        assert world.owner_token.get_secret_value() not in output and world.node_token.get_secret_value() not in output
