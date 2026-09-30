"""Tests for template code-source metadata.

A template saved from a live-mounted environment must record ``local_path``
(not a bogus path in ``repo_url``), and creating from such a template must
re-establish the live-mount when allow_local_path is enabled, fail clearly
when it is disabled, and let an explicit repo_url override the template's
local_path.
"""

from __future__ import annotations

import json
import os
from unittest.mock import MagicMock, patch

import pytest
from fastmcp.exceptions import ToolError
from tool_helpers import call_tool as _call_tool  # noqa: E402

from oduflow.docker_ops.system_ops import _source_env_metadata
from oduflow.settings import Settings, TeamSettings


def _make_env(tmp_path, allow_local_path: bool = True):
    team = TeamSettings(
        team_id="1",
        data_dir=str(tmp_path / "team"),
        port_registry_path=str(tmp_path / "ports.json"),
    )
    settings = Settings(
        base_data_dir=str(tmp_path),
        disable_telemetry=True,
        allow_local_path=allow_local_path,
        teams={"1": team},
    )
    return settings, team


@pytest.fixture
def tool_env(tmp_path):
    """Inject per-test settings/team into the MCP server module."""
    import oduflow.server as server

    settings, team = _make_env(tmp_path)
    old = server._settings
    server._settings = settings
    with patch("oduflow.server._resolve_team", return_value=team):
        yield settings, team
    server._settings = old


def _write_template(team: TeamSettings, name: str, metadata: dict) -> None:
    path = team.get_template_metadata_path(name)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        json.dump(metadata, f)


_CREATE_RESULT = {
    "url": "http://localhost:50000",
    "odoo_container": "oduflow-t-odoo",
    "database": "oduflow_1_t",
    "workspace": "/tmp/ws",
}


# --- save_as_template metadata -------------------------------------------


class TestSourceEnvMetadata:
    SETTINGS = Settings(teams={})

    def test_git_env_records_repo_url(self):
        labels = {
            self.SETTINGS.image_label: "odoo:19.0",
            self.SETTINGS.repo_label: "https://github.com/x/y.git",
        }
        metadata = _source_env_metadata(self.SETTINGS, labels)
        assert metadata["repo_url"] == "https://github.com/x/y.git"
        assert "local_path" not in metadata

    def test_live_mount_env_records_local_path(self):
        # Live-mount envs carry the path in BOTH labels (repo label included);
        # the metadata must not present that path as a repo URL.
        labels = {
            self.SETTINGS.image_label: "odoo:19.0",
            self.SETTINGS.repo_label: "/Users/dev/addons",
            "oduflow.local_path": "/Users/dev/addons",
        }
        metadata = _source_env_metadata(self.SETTINGS, labels)
        assert metadata["local_path"] == "/Users/dev/addons"
        assert metadata["repo_url"] == ""


# --- create_environment from a live-mount template ------------------------


class TestCreateFromLiveMountTemplate:
    @patch("oduflow.docker_ops.env_ops.create_environment")
    def test_recreates_live_mount_when_allowed(self, mock_create, tool_env, tmp_path):
        settings, team = tool_env
        code_dir = tmp_path / "addons"
        code_dir.mkdir()
        _write_template(
            team,
            "tpl",
            {"odoo_image": "odoo:19.0", "repo_url": "", "local_path": str(code_dir)},
        )
        mock_create.return_value = {**_CREATE_RESULT, "local_path": str(code_dir)}

        result = _call_tool("create_environment", branch="t", template_name="tpl")

        assert mock_create.call_args.kwargs["local_path"] == str(code_dir)
        assert "Live-mount:" in result

    @patch("oduflow.docker_ops.env_ops.create_environment")
    def test_disabled_rejects_with_clear_error(self, mock_create, tmp_path):
        import oduflow.server as server

        settings, team = _make_env(tmp_path, allow_local_path=False)
        old = server._settings
        server._settings = settings
        _write_template(
            team,
            "tpl",
            {"odoo_image": "odoo:19.0", "repo_url": "", "local_path": "/x/addons"},
        )
        try:
            with patch("oduflow.server._resolve_team", return_value=team):
                with pytest.raises(ToolError, match="live-mounted environment"):
                    _call_tool("create_environment", branch="t", template_name="tpl")
            mock_create.assert_not_called()
        finally:
            server._settings = old

    @patch("oduflow.docker_ops.env_ops.create_environment")
    def test_explicit_repo_url_overrides(self, mock_create, tool_env, tmp_path):
        settings, team = tool_env
        _write_template(
            team,
            "tpl",
            {"odoo_image": "odoo:19.0", "repo_url": "", "local_path": "/x/addons"},
        )
        mock_create.return_value = dict(_CREATE_RESULT)

        result = _call_tool(
            "create_environment",
            branch="t",
            template_name="tpl",
            repo_url="https://github.com/x/y.git",
        )

        assert mock_create.call_args.args[3] == "https://github.com/x/y.git"
        assert mock_create.call_args.kwargs["local_path"] == ""
        assert "Environment provisioned successfully!" in result

    @patch("oduflow.docker_ops.env_ops.create_environment")
    def test_missing_dir_fails(self, mock_create, tool_env, tmp_path):
        settings, team = tool_env
        _write_template(
            team,
            "tpl",
            {"odoo_image": "odoo:19.0", "repo_url": "", "local_path": "/gone/addons"},
        )
        with pytest.raises(ToolError, match="does not exist"):
            _call_tool("create_environment", branch="t", template_name="tpl")
        mock_create.assert_not_called()


# --- Web UI ----------------------------------------------------------------


def _web_app(settings):
    from starlette.applications import Starlette

    from oduflow.locking import LockManager
    from oduflow.web_ui import mount_web_ui

    app = Starlette()
    mount_web_ui(app, lambda: settings, LockManager())
    return app


class TestWebUILiveMountTemplate:
    @patch("oduflow.docker_ops.env_ops.create_environment")
    def test_create_passes_local_path_when_allowed(self, mock_create, tmp_path):
        from starlette.testclient import TestClient

        settings, team = _make_env(tmp_path)
        code_dir = tmp_path / "addons"
        code_dir.mkdir()
        _write_template(
            team,
            "tpl",
            {"odoo_image": "odoo:19.0", "repo_url": "", "local_path": str(code_dir)},
        )
        mock_create.return_value = {**_CREATE_RESULT, "local_path": str(code_dir)}
        client = TestClient(_web_app(settings))
        resp = client.post(
            "/api/environments/create",
            json={"env_name": "t", "template_name": "tpl"},
        )
        assert resp.status_code == 200
        assert mock_create.call_args.kwargs["local_path"] == str(code_dir)

    def test_create_returns_clear_error_when_disabled(self, tmp_path):
        from starlette.testclient import TestClient

        settings, team = _make_env(tmp_path, allow_local_path=False)
        _write_template(
            team,
            "tpl",
            {"odoo_image": "odoo:19.0", "repo_url": "", "local_path": "/x/addons"},
        )
        client = TestClient(_web_app(settings))
        resp = client.post(
            "/api/environments/create",
            json={"env_name": "t", "template_name": "tpl"},
        )
        assert resp.status_code == 400
        assert "live-mounted environment" in resp.json()["error"]

    @patch("oduflow.docker_ops.env_ops._ensure_system_ready")
    @patch("oduflow.docker_ops.system_ops.check_disk_space")
    @patch("oduflow.docker_ops.system_ops.estimate_new_db_bytes", return_value=0)
    @patch("oduflow.docker_ops.env_ops.create_environment")
    @patch("oduflow.docker_ops.env_ops.delete_environment")
    @patch("oduflow.docker_ops.client.get_client")
    def test_recreate_passes_local_path(
        self,
        mock_client,
        mock_delete,
        mock_create,
        mock_est,
        mock_disk,
        mock_ready,
        tmp_path,
    ):
        from starlette.testclient import TestClient

        settings, team = _make_env(tmp_path)
        container = MagicMock()
        container.labels = {
            settings.repo_label: "/x/addons",
            settings.image_label: "odoo:19.0",
            "oduflow.local_path": "/x/addons",
            "oduflow.template": "none",
        }
        mock_client.return_value.containers.get.return_value = container
        mock_create.return_value = dict(_CREATE_RESULT)

        client = TestClient(_web_app(settings))
        resp = client.post("/api/environments/main/recreate")

        assert resp.status_code == 200
        assert mock_create.call_args.kwargs["local_path"] == "/x/addons"
