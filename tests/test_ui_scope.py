"""The default-deny policy behind a shared single-environment dashboard.

``ui_scope.is_allowed`` is the boundary a share link runs against: it decides
what a visitor holding only the scoped cookie may reach. The table below is
the contract — anything not listed must be refused, and every env-addressed
request must address the visitor's own environment.
"""

from __future__ import annotations

import pytest

from oduflow import ui_scope

ENV = "feature/scope"


@pytest.mark.parametrize(
    "method,path",
    [
        ("GET", f"/env/{ENV}"),
        ("GET", f"/env/{ENV}/"),
        ("GET", "/api/environments"),
        ("GET", "/api/agent"),
        ("POST", "/api/feedback/link"),
        ("POST", "/logout"),
        ("GET", f"/api/environments/{ENV}/logs"),
        ("GET", f"/api/environments/{ENV}/modules"),
        ("GET", f"/api/environments/{ENV}/users"),
        ("GET", f"/api/environments/{ENV}/connect-open"),
        ("GET", f"/api/environments/{ENV}/mcp-access"),
        ("GET", f"/api/environments/{ENV}/agent-acp/info"),
        ("POST", f"/api/environments/{ENV}/start"),
        ("POST", f"/api/environments/{ENV}/stop"),
        ("POST", f"/api/environments/{ENV}/restart"),
        ("POST", f"/api/environments/{ENV}/sync"),
        ("POST", f"/api/environments/{ENV}/modules"),
        ("POST", f"/api/environments/{ENV}/storage/refresh"),
        ("POST", f"/api/environments/{ENV}/connect-as"),
        ("POST", f"/api/environments/{ENV}/agent-acp/session"),
        ("POST", f"/api/environments/{ENV}/agent-acp/attachments"),
        ("DELETE", f"/api/environments/{ENV}/agent-acp/attachments/abc123"),
        ("WEBSOCKET", f"/api/environments/{ENV}/terminal"),
        ("WEBSOCKET", f"/api/environments/{ENV}/sql"),
        ("WEBSOCKET", f"/api/environments/{ENV}/agent-acp"),
    ],
)
def test_single_environment_surface_is_allowed(method, path):
    assert ui_scope.is_allowed(method, path, ENV)


@pytest.mark.parametrize(
    "method,path",
    [
        # The full dashboard and everything team-wide.
        ("GET", "/"),
        ("GET", "/api/templates"),
        ("GET", "/api/services"),
        ("GET", "/api/volumes"),
        ("GET", "/api/extra-repos"),
        ("GET", "/api/credentials"),
        ("GET", "/api/productions"),
        ("GET", "/api/stats"),
        ("GET", "/api/usage"),
        ("GET", "/api/license"),
        ("POST", "/api/environments/create"),
        # Provisioning and protection of the shared environment itself.
        ("POST", f"/api/environments/{ENV}/delete"),
        ("POST", f"/api/environments/{ENV}/update"),
        ("POST", f"/api/environments/{ENV}/recreate"),
        ("POST", f"/api/environments/{ENV}/switch-branch"),
        ("POST", f"/api/environments/{ENV}/protect"),
        ("POST", f"/api/environments/{ENV}/unprotect"),
        ("POST", f"/api/environments/{ENV}/save-as-template"),
        ("POST", f"/api/environments/{ENV}/note"),
        ("GET", f"/api/environments/{ENV}/env-vars"),
        # Sharing is an operator action: a share link cannot read, reissue or
        # revoke itself.
        ("GET", f"/api/environments/{ENV}/share"),
        ("POST", f"/api/environments/{ENV}/share"),
        ("POST", f"/api/environments/{ENV}/share/rotate"),
        ("POST", f"/api/environments/{ENV}/share/revoke"),
    ],
)
def test_everything_else_is_denied(method, path):
    assert not ui_scope.is_allowed(method, path, ENV)


def test_agent_cli_is_denied_while_agent_chat_is_allowed():
    """A share link exposes the agent through Chat only, never a raw PTY in
    the per-team agent container."""
    assert not ui_scope.is_allowed("WEBSOCKET", f"/api/environments/{ENV}/agent", ENV)
    assert ui_scope.is_allowed("WEBSOCKET", f"/api/environments/{ENV}/agent-acp", ENV)


@pytest.mark.parametrize(
    "path",
    [
        "/api/environments/other/logs",
        "/api/environments/feature/other/logs",
        f"/api/environments/{ENV}-2/logs",
        f"/api/environments/prefix{ENV}/logs",
    ],
)
def test_other_environments_are_denied(path):
    assert not ui_scope.is_allowed("GET", path, ENV)


def test_other_environment_page_is_denied():
    assert not ui_scope.is_allowed("GET", "/env/other", ENV)
    assert ui_scope.is_scoped_page(f"/env/{ENV}", ENV)
    assert not ui_scope.is_scoped_page("/env/other", ENV)


def test_method_must_match():
    assert not ui_scope.is_allowed("POST", f"/api/environments/{ENV}/logs", ENV)
    assert not ui_scope.is_allowed("DELETE", f"/api/environments/{ENV}/modules", ENV)
    assert not ui_scope.is_allowed("POST", "/api/environments", ENV)
    assert not ui_scope.is_allowed("GET", "/logout", ENV)
    assert not ui_scope.is_allowed("GET", "/api/feedback/link", ENV)


def test_attachment_pattern_does_not_widen_the_surface():
    ok = f"/api/environments/{ENV}/agent-acp/attachments/abc"
    assert ui_scope.is_allowed("DELETE", ok, ENV)
    # Nothing deeper, and only the DELETE method.
    assert not ui_scope.is_allowed("DELETE", ok + "/more", ENV)
    assert not ui_scope.is_allowed("GET", ok, ENV)


def test_traversal_and_empty_scope_are_denied():
    assert not ui_scope.is_allowed(
        "POST", f"/api/environments/{ENV}/../other/delete", ENV
    )
    assert not ui_scope.is_allowed("GET", f"/api/environments/{ENV}/logs", "")
