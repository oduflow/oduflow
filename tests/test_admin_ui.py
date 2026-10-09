"""Server settings console (/admin): auth boundary and the save pipeline."""

from __future__ import annotations

import threading
from pathlib import Path

import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

from oduflow import admin_ui
from oduflow import config_store as cs
from oduflow.locking import LockManager
from oduflow.settings import Settings
from oduflow.web_ui import _AUTH_COOKIE, _make_ui_token, mount_web_ui

ADMIN_PW = "console-password-123"
TEAM_PW = "team-password-aaaa"

CONFIG = f"""\
# hand-written header
[admin]
password = "{ADMIN_PW}"

[lifecycle]
auto_stop_hours = 48

[team.1]
hostname = "localhost"
auth_token = "token-one-aaaaaaaaaaaa"
ui_password = "{TEAM_PW}"
"""


class Server:
    """A mounted dashboard + console over a real oduflow.toml on disk."""

    def __init__(self, tmp_path: Path, text: str = CONFIG) -> None:
        self.path = tmp_path / "etc" / "oduflow.toml"
        self.path.parent.mkdir()
        self.data = tmp_path / "data"
        text += f'\n[storage]\ndata_dir = "{self.data}"\n'
        self.path.write_text(text, encoding="utf-8")
        self.path.chmod(0o600)
        self.raw = cs.parse_text(text)
        self.settings = self._build(self.raw)
        self.restarts = 0
        self.busy: list[str] = []
        self.runtime = cs.ConfigRuntime(
            running=lambda: (self.settings, self.raw),
            swap=self._swap,
            restart=self._restart,
            busy=lambda: list(self.busy),
            boot=self.settings,
        )
        app = Starlette()
        mount_web_ui(app, lambda: self.settings, LockManager(), self.runtime)
        self.client = TestClient(app, base_url="http://testserver")

    def _build(self, raw):
        return Settings.from_raw(raw, str(self.path))

    def _swap(self, settings, raw):
        self.settings, self.raw = settings, raw

    def _restart(self):
        self.restarts += 1

    def login(self, password: str = ADMIN_PW):
        return self.client.post(
            "/admin/login", data={"password": password}, follow_redirects=False
        )

    def state(self):
        r = self.client.get("/admin/api/state")
        assert r.status_code == 200, r.text
        return r.json()


@pytest.fixture
def srv(tmp_path):
    return Server(tmp_path)


def _team_cookie(srv: Server) -> str:
    return _make_ui_token(srv.settings.teams["1"], srv.settings)


class TestAuthBoundary:
    def test_console_is_absent_without_a_password(self, tmp_path):
        s = Server(tmp_path, CONFIG.replace(f'password = "{ADMIN_PW}"', ""))
        assert s.client.get("/admin", follow_redirects=False).status_code == 404
        assert s.client.get("/admin/login").status_code == 404
        assert s.client.get("/admin/api/state").status_code == 404

    def test_page_redirects_to_its_own_login(self, srv):
        r = srv.client.get("/admin", follow_redirects=False)
        assert r.status_code == 302
        assert r.headers["location"] == "/admin/login"
        assert srv.client.get("/admin/api/state").status_code == 401

    def test_login_sets_a_strict_http_only_cookie(self, srv):
        r = srv.login()
        assert r.status_code == 303
        cookie = r.headers["set-cookie"]
        assert admin_ui.ADMIN_COOKIE in cookie
        assert "HttpOnly" in cookie and "SameSite=strict" in cookie
        assert srv.client.get("/admin").status_code == 200

    def test_team_password_does_not_open_the_console(self, srv):
        assert srv.login(TEAM_PW).status_code == 401
        assert srv.client.get("/admin/api/state").status_code == 401

    def test_team_session_does_not_open_the_console(self, srv):
        srv.client.cookies.set(_AUTH_COOKIE, _team_cookie(srv))
        assert srv.client.get("/admin/api/state").status_code == 401

    def test_console_session_does_not_open_the_team_dashboard(self, srv):
        srv.login()
        assert srv.client.get("/api/stats").status_code == 401

    def test_login_is_rate_limited(self, srv):
        for _ in range(10):
            srv.login("wrong")
        assert srv.login().status_code == 429

    def test_cross_origin_posts_are_rejected(self, srv):
        srv.login()
        r = srv.client.post(
            "/admin/api/preview",
            json={"changes": [{"path": ["server", "port"], "value": 1}]},
            headers={"Origin": "https://evil.example"},
        )
        assert r.status_code == 403

    def test_password_change_revokes_console_sessions(self, srv):
        srv.login()
        assert srv.client.get("/admin/api/state").status_code == 200
        raw = dict(srv.raw)
        raw["admin"] = {"password": "another-password-456"}
        srv.settings = srv._build(raw)
        assert srv.client.get("/admin/api/state").status_code == 401

    def test_dashboard_link_only_for_console_sessions(self, srv):
        srv.client.cookies.set(_AUTH_COOKIE, _team_cookie(srv))
        page = srv.client.get("/").text
        assert (
            'href="/admin" title="Edit the server configuration (oduflow.toml)" hidden'
            in page
        )
        srv.login()
        page = srv.client.get("/").text
        assert (
            'href="/admin" title="Edit the server configuration (oduflow.toml)" >'
            in page
        )


class TestState:
    def test_secrets_are_masked(self, srv):
        srv.login()
        body = srv.client.get("/admin/api/state").text
        assert TEAM_PW not in body
        assert ADMIN_PW not in body
        state = srv.state()
        assert state["values"]["team"]["1"]["ui_password"] == {
            "$secret": True,
            "set": True,
        }
        assert state["suggest"] == {"team_id": "2", "port_range": [50100, 50200]}
        assert state["file_error"] == ""

    def test_reveal_returns_one_secret(self, srv):
        srv.login()
        r = srv.client.post(
            "/admin/api/reveal", json={"path": ["team", "1", "ui_password"]}
        )
        assert r.json() == {"ok": True, "value": TEAM_PW}
        r = srv.client.post(
            "/admin/api/reveal", json={"path": ["team", "1", "hostname"]}
        )
        assert r.status_code == 400

    def test_invalid_file_is_reported_not_fatal(self, srv):
        srv.login()
        srv.path.write_text("[team.1\n", encoding="utf-8")
        state = srv.state()
        assert "syntax" in state["file_error"]
        assert state["values"] is None

    def test_reload_applies_hand_edited_live_values(self, srv):
        srv.login()
        text = srv.path.read_text().replace(
            "auto_stop_hours = 48", "auto_stop_hours = 7"
        )
        text = text.replace('hostname = "localhost"', 'hostname = "moved.local"')
        srv.path.write_text(text, encoding="utf-8")
        pending = {tuple(c["path"]): c["apply"] for c in srv.state()["pending"]}
        assert pending == {
            ("lifecycle", "auto_stop_hours"): "live",
            ("team", "1", "hostname"): "restart",
        }
        data = srv.client.post("/admin/api/reload", json={}).json()
        assert [c["path"] for c in data["applied"]] == [
            ["lifecycle", "auto_stop_hours"]
        ]
        assert srv.settings.auto_stop_hours == 7
        assert srv.settings.teams["1"].hostname == "localhost"


class TestSave:
    def _change(self, srv, *changes, revision=None):
        body = {
            "changes": list(changes),
            "revision": revision or srv.state()["revision"],
        }
        preview = srv.client.post("/admin/api/preview", json=body).json()
        applied = srv.client.post("/admin/api/apply", json=body)
        return preview, applied

    def test_live_change_is_applied_without_restart(self, srv):
        srv.login()
        preview, r = self._change(
            srv, {"path": ["lifecycle", "auto_stop_hours"], "value": 6}
        )
        assert preview["ok"] and preview["apply"] == "live"
        assert r.status_code == 200, r.text
        data = r.json()
        assert [c["path"] for c in data["applied"]] == [
            ["lifecycle", "auto_stop_hours"]
        ]
        assert data["pending"] == []
        assert srv.settings.auto_stop_hours == 6
        text = srv.path.read_text()
        assert "auto_stop_hours = 6" in text and text.startswith("# hand-written")

    def test_restart_change_is_saved_but_pending(self, srv):
        srv.login()
        preview, r = self._change(srv, {"path": ["server", "port"], "value": 9001})
        assert preview["apply"] == "restart"
        data = r.json()
        assert data["applied"] == []
        assert [c["path"] for c in data["pending"]] == [["server", "port"]]
        assert srv.settings.port == 8000
        assert [c["path"] for c in srv.state()["pending"]] == [["server", "port"]]

    def test_live_keys_stay_live_after_being_live_set_to_zero(self, srv):
        """Turning lifecycle off live does not make turning it back on a restart.

        The reaper thread exists because the *boot* config enabled it, so the
        console must classify against the boot state, not against what it has
        already live-swapped in.
        """
        srv.login()
        _, r = self._change(srv, {"path": ["lifecycle", "auto_stop_hours"], "value": 0})
        assert r.json()["applied"] and srv.settings.auto_stop_hours == 0
        preview, r = self._change(
            srv, {"path": ["lifecycle", "auto_stop_hours"], "value": 24}
        )
        assert preview["apply"] == "live"
        data = r.json()
        assert [c["path"] for c in data["applied"]] == [
            ["lifecycle", "auto_stop_hours"]
        ]
        assert data["pending"] == [] and srv.settings.auto_stop_hours == 24

    def test_invalid_candidate_is_refused_with_the_parser_message(self, srv):
        srv.login()
        preview, r = self._change(
            srv, {"path": ["team", "1", "environment_hostname_mode"], "value": "slots"}
        )
        assert not preview["ok"]
        assert "traefik" in preview["error"]
        assert r.status_code == 400
        assert "slots" not in srv.path.read_text()

    def test_stale_revision_is_a_conflict(self, srv):
        srv.login()
        revision = srv.state()["revision"]
        srv.path.write_text(srv.path.read_text() + "# hand edit\n", encoding="utf-8")
        _, r = self._change(
            srv, {"path": ["server", "port"], "value": 9001}, revision=revision
        )
        assert r.status_code == 409
        assert srv.path.read_text().endswith("# hand edit\n")

    def test_add_team_then_revert_from_history(self, srv):
        srv.login()
        team = {
            "hostname": "two.example.com",
            "port_range": [50100, 50200],
            "auth_token": "token-two-bbbbbbbbbbbb",
            "ui_password": "team-two-password",
        }
        preview, r = self._change(srv, {"path": ["team", "2"], "value": team})
        assert preview["changes"][0]["presence"] is True
        assert r.status_code == 200
        assert "[team.2]" in srv.path.read_text()

        history = srv.client.get("/admin/api/history").json()["entries"]
        assert history[0]["paths"] == ["team.2"]
        entry = srv.client.get(f"/admin/api/history/{history[0]['id']}").json()
        assert "+[team.2]" in entry["diff"]
        assert "team-two-password" not in entry["diff"]

        body = {"restore": history[0]["id"], "revision": srv.state()["revision"]}
        assert srv.client.post("/admin/api/apply", json=body).status_code == 200
        assert "[team.2]" not in srv.path.read_text()

    def test_raw_edit(self, srv):
        srv.login()
        raw = srv.client.get("/admin/api/raw").json()
        text = raw["text"].replace("auto_stop_hours = 48", "auto_stop_hours = 1")
        r = srv.client.post(
            "/admin/api/apply", json={"raw": text, "revision": raw["revision"]}
        )
        assert r.status_code == 200
        assert srv.settings.auto_stop_hours == 1

    def test_console_password_policy(self, srv):
        srv.login()
        preview, r = self._change(
            srv, {"path": ["admin", "password"], "value": "short"}
        )
        assert not preview["ok"] and "12" in preview["error"]
        assert r.status_code == 400

    def test_console_password_change_signs_out(self, srv):
        srv.login()
        preview, r = self._change(
            srv, {"path": ["admin", "password"], "value": "brand-new-password-1"}
        )
        assert preview["signs_out"] is True
        assert r.status_code == 200
        assert srv.client.get("/admin/api/state").status_code == 401


class TestRestart:
    def test_refused_while_operations_run(self, srv):
        srv.login()
        srv.busy = ["environment 'feature-x' (pull_and_apply, running for 1m00s)"]
        r = srv.client.post("/admin/api/restart", json={})
        assert r.status_code == 409
        assert r.json()["busy"] == srv.busy
        assert srv.restarts == 0

    def test_restart_is_scheduled(self, srv, monkeypatch):
        class ImmediateTimer:
            def __init__(self, _delay, fn):
                self.fn = fn

            def start(self):
                self.fn()

        monkeypatch.setattr(threading, "Timer", ImmediateTimer)
        srv.login()
        r = srv.client.post("/admin/api/restart", json={})
        assert r.status_code == 200
        assert r.json()["boot_id"] == srv.runtime.boot_id
        assert srv.restarts == 1


def test_lock_manager_lists_active_operations():
    locks = LockManager()
    assert locks.active_operations() == []
    locks.acquire_env("feature", "1", operation="pull_and_apply")
    locks.acquire_team("2", operation="delete_template")
    busy = locks.active_operations()
    assert busy[0].startswith("environment 'feature' (pull_and_apply")
    assert busy[1].startswith("team '2' (delete_template")
    locks.release_env("feature")
    locks.release_team("2")
    assert locks.active_operations() == []


_TEMPLATES = Path(__file__).parents[1] / "src" / "oduflow" / "templates"


def _root_tokens(html: str, selector: str) -> dict[str, str]:
    import re

    start = html.index(selector + " {")
    block = html[start : html.index("}", start)]
    return dict(re.findall(r"(--[a-z0-9-]+):\s*([^;]+);", block))


def test_console_follows_the_dashboard_design_tokens():
    """DESIGN.md: one token system; every var(--*) declared; no external assets."""
    import re

    admin = (_TEMPLATES / "admin.html").read_text(encoding="utf-8")
    dashboard = (_TEMPLATES / "dashboard.html").read_text(encoding="utf-8")
    for selector in (":root", ':root[data-theme="light"]'):
        ours = _root_tokens(admin, selector)
        theirs = _root_tokens(dashboard, selector)
        drifted = {k: (v, ours.get(k)) for k, v in theirs.items() if ours.get(k) != v}
        assert not drifted, f"{selector} tokens drifted from the dashboard: {drifted}"
    declared = set(_root_tokens(admin, ":root"))
    used = set(re.findall(r"var\((--[a-z0-9-]+)\)", admin))
    assert used <= declared, f"undeclared tokens: {sorted(used - declared)}"
    # Values from the file (team ids, route names) reach inline handlers only
    # as JSON literals; a quote in a TOML key must not break out of them.
    assert "\\'' + esc(" not in admin
    external = re.findall(r'(?:src|href)="(https?://[^"]+)"', admin)
    assert external == ["https://docs.oduflow.dev/admin/"]


def test_fresh_install_gets_a_console_password():
    from oduflow import server

    template = (_TEMPLATES / "oduflow.toml").read_text(encoding="utf-8")
    rendered = server._inject_admin_password(template, "generated-console-pw")
    raw = cs.parse_text(rendered)
    assert raw["admin"]["password"] == "generated-console-pw"
    # Only the [admin] password is filled, never another empty "password".
    assert rendered.count("generated-console-pw") == 1


def test_admin_cli_enable_and_disable(tmp_path, monkeypatch, capsys):
    from oduflow import server

    s = Server(tmp_path, CONFIG.replace(f'password = "{ADMIN_PW}"', ""))
    monkeypatch.setattr(server, "_get_settings", lambda: s.settings)

    server._admin_cli("enable")
    out = capsys.readouterr().out
    password = cs.parse_text(s.path.read_text())["admin"]["password"]
    assert len(password) >= cs.MIN_ADMIN_PASSWORD
    assert password in out and "/admin" in out
    assert cs.list_history(str(s.path))[0]["note"] == "Console enabled (CLI)"
    assert s.path.read_text().startswith("# hand-written header")

    server._admin_cli("enable")
    assert "already enabled" in capsys.readouterr().out
    server._admin_cli("enable", reset=True)
    assert cs.parse_text(s.path.read_text())["admin"]["password"] != password

    server._admin_cli("disable")
    assert "password" not in cs.parse_text(s.path.read_text()).get("admin", {})
