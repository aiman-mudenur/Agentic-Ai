"""Protocol-level tests for the OpenClaw Gateway client.

These start a *local* in-process WebSocket server to exercise the real
handshake/RPC framing. No external network and no OpenClaw install is needed.
"""

import asyncio
import atexit
import json
import os
import shutil
import tempfile
from dataclasses import replace
from unittest.mock import patch

import pytest

websockets = pytest.importorskip("websockets")

from services.openclaw_client import (
    OpenClawClient,
    OpenClawError,
    OpenClawTimeout,
    chat_sync,
    get_health_sync,
)
from services.openclaw_config import CONFIG, OpenClawConfig

TMP_DIR = tempfile.mkdtemp(prefix="test_openclaw_client_")
atexit.register(shutil.rmtree, TMP_DIR, ignore_errors=True)


def _cfg(port: int, **overrides) -> OpenClawConfig:
    base = OpenClawConfig(
        enabled=True,
        gateway_url=f"ws://127.0.0.1:{port}",
        token="test-shared-token",
        role="operator",
        scopes=("operator.read", "operator.write"),
        client_id="course-organizer",
        client_version="1.0.0",
        min_protocol=4,
        max_protocol=4,
        connect_timeout_s=2.0,
        request_timeout_s=2.0,
        agent_id=None,
        assistant_prompt="test prompt",
        allowed_tools=("list_assignments",),
    )
    return replace(base, **overrides)


def _run(coro):
    return asyncio.run(coro)


async def _start_gateway(handler):
    server = await websockets.serve(handler, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    return server, port


async def _stop_gateway(server):
    server.close()
    await server.wait_closed()


def test_connect_handshake_and_chat():
    """Client sends connect first, echoes challenge, then chats via chat.send."""
    seen = {}
    identity_dir = os.path.join(TMP_DIR, "device_identity")
    os.makedirs(identity_dir, exist_ok=True)
    identity_path = os.path.join(identity_dir, "device.json")
    os.environ["OPENCLAW_DEVICE_IDENTITY_PATH"] = identity_path

    async def handler(ws):
        await ws.send(json.dumps({
            "type": "event",
            "event": "connect.challenge",
            "payload": {"nonce": "abc123", "ts": 1700000000},
        }))
        async for raw in ws:
            msg = json.loads(raw)
            method, rid = msg.get("method"), msg.get("id")
            params = msg.get("params") or {}
            if method == "connect":
                seen["params"] = params
                await ws.send(json.dumps({"type": "res", "id": rid, "ok": True,
                                          "payload": {"protocol": 4, "server": "fake"}}))
            elif method == "status":
                await ws.send(json.dumps({"type": "res", "id": rid, "ok": True,
                                          "payload": {"healthy": True}}))
            elif method == "chat.send":
                run_id = params.get("idempotencyKey") or rid
                await ws.send(json.dumps({"type": "res", "id": rid, "ok": True,
                                          "payload": {"runId": run_id, "status": "started"}}))
                await ws.send(json.dumps({
                    "type": "event", "event": "agent", "seq": 2,
                    "payload": {"runId": run_id, "stream": "assistant",
                                "data": {"text": "hello from gateway", "delta": "", "replace": True}},
                }))
                await ws.send(json.dumps({
                    "type": "event", "event": "chat", "seq": 3,
                    "payload": {"runId": run_id, "state": "final"},
                }))
            else:
                await ws.send(json.dumps({"type": "res", "id": rid, "ok": False,
                                          "error": {"message": f"unknown {method}"}}))

    async def scenario():
        server, port = await _start_gateway(handler)
        try:
            async with OpenClawClient(_cfg(port)) as client:
                health = await client.health()
                chat = await client.chat("list my assignments")
            return health, chat
        finally:
            await _stop_gateway(server)

    try:
        health, chat = _run(scenario())
    finally:
        os.environ.pop("OPENCLAW_DEVICE_IDENTITY_PATH", None)

    assert health["connected"] is True
    assert health["status"] == "ok"
    assert health["gateway"]["healthy"] is True
    assert chat["ok"] is True
    assert chat["reply"] == "hello from gateway"
    assert chat["state"] == "final"

    params = seen["params"]
    assert params["role"] == "operator"
    assert params["minProtocol"] == 4
    assert params["maxProtocol"] == 4
    assert params["auth"]["token"] == "test-shared-token"
    assert params["client"]["id"] == "course-organizer"
    # Challenge nonce/ts echoed for protocol compatibility.
    assert params["device"]["nonce"] == "abc123"
    assert params["device"]["signedAt"] == 1700000000

    # The device signature must verify against the public key and v3 payload.
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    from services.openclaw_device import build_device_auth_payload_v3

    raw_pub = _b64url_decode(params["device"]["publicKey"])
    pub = Ed25519PublicKey.from_public_bytes(raw_pub)
    payload = build_device_auth_payload_v3(
        device_id=params["device"]["id"],
        client_id=params["client"]["id"],
        client_mode=params["client"]["mode"],
        role=params["role"],
        scopes=params["scopes"],
        signed_at_ms=params["device"]["signedAt"],
        token=params["auth"]["token"],
        nonce=params["device"]["nonce"],
        platform="windows",
        device_family=None,
    )
    pub.verify(_b64url_decode(params["device"]["signature"]), payload.encode("utf-8"))


def _b64url_decode(value: str) -> bytes:
    import base64
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def test_chat_timeout_raises():
    """A Gateway that never answers a chat request surfaces a timeout."""

    async def handler(ws):
        async for raw in ws:
            msg = json.loads(raw)
            if msg.get("method") == "connect":
                await ws.send(json.dumps({"type": "res", "id": msg["id"], "ok": True,
                                          "payload": {"protocol": 4}}))
            # Deliberately never answer chat.send.

    async def scenario():
        server, port = await _start_gateway(handler)
        try:
            async with OpenClawClient(_cfg(port, request_timeout_s=0.3)) as client:
                await client.chat("no answer", timeout=0.3)
        finally:
            await _stop_gateway(server)

    try:
        _run(scenario())
    except OpenClawTimeout as exc:
        assert "timed out" in str(exc).lower()
    else:  # pragma: no cover
        raise AssertionError("Expected OpenClawTimeout")


def test_rpc_error_is_surfaced():
    async def handler(ws):
        async for raw in ws:
            msg = json.loads(raw)
            if msg.get("method") == "connect":
                await ws.send(json.dumps({"type": "res", "id": msg["id"], "ok": True,
                                          "payload": {"protocol": 4}}))
            else:
                await ws.send(json.dumps({"type": "res", "id": msg["id"], "ok": False,
                                          "error": {"message": "permission denied"}}))

    async def scenario():
        server, port = await _start_gateway(handler)
        try:
            async with OpenClawClient(_cfg(port)) as client:
                await client.chat("hi")
        finally:
            await _stop_gateway(server)

    try:
        _run(scenario())
    except OpenClawError as exc:
        assert "permission denied" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("Expected OpenClawError")


def test_connect_refused_is_clean_error():
    cfg = _cfg(1, connect_timeout_s=0.5)  # port 1: nothing listens there
    try:
        _run(OpenClawClient(cfg).connect())
    except OpenClawError as exc:
        assert "Could not connect" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("Expected OpenClawError")


def test_sync_helpers_report_not_configured():
    disabled = replace(CONFIG, enabled=False)
    with patch("services.openclaw_client.CONFIG", disabled):
        health = get_health_sync()
        chat = chat_sync("hello")
    assert health["status"] == "not_configured"
    assert health["connected"] is False
    assert chat["ok"] is False
    assert chat["configured"] is False
