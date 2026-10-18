"""White label under a custom license (oduflow.branding)."""

from __future__ import annotations

import base64
import tempfile
from datetime import datetime, timedelta, timezone

import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

from oduflow import branding, git_ops, licensing
from oduflow.locking import LockManager
from oduflow.settings import Settings, TeamSettings
from oduflow.web_ui import mount_web_ui
from tests.test_licensing import TestVerifyLicenseText

_PW = "s3cret"
_DATA_DIR = tempfile.mkdtemp(prefix="oduflow-branding-test-")
_PNG = b"\x89PNG\r\n\x1a\ncustom-logo"


@pytest.fixture(autouse=True)
def _reset_branding():
    yield
    branding._get_settings = None
    branding._cached = None


@pytest.fixture
def sign(monkeypatch):
    private, pem = TestVerifyLicenseText._keypair()
    monkeypatch.setattr(licensing, "_PUBLIC_KEY_PEM", pem)
    return lambda payload: TestVerifyLicenseText._sign(private, payload)


def _custom(**changes):
    return {
        "version": 4,
        "type": "custom",
        "plan": "custom",
        "license_id": "11111111-2222-4333-8444-555555555555",
        "name": "Acme Cloud",
        "email": "ops@acme.example",
        "brand_name": "AcmeFlow",
        "domains": ["acme-cloud.com"],
        "valid_from": "2026-01-01T00:00:00Z",
        "expires": "2099-01-01T00:00:00Z",
        **changes,
    }


def _settings(tmp_path, hostname="dev.acme-cloud.com") -> Settings:
    return Settings(
        base_data_dir=_DATA_DIR,
        etc_dir=str(tmp_path),
        teams={"1": TeamSettings(team_id="1", hostname=hostname, ui_password=_PW)},
    )


def _install(tmp_path, sign, **changes) -> None:
    (tmp_path / "license.key").write_text(sign(_custom(**changes)))


def test_no_custom_license_keeps_the_stock_brand(tmp_path, sign):
    (tmp_path / "license.key").write_text(
        sign({"type": "business", "name": "Acme", "email": "a@b.example"})
    )
    brand = branding.compute(_settings(tmp_path))
    assert brand == branding.Brand()
    assert brand.rebrand("Restart Oduflow.") == "Restart Oduflow."


def test_custom_license_on_a_licensed_domain_turns_white_label_on(tmp_path, sign):
    _install(tmp_path, sign)
    brand = branding.compute(_settings(tmp_path))
    assert brand.white_label and brand.name == "AcmeFlow" and brand.slug == "acmeflow"
    assert brand.rebrand("Restart Oduflow. See oduflow.toml.") == (
        "Restart AcmeFlow. See oduflow.toml."
    )


@pytest.mark.parametrize(
    "hostname", ["localhost", "acme-cloud.com.evil.example", "notacme-cloud.com"]
)
def test_a_key_copied_to_another_domain_brands_nothing(tmp_path, sign, hostname):
    _install(tmp_path, sign)
    brand = branding.compute(_settings(tmp_path, hostname=hostname))
    assert not brand.white_label and brand.name == "Oduflow"
    assert "not covered" in brand.reason


def test_every_team_hostname_must_be_covered(tmp_path, sign):
    _install(tmp_path, sign)
    settings = _settings(tmp_path)
    settings.teams["2"] = TeamSettings(team_id="2", hostname="other.example")
    assert not branding.compute(settings).white_label


def test_white_label_survives_the_grace_period_only(tmp_path, sign):
    expires = datetime(2030, 1, 1, tzinfo=timezone.utc)
    _install(tmp_path, sign, expires=expires.isoformat())
    settings = _settings(tmp_path)
    inside = expires + timedelta(days=branding.GRACE_DAYS - 1)
    after = expires + timedelta(days=branding.GRACE_DAYS + 1)
    assert branding.compute(settings, now=inside).white_label
    late = branding.compute(settings, now=after)
    assert not late.white_label and "expired" in late.reason


def test_icon_falls_back_to_the_logo(tmp_path, sign):
    _install(tmp_path, sign)
    (tmp_path / "branding").mkdir()
    (tmp_path / "branding" / "logo.png").write_bytes(_PNG)
    brand = branding.compute(_settings(tmp_path))
    assert brand.logo_path.endswith("logo.png") and brand.icon_path == brand.logo_path


def test_new_ssh_keys_carry_the_brand(tmp_path, sign):
    _install(tmp_path, sign)
    settings = _settings(tmp_path)
    assert git_ops.ssh_key_comment("1") == "oduflow-1"
    branding.configure(lambda: settings)
    assert git_ops.ssh_key_comment("1") == "acmeflow-1"


def _client(settings: Settings) -> TestClient:
    app = Starlette()
    mount_web_ui(app, lambda: settings, LockManager())
    client = TestClient(app, base_url="https://dev.acme-cloud.com")
    client.post("/login", data={"password": _PW})
    return client


_VENDOR_MARKERS = (
    "a product by",
    "docs.oduflow.dev",
    'id="license-modal"',
    'id="feedback-modal"',
    'id="version-modal"',
    'id="license-badge"',
    'onclick="openVersionModal()"',
    "oduflow.dev/odoo-sh-import-notes",
)


def test_white_label_dashboard_has_the_brand_and_no_vendor_surfaces(tmp_path, sign):
    _install(tmp_path, sign)
    client = _client(_settings(tmp_path))
    page = client.get("/").text
    assert "<title>AcmeFlow</title>" in page
    assert 'data-white-label="1"' in page
    assert '<span class="brand-version" id="brand-version">v' in page
    assert "IF-STOCK" not in page and "IF-WHITE-LABEL" not in page
    for marker in _VENDOR_MARKERS:
        assert marker not in page, marker
    assert client.get("/api/license").status_code == 404
    assert client.post("/api/license/refresh").status_code == 404


def test_stock_dashboard_keeps_vendor_surfaces(tmp_path):
    client = _client(_settings(tmp_path))
    page = client.get("/").text
    assert "<title>Oduflow</title>" in page
    assert 'data-white-label=""' in page
    for marker in _VENDOR_MARKERS:
        assert marker in page, marker
    assert "IF-STOCK" not in page and "IF-WHITE-LABEL" not in page
    assert client.get("/api/license").status_code == 200


def test_admin_console_login_follows_the_brand(tmp_path, sign):
    from oduflow import admin_ui

    page = admin_ui._render_login()
    assert "<title>Oduflow — Server settings</title>" in page
    assert "__" not in page.split("<body", 1)[1].split("<script", 1)[0]

    _install(tmp_path, sign)
    branding.configure(lambda: _settings(tmp_path))
    page = admin_ui._render_login()
    assert "<title>AcmeFlow — Server settings</title>" in page
    assert 'alt="AcmeFlow logo"' in page
    assert "__BRAND_NAME__" not in page and "__LOGO_CLASS__" not in page


def test_white_label_images_come_from_the_config_dir(tmp_path, sign):
    _install(tmp_path, sign)
    (tmp_path / "branding").mkdir()
    (tmp_path / "branding" / "logo.png").write_bytes(_PNG)
    client = _client(_settings(tmp_path))
    login = TestClient(client.app, base_url="https://dev.acme-cloud.com").get("/login")
    assert "AcmeFlow — Sign in" in login.text and "custom-logo" in login.text
    for path in (
        "/logo.png",
        "/favicon.ico",
        "/static/icon.png",
        "/static/apple-icon.png",
    ):
        response = client.get(path)
        assert response.content == _PNG, path
    assert 'class="custom-logo"' in client.get("/").text


def test_handle_errors_rebrands_results_and_errors(tmp_path, sign):
    import asyncio

    from fastmcp.exceptions import ToolError

    from oduflow.errors import FlowError
    from oduflow.server import handle_errors

    _install(tmp_path, sign)
    settings = _settings(tmp_path)
    branding.configure(lambda: settings)

    @handle_errors
    def ok() -> str:
        return "Oduflow created the environment."

    @handle_errors
    def flow_error() -> str:
        raise FlowError("Restart Oduflow.")

    @handle_errors
    def tool_error() -> str:
        raise ToolError("Ask the Oduflow operator.")

    assert asyncio.run(ok()) == "AcmeFlow created the environment."
    with pytest.raises(ToolError, match="Restart AcmeFlow"):
        asyncio.run(flow_error())
    with pytest.raises(ToolError, match="Ask the AcmeFlow operator"):
        asyncio.run(tool_error())


def test_rest_errors_are_rebranded(tmp_path, sign, monkeypatch):
    """Dashboard errors are rebranded at the one place they pass through.

    _public_flow_error is to the REST and WebSocket surface what handle_errors
    is to MCP, so messages raised anywhere in the codebase stay written as
    "Oduflow".
    """
    from oduflow import web_ui
    from oduflow.errors import ProtectedError

    def _protected(*args, **kwargs):
        raise ProtectedError("Unprotect it in the Oduflow dashboard first.")

    _install(tmp_path, sign)
    client = _client(_settings(tmp_path))
    monkeypatch.setattr(web_ui.env_ops, "list_environments", _protected)

    response = client.get("/api/environments")

    assert response.status_code == 400
    assert response.json()["error"] == "Unprotect it in the AcmeFlow dashboard first."


def test_terminal_errors_are_rebranded(tmp_path, sign):
    from oduflow import web_ui
    from oduflow.errors import FlowError

    _install(tmp_path, sign)
    branding.configure(lambda: _settings(tmp_path))

    line = web_ui._term_error(FlowError("Restart Oduflow and retry."))

    assert "Restart AcmeFlow and retry." in line
    assert "Oduflow" not in line


@pytest.mark.parametrize(
    "agent_type,code,provider_error,marker",
    [
        (
            "claude",
            -32603,
            "401 failed to authenticate",
            "AcmeFlow authentication guidance:",
        ),
        (
            "opencode",
            -32603,
            "provider authentication required",
            "AcmeFlow OpenCode authentication guidance:",
        ),
        (
            "codex",
            -32000,
            "Authentication required",
            "AcmeFlow Codex authentication guidance:",
        ),
    ],
)
def test_acp_auth_guidance_is_rebranded_and_appended_once(
    tmp_path, sign, agent_type, code, provider_error, marker
):
    """The marker is both what the client reads and the idempotency check.

    Rebranding the guidance but not the marker would stop matching the frame it
    already annotated, appending the advice again on every error frame.
    """
    import json

    from oduflow import web_ui

    _install(tmp_path, sign)
    branding.configure(lambda: _settings(tmp_path))
    frame = json.dumps({"error": {"code": code, "message": provider_error}})

    once = web_ui._annotate_acp_auth_error(frame, agent_type, "setup_token", "1")
    twice = web_ui._annotate_acp_auth_error(once, agent_type, "setup_token", "1")

    assert marker in once
    assert "Oduflow" not in once
    assert twice == once


def test_seed_commits_carry_the_brand(tmp_path, sign):
    # The seed commit of a local extra repo lands in the client's own history.
    from oduflow import extra_addons

    _install(tmp_path, sign)
    assert extra_addons._seed_commit_env()["GIT_AUTHOR_NAME"] == "Oduflow"

    branding.configure(lambda: _settings(tmp_path))
    seed_env = extra_addons._seed_commit_env()

    assert seed_env["GIT_AUTHOR_NAME"] == "AcmeFlow"
    assert seed_env["GIT_COMMITTER_EMAIL"] == "import@acmeflow.local"


def test_mcp_surface_is_rebranded_and_vendor_tools_hidden(tmp_path, sign):
    from oduflow import server

    _install(tmp_path, sign)
    settings = _settings(tmp_path)
    branding.configure(lambda: settings)
    tools = server.mcp._tool_manager._tools
    saved = (
        server.mcp._mcp_server.name,
        server.mcp.instructions,
        {key: tool.description for key, tool in tools.items()},
        tools["report_issue"].enabled,
    )
    try:
        server._apply_branding()
        assert server.mcp.name == "AcmeFlow"
        assert "Oduflow" not in (server.mcp.instructions or "")
        assert not any("Oduflow" in (t.description or "") for t in tools.values())
        assert not tools["report_issue"].enabled
        assert not tools["submit_agent_feedback"].enabled
    finally:
        server.mcp._mcp_server.name = saved[0]
        server.mcp.instructions = saved[1]
        for key, description in saved[2].items():
            tools[key].description = description
        if saved[3]:
            tools["report_issue"].enable()


def test_license_cli_status_explains_white_label(tmp_path, sign, capsys):
    import argparse

    from oduflow.server import _run_license_cli

    _install(tmp_path, sign)
    _run_license_cli(
        _settings(tmp_path, hostname="localhost"),
        argparse.Namespace(license_action="status"),
    )
    out = capsys.readouterr().out
    assert "Brand: AcmeFlow" in out and "White label: off" in out
    assert "localhost" in out


def test_grace_renewal_installs_a_renewed_key(tmp_path, sign, monkeypatch):
    now = datetime.now(timezone.utc)
    _install(
        tmp_path,
        sign,
        valid_from="2025-01-01T00:00:00Z",
        expires=(now - timedelta(days=2)).isoformat(),
    )
    renewed = sign(
        _custom(
            valid_from=(now - timedelta(days=2)).isoformat(),
            expires=(now + timedelta(days=363)).isoformat(),
        )
    )
    import httpx

    monkeypatch.setattr(
        httpx,
        "post",
        lambda url, **kw: httpx.Response(
            200,
            json={"renewed": True, "license_key": renewed},
            request=httpx.Request("POST", url),
        ),
    )
    settings = _settings(tmp_path)
    assert branding.grace_days_left(settings) == branding.GRACE_DAYS - 2
    assert branding._renew_in_grace(settings)
    assert (tmp_path / "license.key").read_text() == renewed
    assert branding.grace_days_left(settings) is None
    assert not branding._renew_in_grace(settings)


def test_white_label_withholds_the_agent_feedback_surface(tmp_path, sign):
    """A hidden tool must not be advertised in the MCP instructions.

    White label hides submit_agent_feedback, so publishing its hint would tell
    every client to call a tool that answers "Unknown tool".
    """
    import dataclasses

    from oduflow import agent_feedback, server

    _install(tmp_path, sign)
    settings = dataclasses.replace(_settings(tmp_path), agent_feedback=True)
    branding.configure(lambda: settings)
    saved = server.mcp.instructions
    try:
        server._apply_agent_feedback(settings)

        assert agent_feedback.MCP_HINT not in (server.mcp.instructions or "")
        assert not server.mcp._tool_manager._tools["submit_agent_feedback"].enabled
    finally:
        server.mcp.instructions = saved
        server.submit_agent_feedback.disable()


def test_grace_renewal_attempts_once_at_startup(tmp_path, monkeypatch):
    """The daily gate must not be measured against the host's uptime.

    time.monotonic() counts from boot, so starting the gate at zero made the
    first attempt wait for a day of uptime; a server restarted more often than
    that never renewed at all, and white label lapsed after the grace period
    despite a paid renewal. Here the host has been up for five minutes.
    """
    import threading

    attempts: list[Settings] = []
    parked = threading.Event()
    forever = threading.Event()

    class _JustBooted:
        """Stands in for the ``time`` module inside oduflow.branding."""

        @staticmethod
        def monotonic() -> float:
            return 300.0

        @staticmethod
        def sleep(seconds: float) -> None:
            # One iteration is enough; park the daemon thread for good.
            parked.set()
            forever.wait()

    def _attempt(settings: Settings) -> bool:
        attempts.append(settings)
        return True

    monkeypatch.setattr(branding, "time", _JustBooted)
    monkeypatch.setattr(branding, "_renew_in_grace", _attempt)
    settings = _settings(tmp_path)

    branding.start_grace_renewal(lambda: settings)

    assert parked.wait(timeout=10)
    assert attempts == [settings]


def test_payload_helpers_are_base64():
    # Guard for the helper the tests rely on: a key is "<sig>.<payload>".
    private, _ = TestVerifyLicenseText._keypair()
    key = TestVerifyLicenseText._sign(private, {"type": "custom"})
    assert base64.b64decode(key.split(".")[1]) == b'{"type": "custom"}'


def _feedback_instance(monkeypatch, settings: Settings) -> None:
    from oduflow import server

    team = settings.teams["1"]
    monkeypatch.setattr(server, "_get_settings", lambda: settings)
    monkeypatch.setattr(server, "_resolve_team", lambda ctx: team)
    monkeypatch.setattr(server.env_ops, "list_environments", lambda *a, **k: [])


def test_white_label_agent_instructions_omit_the_feedback_section(
    tmp_path, sign, monkeypatch
):
    """The guide must not send agents to submit_agent_feedback, hidden there."""
    import dataclasses

    from tool_helpers import call_tool

    from oduflow import server

    _install(tmp_path, sign)
    settings = dataclasses.replace(_settings(tmp_path), agent_feedback=True)
    _feedback_instance(monkeypatch, settings)

    saved = server.mcp.instructions
    try:
        # Stock startup publishes the tool, so the guide documents it.
        server._apply_agent_feedback(settings)
        assert "Session Feedback" in call_tool("get_agent_instructions", ctx=None)
    finally:
        server.mcp.instructions = saved
        server.submit_agent_feedback.disable()

    # Under white label startup withholds the tool, and the guide follows the
    # published surface rather than re-deciding per call.
    branding.configure(lambda: settings)
    server._apply_agent_feedback(settings)
    guide = call_tool("get_agent_instructions", ctx=None)

    assert "Session Feedback" not in guide
    assert "submit_agent_feedback" not in guide
    assert "AcmeFlow" in guide and "Oduflow" not in guide


def test_agent_instructions_follow_the_published_feedback_tool(
    tmp_path, sign, monkeypatch
):
    """An expiring license must not start advertising a withheld tool.

    The tool surface is published once at startup, while the brand is re-read
    per call: once the license is gone the prose is stock again, but
    submit_agent_feedback is still absent from list_tools until a restart.
    """
    import dataclasses

    from tool_helpers import call_tool

    from oduflow import server

    _install(tmp_path, sign)
    settings = dataclasses.replace(_settings(tmp_path), agent_feedback=True)
    _feedback_instance(monkeypatch, settings)
    branding.configure(lambda: settings)
    server._apply_agent_feedback(settings)
    assert not server.submit_agent_feedback.enabled

    (tmp_path / "license.key").unlink()
    assert not branding.is_white_label()

    guide = call_tool("get_agent_instructions", ctx=None)

    assert "Session Feedback" not in guide
    assert "submit_agent_feedback" not in guide


def _guide_settings(tmp_path) -> Settings:
    import dataclasses

    settings = _settings(tmp_path)
    settings.teams["1"] = dataclasses.replace(
        settings.teams["1"], data_dir=str(tmp_path / "team")
    )
    (tmp_path / "team" / "agent_guides").mkdir(parents=True)
    (tmp_path / "team" / "agent_guides" / "team_notes.md").write_text(
        "# Oduflow team notes\n\nAsk the Oduflow operator.\n", encoding="utf-8"
    )
    # No heading: the viewer falls back to the file name for the title.
    (tmp_path / "team" / "agent_guides" / "oduflow_handover.md").write_text(
        "Ask the operator before deploying.\n", encoding="utf-8"
    )
    return settings


def test_white_label_agent_guides_viewer_is_rebranded(tmp_path, sign):
    """Bundled and team guides are rebranded for display, not on disk."""
    _install(tmp_path, sign)
    client = _client(_guide_settings(tmp_path))

    titles = {
        g["filename"]: g["title"]
        for g in client.get("/api/agent-guides").json()["guides"]
    }
    bundled = client.get("/api/agent-guides/agent_instructions.md").json()["content"]
    team = client.get("/api/agent-guides/team_notes.md").json()["content"]

    assert titles["team_notes.md"] == "AcmeFlow team notes"
    assert titles["oduflow_handover.md"] == "AcmeFlow Handover"
    assert titles["agent_instructions.md"].startswith("AcmeFlow")
    assert "Oduflow" not in bundled and "AcmeFlow" in bundled
    assert team == "# AcmeFlow team notes\n\nAsk the AcmeFlow operator.\n"
    on_disk = tmp_path / "team" / "agent_guides" / "team_notes.md"
    assert "Oduflow" in on_disk.read_text(encoding="utf-8")


def test_stock_agent_guides_viewer_serves_the_files_as_they_are(tmp_path):
    from oduflow.web_ui import _TEMPLATE_DIR

    client = _client(_guide_settings(tmp_path))

    titles = {
        g["filename"]: g["title"]
        for g in client.get("/api/agent-guides").json()["guides"]
    }
    bundled = client.get("/api/agent-guides/agent_instructions.md").json()["content"]

    assert titles["team_notes.md"] == "Oduflow team notes"
    assert titles["oduflow_handover.md"] == "Oduflow Handover"
    assert bundled == (
        _TEMPLATE_DIR / "agent_guides" / "agent_instructions.md"
    ).read_text(encoding="utf-8")


def test_validation_errors_are_rebranded(tmp_path, sign, monkeypatch):
    """ValueError messages reach the dashboard through _error_text_response."""
    import dataclasses

    from oduflow import web_ui

    def _managed_key(*args, **kwargs):
        raise ValueError(
            "odoo.conf keys managed by Oduflow cannot be overridden: db_host"
        )

    monkeypatch.setattr(web_ui.production_ops, "set_production_odoo_conf", _managed_key)
    settings = dataclasses.replace(_settings(tmp_path), prod_enabled=True)
    body = {"options": {"db_host": "elsewhere"}}

    stock = _client(settings).post("/api/productions/erp/odoo-conf", json=body)
    assert stock.status_code == 400
    assert stock.json()["error"] == (
        "odoo.conf keys managed by Oduflow cannot be overridden: db_host"
    )

    _install(tmp_path, sign)
    branded = _client(settings).post("/api/productions/erp/odoo-conf", json=body)
    assert branded.status_code == 400
    assert branded.json() == {
        "ok": False,
        "error": "odoo.conf keys managed by AcmeFlow cannot be overridden: db_host",
    }


def test_import_script_is_rebranded(tmp_path, sign):
    import subprocess

    from oduflow.web_ui import _TEMPLATE_DIR

    on_disk = (_TEMPLATE_DIR / "import-odoo.sh").read_text(encoding="utf-8")
    client = _client(_settings(tmp_path))
    assert client.get("/import-odoo.sh").text == on_disk
    assert "https://YOUR-SERVER/import-odoo.sh" in on_disk

    _install(tmp_path, sign)
    script = _client(_settings(tmp_path)).get("/import-odoo.sh").text

    assert "Oduflow" not in script
    assert ">> Importing '$DB' into AcmeFlow at $SERVER" in script
    assert "points at your AcmeFlow dashboard URL" in script
    assert "unexpected finalize response (not AcmeFlow JSON):" in script
    assert "$HOME/.oduflow-addon-" in script  # technical names stay
    subprocess.run(["bash", "-n"], input=script, text=True, check=True)


def test_import_script_brand_is_inert_in_shell_quotes(tmp_path, sign):
    """The brand lands in bash and inline-Python quotes; quoting chars are dropped."""
    import subprocess

    _install(tmp_path, sign, brand_name='O\'Hara "Cloud" $(id)')
    script = _client(_settings(tmp_path)).get("/import-odoo.sh").text

    assert ">> Importing '$DB' into OHara Cloud id at $SERVER" in script
    subprocess.run(["bash", "-n"], input=script, text=True, check=True)


def test_vendor_endpoints_are_hidden_under_white_label(tmp_path, sign, monkeypatch):
    from oduflow import updates, web_ui

    checks = []

    def _check(*args, **kwargs):
        checks.append(1)
        return updates.UpdateCheck(current="1.0.0", status="current")

    monkeypatch.setattr(web_ui.updates, "check_for_update", _check)
    feedback = {"kind": "bug", "title": "t", "details": "d"}

    stock = _client(_settings(tmp_path))
    assert stock.get("/api/version").status_code == 200
    link = stock.post("/api/feedback/link", json=feedback)
    assert link.status_code == 200 and "github.com/oduflow" in link.json()["url"]

    _install(tmp_path, sign)
    branded = _client(_settings(tmp_path))
    assert branded.get("/api/version").status_code == 404
    assert branded.post("/api/feedback/link", json=feedback).status_code == 404
    assert len(checks) == 1


def _admin_client(tmp_path) -> TestClient:
    import dataclasses

    settings = dataclasses.replace(_settings(tmp_path), admin_password="console-pw-1")
    client = _client(settings)
    client.post("/admin/login", data={"password": "console-pw-1"})
    return client


def test_admin_console_hides_vendor_docs_and_agent_feedback(tmp_path, sign):
    from oduflow import config_schema

    def _feedback_field(schema):
        server = next(g for g in schema if g["id"] == "server")
        return [f for f in server["fields"] if f["key"] == "agent_feedback"]

    stock = _admin_client(tmp_path).get("/admin").text
    assert "https://docs.oduflow.dev/admin/" in stock
    assert "IF-STOCK" not in stock
    assert _feedback_field(config_schema.schema_json())

    _install(tmp_path, sign)
    page = _admin_client(tmp_path).get("/admin").text
    assert "<h1>Server settings</h1>" in page
    assert "docs.oduflow.dev" not in page and "IF-STOCK" not in page
    assert "Oduflow" not in page.split("<body", 1)[1]
    assert not _feedback_field(config_schema.schema_json(white_label=True))
    # The key stays known to the parser and validator; it is only not offered.
    assert config_schema.find_field(("server", "agent_feedback")) is not None


def test_outbound_user_agents_follow_the_brand(tmp_path, sign, monkeypatch):
    """Requests seen by the client's production and git hosts carry the brand."""
    import io
    import urllib.request

    import httpx

    from oduflow import production_mcp, production_registry, walg

    seen: dict[str, str] = {}

    class _Response(io.BytesIO):
        status = 200

    def _urlopen(request, timeout=None):
        seen[request.full_url] = request.get_header("User-agent")
        return _Response(b"")

    def _odumcp(request: httpx.Request) -> httpx.Response:
        seen[str(request.url)] = request.headers["User-Agent"]
        return httpx.Response(200, json={"ok": True, "data": {}})

    original = httpx.Client
    monkeypatch.setattr(
        production_mcp.httpx,
        "Client",
        lambda **kwargs: original(transport=httpx.MockTransport(_odumcp), **kwargs),
    )
    monkeypatch.setattr(urllib.request, "urlopen", _urlopen)
    team = TeamSettings(
        team_id="1", data_dir=str(tmp_path / "team"), production_token="p" * 40
    )
    prod = Settings(teams={"1": team}, prod_enabled=True, routing_mode="traefik")
    production_registry.create_production(team, "erp", {"domain": "erp.example.com"})

    def _requests() -> list[str]:
        seen.clear()
        production_mcp.execute(prod, team, "erp", "changes.status", {})
        git_ops._check_token_with_provider("github.com", "", "t")
        walg._download("https://github.com/wal-g/x", str(tmp_path / "walg"))
        return sorted(seen.values())

    assert _requests() == ["oduflow", "oduflow", "oduflow-production"]
    _install(tmp_path, sign)
    branding.configure(lambda: _settings(tmp_path))
    assert _requests() == ["acmeflow", "acmeflow", "acmeflow-production"]


def test_opencode_provider_warning_is_rebranded(tmp_path, sign):
    """Shown in Agent CLI and Agent Chat, outside _public_flow_error."""
    import dataclasses

    from oduflow.settings import OpenCodeProviderSettings
    from oduflow.web_ui import _opencode_provider_env_warning

    settings = dataclasses.replace(
        _settings(tmp_path),
        agent_opencode_model="litellm/glm-5.3",
        agent_opencode_provider=OpenCodeProviderSettings(
            id="litellm", name="LiteLLM", models=("glm-5.3",)
        ),
    )
    team = settings.teams["1"]
    assert "and restart Oduflow;" in _opencode_provider_env_warning(settings, team)

    _install(tmp_path, sign)
    branding.configure(lambda: settings)
    warning = _opencode_provider_env_warning(settings, team)

    assert "and restart AcmeFlow;" in warning and "Oduflow" not in warning
