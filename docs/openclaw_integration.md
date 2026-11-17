# OpenClaw AI Course Assistant — Integration Guide

OpenClaw is an **optional AI assistant/orchestration layer** for the Course Project,
Notes & Assignment Organizer. It does **not** replace the existing FastAPI, Streamlit,
SQLAlchemy/SQLModel, or LangGraph architecture. Your current app keeps working with
OpenClaw disabled.

## What it does

- Connects your backend to an **OpenClaw Gateway** using the documented external
  client protocol: **WebSocket transport + JSON RPC frames** (`connect` handshake,
  operator role, `operator.read` / `operator.write` scopes, **Ed25519 device
  identity**).
- Provides a **Faculty → AI Course Assistant** page to ask questions.
- Exposes only a **restricted allowlist of backend tools** to the assistant:
  - `list_assignments`
  - `get_submission_status`
  - `find_late_submissions`
  - `read_github_activity`
  - `summarize_course_notes`
  - `set_grade` / `approve_grade` / `publish_release` (approval-only)
- **No unrestricted database or shell access** is exposed.
- Enforces **user roles** and **course-level permissions**, writes **audit logs**, and
  requires **explicit human approval** for grade/publishing changes.

## Architecture

```
Streamlit (Faculty → AI Course Assistant)
        │  HTTP
        ▼
FastAPI  /openclaw/*
        │  WebSocket (Gateway protocol, Ed25519 device identity)
        ▼
OpenClaw Gateway (agent runtime)
        │  MCP/plugin bridge (restricted tools)
        ▼
Course Organizer backend tools (services/openclaw_tools.py)
        │
        ▼
SQLModel DB + AuditLog + OpenClawApproval
```

- `services/openclaw_config.py` — secure settings (env/.env), defaults to disabled.
- `services/openclaw_device.py` — Ed25519 device identity: keypair persistence,
  device id, and the signed v3 handshake payload.
- `services/openclaw_client.py` — Gateway WebSocket client: handshake, health,
  asynchronous `chat.send` with streamed replies, timeouts.
- `services/openclaw_tools.py` — restricted tool allowlist + permission checks + audit.
- `api/routers/openclaw.py` — `/openclaw/health`, `/openclaw/chat`, `/openclaw/tools`,
  `/openclaw/approvals`.
- `ui_roles/pages/17_OpenClaw_Assistant.py` — faculty UI.
- `models/openclaw.py` — `OpenClawApproval` table for human-in-the-loop changes.

## Local setup

### 1. Run the OpenClaw Gateway

The Gateway is a separate Node.js service (usually `openclaw gateway run`, or the
OpenClaw desktop app). It listens on `ws://127.0.0.1:18789` by default.

If the Gateway runs inside WSL (as on this machine, distro `OpenClawGateway`),
Windows reaches it through the WSL localhost relay, so `ws://127.0.0.1:18789`
still works from the API.

Find the gateway token:

- Config file (inside the Gateway's environment):
  `~/.openclaw/openclaw.json` → `gateway.auth.token`
  (on Windows with a WSL gateway:
  `\\wsl.localhost\<distro>\home\<user>\.openclaw\openclaw.json`)
- CLI: `openclaw config get gateway.auth.token`
- Generate a new one: `openclaw doctor --generate-gateway-token` (then restart the
  Gateway and update `.env`).

### 2. Configure this app

In `.env`:

```env
OPENCLAW_ENABLED=true
OPENCLAW_GATEWAY_URL=ws://127.0.0.1:18789
OPENCLAW_GATEWAY_TOKEN=<gateway token>
```

On first connect the app generates a device identity (Ed25519 keypair) and stores it
at `OPENCLAW_DEVICE_IDENTITY_PATH` (default `~/.course_organizer/openclaw_device.json`).
Treat that file like a password — it is the credential the Gateway pairs.

### 3. Pair the device (first time only)

The Gateway requires every client to have an approved device pairing. The first
connection creates a pending request; approve it on the Gateway host:

```bash
openclaw devices list                 # find the pending requestId
openclaw devices approve <requestId>  # approve this PC's AI Course Assistant
```

(If the Gateway is in WSL: `wsl -d OpenClawGateway -e openclaw devices list`.)

### 4. Choose a model

The assistant turn runs on the Gateway's default agent model. If it is missing or
unsupported you will see `This model was not found. Choose another model in the
Control UI.` Pick a supported default model in the OpenClaw Control UI / desktop app
(→ Model → Default model), for example one listed by `openclaw models list --plain`.

### 5. Restart

```cmd
.venv\Scripts\python.exe -m uvicorn api.main:app --port 8000
C:\Users\ummea\.virtualenvs\course-organizer\Scripts\python.exe -m streamlit run ui_roles\Home.py --server.port 8501
```

## How to verify it is working (UI)

1. Log in as **Faculty** (or HoD/Admin).
2. Open **AI Course Assistant**.
3. Click **Check OpenClaw health**:
   - `Configured: Yes`, `Connected: Yes`, `Status: ok` → active.
   - Any error text is shown directly under the status cards.
4. Ask a question. The reply is streamed from the Gateway's `chat.send` events and
   displayed in the page; the model/provider used is shown underneath.
5. Grade/publish requests create a **pending approval**; approve/reject it in the same page.

## Teacher-friendly answers

Common faculty questions (assignments, late submissions, GitHub activity, notes)
are answered directly from the restricted, audited backend tools
(`services/openclaw_intents.py`), not from the Gateway agent's own tools. This means:

- replies are plain language, never raw JSON;
- the Gateway's `memory_search` / shell tools are never used for them;
- permission errors become readable messages (e.g. "only assignments you own are visible").

Other questions fall back to the assistant with a strict system prompt forbidding
memory/shell use; if the agent still reports a tool failure, the API replaces it
with a short, helpful suggestion.

## Safety / permissions

- Only `faculty`, `hod`, `admin` can use the assistant endpoints.
- Faculty are limited to assignments/notes/submissions they own (course access check).
- Every tool call writes an `AuditLog` row; every assistant prompt is audited.
- `set_grade`, `approve_grade`, `publish_release` **never mutate directly**; they create a
  pending `OpenClawApproval` that a human must approve.
- The gateway token and the device private key are read from env/files and never logged.
- The Gateway connection uses timeouts; a slow model does not block the API forever
  (`OPENCLAW_CHAT_TIMEOUT_MS`, default 300 s).

## Tests

Mocked tests (no live Gateway required):

```cmd
.venv\Scripts\python.exe -m pytest tests/test_openclaw_client.py tests/test_openclaw_integration.py -q
```

- `tests/test_openclaw_client.py` starts a local in-process WebSocket server and
  exercises the real handshake (challenge echo + Ed25519 device signature
  verification), async chat streaming, timeouts, and connection-refused handling.
- `tests/test_openclaw_integration.py` exercises the API: role enforcement, tool
  allowlisting, course-scoped permissions, audit logging, and approval gating.

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `not_configured` | Set `OPENCLAW_ENABLED=true` + URL + token in `.env`, restart API |
| `Could not connect to OpenClaw Gateway` | Start the Gateway (`openclaw gateway run`) / check WSL is running |
| `device identity required` / `NOT_PAIRED` | Approve the device: `openclaw devices approve <requestId>` |
| `invalid connect params: ... /client/id or /client/mode` | Must use a Gateway-allowed id/mode, e.g. `OPENCLAW_CLIENT_ID=cli`, `OPENCLAW_CLIENT_MODE=cli` |
| `session mutation target is unavailable` | Older bug; fixed by always sending `sessionKey` + `idempotencyKey` (this client does) |
| `This model was not found` | Choose a supported default model in the Control UI (`openclaw models list --plain`) |
| `chat.send` timed out | Model took longer than `OPENCLAW_CHAT_TIMEOUT_MS`; increase it |
| 403 on tool invoke | User lacks course permission or role |

## Protocol reference (summary)

- WebSocket text frames, JSON payloads.
- Optional pre-connect event: `connect.challenge` with `{nonce, ts}`.
- First request frame: `connect` with `minProtocol`/`maxProtocol` (4), `client`
  (`id`/`mode` from the Gateway's allowed enums), `role`, `scopes`, `auth.token`, and
  a `device` block: `id` (SHA-256 hex of the raw public key), `publicKey`
  (base64url raw key), `signature` (base64url Ed25519 over the v3 payload below),
  `signedAt` (challenge `ts`), `nonce`.
- Device payload (pipe-separated, UTF-8):
  `v3|deviceId|clientId|clientMode|role|scopesCsv|signedAtMs|token|nonce|platformLower|deviceFamilyLower`
- Requests: `{"type":"req","id":...,"method":...,"params":...}`.
- Responses: `{"type":"res","id":...,"ok":true,"payload":...}` or `ok:false,error`.
- Events: `{"type":"event","event":...,"payload":...}`.
- `chat.send` is asynchronous: it returns `{runId, status:"started"}` plus
  `sessionKey`/`idempotencyKey`; the reply arrives as `agent`/`chat` events with the
  same `runId` (terminal `chat` state `final` on success, `error` on failure).
- RPC families used: `status`, `health`, `chat.send`, `tools.invoke` (via bridge).
