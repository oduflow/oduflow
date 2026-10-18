import re
from unittest.mock import patch

from starlette.applications import Starlette
from starlette.testclient import TestClient

from oduflow.locking import LockManager
from oduflow.settings import Settings, TeamSettings
from oduflow.web_ui import mount_web_ui


def _client(tmp_path, *, enabled: bool, production_token: str = "") -> TestClient:
    team = TeamSettings(
        team_id="1",
        data_dir=str(tmp_path / "team_1"),
        production_token=production_token,
    )
    settings = Settings(
        base_data_dir=str(tmp_path),
        prod_enabled=enabled,
        teams={"1": team},
    )
    app = Starlette()
    mount_web_ui(app, lambda: settings, LockManager())
    return TestClient(app)


def _production_tab(html: str) -> str:
    match = re.search(r'<button[^>]+data-tab="production"[^>]*>', html)
    assert match is not None
    return match.group(0)


def _token_notice(html: str) -> str:
    match = re.search(r'<div id="prod-token-notice"[^>]*>', html)
    assert match is not None
    return match.group(0)


def test_disabled_hides_production_tab_and_omits_routes(tmp_path):
    client = _client(tmp_path, enabled=False)

    dashboard = client.get("/")

    assert dashboard.status_code == 200
    assert " hidden" in _production_tab(dashboard.text)
    assert "__PRODUCTION_TAB_HIDDEN__" not in dashboard.text
    assert ".tab-btn:not([hidden])" in dashboard.text
    assert "if (!targetTab || targetTab.hidden) return;" in dashboard.text
    assert client.get("/api/productions").status_code == 404
    assert client.post("/api/webhooks/github").status_code == 404
    assert client.get("/api/productions/wal-status").status_code == 404


def test_enabled_shows_production_tab_and_registers_routes(tmp_path):
    client = _client(tmp_path, enabled=True)

    dashboard = client.get("/")

    assert dashboard.status_code == 200
    assert " hidden" not in _production_tab(dashboard.text)
    with patch("oduflow.web_ui.production_ops.list_productions", return_value=[]):
        response = client.get("/api/productions")
    assert response.status_code == 200
    assert response.json()["productions"] == []


def test_wal_status_reports_stale_without_synchronous_docker_probe(tmp_path):
    client = _client(tmp_path, enabled=True)
    with patch("oduflow.docker_ops.client.get_client") as docker:
        response = client.get("/api/productions/wal-status")
    assert response.status_code == 200
    assert response.json()["stale"] is True
    assert response.json()["status"] == "error"
    docker.assert_not_called()


def test_wal_status_failure_stays_json(tmp_path):
    client = _client(tmp_path, enabled=True)
    with patch("oduflow.wal_monitor.status", side_effect=RuntimeError("boom")):
        response = client.get("/api/productions/wal-status")
    assert response.status_code == 500
    assert response.json() == {"ok": False, "error": "Internal server error."}


def test_wal_control_is_not_a_team_dashboard_route(tmp_path):
    # Cluster-wide WAL control acts on every team at once, so it lives in the
    # server settings console (/admin), not behind any team's session.
    client = _client(tmp_path, enabled=True)
    # 405: the path only matches the GET-only /api/productions/{name} route.
    assert client.post("/api/productions/wal-control", json={}).status_code in {
        404,
        405,
    }


def test_missing_production_token_is_announced_before_the_form(tmp_path):
    client = _client(tmp_path, enabled=True)

    dashboard = client.get("/")

    assert dashboard.status_code == 200
    assert " hidden" not in _token_notice(dashboard.text)
    assert "__PROD_TOKEN_NOTICE_HIDDEN__" not in dashboard.text
    # Only a signed-in Server settings browser gets the console link.
    assert '<a href="/admin" hidden>Open Server settings</a>' in dashboard.text
    # The dialog repeats the notice and cannot be submitted meanwhile.
    assert "(tokenMissing ? ' disabled" in dashboard.text
    with patch("oduflow.web_ui.production_ops.list_productions", return_value=[]):
        response = client.get("/api/productions")
    assert response.json()["production_token_set"] is False


def test_configured_production_token_hides_the_notice(tmp_path):
    client = _client(tmp_path, enabled=True, production_token="p" * 40)

    dashboard = client.get("/")

    assert " hidden" in _token_notice(dashboard.text)
    with patch("oduflow.web_ui.production_ops.list_productions", return_value=[]):
        response = client.get("/api/productions")
    assert response.json()["production_token_set"] is True


def test_disabled_production_never_shows_the_token_notice(tmp_path):
    dashboard = _client(tmp_path, enabled=False).get("/")

    assert " hidden" in _token_notice(dashboard.text)
