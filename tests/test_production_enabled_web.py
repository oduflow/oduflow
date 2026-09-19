import re
from unittest.mock import patch

from starlette.applications import Starlette
from starlette.testclient import TestClient

from oduflow.locking import LockManager
from oduflow.settings import Settings, TeamSettings
from oduflow.web_ui import mount_web_ui


def _client(tmp_path, *, enabled: bool) -> TestClient:
    team = TeamSettings(team_id="1", data_dir=str(tmp_path / "team_1"))
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
    assert client.post("/api/productions/wal-control").status_code == 404


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


def test_wal_control_requires_cluster_confirmation(tmp_path):
    client = _client(tmp_path, enabled=True)
    with patch("oduflow.docker_ops.client.get_client"):
        response = client.post("/api/productions/wal-control", json={"action": "pause"})
    assert response.json()["ok"] is False
    assert "ALL-PRODUCTIONS" in response.json()["error"]
