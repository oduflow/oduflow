"""REST coverage for Agent Chat session history."""

from __future__ import annotations

import asyncio
import json
from unittest.mock import patch

import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

from oduflow.locking import LockManager
from oduflow.settings import Settings, TeamSettings
from oduflow.web_ui import _AUTH_COOKIE, _annotate_acp_auth_error, mount_web_ui

_PW = "s3cret"
_BRANCH = "feature-x"
_INFO_URL = f"/api/environments/{_BRANCH}/agent-acp/info?type=claude"
_SESSION_URL = f"/api/environments/{_BRANCH}/agent-acp/session"
_ATTACHMENTS_URL = f"/api/environments/{_BRANCH}/agent-acp/attachments"


def _app(tmp_path, *, agent_enabled: bool = False) -> Starlette:
    team = TeamSettings(
        team_id="1",
        data_dir=str(tmp_path / "team-1"),
        ui_password=_PW,
        agent_enabled=agent_enabled,
    )
    settings = Settings(base_data_dir=str(tmp_path), teams={"1": team})
    app = Starlette()
    mount_web_ui(app, lambda: settings, LockManager())
    return app


def _client(tmp_path, *, agent_enabled: bool = False) -> TestClient:
    client = TestClient(_app(tmp_path, agent_enabled=agent_enabled))
    assert (
        client.post(
            "/login", data={"password": _PW}, follow_redirects=False
        ).status_code
        == 303
    )
    return client


def _post(client: TestClient, session_id: str, title: str | None = None):
    body = {"type": "claude", "session_id": session_id}
    if title is not None:
        body["title"] = title
    return client.post(_SESSION_URL, json=body)


@pytest.mark.parametrize("endpoint", ["terminal", "agent", "agent-acp"])
def test_websockets_reject_invalid_branch_before_resource_lookup(tmp_path, endpoint):
    client = _client(tmp_path)
    with client.websocket_connect(
        f"/api/environments/bad%5Cbranch/{endpoint}"
    ) as websocket:
        message = websocket.receive_text()
        close = websocket.receive()

    assert "Invalid environment name" in message
    assert close["type"] == "websocket.close"
    assert close["code"] == 1008


def test_info_starts_with_empty_history(tmp_path):
    response = _client(tmp_path).get(_INFO_URL)

    assert response.status_code == 200
    assert response.json()["session_id"] is None
    assert response.json()["history"] == []
    assert response.json()["attachment_limits"] == {
        "max_file_bytes": 25 * 1024 * 1024,
        "max_files": 5,
    }


def test_attachment_upload_streams_raw_body_and_returns_descriptor(tmp_path):
    client = _client(tmp_path, agent_enabled=True)
    captured = {}
    descriptor = {
        "id": "a" * 32,
        "name": "notes.txt",
        "path": "/workspace/.oduflow-uploads/feature-x/a/notes.txt",
        "uri": "file:///workspace/.oduflow-uploads/feature-x/a/notes.txt",
        "mimeType": "text/plain",
        "size": 5,
    }

    def _store(*args):
        captured["body"] = args[5].read()
        return descriptor

    with patch(
        "oduflow.web_ui.agent_uploads.store_attachment", side_effect=_store
    ) as store:
        response = client.post(
            _ATTACHMENTS_URL + "?name=notes.txt",
            content=b"hello",
            headers={"Content-Type": "text/plain"},
        )

    assert response.status_code == 200
    assert response.json() == {"ok": True, "attachment": descriptor}
    args = store.call_args.args
    assert args[2:5] == (_BRANCH, "notes.txt", "text/plain")
    assert captured["body"] == b"hello"
    assert args[6] == 5


def test_attachment_upload_rejects_oversized_body_before_storage(tmp_path, monkeypatch):
    client = _client(tmp_path, agent_enabled=True)
    monkeypatch.setattr("oduflow.web_ui.agent_uploads.MAX_FILE_BYTES", 3)

    with patch("oduflow.web_ui.agent_uploads.store_attachment") as store:
        response = client.post(_ATTACHMENTS_URL + "?name=large.bin", content=b"four")

    assert response.status_code == 413
    assert "no larger" in response.json()["error"]
    store.assert_not_called()


def test_attachment_upload_handles_client_disconnect_quietly(tmp_path):
    """An aborted XHR (Remove during upload) must not raise through ASGI."""
    app = _app(tmp_path, agent_enabled=True)
    client = TestClient(app)
    client.post("/login", data={"password": _PW}, follow_redirects=False)
    cookie = client.cookies.get(_AUTH_COOKIE)
    messages = [
        {"type": "http.request", "body": b"partial", "more_body": True},
        {"type": "http.disconnect"},
    ]
    sent = []

    async def receive():
        return messages.pop(0)

    async def send(message):
        sent.append(message)

    scope = {
        "type": "http",
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "server": ("testserver", 80),
        "client": ("testclient", 123),
        "root_path": "",
        "path": f"/api/environments/{_BRANCH}/agent-acp/attachments",
        "query_string": b"name=notes.txt",
        "headers": [(b"cookie", f"{_AUTH_COOKIE}={cookie}".encode())],
    }

    with patch("oduflow.web_ui.agent_uploads.store_attachment") as store:
        asyncio.run(app(scope, receive, send))

    assert sent[0]["status"] == 400
    store.assert_not_called()


def test_attachment_delete_calls_scoped_storage(tmp_path):
    client = _client(tmp_path, agent_enabled=True)

    with patch("oduflow.web_ui.agent_uploads.delete_attachment") as delete:
        response = client.delete(_ATTACHMENTS_URL + "/" + "a" * 32)

    assert response.status_code == 200
    assert response.json() == {"ok": True}
    assert delete.call_args.args[2:] == (_BRANCH, "a" * 32)


def test_post_adds_session_and_title_is_first_write_wins(tmp_path):
    client = _client(tmp_path)

    response = _post(client, "sid-1")
    assert response.status_code == 200
    assert response.json()["session_id"] == "sid-1"
    assert response.json()["history"][0]["title"] is None

    response = _post(client, "sid-1", "  Fix invoice totals  ")
    assert response.json()["history"][0]["title"] == "Fix invoice totals"

    response = _post(client, "sid-1", "Replacement")
    assert response.json()["history"][0]["title"] == "Fix invoice totals"


def test_empty_session_clears_current_but_preserves_history(tmp_path):
    client = _client(tmp_path)
    _post(client, "sid-1", "First")

    response = _post(client, "")

    assert response.status_code == 200
    assert response.json()["session_id"] is None
    assert [entry["session_id"] for entry in response.json()["history"]] == ["sid-1"]


def test_posting_historical_session_selects_and_moves_it_to_front(tmp_path):
    client = _client(tmp_path)
    _post(client, "sid-1", "First")
    _post(client, "sid-2", "Second")

    response = _post(client, "sid-1")

    assert response.status_code == 200
    assert response.json()["session_id"] == "sid-1"
    assert [entry["session_id"] for entry in response.json()["history"]] == [
        "sid-1",
        "sid-2",
    ]


@pytest.mark.parametrize(
    ("auth_mode", "expected"),
    [
        ("setup_token", "CLAUDE_CODE_OAUTH_TOKEN"),
        ("api_key", "ANTHROPIC_API_KEY"),
        ("interactive", "run `/login`"),
    ],
)
def test_claude_auth_errors_get_mode_specific_guidance(auth_mode, expected):
    original = {
        "jsonrpc": "2.0",
        "id": 3,
        "error": {
            "code": -32603,
            "message": (
                "Internal error: Failed to authenticate. API Error: 401 "
                '{"type":"error","error":{"type":"authentication_error",'
                '"message":"Invalid bearer token"}}'
            ),
            "data": {"provider": "anthropic"},
        },
    }

    annotated = json.loads(
        _annotate_acp_auth_error(json.dumps(original), "claude", auth_mode, "7")
    )

    assert annotated["id"] == original["id"]
    assert annotated["error"]["code"] == original["error"]["code"]
    assert annotated["error"]["data"] == original["error"]["data"]
    assert annotated["error"]["message"].startswith(original["error"]["message"])
    assert "Oduflow authentication guidance:" in annotated["error"]["message"]
    assert expected in annotated["error"]["message"]


@pytest.mark.parametrize(
    "provider_message",
    [
        "Invalid API key · Fix external API key",
        "OAuth token has expired. Please obtain a new token.",
        "OAuth token revoked",
    ],
)
def test_specific_claude_auth_errors_are_recognized(provider_message):
    frame = json.dumps(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "error": {"code": -32603, "message": provider_message},
        }
    )

    annotated = _annotate_acp_auth_error(frame, "claude", "setup_token", "1")

    assert (
        "Oduflow authentication guidance:" in json.loads(annotated)["error"]["message"]
    )


def test_opencode_auth_error_gets_recovery_guidance():
    original = {
        "jsonrpc": "2.0",
        "id": 4,
        "error": {
            "code": -32000,
            "message": "Authentication required: provider authentication required",
            "data": {"providerId": "anthropic"},
        },
    }

    annotated = json.loads(
        _annotate_acp_auth_error(json.dumps(original), "opencode", "interactive", "9")
    )

    assert annotated["error"]["code"] == original["error"]["code"]
    assert annotated["error"]["data"] == original["error"]["data"]
    assert "Oduflow OpenCode authentication guidance:" in annotated["error"]["message"]
    assert "opencode auth login" in annotated["error"]["message"]
    assert "[team.9.agent_env]" in annotated["error"]["message"]


@pytest.mark.parametrize(
    ("frame", "agent_type"),
    [
        ("not-json", "claude"),
        (
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "error": {
                        "code": -32603,
                        "message": "You've hit your monthly spend limit",
                    },
                }
            ),
            "claude",
        ),
        (
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "error": {"code": 401, "message": "Repository access denied"},
                }
            ),
            "claude",
        ),
        (
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "error": {"code": -32603, "message": "Invalid bearer token"},
                }
            ),
            "codex",
        ),
        (
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "error": {"code": -32603, "message": "Model quota exhausted"},
                }
            ),
            "opencode",
        ),
    ],
)
def test_unrecognized_auth_failures_are_unchanged(frame, agent_type):
    assert _annotate_acp_auth_error(frame, agent_type, "setup_token", "1") == frame


def test_auth_guidance_is_not_appended_twice():
    frame = json.dumps(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "error": {"code": -32603, "message": "Invalid bearer token"},
        }
    )
    once = _annotate_acp_auth_error(frame, "claude", "setup_token", "1")

    assert _annotate_acp_auth_error(once, "claude", "setup_token", "1") == once

    opencode = json.dumps(
        {
            "jsonrpc": "2.0",
            "id": 2,
            "error": {"code": -32000, "message": "provider authentication required"},
        }
    )
    once = _annotate_acp_auth_error(opencode, "opencode", "interactive", "1")
    assert _annotate_acp_auth_error(once, "opencode", "interactive", "1") == once
