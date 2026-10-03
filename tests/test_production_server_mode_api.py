"""The dashboard endpoints and MCP tools for production create/reconfigure
carry server_mode through to production_ops (absent = default on create,
unchanged on reconfigure)."""

import asyncio
import inspect
from unittest.mock import patch

import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

from oduflow.locking import LockManager
from oduflow.settings import Settings, TeamSettings
from oduflow.web_ui import mount_web_ui


def _client(tmp_path) -> TestClient:
    team = TeamSettings(team_id="1", data_dir=str(tmp_path / "team_1"))
    settings = Settings(
        base_data_dir=str(tmp_path),
        prod_enabled=True,
        teams={"1": team},
    )
    app = Starlette()
    mount_web_ui(app, lambda: settings, LockManager())
    return TestClient(app)


_CREATE_BODY = {
    "name": "erp",
    "repo_url": "https://github.com/o/r.git",
    "branch": "production",
    "domain": "erp.example.com",
    "odoo_image": "odoo:19.0",
}


def test_create_passes_server_mode(tmp_path):
    client = _client(tmp_path)
    with patch(
        "oduflow.web_ui.production_ops.create_production",
        return_value={"name": "erp"},
    ) as create:
        response = client.post(
            "/api/productions/create", json={**_CREATE_BODY, "server_mode": "gevent"}
        )
    assert response.status_code == 200, response.text
    assert create.call_args.kwargs["server_mode"] == "gevent"


def test_create_defaults_to_workers(tmp_path):
    client = _client(tmp_path)
    with patch(
        "oduflow.web_ui.production_ops.create_production",
        return_value={"name": "erp"},
    ) as create:
        client.post("/api/productions/create", json=_CREATE_BODY)
    assert create.call_args.kwargs["server_mode"] == "workers"


def test_create_reports_invalid_mode_as_bad_request(tmp_path):
    client = _client(tmp_path)
    response = client.post(
        "/api/productions/create", json={**_CREATE_BODY, "server_mode": "threaded"}
    )
    assert response.status_code == 400
    assert "server_mode" in response.json()["error"]


def test_reconfigure_passes_server_mode_and_absent_means_unchanged(tmp_path):
    client = _client(tmp_path)
    with patch(
        "oduflow.web_ui.production_ops.reconfigure_production",
        return_value={"name": "erp", "changed": []},
    ) as reconfigure:
        client.post("/api/productions/erp/reconfigure", json={"server_mode": "gevent"})
        assert reconfigure.call_args.kwargs["server_mode"] == "gevent"
        client.post("/api/productions/erp/reconfigure", json={})
        assert reconfigure.call_args.kwargs["server_mode"] is None


@pytest.fixture
def mcp_tool(tmp_path):
    """Invoke a registered MCP tool against this test's settings and team."""
    import oduflow.server
    from oduflow.server import mcp as mcp_server

    team = TeamSettings(team_id="1", data_dir=str(tmp_path / "team_1"))
    oduflow.server._settings = Settings(
        base_data_dir=str(tmp_path),
        routing_mode="traefik",
        prod_enabled=True,
        teams={"1": team},
    )

    def call(tool, **kwargs):
        result = mcp_server._tool_manager._tools[tool].fn(**kwargs)
        return asyncio.run(result) if inspect.isawaitable(result) else result

    with patch("oduflow.server._resolve_team", return_value=team):
        yield call
    oduflow.server._settings = None


@pytest.mark.parametrize("mode,expected", [("gevent", "gevent"), ("", "workers")])
def test_mcp_create_passes_server_mode(mcp_tool, mode, expected):
    # Clients that send every optional string as "" get the default, as on
    # the dashboard API.
    with patch(
        "oduflow.server.production_ops.create_production",
        return_value={
            "name": "erp",
            "url": "https://erp.example.com",
            "elapsed_seconds": 1,
            "database": "oduflow_1_prod-erp",
            "commit": "c0ffee1234",
            "odoo_container": "oduflow-1-prod-erp-odoo",
            "server_mode": "gevent",
        },
    ) as create:
        out = mcp_tool(
            "create_production",
            name="erp",
            repo_url="https://github.com/o/r.git",
            branch="production",
            domain="erp.example.com",
            odoo_image="odoo:19.0",
            server_mode=mode,
        )
    assert create.call_args.kwargs["server_mode"] == expected
    assert "Server mode: gevent" in out


def test_mcp_reconfigure_empty_mode_means_unchanged(mcp_tool):
    result = {
        "changed": [],
        "url": "https://erp.example.com",
        "server_mode": "workers",
        "healthy": True,
    }
    with patch(
        "oduflow.server.production_ops.reconfigure_production", return_value=result
    ) as reconfigure:
        mcp_tool("reconfigure_production", name="erp")
        assert reconfigure.call_args.kwargs["server_mode"] is None
        mcp_tool("reconfigure_production", name="erp", server_mode="gevent")
        assert reconfigure.call_args.kwargs["server_mode"] == "gevent"
