"""Credentials stay out of the server log.

Two writers are covered: ``handle_errors`` (a preview of every MCP tool result)
and uvicorn's access log (the request line, query string included).
"""

from __future__ import annotations

import logging
from unittest.mock import patch

import pytest
from fastmcp.exceptions import ToolError

from oduflow import log_redaction
from oduflow.log_redaction import (
    REDACTED,
    AccessLogQueryFilter,
    collecting,
    redact_query,
    scrub,
    secret,
)
from oduflow.settings import Settings, TeamSettings

# -- The marking mechanism --


def test_secret_returns_its_value_unchanged():
    assert secret("s3cr3t") == "s3cr3t"
    with collecting():
        assert secret("s3cr3t") == "s3cr3t"


def test_scrub_masks_what_was_marked_during_the_call():
    with collecting():
        text = f"value: {secret('abc123def')}; again abc123def; other xyz"
        assert scrub(text) == f"value: {REDACTED}; again {REDACTED}; other xyz"


def test_marks_do_not_outlive_or_leak_between_calls():
    with collecting():
        secret("first-call-secret")
    # Outside a call nothing is remembered...
    assert scrub("first-call-secret") == "first-call-secret"
    # ...and the next call starts clean.
    with collecting():
        assert scrub("first-call-secret") == "first-call-secret"


def test_secret_outside_a_call_is_a_no_op():
    assert secret("free-standing") == "free-standing"
    assert scrub("free-standing") == "free-standing"


def test_empty_secret_is_ignored():
    with collecting():
        secret("")
        assert scrub("nothing to hide") == "nothing to hide"


def test_longest_secret_is_replaced_first():
    # A DATABASE_URL embeds the password: masking the password first would
    # leave the rest of the URL (user, host) dangling around the mask.
    with collecting():
        secret("pw")
        secret("postgresql://svc:pw@db:5432/x")
        assert scrub("URL postgresql://svc:pw@db:5432/x, password pw") == (
            f"URL {REDACTED}, password {REDACTED}"
        )


# -- handle_errors: tool result previews --

TEAM = TeamSettings(team_id="1", data_dir="/tmp/flow-test")
SETTINGS = Settings(
    base_data_dir="/tmp/flow-test",
    db_user="odoo",
    db_password="odoo",
    teams={"1": TEAM},
)


@pytest.fixture
def server():
    import oduflow.server

    oduflow.server._settings = SETTINGS
    with patch("oduflow.server._resolve_team", return_value=TEAM):
        yield oduflow.server
    oduflow.server._settings = None


def _call(tool_name: str, **kwargs):
    """Invoke a registered MCP tool (its handle_errors wrapper included)."""
    import asyncio

    from oduflow.server import mcp

    return asyncio.run(mcp._tool_manager._tools[tool_name].fn(**kwargs))


def _run_handled(fn, *args):
    import asyncio

    from oduflow.server import handle_errors

    return asyncio.run(handle_errors(fn)(*args))


def test_preview_is_scrubbed_before_it_is_truncated(caplog):
    """A credential straddling the 200-character cut must not leak its head."""
    credential = "K" * 40

    def straddling_tool() -> str:
        return "x" * 180 + log_redaction.secret(credential) + " tail"

    with caplog.at_level(logging.INFO, logger="oduflow"):
        assert credential in _run_handled(straddling_tool)
    assert "[straddling_tool] ->" in caplog.text
    assert "KKKK" not in caplog.text


def test_error_messages_are_scrubbed_too(caplog):
    from oduflow.errors import ConflictError

    def failing_tool() -> str:
        token = log_redaction.secret("tok-123456789")
        raise ConflictError(f"could not store {token}")

    with (
        caplog.at_level(logging.INFO, logger="oduflow"),
        pytest.raises(ToolError, match="tok-123456789"),  # the caller still sees it
    ):
        _run_handled(failing_tool)
    assert "[failing_tool] Error:" in caplog.text
    assert "tok-123456789" not in caplog.text


def test_non_string_results_are_scrubbed(caplog):
    def structured_tool() -> dict:
        return {"url": "https://h/x?token=" + log_redaction.secret("tok-abcdefgh")}

    with caplog.at_level(logging.INFO, logger="oduflow"):
        result = _run_handled(structured_tool)
    assert result["url"].endswith("tok-abcdefgh")
    assert "tok-abcdefgh" not in caplog.text
    assert REDACTED in caplog.text


def test_ordinary_results_are_logged_as_before(caplog):
    def plain_tool() -> str:
        return "Environment restarted successfully!"

    with caplog.at_level(logging.INFO, logger="oduflow"):
        _run_handled(plain_tool)
    assert "[plain_tool] -> Environment restarted successfully!" in caplog.text


@patch("oduflow.docker_ops.env_ops.ensure_running", return_value=False)
@patch("oduflow.docker_ops.odoo_ops.connect_as_user")
def test_connect_as_user_session_id_is_not_logged(
    mock_connect, _ensure, server, caplog
):
    sid = "".join(f"{i:02x}" for i in range(40))  # 80 chars, no repeats
    mock_connect.return_value = {
        "sid": sid,
        "login": "jane@acme.com",
        "uid": "7",
        "base_url": "https://feature.example.com",
        "cookie_domain": "feature.example.com",
        "url": "https://feature.example.com/web",
        "expires_at": "2026-07-16T00:00:00Z",
    }
    with caplog.at_level(logging.INFO, logger="oduflow"):
        result = _call("connect_as_user", env_name="feature", user="jane@acme.com")
    assert sid in result  # the caller gets the real cookie
    assert "[connect_as_user] -> Connected as jane@acme.com" in caplog.text
    # Not even its head: the preview used to cut the session id mid-way.
    assert sid[:12] not in caplog.text


@patch("oduflow.docker_ops.odoo_rpc.invalidate_sessions")
@patch("oduflow.docker_ops.env_ops.ensure_running", return_value=False)
@patch("oduflow.docker_ops.odoo_ops.reset_admin_password")
def test_reset_admin_password_is_not_logged(
    mock_reset, _ensure, _invalidate, server, caplog
):
    mock_reset.return_value = {"status": "ok", "login": "admin"}
    with caplog.at_level(logging.INFO, logger="oduflow"):
        result = _call(
            "reset_admin_password", env_name="feature", new_password="Tr0ub4dor&3"
        )
    assert "Tr0ub4dor&3" in result
    assert "[reset_admin_password] ->" in caplog.text
    assert "Tr0ub4dor&3" not in caplog.text


_DB = {
    "name": "events",
    "status": "ready",
    "cluster": "dev",
    "protected": False,
    "host": "oduflow-db",
    "port": 5432,
    "database": "oduflow_service_1_events",
    "username": "svc_1_events",
    # A password whose URL form differs from its raw form.
    "password": "p@ss/w0rd+Zq9",
    "url": "postgresql://svc_1_events:p%40ss%2Fw0rd%2BZq9@oduflow-db:5432/"
    "oduflow_service_1_events",
}


@pytest.mark.parametrize(
    ("tool", "op"),
    [
        ("get_service_database", "get_database"),
        ("create_service_database", "create_database"),
        ("rotate_service_database_password", "rotate_password"),
    ],
)
def test_service_database_credentials_are_not_logged(tool, op, server, caplog):
    with (
        patch(f"oduflow.docker_ops.service_database_ops.{op}", return_value=dict(_DB)),
        caplog.at_level(logging.INFO, logger="oduflow"),
    ):
        result = _call(tool, name="events")
    assert "Password: p@ss/w0rd+Zq9" in result
    assert _DB["url"] in result
    assert f"[{tool}] ->" in caplog.text
    # Neither the raw password nor its percent-encoded form inside the URL.
    # Not even its head (the 200-character preview ends inside it).
    assert "p@ss" not in caplog.text
    assert "p%40ss" not in caplog.text


def test_artifact_download_token_is_not_logged(server, caplog):
    """The one-time download URL every artifact-producing tool builds."""

    def exporting_tool() -> str:
        return "Download: " + server._artifact_url(SETTINGS, TEAM, "tok-ZYXWVUT")

    server._web_bind = ("0.0.0.0", 8000)
    try:
        with caplog.at_level(logging.INFO, logger="oduflow"):
            assert _run_handled(exporting_tool).endswith("token=tok-ZYXWVUT")
    finally:
        server._web_bind = None
    assert f"/oduflow-artifact?token={REDACTED}" in caplog.text
    assert "tok-ZYXWVUT" not in caplog.text


# Literal env-var values are routinely API keys. Each case is a tool whose
# result lists them inside the logged preview.
_ENV_VARS = {"MEILI_MASTER_KEY": "mk-9f8e7d6c5b4a", "WORKERS": "2"}
_ENV_LISTING_TOOLS = [
    (
        "list_services",
        "oduflow.docker_ops.service_ops.list_services",
        [
            {
                "name": "search",
                "container_name": "oduflow-1-svc-search",
                "image": "getmeili/meilisearch:v1",
                "status": "running",
                "port": 7700,
                "env_vars": _ENV_VARS,
            }
        ],
        {},
    ),
    (
        "get_service_info",
        "oduflow.docker_ops.service_ops.get_service_info",
        {
            "name": "search",
            "container_name": "oduflow-1-svc-search",
            "status": "running",
            "image": "getmeili/meilisearch:v1",
            "port": 7700,
            "env_vars": _ENV_VARS,
        },
        {"name": "search"},
    ),
    (
        "list_service_presets",
        "oduflow.docker_ops.service_presets.list_presets",
        [
            {
                "name": "search",
                "image": "getmeili/meilisearch:v1",
                "port": 7700,
                "env_vars": _ENV_VARS,
            }
        ],
        {},
    ),
    (
        "get_environment_info",
        "oduflow.docker_ops.env_ops.get_environment_info",
        {
            "db_name": "oduflow_1_main",
            "all_running": True,
            "env_vars": _ENV_VARS,
            "odoo": {"status": "running", "running": True},
            "db": {"status": "running", "running": True},
        },
        {"env_name": "main"},
    ),
    (
        "update_environment",
        "oduflow.docker_ops.env_ops.update_environment",
        {
            "url": "http://localhost:50000",
            "odoo_container": "oduflow-main-odoo",
            "database": "oduflow_1_main",
            "workspace": "/tmp/ws",
            "image": "odoo:17.0",
            "env_vars": _ENV_VARS,
        },
        {"env_name": "main", "env_vars": "MEILI_MASTER_KEY=mk-9f8e7d6c5b4a,WORKERS=2"},
    ),
]


@pytest.mark.parametrize(
    ("tool", "target", "data", "kwargs"),
    _ENV_LISTING_TOOLS,
    ids=[case[0] for case in _ENV_LISTING_TOOLS],
)
def test_env_var_values_are_not_logged(tool, target, data, kwargs, server, caplog):
    with (
        patch(target, return_value=data),
        caplog.at_level(logging.INFO, logger="oduflow"),
    ):
        result = _call(tool, **kwargs)
    # The caller reads the values back as before...
    assert "MEILI_MASTER_KEY=mk-9f8e7d6c5b4a, WORKERS=2" in result
    # ...while the log line shows the listing masked (the 200-character
    # preview may end inside the mask itself).
    line = next(r.getMessage() for r in caplog.records if f"[{tool}] ->" in r.message)
    assert "mk-9f8e7d6c5b4a" not in line
    assert "MEILI_MASTER_KEY" not in line
    assert REDACTED[:4] in line


def test_env_var_listing_is_masked_whole_not_value_by_value(server, caplog):
    """Scrubbing is substring replacement: marking the value "2" by itself
    would also mask the 2 in an unrelated port, image tag or timestamp."""

    def listing_tool() -> str:
        return f"Port: 6222\nEnv: {server._env_pairs({'WORKERS': '2', 'KEY': 'k9'})}"

    with caplog.at_level(logging.INFO, logger="oduflow"):
        assert _run_handled(listing_tool) == "Port: 6222\nEnv: WORKERS=2, KEY=k9"
    assert f"[listing_tool] -> Port: 6222\nEnv: {REDACTED}" in caplog.text


# -- The access log --


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        (
            "/oduflow-connect?token=AbC-123_xyz",
            f"/oduflow-connect?token={REDACTED}",
        ),
        (
            "/oduflow-artifact?token=AbC-123_xyz",
            f"/oduflow-artifact?token={REDACTED}",
        ),
        ("/env/18.0?key=sharekey123", f"/env/18.0?key={REDACTED}"),
        (
            "/x?a=1&token=T0K&b=2&KEY=K3Y",
            f"/x?a=1&token={REDACTED}&b=2&KEY={REDACTED}",
        ),
        # A percent-encoded parameter name is still that parameter to the
        # server; the name is logged as it arrived, the value masked.
        ("/oduflow-connect?tok%65n=AbC-123", f"/oduflow-connect?tok%65n={REDACTED}"),
        (
            "/x?a=1&%74oken=T0K&k%45y=K3Y&b=2",
            f"/x?a=1&%74oken={REDACTED}&k%45y={REDACTED}&b=2",
        ),
        ("/x?acc%65ss%5Ftoken=T0K#frag", f"/x?acc%65ss%5Ftoken={REDACTED}#frag"),
        # Not credentials: left readable.
        ("/api/environments/18.0/logs?n=200", "/api/environments/18.0/logs?n=200"),
        ("/api/x?tokenizer=1&monkey=2", "/api/x?tokenizer=1&monkey=2"),
        ("/static/chat.js?v=12", "/static/chat.js?v=12"),
        # Decoded once, as the server does: this name is "tok%65n", not "token".
        ("/x?tok%2565n=plain", "/x?tok%2565n=plain"),
    ],
)
def test_redact_query(line, expected):
    assert redact_query(line) == expected


def test_encoded_parameter_name_is_honoured_by_the_landing_and_masked(tmp_path):
    """Why redact_query decodes names: the landing accepts ``tok%65n=`` as
    ``token=``, so the access-log line of that request carries a live token."""
    from starlette.applications import Starlette
    from starlette.testclient import TestClient

    from oduflow import connect_tokens
    from oduflow.locking import LockManager
    from oduflow.web_ui import mount_web_ui

    host = "180.dev.example.com"
    settings = Settings(
        routing_mode="traefik",
        routing_tls=False,
        base_data_dir=str(tmp_path),
        teams={"1": TeamSettings(team_id="1", hostname="dev.example.com")},
    )
    app = Starlette()
    mount_web_ui(app, lambda: settings, LockManager())
    token = connect_tokens.issue_link(
        host,
        connect_tokens.LinkGrant(team_id="1", env_name="18.0", user="", login="admin"),
    )
    target = f"/oduflow-connect?tok%65n={token}"

    resp = TestClient(app).get(target, headers={"host": host})
    assert resp.status_code == 200  # recognised as a valid link token
    assert 'name="token"' in resp.text

    record = _access_record(target)
    AccessLogQueryFilter().filter(record)
    assert token not in record.getMessage()
    assert f"/oduflow-connect?tok%65n={REDACTED}" in record.getMessage()


def _access_record(path: str) -> logging.LogRecord:
    # Exactly what uvicorn's protocol implementations emit.
    return logging.LogRecord(
        "uvicorn.access",
        logging.INFO,
        __file__,
        0,
        '%s - "%s %s HTTP/%s" %d',
        ("10.0.0.7:51234", "GET", path, "1.1", 200),
        None,
    )


def test_access_log_filter_masks_the_token_for_every_formatter():
    from uvicorn.logging import AccessFormatter

    record = _access_record("/oduflow-connect?token=AbC-123_xyz")
    assert AccessLogQueryFilter().filter(record) is True
    plain = record.getMessage()
    assert "AbC-123_xyz" not in plain
    assert f'"GET /oduflow-connect?token={REDACTED} HTTP/1.1" 200' in plain
    # uvicorn's own formatter rebuilds the line from record.args.
    formatted = AccessFormatter(
        '%(client_addr)s - "%(request_line)s" %(status_code)s', use_colors=False
    ).format(record)
    assert "AbC-123_xyz" not in formatted
    assert f"/oduflow-connect?token={REDACTED}" in formatted


def test_access_log_filter_survives_uvicorn_logging_setup():
    """The filter is installed before uvicorn configures logging; uvicorn's
    dictConfig must not drop it."""
    import io

    import uvicorn

    access_logger = logging.getLogger("uvicorn.access")
    before = list(access_logger.filters)
    try:
        log_redaction.install_access_log_filter()
        log_redaction.install_access_log_filter()  # idempotent
        installed = [
            f for f in access_logger.filters if isinstance(f, AccessLogQueryFilter)
        ]
        assert len(installed) == 1

        async def app(scope, receive, send):  # pragma: no cover - never served
            pass

        uvicorn.Config(app)  # runs uvicorn's dictConfig, as server startup does
        assert installed[0] in access_logger.filters

        stream = io.StringIO()
        handler = logging.StreamHandler(stream)
        access_logger.addHandler(handler)
        try:
            access_logger.info(
                '%s - "%s %s HTTP/%s" %d',
                "10.0.0.7:51234",
                "GET",
                "/oduflow-connect?token=AbC-123_xyz",
                "1.1",
                200,
            )
        finally:
            access_logger.removeHandler(handler)
        assert "AbC-123_xyz" not in stream.getvalue()
        assert f"token={REDACTED}" in stream.getvalue()
    finally:
        access_logger.filters[:] = before


def test_http_startup_installs_the_access_log_filter():
    """Guards the wiring: the filter is only worth anything if _start_http adds it."""
    import inspect

    import oduflow.server

    assert "install_access_log_filter()" in inspect.getsource(
        oduflow.server._start_http
    )
