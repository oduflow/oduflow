"""Dashboard endpoints that toggle protection on auxiliary services."""

from unittest.mock import patch

from starlette.applications import Starlette
from starlette.testclient import TestClient

from oduflow.errors import ProtectedError
from oduflow.locking import LockManager
from oduflow.settings import Settings, TeamSettings
from oduflow.web_ui import mount_web_ui


def _client(tmp_path):
    team = TeamSettings(team_id="1", data_dir=str(tmp_path / "team"))
    settings = Settings(base_data_dir=str(tmp_path), teams={"1": team})
    app = Starlette()
    mount_web_ui(app, lambda: settings, LockManager())
    return TestClient(app)


def test_protect_and_unprotect_endpoints_toggle_the_flag(tmp_path):
    client = _client(tmp_path)
    with patch(
        "oduflow.web_ui.service_ops.set_service_protected",
        return_value={"name": "redis", "protected": True},
    ) as set_protected:
        response = client.post("/api/services/redis/protect")

    assert response.status_code == 200
    assert response.json() == {
        "ok": True,
        "result": {"name": "redis", "protected": True},
    }
    assert set_protected.call_args.args[2:] == ("redis", True)

    with patch(
        "oduflow.web_ui.service_ops.set_service_protected",
        return_value={"name": "redis", "protected": False},
    ) as set_protected:
        response = client.post("/api/services/redis/unprotect")

    assert response.status_code == 200
    assert set_protected.call_args.args[2:] == ("redis", False)


def test_delete_surfaces_protection_as_a_client_error(tmp_path):
    client = _client(tmp_path)
    with patch(
        "oduflow.web_ui.service_ops.delete_service",
        side_effect=ProtectedError("Service 'redis' is protected."),
    ):
        response = client.post("/api/services/redis/delete")

    assert response.status_code != 500
    body = response.json()
    assert body["ok"] is False
    assert "protected" in body["error"]
