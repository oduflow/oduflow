"""Unit tests for the hidden agent-feedback channel."""

import asyncio
import json
import os
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest
from fastmcp import Client
from tool_helpers import call_tool

from oduflow import agent_feedback, server
from oduflow.settings import Settings, TeamSettings


class TestScrub:
    def test_email(self):
        assert "@" not in agent_feedback.scrub("ping max@acme.example for details")

    def test_url(self):
        out = agent_feedback.scrub("cloned https://github.com/acme/addons.git")
        assert "github" not in out and "acme" not in out

    def test_bare_host(self):
        out = agent_feedback.scrub("failed against odoo.acme.example:8069")
        assert "acme" not in out

    def test_ipv4(self):
        assert "10.0.0.7" not in agent_feedback.scrub("connect 10.0.0.7:5432 refused")

    def test_absolute_path(self):
        out = agent_feedback.scrub("wrote /home/max/work/addons/sale_ext/models.py")
        assert "/home" not in out and "sale_ext" not in out

    def test_relative_path(self):
        out = agent_feedback.scrub("read addons/acme_secret/models/invoice.py")
        assert "addons" not in out and "acme_secret" not in out

    def test_slash_separated_terms_are_not_paths(self):
        assert agent_feedback.scrub("add limit/offset arguments") == (
            "add limit/offset arguments"
        )

    def test_windows_path(self):
        assert "Users" not in agent_feedback.scrub(r"read C:\Users\max\addons")

    def test_database_name(self):
        assert "oduflow_feature" not in agent_feedback.scrub("psql oduflow_feature_x")

    def test_long_token(self):
        secret = "a" * 40
        assert secret not in agent_feedback.scrub(f"token {secret} rejected")

    def test_hex_token(self):
        secret = "5ae286c3f9a162c178577659db75f782"
        assert secret not in agent_feedback.scrub(f"header {secret} was ignored")

    def test_long_tool_names_survive(self):
        # The whole point of the channel: long snake_case tool names are not
        # secrets and must reach the report intact.
        text = (
            "get_environment_logs and set_production_backup_schedule return "
            "output that install_odoo_modules does not"
        )
        assert agent_feedback.scrub(text) == text

    def test_known_names(self):
        out = agent_feedback.scrub(
            "environment payments broke", known_names=("payments",)
        )
        assert "payments" not in out

    def test_known_names_are_case_insensitive(self):
        out = agent_feedback.scrub(
            "environment Payments broke", known_names=("payments",)
        )
        assert "payments" not in out.lower()

    def test_short_known_names_ignored(self):
        # A two-letter name would shred ordinary prose; leave it alone.
        out = agent_feedback.scrub("it is an ok result", known_names=("ok",))
        assert "ok" in out

    def test_keeps_the_actual_point(self):
        out = agent_feedback.scrub(
            "pull_and_apply did not name the module that failed to upgrade"
        )
        assert out == "pull_and_apply did not name the module that failed to upgrade"

    def test_length_cap(self):
        out = agent_feedback.scrub("word " * 2000)
        assert len(out) <= agent_feedback.MAX_SUGGESTION_CHARS + 1

    def test_empty(self):
        assert agent_feedback.scrub("") == ""


class TestNormalizeTools:
    def test_comma_separated(self):
        assert agent_feedback.normalize_tools("pull_and_apply, run_odoo_tests") == [
            "pull_and_apply",
            "run_odoo_tests",
        ]

    def test_list_input(self):
        assert agent_feedback.normalize_tools(["read_output"]) == ["read_output"]

    def test_dedupes_and_caps(self):
        assert agent_feedback.normalize_tools("a_tool, a_tool") == ["a_tool"]
        many = ", ".join(f"tool_{i}" for i in range(50))
        assert len(agent_feedback.normalize_tools(many)) == agent_feedback.MAX_TOOLS

    def test_strips_punctuation(self):
        assert agent_feedback.normalize_tools("`pull_and_apply()`") == [
            "pull_and_apply"
        ]

    def test_filters_names_outside_the_allowed_set(self):
        assert agent_feedback.normalize_tools(
            "pull_and_apply, acme_customer_database",
            {"pull_and_apply", "run_odoo_tests"},
        ) == ["pull_and_apply"]

    def test_empty(self):
        assert agent_feedback.normalize_tools("") == []


class TestPayload:
    def test_contains_only_expected_fields(self):
        payload = agent_feedback.build_payload(
            category="friction",
            tools=["pull_and_apply"],
            suggestion="error text is vague",
            version="1.68.0",
            instance_id="abc",
        )
        assert set(payload) == {
            "category",
            "tools",
            "suggestion",
            "version",
            "instance_id",
        }


class TestSettings:
    def test_off_by_default(self):
        assert Settings().agent_feedback is False

    def test_read_from_toml(self, tmp_path):
        toml = tmp_path / "oduflow.toml"
        toml.write_text("[server]\nagent_feedback = true\n\n[team.1]\nname = 'Team'\n")
        assert Settings.from_toml(str(toml)).agent_feedback is True


@pytest.fixture
def feedback_enabled(monkeypatch):
    """Enable the tool for one test and always put it back."""
    original_instructions = server.mcp.instructions
    server._apply_agent_feedback(Settings(agent_feedback=True))
    yield
    server.submit_agent_feedback.disable()
    server.mcp.instructions = original_instructions


def _tool_names() -> list[str]:
    async def go():
        async with Client(server.mcp) as client:
            return [t.name for t in await client.list_tools()]

    return asyncio.run(go())


class TestExposure:
    def test_hidden_by_default(self):
        assert "submit_agent_feedback" not in _tool_names()

    def test_disabled_toggle_is_a_noop(self):
        server._apply_agent_feedback(Settings(agent_feedback=False))
        assert "submit_agent_feedback" not in _tool_names()
        assert agent_feedback.MCP_HINT not in (server.mcp.instructions or "")

    def test_exposed_when_enabled(self, feedback_enabled):
        assert "submit_agent_feedback" in _tool_names()
        assert agent_feedback.MCP_HINT in server.mcp.instructions

    def test_enabling_twice_does_not_duplicate_the_hint(self, feedback_enabled):
        server._apply_agent_feedback(Settings(agent_feedback=True))
        assert server.mcp.instructions.count(agent_feedback.MCP_HINT) == 1


def _patch_instance(monkeypatch, settings: Settings, envs: list[dict] | None = None):
    team = TeamSettings(team_id="1", hostname="odoo.acme.example")
    monkeypatch.setattr(server, "_get_settings", lambda: settings)
    monkeypatch.setattr(server, "_resolve_team", lambda ctx: team)
    monkeypatch.setattr(server.env_ops, "list_environments", lambda *a, **k: envs or [])


class TestGuideSection:
    def test_absent_by_default(self, monkeypatch):
        _patch_instance(monkeypatch, Settings())
        assert "Session Feedback" not in call_tool("get_agent_instructions", ctx=None)

    def test_appended_when_enabled(self, monkeypatch, feedback_enabled):
        # The section documents a published tool, so the guide follows the
        # surface startup exposed rather than reading the option again.
        _patch_instance(monkeypatch, Settings(agent_feedback=True))
        guide = call_tool("get_agent_instructions", ctx=None)
        assert "Session Feedback" in guide
        assert "submit_agent_feedback" in guide

    def test_absent_while_the_option_is_on_but_the_tool_is_withheld(self, monkeypatch):
        # White label withholds the tool at startup (see test_branding); the
        # guide must not advertise it even though the option is on.
        _patch_instance(monkeypatch, Settings(agent_feedback=True))
        assert "Session Feedback" not in call_tool("get_agent_instructions", ctx=None)


class TestSubmitTool:
    def _call(self, monkeypatch, **kwargs):
        sent: list[dict] = []
        _patch_instance(
            monkeypatch, Settings(agent_feedback=True), [{"env_name": "acme-crm"}]
        )
        monkeypatch.setattr(
            agent_feedback, "send", lambda payload: sent.append(payload)
        )
        result = call_tool("submit_agent_feedback", **kwargs)
        return result, sent

    def test_scrubs_before_sending(self, monkeypatch):
        _, sent = self._call(
            monkeypatch,
            category="friction",
            tools="pull_and_apply",
            suggestion=(
                "env acme-crm on https://git.acme.example failed and the error "
                "did not name the module"
            ),
        )
        assert len(sent) == 1
        text = sent[0]["suggestion"]
        assert "acme" not in text
        assert "did not name the module" in text
        assert sent[0]["tools"] == ["pull_and_apply"]
        assert sent[0]["category"] == "friction"

    def test_drops_unregistered_tool_identifiers(self, monkeypatch):
        _, sent = self._call(
            monkeypatch,
            category="friction",
            tools="pull_and_apply, acme_customer_database",
            suggestion="the workflow took too many calls",
        )
        assert sent[0]["tools"] == ["pull_and_apply"]

    def test_rejects_unknown_category(self, monkeypatch):
        with pytest.raises(Exception, match="category must be one of"):
            self._call(monkeypatch, category="praise", tools="", suggestion="all good")

    def test_rejects_empty_suggestion(self, monkeypatch):
        with pytest.raises(Exception, match="suggestion is required"):
            self._call(monkeypatch, category="docs", tools="", suggestion="   ")


def test_cli_waits_for_feedback_receiver(tmp_path):
    received = []

    class Receiver(BaseHTTPRequestHandler):
        def do_POST(self):
            received.append(
                json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            )
            self.send_response(204 if self.path == "/accept" else 503)
            self.end_headers()

    with HTTPServer(("127.0.0.1", 0), Receiver) as receiver:
        worker = threading.Thread(target=receiver.serve_forever)
        worker.start()
        try:
            for enabled, path in (
                (False, "accept"),
                (True, "accept"),
                (True, "reject"),
            ):
                config = tmp_path / "oduflow.toml"
                config.write_text(
                    f'[server]\nagent_feedback = {str(enabled).lower()}\n[storage]\ndata_dir = "{tmp_path}"\n[team.1]\nhostname = "localhost"\n'
                )
                before = len(received)
                run = subprocess.run(
                    [
                        str(Path(sys.executable).with_name("oduflow")),
                        "--no-telemetry",
                        "call",
                        "submit_agent_feedback",
                        "docs",
                        "read_output",
                        "Clarify pagination",
                    ],
                    env={
                        **os.environ,
                        "ODUFLOW_TOML": str(config),
                        "DOCKER_HOST": f"unix://{tmp_path}/absent.sock",
                        "ODUFLOW_AGENT_FEEDBACK_URL": f"http://127.0.0.1:{receiver.server_port}/{path}",
                    },
                    capture_output=True,
                    text=True,
                    timeout=30,
                )
                assert len(received) == before + int(enabled)
                if enabled:
                    assert received[-1]["suggestion"] == "Clarify pagination"
                success = enabled and path == "accept"
                assert (run.returncode == 0) == success, run.stderr
                assert ("Feedback submitted" in run.stdout) == success
        finally:
            receiver.shutdown()
            worker.join()
