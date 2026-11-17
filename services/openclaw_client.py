"""OpenClaw Gateway WebSocket client (external client protocol).

Implements the documented Gateway wire protocol:
- WebSocket text frames carrying JSON payloads.
- After an optional ``connect.challenge`` event, the first request frame must be
  ``connect`` (client metadata, operator role, scopes, shared token).
- Request/response frames: ``{"type":"req","id":...,"method":...,"params":...}``
  -> ``{"type":"res","id":...,"ok":true,"payload":...}`` or ``ok:false,error``.
- Events: ``{"type":"event","event":...,"payload":...}``.

The client is intentionally small and dependency-light. It powers only the
optional AI Course Assistant layer; the existing FastAPI/Streamlit/SQLAlchemy
stack keeps working when OpenClaw is absent.

Docs: docs/openclaw_integration.md
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from typing import Any, Optional

try:  # optional dependency: the app must import cleanly without it
    import websockets
except ImportError:  # pragma: no cover - exercised only on minimal installs
    websockets = None

from services.openclaw_config import CONFIG, OpenClawConfig
from services.openclaw_device import (
    build_device_auth_payload_v3,
    load_or_create_identity,
    sign_payload,
)

class OpenClawError(Exception):
    """Raised for Gateway connection/handshake/RPC failures."""


class OpenClawTimeout(OpenClawError):
    """Raised when a Gateway RPC or handshake wait exceeds its timeout."""


class OpenClawClient:
    """Async Gateway protocol client with handshake, health check, and RPCs.

    Usage::

        async with OpenClawClient() as client:
            health = await client.health()
            result = await client.chat("Which submissions are late?")
    """

    def __init__(self, config: OpenClawConfig | None = None):
        self.config = config or CONFIG
        self._ws: Any = None
        self._req_id = 0
        self._pending: dict[str, asyncio.Future] = {}
        self._reader_task: Optional[asyncio.Task] = None
        self._hello: dict[str, Any] | None = None
        self._challenge: dict[str, Any] | None = None
        self._events: list[dict[str, Any]] = []
        self._event_queue: "asyncio.Queue[dict[str, Any]]" = asyncio.Queue()
        self._closed = False

    # ------------------------------------------------------------------ lifecycle
    async def __aenter__(self) -> "OpenClawClient":
        await self.connect()
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self.close()

    async def connect(self) -> dict[str, Any]:
        """Connect, complete the handshake, and return the hello-ok payload."""
        if not self.config.configured:
            raise OpenClawError(
                "OpenClaw is not configured. Set OPENCLAW_ENABLED=true, "
                "OPENCLAW_GATEWAY_URL and OPENCLAW_GATEWAY_TOKEN in .env."
            )
        if websockets is None:
            raise OpenClawError(
                "The optional 'websockets' package is not installed. "
                "Install it with: pip install websockets"
            )
        if self._ws is not None:
            return self._hello or {}

        self._closed = False
        try:
            self._ws = await asyncio.wait_for(
                websockets.connect(
                    self.config.gateway_url,
                    max_size=8 * 1024 * 1024,
                    open_timeout=self.config.connect_timeout_s,
                    close_timeout=5,
                ),
                timeout=self.config.connect_timeout_s,
            )
        except Exception as exc:
            raise OpenClawError(f"Could not connect to OpenClaw Gateway: {exc}") from exc

        try:
            # The Gateway may send connect.challenge before we send connect.
            # Read it *before* starting the reader loop so the two never race.
            await self._read_challenge()
            self._reader_task = asyncio.create_task(self._read_loop())
            hello = await self._handshake()
        except Exception:
            await self.close()
            raise
        self._hello = hello
        return hello

    async def _read_challenge(self) -> None:
        try:
            first = await asyncio.wait_for(
                self._ws.recv(), timeout=min(self.config.connect_timeout_s, 0.5)
            )
        except Exception:
            # No challenge (or connection already failed); connect without it.
            return
        try:
            data = json.loads(first)
        except Exception:
            return
        if data.get("type") == "event" and data.get("event") == "connect.challenge":
            payload = data.get("payload")
            if isinstance(payload, dict):
                self._challenge = payload

    def _connect_params(self) -> dict[str, Any]:
        challenge = self._challenge or {}
        nonce = str(challenge.get("nonce") or "")
        ts = challenge.get("ts")
        signed_at = int(ts) if isinstance(ts, int) and ts >= 0 else int(time.time() * 1000)

        identity = load_or_create_identity(self.config.device_identity_path)
        payload = build_device_auth_payload_v3(
            device_id=identity.device_id,
            client_id=self.config.client_id,
            client_mode=self.config.client_mode,
            role=self.config.role,
            scopes=self.config.scopes,
            signed_at_ms=signed_at,
            token=self.config.token or None,
            nonce=nonce,
            platform=self.config.platform,
            device_family=self.config.device_family,
        )
        signature = sign_payload(identity.private_key, payload)

        return {
            "minProtocol": self.config.min_protocol,
            "maxProtocol": self.config.max_protocol,
            "client": {
                "id": self.config.client_id,
                "version": self.config.client_version,
                "platform": self.config.platform,
                "mode": self.config.client_mode,
            },
            "role": self.config.role,
            "scopes": list(self.config.scopes),
            "caps": [],
            "commands": [],
            "permissions": {},
            "auth": {"token": self.config.token},
            "locale": "en-US",
            "userAgent": f"course-organizer/{self.config.client_version}",
            "device": {
                "id": identity.device_id,
                "publicKey": identity.public_key_b64url,
                "signature": signature,
                "signedAt": signed_at,
                "nonce": nonce,
            },
        }

    async def _handshake(self) -> dict[str, Any]:
        resp = await self.rpc("connect", self._connect_params(), timeout=self.config.connect_timeout_s)
        if not isinstance(resp, dict):
            raise OpenClawError("Unexpected connect response from OpenClaw Gateway.")
        return resp

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._reader_task is not None:
            self._reader_task.cancel()
            try:
                await self._reader_task
            except asyncio.CancelledError:
                pass
            except Exception:
                pass
            self._reader_task = None
        if self._ws is not None:
            try:
                await self._ws.close()
            except Exception:
                pass
            self._ws = None
        for fut in list(self._pending.values()):
            if not fut.done():
                fut.set_exception(OpenClawError("Connection closed"))
        self._pending.clear()

    # ------------------------------------------------------------------ framing
    async def _read_loop(self) -> None:
        try:
            if self._ws is None:
                return
            async for raw in self._ws:
                try:
                    frame = json.loads(raw)
                except Exception:
                    continue
                ftype = frame.get("type")
                if ftype == "res":
                    fid = str(frame.get("id"))
                    fut = self._pending.pop(fid, None)
                    if fut is None or fut.done():
                        continue
                    if frame.get("ok"):
                        fut.set_result(frame.get("payload"))
                    else:
                        err = frame.get("error") or "Gateway RPC error"
                        if isinstance(err, dict):
                            err = err.get("message") or json.dumps(err)
                        fut.set_exception(OpenClawError(str(err)))
                elif ftype == "event":
                    # Events are diagnostic only; restricted tools do not rely
                    # on streaming. Keep the latest challenge for reconnects.
                    if frame.get("event") == "connect.challenge":
                        payload = frame.get("payload")
                        if isinstance(payload, dict):
                            self._challenge = payload
                    self._events.append(frame)
                    self._event_queue.put_nowait(frame)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            for fut in list(self._pending.values()):
                if not fut.done():
                    fut.set_exception(OpenClawError(f"Gateway read error: {exc}"))
            self._pending.clear()

    async def rpc(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        timeout: float | None = None,
    ) -> Any:
        """Send one request frame and await its matching response."""
        if self._ws is None:
            raise OpenClawError("Not connected to OpenClaw Gateway.")
        budget = timeout or self.config.request_timeout_s
        self._req_id += 1
        rid = f"co-{self._req_id}-{uuid.uuid4().hex[:8]}"
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[rid] = fut
        frame = {"type": "req", "id": rid, "method": method, "params": params or {}}
        try:
            await asyncio.wait_for(self._ws.send(json.dumps(frame)), timeout=budget)
        except Exception as exc:
            self._pending.pop(rid, None)
            raise OpenClawError(f"Failed to send {method}: {exc}") from exc

        try:
            return await asyncio.wait_for(fut, timeout=budget)
        except asyncio.TimeoutError as exc:
            self._pending.pop(rid, None)
            raise OpenClawTimeout(f"OpenClaw {method} timed out after {budget}s") from exc
        except OpenClawError:
            raise
        except Exception as exc:  # pragma: no cover - defensive
            self._pending.pop(rid, None)
            raise OpenClawError(f"OpenClaw {method} failed: {exc}") from exc

    async def wait_for_event(
        self,
        timeout: float = 60,
        match: Optional[Any] = None,
    ) -> dict[str, Any]:
        """Wait for the next Gateway event, optionally filtered by ``match``.

        ``match`` may be a callable ``frame -> bool`` or a set of event names.
        """
        names = set(match) if isinstance(match, (set, list, tuple)) else None
        end = asyncio.get_event_loop().time() + (timeout or self.config.request_timeout_s)
        while True:
            remaining = end - asyncio.get_event_loop().time()
            if remaining <= 0:
                raise OpenClawTimeout(f"No matching Gateway event within {timeout}s")
            try:
                frame = await asyncio.wait_for(self._event_queue.get(), timeout=remaining)
            except asyncio.TimeoutError as exc:
                raise OpenClawTimeout(f"No matching Gateway event within {timeout}s") from exc
            if names is not None:
                ok = frame.get("event") in names
            elif match is None:
                ok = True
            else:
                ok = bool(match(frame))
            if ok:
                return frame

    # ------------------------------------------------------------------ helpers
    async def health(self) -> dict[str, Any]:
        """Return connection health using the hello snapshot and a status RPC."""
        if self._ws is None:
            return {
                "configured": self.config.configured,
                "enabled": self.config.enabled,
                "connected": False,
                "status": "not_connected",
                "gateway_url": self.config.gateway_url,
            }
        info: dict[str, Any] = {
            "configured": self.config.configured,
            "enabled": self.config.enabled,
            "connected": True,
            "status": "connected",
            "gateway_url": self.config.gateway_url,
            "protocol": f"{self.config.min_protocol}-{self.config.max_protocol}",
        }
        if isinstance(self._hello, dict):
            # Never echo auth material; hello-ok does not include the token.
            info["hello"] = {
                k: v for k, v in self._hello.items() if k.lower() not in {"token", "auth"}
            }
        try:
            status = await self.rpc("status", {}, timeout=self.config.request_timeout_s)
            info["status"] = "ok"
            if isinstance(status, dict):
                info["gateway"] = status
        except OpenClawError:
            # Some Gateways do not expose status; a completed hello is enough.
            info["status"] = "connected_no_status"
        return info

    async def chat(
        self,
        message: str,
        session_key: str | None = None,
        agent_id: str | None = None,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        """Send one assistant turn and collect the streamed reply.

        ``chat.send`` is asynchronous: it returns ``{runId, status:"started"}``
        and the reply arrives as ``agent``/``chat`` events on the same socket.
        We accumulate the assistant stream and stop on the terminal ``chat``
        event for our run id (``state: "error"`` or a completion state).
        """
        budget = timeout or self.config.chat_timeout_s
        idempotency_key = uuid.uuid4().hex
        agent = agent_id or self.config.agent_id or "main"
        session_key = (
            session_key
            or self.config.session_key
            or (self._hello or {}).get("snapshot", {}).get("sessionDefaults", {}).get("mainSessionKey")
            or f"agent:{agent}:main"
        )
        params: dict[str, Any] = {
            "message": CONFIG.assistant_prompt + "\n\nTeacher's question: " + message,
            "idempotencyKey": idempotency_key,
            "sessionKey": session_key,
            "agentId": agent,
        }

        try:
            started = await self.rpc("chat.send", params, timeout=budget)
        except OpenClawError as exc:
            text = str(exc).lower()
            if "unknown method" in text or "not found" in text or "invalid_request" in text:
                started = await self.rpc("agent", params, timeout=budget)
            else:
                raise

        run_id = (started or {}).get("runId") or idempotency_key
        texts: list[str] = []
        model_info: dict[str, Any] = {}
        error: str | None = None
        state = "started"
        end = asyncio.get_event_loop().time() + budget

        def is_ours(frame: dict[str, Any]) -> bool:
            payload = frame.get("payload") or {}
            return payload.get("runId") == run_id

        while True:
            remaining = end - asyncio.get_event_loop().time()
            if remaining <= 0:
                raise OpenClawTimeout(
                    f"Assistant reply for run {run_id} did not finish within {budget}s"
                )
            try:
                frame = await self.wait_for_event(timeout=remaining, match=is_ours)
            except OpenClawTimeout:
                raise OpenClawTimeout(
                    f"Assistant reply for run {run_id} did not finish within {budget}s"
                )
            event = frame.get("event")
            payload = frame.get("payload") or {}
            if event == "agent":
                stream = payload.get("stream")
                data = payload.get("data") or {}
                if stream == "assistant":
                    chunk = str(data.get("delta") or data.get("text") or "")
                    if data.get("replace"):
                        texts = [str(data.get("text") or "")]
                    elif chunk:
                        texts.append(chunk)
                elif stream == "lifecycle" and data.get("phase") == "model":
                    model_info = {"provider": data.get("provider"), "model": data.get("model")}
            elif event == "chat":
                state = str(payload.get("state") or state)
                if payload.get("errorMessage"):
                    error = str(payload.get("errorMessage"))
                if state == "error" or state in {
                    "done",
                    "complete",
                    "completed",
                    "finished",
                    "final",
                    "success",
                    "idle",
                    "aborted",
                    "cancelled",
                    "canceled",
                }:
                    break

        return {
            "ok": state != "error" and not error,
            "reply": "".join(texts).strip(),
            "state": state,
            "run_id": run_id,
            "model": model_info,
            "error": error,
        }


# ---------------------------------------------------------------------- sync wrappers
def get_health_sync() -> dict[str, Any]:
    """Synchronous health check (opens and closes one short connection)."""

    async def _run() -> dict[str, Any]:
        if not CONFIG.configured:
            return {
                "enabled": CONFIG.enabled,
                "configured": False,
                "connected": False,
                "status": "not_configured",
                "gateway_url": CONFIG.gateway_url,
            }
        try:
            async with OpenClawClient() as client:
                return await client.health()
        except OpenClawError as exc:
            return {
                "enabled": CONFIG.enabled,
                "configured": CONFIG.configured,
                "connected": False,
                "status": "error",
                "error": str(exc),
                "gateway_url": CONFIG.gateway_url,
            }

    return asyncio.run(_run())


def chat_sync(
    message: str,
    session_key: str | None = None,
    agent_id: str | None = None,
    timeout: float | None = None,
) -> dict[str, Any]:
    """Synchronous one-shot assistant turn."""

    async def _run() -> dict[str, Any]:
        if not CONFIG.configured:
            return {
                "ok": False,
                "error": "OpenClaw is not configured. Set OPENCLAW_ENABLED=true, "
                         "OPENCLAW_GATEWAY_URL and OPENCLAW_GATEWAY_TOKEN in .env.",
                "configured": False,
            }
        try:
            async with OpenClawClient() as client:
                return await client.chat(
                    message, session_key=session_key, agent_id=agent_id, timeout=timeout
                )
        except OpenClawError as exc:
            return {"ok": False, "error": str(exc), "configured": CONFIG.configured}

    return asyncio.run(_run())
