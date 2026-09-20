from unittest.mock import patch

from starlette.applications import Starlette
from starlette.testclient import TestClient

from oduflow.locking import LockManager
from oduflow.settings import Settings, TeamSettings
from oduflow.web_ui import mount_web_ui


def _client(tmp_path):
    team = TeamSettings(team_id="1", data_dir=str(tmp_path / "team"))
    settings = Settings(base_data_dir=str(tmp_path), teams={"1": team})
    app = Starlette()
    mount_web_ui(app, lambda: settings, LockManager())
    return TestClient(app)


def test_list_service_databases_never_contains_password(tmp_path):
    client = _client(tmp_path)
    rows = [
        {
            "name": "events",
            "status": "ready",
            "database": "oduflow_service_1_events",
            "username": "svc_1_events",
            "host": "oduflow-db",
            "port": 5432,
            "size_bytes": 8192,
            "connections": 0,
        }
    ]
    with patch("oduflow.web_ui.service_database_ops.list_databases", return_value=rows):
        response = client.get("/api/service-databases")

    assert response.status_code == 200
    assert response.json() == {"ok": True, "databases": rows}
    assert "password" not in response.text.lower()


def test_create_returns_credentials_from_scoped_operation(tmp_path):
    client = _client(tmp_path)
    result = {
        "name": "events",
        "status": "ready",
        "database": "oduflow_service_1_events",
        "username": "svc_1_events",
        "password": "generated",
        "host": "oduflow-db",
        "port": 5432,
        "url": "postgresql://generated",
    }
    with patch(
        "oduflow.web_ui.service_database_ops.create_database", return_value=result
    ) as create:
        response = client.post("/api/service-databases/create", json={"name": "events"})

    assert response.status_code == 200
    assert response.json() == {"ok": True, "result": result}
    assert response.headers["cache-control"] == "no-store"
    assert create.call_args.args[2] == "events"


def test_credentials_endpoint_explicitly_requests_secret(tmp_path):
    client = _client(tmp_path)
    result = {"name": "events", "password": "generated", "status": "ready"}
    with patch(
        "oduflow.web_ui.service_database_ops.get_database", return_value=result
    ) as get_database:
        response = client.post("/api/service-databases/events/credentials")

    assert response.status_code == 200
    assert response.json()["result"]["password"] == "generated"
    assert response.headers["cache-control"] == "no-store"
    assert get_database.call_args.kwargs["reveal_password"] is True


def test_credentials_are_not_reachable_over_get(tmp_path):
    """The only unmasked-secret endpoint must sit behind the CSRF backstop in
    UIAuthMiddleware, which by construction only guards unsafe methods."""
    client = _client(tmp_path)
    with patch("oduflow.web_ui.service_database_ops.get_database") as get_database:
        response = client.get("/api/service-databases/events/credentials")

    assert response.status_code == 405
    get_database.assert_not_called()


def test_delete_uses_resource_scoped_endpoint(tmp_path):
    client = _client(tmp_path)
    result = {
        "name": "events",
        "database": "oduflow_service_1_events",
        "username": "svc_1_events",
    }
    with patch(
        "oduflow.web_ui.service_database_ops.delete_database", return_value=result
    ) as delete:
        response = client.post("/api/service-databases/events/delete")

    assert response.status_code == 200
    assert response.json() == {"ok": True, "result": result}
    assert delete.call_args.args[2] == "events"


def test_create_passes_cluster_through(tmp_path):
    client = _client(tmp_path)
    with patch(
        "oduflow.web_ui.service_database_ops.create_database", return_value={}
    ) as create:
        response = client.post(
            "/api/service-databases/create",
            json={"name": "events", "cluster": "prod"},
        )

    assert response.status_code == 200
    assert create.call_args.kwargs["cluster"] == "prod"


def test_create_rejects_unknown_cluster(tmp_path):
    client = _client(tmp_path)
    with patch("oduflow.web_ui.service_database_ops.create_database") as create:
        response = client.post(
            "/api/service-databases/create",
            json={"name": "events", "cluster": "qa"},
        )

    assert response.status_code == 400
    create.assert_not_called()


def test_protect_and_unprotect_endpoints_toggle_the_flag(tmp_path):
    client = _client(tmp_path)
    with patch(
        "oduflow.web_ui.service_database_ops.set_protected",
        return_value={"name": "events", "protected": True},
    ) as set_protected:
        response = client.post("/api/service-databases/events/protect")

    assert response.status_code == 200
    assert set_protected.call_args.args[1:] == ("events", True)

    with patch(
        "oduflow.web_ui.service_database_ops.set_protected",
        return_value={"name": "events", "protected": False},
    ) as set_protected:
        response = client.post("/api/service-databases/events/unprotect")

    assert response.status_code == 200
    assert set_protected.call_args.args[1:] == ("events", False)
