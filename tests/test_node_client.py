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


def test_cached_status_rebind_ack_and_network_execution(tmp_path):
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
                    assert set(hello.payload.model_dump()) == {"node_id", "node_version", "tools", "roots", "boot_id", "command_digests", "interaction_digests"}
                    assert hello.payload.command_digests == {}
                    session = welcome()
                    await ws.send(wire_message("node.welcome", session))
                    await ws.send(wire_message("command.status", CommandStatus(session_id=session.session_id, attempt_id=item.attempt_id)))
                    response = await next_non_heartbeat(ws)
                    assert response.type == "command.status_result"
                    assert response.payload.result.session_id == session.session_id
                    assert response.payload.result.observed_at == original.result.observed_at
                    await ws.send(wire_message("result.ack", ResultAck(session_id=session.session_id, attempt_id=item.attempt_id, status="stored")))
                    dispatched = command(cfg).model_copy(update={"session_id": session.session_id})
                    await ws.send(wire_message("command.execute", dispatched))
                    for kind in ("command.accepted", "command.started", "command.result"):
                        response = await next_non_heartbeat(ws)
                        assert response.type == kind
                    assert response.payload.outcome == "succeeded"
                    assert runtime.receipts.get(dispatched.attempt_id).phase == "completed"
                    # Same immutable action with a new transport message is cached.
                    await ws.send(wire_message("command.execute", dispatched))
                    cached = await next_non_heartbeat(ws)
                    assert cached.type == "command.result"
                    assert cached.payload.observed_at == response.payload.observed_at
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
            runtime._worker.run = lambda *args: pytest.fail("invalid Core messages must not execute tools")
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


def test_heartbeat_and_status_remain_responsive_during_managed_execution(tmp_path, monkeypatch):
    from test_node_runtime import inject_blocker, assert_process_dead
    inject_blocker(monkeypatch, tmp_path / "worker.pid")
    async def scenario():
        cfg = configuration(tmp_path, execution_timeout_seconds=3)
        stop = asyncio.Event()
        finished = asyncio.get_running_loop().create_future()
        with NodeRuntime(cfg) as runtime:
            async def handler(ws):
                try:
                    await ws.recv()
                    session = welcome()
                    await ws.send(wire_message("node.welcome", session))
                    item = command(cfg, session_id=session.session_id)
                    await ws.send(wire_message("command.execute", item))
                    heartbeats = 0
                    queried = False
                    status_replies = 0
                    async with asyncio.timeout(8):
                        while True:
                            message = parse_message(await ws.recv())
                            if message.type == "node.heartbeat" and message.payload.active_attempt_id == item.attempt_id:
                                heartbeats += 1
                            elif message.type == "command.started" and not queried:
                                await ws.send(wire_message("command.status", CommandStatus(session_id=session.session_id, attempt_id=item.attempt_id)))
                                await ws.send(wire_message("command.execute", item))
                                queried = True
                            elif message.type == "command.status_result":
                                assert message.payload.phase == "started"
                                status_replies += 1
                            elif message.type == "command.result":
                                assert message.payload.error.code == ErrorCode.TOOL_TIMEOUT
                                assert heartbeats >= 2 and queried and status_replies == 2
                                break
                    assert_process_dead(int((tmp_path / "worker.pid").read_text()))
                    finished.set_result(True)
                except BaseException as exc:
                    if not finished.done():
                        finished.set_exception(exc)
                finally:
                    stop.set()
            async with serve(handler, "127.0.0.1", 0, compression=None) as server:
                service = NodeServiceConfig(**cfg.model_dump(), core_url=f"ws://127.0.0.1:{server.sockets[0].getsockname()[1]}/ws/v1/node", local_http=True)
                async with asyncio.timeout(12):
                    await NodeClient(service, runtime, SecretStr(secrets.token_urlsafe(32))).run(stop)
                    assert await finished
    asyncio.run(scenario())


def test_next_command_waits_for_result_sender_cleanup_without_false_busy(tmp_path):
    async def scenario():
        cfg = configuration(tmp_path)
        stop = asyncio.Event()
        finished = asyncio.get_running_loop().create_future()
        with NodeRuntime(cfg) as runtime:
            async def handler(ws):
                try:
                    await ws.recv()
                    session = welcome()
                    await ws.send(wire_message("node.welcome", session))
                    for _ in range(2):
                        item = command(cfg, session_id=session.session_id)
                        await ws.send(wire_message("command.execute", item))
                        while True:
                            incoming = await next_non_heartbeat(ws)
                            assert incoming.type != "error", "a completed worker must release the next execution slot"
                            if incoming.type == "command.result":
                                assert incoming.payload.attempt_id == item.attempt_id
                                assert incoming.payload.outcome == "succeeded"
                                break
                    finished.set_result(True)
                except BaseException as exc:
                    if not finished.done():
                        finished.set_exception(exc)
                finally:
                    stop.set()
            async with serve(handler, "127.0.0.1", 0, compression=None) as server:
                service = NodeServiceConfig(**cfg.model_dump(), core_url=f"ws://127.0.0.1:{server.sockets[0].getsockname()[1]}/ws/v1/node", local_http=True)
                client = NodeClient(service, runtime, SecretStr(secrets.token_urlsafe(32)))
                original_send = client._send
                async def delayed_result(*args, **kwargs):
                    await original_send(*args, **kwargs)
                    if args[1] == "command.result":
                        await asyncio.sleep(0.15)  # result arrives before sender bookkeeping returns
                client._send = delayed_result
                async with asyncio.timeout(10):
                    await client.run(stop)
                    assert await finished
    asyncio.run(scenario())
