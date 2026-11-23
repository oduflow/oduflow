"""Mock server for trying the shared-environment workspace without Docker.

Serves the real page and API plumbing (``web_ui``) for one made-up
environment, ``feature/crm``. The endpoints the workspace calls are mocked,
and a scripted fake agent speaks ACP over the chat WebSocket. Every reply
plays the same scenario: reasoning, a plan, tool calls, a screenshot, and a
streamed answer with a link into Odoo. A message containing "approve" also
raises a permission request.

Run from the repository root with Oduflow installed (``pip install -e .``):

    python frontend/workspace/dev/mock_server.py                 # 127.0.0.1:8765
    python frontend/workspace/dev/mock_server.py --host 0.0.0.0  # reachable from the LAN
    python frontend/workspace/dev/mock_server.py --status stopped

Then open http://127.0.0.1:8765/env/feature/crm. The page is whatever build is
committed to src/oduflow/templates/static/workspace/: rebuild with
``npm run build`` and reload. For hot reload instead, run ``npm run dev`` in
frontend/workspace with ODUFLOW_URL=http://127.0.0.1:8765 and open
http://localhost:5173/?env=feature/crm.

Nothing here touches Docker: only the workspace's own paths are reachable,
everything else answers 404. The real dashboard routes (and the Engineer
view's calls) would act on the host's real Docker, so they stay out.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import html
import json
import struct
import tempfile
import zlib
from typing import Any

import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from starlette.routing import Route, WebSocketRoute
from starlette.types import Receive, Scope, Send
from starlette.websockets import WebSocket

from oduflow import web_ui
from oduflow.locking import LockManager
from oduflow.settings import Settings, TeamSettings

ENV = "feature/crm"
ENV_URL = "http://localhost:58069"
MODELS = {
    "availableModels": [
        {"modelId": "sonnet", "name": "Claude Sonnet"},
        {"modelId": "opus", "name": "Claude Opus"},
    ],
    "currentModelId": "sonnet",
}

STATE: dict[str, Any] = {"status": "running"}
# Conversations the workspace saved, newest first (in memory only).
SESSIONS: dict[str, Any] = {"current": None, "history": []}


def _list_environments(settings: Settings, team: TeamSettings) -> list[dict[str, Any]]:
    return [
        {
            "env_name": ENV,
            "branch": ENV,
            "git_branch": ENV,
            "status": STATE["status"],
            "url": ENV_URL,
            "containers": [
                {"name": "oduflow-1-feature-crm", "status": STATE["status"]}
            ],
        }
    ]


def _screenshot_png(width: int = 640, height: int = 360) -> str:
    """A tiny Odoo-looking form (top bar, card, highlighted field), base64."""
    rows = bytearray()
    for y in range(height):
        rows.append(0)
        for x in range(width):
            if y < 48:
                color = (113, 75, 103)
            elif 100 < x < 540 and 200 < y < 230:
                color = (230, 240, 255)
            elif 60 < x < 580 and 90 < y < 320:
                color = (255, 255, 255)
            else:
                color = (243, 244, 246)
            rows += bytes(color)

    def chunk(kind: bytes, data: bytes) -> bytes:
        crc = zlib.crc32(kind + data) & 0xFFFFFFFF
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", crc)

    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    png = (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(bytes(rows)))
        + chunk(b"IEND", b"")
    )
    return base64.b64encode(png).decode()


SCREENSHOT = _screenshot_png()


# -- mocked REST --------------------------------------------------------------


async def agent_info(request: Request) -> JSONResponse:
    return JSONResponse({"ok": True, "enabled": True, "default": "claude"})


async def chat_info(request: Request) -> JSONResponse:
    return JSONResponse(
        {
            "ok": True,
            "cwd": "/workspace/feature-crm",
            "type": "claude",
            "session_id": SESSIONS["current"],
            "history": SESSIONS["history"],
            "attachment_limits": {"max_file_bytes": 25 * 1024 * 1024, "max_files": 5},
        }
    )


async def chat_session(request: Request) -> JSONResponse:
    body = await request.json()
    session_id = body.get("session_id")
    if session_id:
        SESSIONS["current"] = session_id
        entry = next(
            (h for h in SESSIONS["history"] if h["session_id"] == session_id), None
        )
        if entry is None:
            entry = {
                "session_id": session_id,
                "title": None,
                "created_at": "2026-10-10T18:00:00Z",
            }
            SESSIONS["history"].insert(0, entry)
        if body.get("title"):
            entry["title"] = body["title"]
    return JSONResponse(
        {"ok": True, "session_id": SESSIONS["current"], "history": SESSIONS["history"]}
    )


async def logs(request: Request) -> JSONResponse:
    lines = [
        f"2026-10-10 20:{minute:02d}:00,000 1 INFO oduflow_1_feature_crm "
        f"odoo.modules.loading: loading {count} modules..."
        for minute, count in enumerate(range(40, 80))
    ]
    return JSONResponse({"ok": True, "logs": "\n".join(lines)})


async def start(request: Request) -> JSONResponse:
    STATE["status"] = "running"
    return JSONResponse({"ok": True, "result": {}})


async def connect_open(request: Request) -> HTMLResponse:
    target = html.escape(request.query_params.get("next") or "/web")
    return HTMLResponse(
        "<p style='font-family:sans-serif'>Mock: Odoo would open here, signed in, "
        f"at <code>{target}</code>.</p>"
    )


# -- fake agent (ACP over the chat WebSocket) ---------------------------------


class FakeAgent:
    def __init__(self, ws: WebSocket) -> None:
        self.ws = ws
        self.sessions = 0
        self.pending: dict[Any, asyncio.Future[Any]] = {}

    async def send(self, message: dict[str, Any]) -> None:
        await self.ws.send_text(json.dumps({"jsonrpc": "2.0", **message}))

    async def update(self, session_id: str, update: dict[str, Any]) -> None:
        await self.send(
            {
                "method": "session/update",
                "params": {"sessionId": session_id, "update": update},
            }
        )

    async def text(self, session_id: str, kind: str, text: str) -> None:
        await self.update(
            session_id,
            {"sessionUpdate": kind, "content": {"type": "text", "text": text}},
        )

    async def handle(self, message: dict[str, Any]) -> None:
        method, msg_id = message.get("method"), message.get("id")
        params = message.get("params") or {}
        if method is None:
            # The browser answering a permission request.
            future = self.pending.pop(msg_id, None)
            if future is not None:
                future.set_result(message.get("result"))
        elif method == "initialize":
            capabilities = {"promptCapabilities": {"image": True}}
            await self.send(
                {
                    "id": msg_id,
                    "result": {"protocolVersion": 1, "agentCapabilities": capabilities},
                }
            )
        elif method == "session/new":
            self.sessions += 1
            await self.send(
                {
                    "id": msg_id,
                    "result": {"sessionId": f"s{self.sessions}", "models": MODELS},
                }
            )
        elif method == "session/load":
            # Replays an earlier exchange, including the workspace's standing
            # instructions, which the page must hide.
            session_id = params["sessionId"]
            await self.text(
                session_id,
                "user_message_chunk",
                "<oduflow-workspace-context>\nrules\n</oduflow-workspace-context>\n\n",
            )
            await self.text(
                session_id, "user_message_chunk", "Earlier: rename the Sales menu"
            )
            await self.text(
                session_id,
                "agent_message_chunk",
                "Done, the menu is now called **Orders**.",
            )
            await self.send({"id": msg_id, "result": {"models": MODELS}})
        elif method == "session/set_model":
            await self.send({"id": msg_id, "result": {}})
        elif method == "session/prompt":
            asyncio.create_task(self.prompt(msg_id, params))
        elif msg_id is not None:
            await self.send({"id": msg_id, "result": {}})

    async def plan(self, session_id: str, done: int, active: bool = True) -> None:
        steps = [
            "Add the field to the partner form view",
            "Upgrade the module",
            "Check the form in the browser",
        ]
        entries = [
            {
                "content": step,
                "status": "completed"
                if i < done
                else ("in_progress" if i == done and active else "pending"),
            }
            for i, step in enumerate(steps)
        ]
        await self.update(session_id, {"sessionUpdate": "plan", "entries": entries})

    async def prompt(self, msg_id: Any, params: dict[str, Any]) -> None:
        session_id = params["sessionId"]
        text = " ".join(
            b.get("text", "")
            for b in params.get("prompt", [])
            if b.get("type") == "text"
        )
        pause = 0.35
        await self.text(
            session_id,
            "agent_thought_chunk",
            "The user wants a Tax ID on the customer form. res.partner already has `vat`; "
            "I only need to show it.",
        )
        await asyncio.sleep(pause)
        await self.plan(session_id, done=0)
        await asyncio.sleep(pause)
        await self.update(
            session_id,
            {
                "sessionUpdate": "tool_call",
                "toolCallId": "t1",
                "title": "Edit custom_crm/views/res_partner_views.xml",
                "kind": "edit",
                "status": "in_progress",
                "rawInput": {"path": "custom_crm/views/res_partner_views.xml"},
            },
        )
        await asyncio.sleep(pause)
        await self.update(
            session_id,
            {
                "sessionUpdate": "tool_call_update",
                "toolCallId": "t1",
                "status": "completed",
                "rawOutput": "1 file changed",
            },
        )
        if "approve" in text.lower():
            request_id = 900 + self.sessions
            future: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
            self.pending[request_id] = future
            await self.send(
                {
                    "id": request_id,
                    "method": "session/request_permission",
                    "params": {
                        "sessionId": session_id,
                        "toolCall": {"title": "upgrade_odoo_modules custom_crm"},
                        "options": [
                            {
                                "optionId": "allow",
                                "name": "Allow",
                                "kind": "allow_once",
                            },
                            {
                                "optionId": "reject",
                                "name": "Reject",
                                "kind": "reject_once",
                            },
                        ],
                    },
                }
            )
            await future
        await self.plan(session_id, done=1)
        await self.update(
            session_id,
            {
                "sessionUpdate": "tool_call",
                "toolCallId": "t2",
                "title": "mcp__oduflow__upgrade_odoo_modules",
                "kind": "other",
                "status": "in_progress",
            },
        )
        await asyncio.sleep(pause * 2)
        await self.update(
            session_id,
            {
                "sessionUpdate": "tool_call_update",
                "toolCallId": "t2",
                "status": "completed",
                "rawOutput": "Upgraded: custom_crm",
            },
        )
        await self.update(
            session_id,
            {
                "sessionUpdate": "tool_call",
                "toolCallId": "t3",
                "title": "mcp__agent_browser__screenshot",
                "kind": "other",
                "status": "completed",
                "content": [
                    {
                        "type": "content",
                        "content": {
                            "type": "image",
                            "mimeType": "image/png",
                            "data": SCREENSHOT,
                        },
                    }
                ],
            },
        )
        await self.plan(session_id, done=3)
        answer = (
            "Done. The customer form now has a **Tax ID** field, right under the address.\n\n"
            "- It uses Odoo's existing tax number, so nothing changes in invoices.\n"
            "- It is visible to everyone who can edit customers.\n\n"
            f"[Open a customer to try it]({ENV_URL}/odoo/contacts/7)"
        )
        for i in range(0, len(answer), 24):
            await self.text(session_id, "agent_message_chunk", answer[i : i + 24])
            await asyncio.sleep(0.03)
        await self.send({"id": msg_id, "result": {"stopReason": "end_turn"}})


async def agent_socket(ws: WebSocket) -> None:
    await ws.accept()
    agent = FakeAgent(ws)
    try:
        while True:
            for line in (await ws.receive_text()).splitlines():
                if line.strip():
                    await agent.handle(json.loads(line))
    except Exception:
        pass  # the page closed the chat


# -- app ----------------------------------------------------------------------

# Only the workspace and the mocked endpoints are reachable; every other real
# web_ui route would act on the host's real Docker.
_ALLOWED_EXACT = {
    "/logo.png",
    "/favicon.ico",
    "/api/agent",
    "/api/environments",
    "/logout",
}
_ALLOWED_PREFIXES = ("/env/", "/static/")
_ALLOWED_ENV_SUFFIXES = (
    "/agent-acp",
    "/agent-acp/info",
    "/agent-acp/session",
    "/logs",
    "/start",
    "/restart",
    "/connect-open",
)


def _allowed(path: str) -> bool:
    if path in _ALLOWED_EXACT or path.startswith(_ALLOWED_PREFIXES):
        return True
    return path.startswith("/api/environments/") and path.endswith(
        _ALLOWED_ENV_SUFFIXES
    )


def build_app() -> Any:
    data_dir = tempfile.mkdtemp(prefix="oduflow-workspace-mock-")
    settings = Settings(
        routing_mode="port",
        base_data_dir=data_dir,
        teams={"1": TeamSettings(team_id="1", data_dir=data_dir + "/team_1")},
    )
    web_ui.env_ops.list_environments = _list_environments

    app = Starlette()
    web_ui.mount_web_ui(app, lambda: settings, LockManager())
    env = "/api/environments/{branch:path}"
    app.router.routes[0:0] = [
        Route("/api/agent", agent_info),
        Route(env + "/agent-acp/info", chat_info),
        Route(env + "/agent-acp/session", chat_session, methods=["POST"]),
        Route(env + "/logs", logs),
        Route(env + "/start", start, methods=["POST"]),
        Route(env + "/restart", start, methods=["POST"]),
        Route(env + "/connect-open", connect_open),
        WebSocketRoute(env + "/agent-acp", agent_socket),
    ]

    async def guarded(scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] in ("http", "websocket"):
            path = scope.get("path", "")
            if scope["type"] == "http" and path == "/":
                await RedirectResponse("/env/" + ENV, status_code=303)(
                    scope, receive, send
                )
                return
            if not _allowed(path):
                if scope["type"] == "websocket":
                    await send({"type": "websocket.close", "code": 1008})
                else:
                    await Response("Not available in the mock.", status_code=404)(
                        scope, receive, send
                    )
                return
        await app(scope, receive, send)

    return guarded


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    parser.add_argument(
        "--host", default="127.0.0.1", help="bind address (default 127.0.0.1)"
    )
    parser.add_argument("--port", type=int, default=8765, help="port (default 8765)")
    parser.add_argument(
        "--status",
        default="running",
        choices=["running", "stopped", "partial", "exited"],
        help="initial environment status (default running)",
    )
    args = parser.parse_args()
    STATE["status"] = args.status
    print(f"Workspace mock: http://{args.host}:{args.port}/env/{ENV}")
    uvicorn.run(build_app(), host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
