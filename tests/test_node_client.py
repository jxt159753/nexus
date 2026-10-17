import asyncio
import secrets
import ssl
from uuid import uuid4

import pytest
from pydantic import SecretStr
from websockets.asyncio.server import serve

from nexus.contracts.errors import ErrorCode
from nexus.contracts.messages import CommandStatus, LinkLimits, NodeHeartbeat, NodeWelcome, ResultAck
from nexus.contracts.protocol import parse_message
from nexus.link import wire_message
from nexus.node import link as client_module
from nexus.node.link import NodeClient
from nexus.node.runtime import NodeRuntime
from nexus.node.service_config import NodeServiceConfig
from node_support import command, configuration


def welcome(session=None, *, max_result_bytes=32768):
    return NodeWelcome(session_id=session or uuid4(), heartbeat_interval_seconds=1, heartbeat_timeout_seconds=2,
        limits=LinkLimits(max_message_bytes=65536, max_result_bytes=max_result_bytes,
                          execution_timeout_seconds=1, max_concurrent_attempts=1))


async def next_non_heartbeat(ws):
    async with asyncio.timeout(5):
        while True:
            message = parse_message(await ws.recv())
            if message.type != "node.heartbeat":
                return message


def test_cached_status_rebind_ack_and_disabled_network_execution(tmp_path):
    async def scenario():
        cfg = configuration(tmp_path)
        token = SecretStr(secrets.token_urlsafe(32))
        stop = asyncio.Event()
        finished = asyncio.get_running_loop().create_future()
        with NodeRuntime(cfg) as runtime:
            item = command(cfg)
            original = runtime.execute(item)
            async def handler(ws):
                try:
                    assert ws.request.headers["Authorization"] == "Bearer " + token.get_secret_value()
                    hello = parse_message(await ws.recv())
                    assert hello.type == "node.hello"
                    assert set(hello.payload.model_dump()) == {"node_id", "node_version", "tools", "roots", "boot_id"}
                    session = welcome()
                    await ws.send(wire_message("node.welcome", session))
                    await ws.send(wire_message("command.status", CommandStatus(session_id=session.session_id, attempt_id=item.attempt_id)))
                    response = await next_non_heartbeat(ws)
                    assert response.type == "command.status_result"
                    assert response.payload.result.session_id == session.session_id
                    assert response.payload.result.observed_at == original.result.observed_at
                    await ws.send(wire_message("result.ack", ResultAck(session_id=session.session_id, attempt_id=item.attempt_id, status="stored")))
                    denied = command(cfg).model_copy(update={"session_id": session.session_id})
                    await ws.send(wire_message("command.execute", denied))
                    response = await next_non_heartbeat(ws)
                    assert response.type == "error" and response.payload.code == ErrorCode.POLICY_DENIED
                    assert runtime.receipts.get(denied.attempt_id) is None
                    assert runtime.receipts.get(item.attempt_id).acknowledged
                    finished.set_result(True)
                except BaseException as exc:
                    if not finished.done():
                        finished.set_exception(exc)
                finally:
                    stop.set()
            async with serve(handler, "127.0.0.1", 0, compression=None) as server:
                port = server.sockets[0].getsockname()[1]
                service = NodeServiceConfig(**cfg.model_dump(), core_url=f"ws://127.0.0.1:{port}/ws/v1/node", local_http=True)
                client = NodeClient(service, runtime, token)
                async with asyncio.timeout(10):
                    await client.run(stop)
                    assert await finished
    asyncio.run(scenario())


@pytest.mark.parametrize("kind", ["wrong_welcome", "wrong_session", "frame_limit"])
def test_core_protocol_errors_close_connection_without_execution(tmp_path, kind):
    async def scenario():
        cfg = configuration(tmp_path)
        stop = asyncio.Event()
        completed = asyncio.get_running_loop().create_future()
        with NodeRuntime(cfg) as runtime:
            runtime._worker.run = lambda *args: pytest.fail("network must not execute a tool in S3")
            async def handler(ws):
                try:
                    await ws.recv()
                    if kind == "wrong_welcome":
                        raw = wire_message("node.heartbeat", NodeHeartbeat(session_id=uuid4(), active_attempt_id=None))
                    else:
                        w = welcome()
                        if kind == "frame_limit":
                            w.limits.max_message_bytes = 4096
                            w.limits.max_result_bytes = 128
                        await ws.send(wire_message("node.welcome", w))
                        raw = wire_message("command.status", CommandStatus(session_id=uuid4() if kind == "wrong_session" else w.session_id, attempt_id=uuid4()))
                        if kind == "frame_limit":
                            raw += " " * 4096
                    await ws.send(raw)
                    await ws.wait_closed()
                    completed.set_result(True)
                except BaseException as exc:
                    if not completed.done():
                        completed.set_exception(exc)
                finally:
                    stop.set()
            async with serve(handler, "127.0.0.1", 0) as server:
                service = NodeServiceConfig(**cfg.model_dump(), core_url=f"ws://127.0.0.1:{server.sockets[0].getsockname()[1]}/ws/v1/node", local_http=True)
                async with asyncio.timeout(10):
                    await NodeClient(service, runtime, SecretStr(secrets.token_urlsafe(32))).run(stop)
                    assert await completed
    asyncio.run(scenario())


def test_tls_verification_redirect_refusal_and_interruptible_backoff(tmp_path, monkeypatch):
    async def scenario():
        cfg = configuration(tmp_path)
        stop = asyncio.Event()
        seen = []
        class FailedConnection:
            async def __aenter__(self):
                raise OSError("controlled offline Core")
            async def __aexit__(self, *_):
                pass
        connection = FailedConnection()
        def configured_connect(uri, **kwargs):
            assert uri == "wss://core.test/ws/v1/node"
            assert kwargs["ssl"].verify_mode == ssl.CERT_REQUIRED
            assert kwargs["ssl"].check_hostname is True
            assert kwargs["proxy"] is None
            assert kwargs["max_size"] == 65536 and kwargs["max_queue"] == 16
            assert kwargs["compression"] is None
            assert kwargs["logger"].propagate is False
            seen.append(True)
            return connection
        monkeypatch.setattr(client_module, "connect", configured_connect)
        with NodeRuntime(cfg) as runtime:
            service = NodeServiceConfig(**cfg.model_dump(), core_url="wss://core.test/ws/v1/node")
            client = NodeClient(service, runtime, SecretStr(secrets.token_urlsafe(32)))
            running = asyncio.create_task(client.run(stop))
            await asyncio.sleep(0.1)
            assert len(seen) == 1 and client.state.phase == "offline"
            rejected = OSError("redirect")
            assert connection.process_redirect(rejected) is rejected
            stop.set()
            async with asyncio.timeout(1):
                await running
    asyncio.run(scenario())
