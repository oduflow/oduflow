"""Check that the dashboard, MCP dev tools and reaper observe the same Odoo."""

import json
import time
from dataclasses import replace
from pathlib import Path

import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient
from tool_helpers import call_tool

from oduflow import activity, reaper, server
from oduflow.docker_ops import env_ops, system_ops
from oduflow.locking import LockManager
from oduflow.web_ui import mount_web_ui

pytestmark = pytest.mark.integration


def test_dashboard_sql_and_mcp_share_data(live_environment, tmp_path, monkeypatch):
    settings, team = live_environment
    settings = replace(
        settings, allow_local_path=True, auto_stop_hours=1, auto_delete_hours=1
    )
    monkeypatch.setattr(server, "_get_settings", lambda: settings)
    monkeypatch.setattr(server, "_resolve_team", lambda _: team)
    system_ops.init_system(settings)
    checkout = tmp_path / "source"
    checkout.mkdir()
    name = "api"
    env_ops.create_environment(
        settings,
        team,
        "main",
        str(checkout),
        "odoo:19.0",
        env_name=name,
        local_path=str(checkout),
    )

    def partner(tool, **params):
        response = call_tool(tool, env_name=name, model="res.partner", **params)
        assert "Error (as " not in response, response
        return json.loads(response.split("Output:\n", 1)[1])

    assert (
        partner("odoo_schema", name_filter="email", attributes="type")["email"]["type"]
        == "char"
    )
    (record,) = partner("odoo_create", values='[{"name":"API partner"}]')
    domain = json.dumps([["id", "=", record]])
    assert partner("odoo_search_read", domain=domain, fields="name") == [
        {"id": record, "name": "API partner"}
    ]
    assert (
        partner("odoo_write", ids=str(record), values='{"name":"Updated partner"}')
        is True
    )
    assert (
        partner(
            "odoo_call",
            method="search_count",
            args=json.dumps([[["name", "=", "Updated partner"]]]),
        )
        == 1
    )

    locks = LockManager()
    app = Starlette()
    mount_web_ui(app, lambda: settings, locks)
    with TestClient(app) as ui:
        resource = f"/api/environments/{name}"
        assert ui.get("/api/environments").json()["environments"][0]["env_name"] == name
        measured = ui.post(resource + "/storage/refresh").json()["storage"]
        assert measured["db_bytes"] > 0 and measured["disk_bytes"] > 0
        assert ui.get("/api/stats").json()["storage"]["envs"][name] == measured
        with ui.websocket_connect(resource + "/sql") as terminal:
            terminal.send_json(
                {
                    "type": "input",
                    "data": f"UPDATE res_partner SET name='SQL terminal' WHERE id={record};\n",
                }
            )
            deadline = time.monotonic() + 30
            while (
                partner("odoo_search_read", domain=domain, fields="name")[0]["name"]
                != "SQL terminal"
            ):
                assert time.monotonic() < deadline, (
                    "SQL terminal never committed the edit"
                )
                time.sleep(0.25)
    assert partner("odoo_unlink", ids=str(record)) is True
    assert partner("odoo_search_read", domain=domain, count_only=True) == 0
    assert "HTTP 200" in call_tool(
        "http_request_to_odoo", env_name=name, path="/web/health"
    )

    def age(field):
        records = activity.get_all(team)
        records[name][field] = "2000-01-01T00:00:00+00:00"
        Path(activity.activity_path(team)).write_text(json.dumps(records))

    age("last_activity")
    reaper.sweep(settings, locks)
    assert env_ops.list_environments(settings, team)[0]["status"] == "stopped"
    assert activity.get_all(team)[name]["stopped_by"] == "auto"
    age("stopped_at")
    reaper.sweep(settings, locks)
    assert env_ops.list_environments(settings, team) == [] and checkout.is_dir()
