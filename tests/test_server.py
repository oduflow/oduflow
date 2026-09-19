import logging
import sys
import time
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

import pytest
from fastmcp.exceptions import ToolError

import docker
from oduflow import po_tools
from oduflow.po_tools import PoEntry, PoSummary
from oduflow.settings import Settings, TeamSettings

TEST_TEAM = TeamSettings(
    team_id="1",
    data_dir="/tmp/flow-test",
    port_registry_path="/tmp/flow-test/ports.json",
    port_range_start=50000,
    port_range_end=50100,
)

TEST_SETTINGS = Settings(
    base_data_dir="/tmp/flow-test",
    db_user="odoo",
    db_password="odoo",
    teams={"1": TEST_TEAM},
)


@pytest.fixture(autouse=True)
def inject_settings():
    import oduflow.server

    oduflow.server._settings = TEST_SETTINGS
    yield
    oduflow.server._settings = None


@pytest.fixture(autouse=True)
def inject_team():
    with patch("oduflow.server._resolve_team", return_value=TEST_TEAM):
        yield


from tool_helpers import call_tool as _call_tool  # noqa: E402


class TestMCPBootstrapInstructions:
    def test_instructions_point_agents_to_dynamic_guides(self):
        import oduflow.server

        instructions = oduflow.server.mcp.instructions

        assert instructions
        assert instructions.startswith(
            "Once at the start of the session, call get_agent_instructions"
        )
        assert "get_agent_instructions" in instructions
        assert "get_odoo_development_guide" in instructions
        assert 'odoo:18.0 means version="18"' in instructions


class TestCLIInitDestroy:
    def test_cli_transport_short_flag_starts_http(self):
        from oduflow import server

        with (
            patch.object(sys, "argv", ["oduflow", "-t", "http"]),
            patch("oduflow.docker_ops.client.wait_for_docker") as mock_wait_for_docker,
            patch.object(server.migrations, "run_pending"),
            patch.object(server, "_ensure_initialized"),
            patch.object(server.quotas, "apply_all"),
            patch.object(server, "_start_stdio") as mock_stdio,
            patch.object(server, "_start_http") as mock_http,
        ):
            server._run_cli()

        mock_stdio.assert_not_called()
        mock_http.assert_called_once_with()
        mock_wait_for_docker.assert_called_once_with()
        assert server.settings_module.TRANSPORT == "http"

    @patch("oduflow.docker_ops.system_ops.destroy_system")
    def test_cli_destroy(self, mock_destroy):
        from oduflow.server import _run_destroy

        mock_destroy.return_value = {
            "status": "destroyed",
            "removed": "flow-db, flow-db-data, flow-net",
        }
        _run_destroy(TEST_SETTINGS)
        mock_destroy.assert_called_once_with(TEST_SETTINGS)

    def test_cli_upgrade_force(self):
        from oduflow import server

        with (
            patch.object(sys, "argv", ["oduflow", "upgrade", "--force"]),
            patch.object(server, "_get_settings", return_value=TEST_SETTINGS),
            patch.object(server, "_run_upgrade") as mock_upgrade,
        ):
            server._run_cli()

        mock_upgrade.assert_called_once_with(TEST_SETTINGS, force=True)

    def test_cli_upgrade_exits_nonzero_when_attention_is_required(self):
        from oduflow import server

        with (
            patch.object(sys, "argv", ["oduflow", "upgrade", "--force"]),
            patch.object(server, "_get_settings", return_value=TEST_SETTINGS),
            patch.object(server, "_run_upgrade", return_value=False),
            pytest.raises(SystemExit) as exc_info,
        ):
            server._run_cli()

        assert exc_info.value.code == 1


class TestCLIUpgrade:
    @staticmethod
    def _settings_with_changed_guide(tmp_path, monkeypatch):
        from oduflow import server

        package_dir = tmp_path / "package"
        bundled_guides = package_dir / "templates" / "agent_guides"
        bundled_guides.mkdir(parents=True)
        (bundled_guides / "guide.md").write_text("bundled v2\n", encoding="utf-8")
        monkeypatch.setattr(server, "__file__", str(package_dir / "server.py"))

        team_dir = tmp_path / "team"
        deployed_guides = team_dir / "agent_guides"
        deployed_guides.mkdir(parents=True)
        deployed_guide = deployed_guides / "guide.md"
        deployed_guide.write_text("bundled v1\n", encoding="utf-8")
        baseline = (
            team_dir / ".bundled_upgrade" / "baselines" / "agent_guides" / "guide.md"
        )
        baseline.parent.mkdir(parents=True)
        baseline.write_text("bundled v1\n", encoding="utf-8")
        team = replace(
            TEST_TEAM,
            data_dir=str(team_dir),
            port_registry_path=str(team_dir / "ports.json"),
        )
        settings = replace(
            TEST_SETTINGS,
            base_data_dir=str(tmp_path),
            etc_dir=str(tmp_path / "conf"),
            teams={"1": team},
        )
        return server, settings, deployed_guide

    def test_upgrade_prompts_by_default(self, tmp_path, monkeypatch):
        server, settings, deployed_guide = self._settings_with_changed_guide(
            tmp_path, monkeypatch
        )
        prompt = patch("builtins.input", return_value="")

        with prompt as mock_input:
            server._run_upgrade(settings)

        mock_input.assert_called_once_with(
            "  Press Enter to continue or Ctrl+C to abort... "
        )
        assert deployed_guide.read_text(encoding="utf-8") == "bundled v2\n"

    def test_upgrade_force_skips_prompt(self, tmp_path, monkeypatch):
        server, settings, deployed_guide = self._settings_with_changed_guide(
            tmp_path, monkeypatch
        )

        with patch("builtins.input", side_effect=AssertionError("unexpected prompt")):
            server._run_upgrade(settings, force=True)

        assert deployed_guide.read_text(encoding="utf-8") == "bundled v2\n"

    @staticmethod
    def _make_legacy(settings, deployed_guide):
        baseline = (
            Path(settings.teams["1"].data_dir)
            / ".bundled_upgrade"
            / "baselines"
            / "agent_guides"
            / "guide.md"
        )
        baseline.unlink()
        deployed_guide.write_text("custom legacy\n", encoding="utf-8")
        return baseline

    def test_upgrade_legacy_file_requires_review_without_force(
        self, tmp_path, monkeypatch
    ):
        server, settings, deployed_guide = self._settings_with_changed_guide(
            tmp_path, monkeypatch
        )
        self._make_legacy(settings, deployed_guide)

        with patch("builtins.input", return_value=""):
            assert server._run_upgrade(settings) is False

        assert deployed_guide.read_text(encoding="utf-8") == "custom legacy\n"
        assert (
            Path(f"{deployed_guide}.oduflow-new").read_text(encoding="utf-8")
            == "bundled v2\n"
        )

    def test_upgrade_force_overwrites_a_legacy_file(self, tmp_path, monkeypatch):
        server, settings, deployed_guide = self._settings_with_changed_guide(
            tmp_path, monkeypatch
        )
        baseline = self._make_legacy(settings, deployed_guide)
        backup = (
            Path(settings.teams["1"].data_dir)
            / ".bundled_upgrade"
            / "backups"
            / "agent_guides"
            / "guide.md"
        )

        with patch("builtins.input", side_effect=AssertionError("unexpected prompt")):
            assert server._run_upgrade(settings, force=True) is True

        assert deployed_guide.read_text(encoding="utf-8") == "bundled v2\n"
        assert baseline.read_text(encoding="utf-8") == "bundled v2\n"
        assert backup.read_text(encoding="utf-8") == "custom legacy\n"
        assert not Path(f"{deployed_guide}.oduflow-new").exists()

    def test_upgrade_does_not_manage_postgresql_conf(self, tmp_path, monkeypatch):
        from oduflow import server

        package_dir = tmp_path / "package"
        templates = package_dir / "templates"
        templates.mkdir(parents=True)
        (templates / "postgresql.conf").write_text("bundled\n", encoding="utf-8")
        monkeypatch.setattr(server, "__file__", str(package_dir / "server.py"))

        conf_dir = tmp_path / "conf"
        conf_dir.mkdir()
        deployed = conf_dir / "postgresql.conf"
        deployed.write_text("custom\n", encoding="utf-8")
        settings = replace(
            TEST_SETTINGS,
            base_data_dir=str(tmp_path),
            etc_dir=str(conf_dir),
            teams={},
        )

        assert server._run_upgrade(settings, force=True) is True
        assert deployed.read_text(encoding="utf-8") == "custom\n"


class TestImportTemplate:
    def _result(self, includes_filestore: bool = False):
        return {
            "template_name": "prod",
            "source_url": "https://odoo.example.com",
            "source_db": "db",
            "odoo_version": "19.0",
            "odoo_image": "odoo:19.0",
            "template_db": "oduflow_template_1_prod",
            "zip_size_mb": 1.2,
            "restore_seconds": 0.3,
            "includes_filestore": includes_filestore,
            "affected_envs": [],
            "remount_failures": [],
        }

    @patch("oduflow.docker_ops.system_ops.import_template")
    def test_cli_import_passes_without_filestore(self, mock_import, capsys):
        from oduflow.server import _run_import_template

        mock_import.return_value = self._result(includes_filestore=False)

        _run_import_template(
            TEST_SETTINGS,
            TEST_TEAM,
            source="https://odoo.example.com",
            master_pwd="master",
            db_name="db",
            template_name="prod",
            without_filestore=True,
        )

        assert mock_import.call_args.kwargs["without_filestore"] is True
        assert "Filestore: not included" in capsys.readouterr().out

    @patch("oduflow.docker_ops.system_ops.import_template")
    def test_mcp_import_passes_without_filestore(self, mock_import):
        mock_import.return_value = self._result(includes_filestore=False)

        result = _call_tool(
            "import_template",
            source="https://odoo.example.com",
            master_pwd="master",
            db_name="db",
            template_name="prod",
            without_filestore=True,
        )

        assert mock_import.call_args.kwargs["without_filestore"] is True
        assert "Filestore: not included" in result

    def _s3_result(self, dump_reloaded: bool = True):
        return {
            "status": "synced",
            "template_name": "prod",
            "source_url": "s3://bucket/backups/prod",
            "source_db": "dump.pgdump",
            "odoo_version": "19.0",
            "odoo_image": "odoo:19.0",
            "template_db": "oduflow_template_1_prod",
            "restore_seconds": 42,
            "dump_reloaded": dump_reloaded,
            "includes_filestore": True,
            "downloaded_files": 12,
            "downloaded_mb": 340.5,
            "reused_files": 8800,
            "removed_files": 3,
            "affected_envs": [],
            "remount_failures": [],
        }

    @patch("oduflow.docker_ops.system_ops.import_template")
    def test_mcp_import_s3_without_master_pwd(self, mock_import):
        mock_import.return_value = self._s3_result()

        result = _call_tool(
            "import_template",
            source="s3://bucket/backups/prod",
            template_name="prod",
            overwrite=True,
        )

        kwargs = mock_import.call_args.kwargs
        assert kwargs["master_pwd"] == ""
        assert kwargs["overwrite"] is True
        assert "synced successfully" in result
        assert "12 fetched (340.5 MB), 8800 reused, 3 removed" in result
        assert "DB restore time: 42s" in result

    @patch("oduflow.docker_ops.system_ops.import_template")
    def test_mcp_import_s3_reports_skipped_reload(self, mock_import):
        mock_import.return_value = self._s3_result(dump_reloaded=False)

        result = _call_tool(
            "import_template",
            source="s3://bucket/backups/prod",
            template_name="prod",
            overwrite=True,
        )

        assert "template DB reload skipped" in result


class TestCreateEnvironmentTool:
    @patch(
        "oduflow.docker_ops.env_ops.adopt_existing_environment",
        return_value=None,
    )
    @patch("oduflow.docker_ops.env_ops.create_environment")
    def test_create(self, mock_create, mock_adopt):
        mock_create.return_value = {
            "url": "http://localhost:50000",
            "odoo_container": "oduflow-main-odoo",
            "database": "oduflow_1_main",
            "workspace": "/tmp/ws",
        }
        result = _call_tool(
            "create_environment",
            branch="main",
            template_name="none",
            repo_url="https://8.8.8.8/repo.git",
            odoo_image="odoo:17.0",
        )
        assert "Environment provisioned successfully!" in result
        assert "Database: oduflow_1_main" in result
        assert "Template: none (init from scratch)" in result

    @patch(
        "oduflow.docker_ops.env_ops.adopt_existing_environment",
        return_value=None,
    )
    @patch("oduflow.docker_ops.env_ops.create_environment")
    def test_create_with_env_vars(self, mock_create, mock_adopt):
        mock_create.return_value = {
            "url": "http://localhost:50000",
            "odoo_container": "oduflow-main-odoo",
            "database": "oduflow_1_main",
            "workspace": "/tmp/ws",
        }
        result = _call_tool(
            "create_environment",
            branch="main",
            template_name="none",
            repo_url="https://8.8.8.8/repo.git",
            odoo_image="odoo:17.0",
            env_vars="FOO=bar,BAZ=qux",
        )
        assert mock_create.call_args.kwargs["env_vars"] == {
            "FOO": "bar",
            "BAZ": "qux",
        }
        assert "Env vars: FOO=bar, BAZ=qux" in result

    @patch(
        "oduflow.docker_ops.env_ops.adopt_existing_environment",
        return_value=None,
    )
    @patch("oduflow.docker_ops.env_ops.create_environment")
    def test_create_without_env_vars(self, mock_create, mock_adopt):
        mock_create.return_value = {
            "url": "http://localhost:50000",
            "odoo_container": "oduflow-main-odoo",
            "database": "oduflow_1_main",
            "workspace": "/tmp/ws",
        }
        _call_tool(
            "create_environment",
            branch="main",
            template_name="none",
            repo_url="https://8.8.8.8/repo.git",
            odoo_image="odoo:17.0",
        )
        assert mock_create.call_args.kwargs["env_vars"] is None

    @patch(
        "oduflow.docker_ops.env_ops.adopt_existing_environment",
        return_value=None,
    )
    @patch("oduflow.docker_ops.env_ops.create_environment")
    def test_create_with_short_hostname(self, mock_create, mock_adopt):
        mock_create.return_value = {
            "url": "https://dev3.example.com",
            "hostname": "dev3",
            "odoo_container": "oduflow-main-odoo",
            "database": "oduflow_1_main",
            "workspace": "/tmp/ws",
        }

        result = _call_tool(
            "create_environment",
            branch="main",
            hostname="dev3",
            template_name="none",
            repo_url="https://8.8.8.8/repo.git",
            odoo_image="odoo:17.0",
        )

        assert mock_create.call_args.kwargs["hostname"] == "dev3"
        assert "Hostname: dev3" in result


class TestCreateEnvironmentAdoptsExisting:
    @patch("oduflow.docker_ops.env_ops.create_environment")
    @patch("oduflow.docker_ops.env_ops.adopt_existing_environment")
    def test_existing_environment_is_returned_without_provisioning(
        self, mock_adopt, mock_create
    ):
        mock_adopt.return_value = {
            "env_name": "main",
            "url": "https://dev1.example.com",
            "git_branch": "main",
            "odoo_image": "odoo:17.0",
            "template_name": "myproject",
            "hostname": "dev1",
            "odoo_container": "oduflow-main-odoo",
            "database": "oduflow_1_main",
            "workspace": "/tmp/ws",
            "local_path": "",
            "status": "exited",
            "started": True,
        }

        result = _call_tool(
            "create_environment",
            branch="main",
            template_name="myproject",
            repo_url="https://8.8.8.8/repo.git",
            odoo_image="odoo:17.0",
        )

        mock_create.assert_not_called()
        assert "Environment already exists" in result
        assert "URL: https://dev1.example.com" in result
        assert "It was stopped and has been started for you." in result
        assert 'get_odoo_development_guide(version="17")' in result

    @patch("oduflow.docker_ops.env_ops.create_environment")
    @patch("oduflow.docker_ops.env_ops.adopt_existing_environment")
    def test_mismatched_image_and_template_are_reported(self, mock_adopt, mock_create):
        mock_adopt.return_value = {
            "env_name": "main",
            "url": "https://dev1.example.com",
            "git_branch": "main",
            "odoo_image": "odoo:17.0",
            "template_name": "myproject",
            "hostname": "dev1",
            "odoo_container": "oduflow-main-odoo",
            "database": "oduflow_1_main",
            "workspace": "/tmp/ws",
            "local_path": "",
            "status": "running",
            "started": False,
        }

        result = _call_tool(
            "create_environment",
            branch="main",
            template_name="other",
            repo_url="https://8.8.8.8/repo.git",
            odoo_image="odoo:18.0",
        )

        mock_create.assert_not_called()
        assert "you asked for image 'odoo:18.0'" in result
        assert "you asked for template 'other'" in result
        assert "It was stopped" not in result


class TestUpdateEnvironmentTool:
    @patch("oduflow.docker_ops.env_ops.update_environment")
    def test_update_rebuild_only(self, mock_update):
        mock_update.return_value = {
            "url": "http://localhost:50000",
            "odoo_container": "oduflow-main-odoo",
            "database": "oduflow_1_main",
            "workspace": "/tmp/ws",
            "image": "odoo:17.0",
            "image_updated": False,
            "env_vars": {},
        }
        result = _call_tool("update_environment", env_name="main")
        assert mock_update.call_args.kwargs == {
            "env_override": None,
            "image_override": None,
            "rename_to": None,
            "hostname_override": None,
        }
        assert "Environment updated successfully!" in result
        assert "Image: odoo:17.0" in result
        assert "(updated)" not in result

    @patch("oduflow.docker_ops.env_ops.update_environment")
    def test_update_image_and_env(self, mock_update):
        mock_update.return_value = {
            "url": "http://localhost:50000",
            "odoo_container": "oduflow-main-odoo",
            "database": "oduflow_1_main",
            "workspace": "/tmp/ws",
            "image": "odoo:17.0",
            "image_updated": True,
            "env_vars": {"FOO": "new"},
        }
        result = _call_tool(
            "update_environment",
            env_name="main",
            env_vars="FOO=new",
            odoo_image="odoo:17.0",
        )
        assert mock_update.call_args.kwargs == {
            "env_override": {"FOO": "new"},
            "image_override": "odoo:17.0",
            "rename_to": None,
            "hostname_override": None,
        }
        assert "Image: odoo:17.0 (updated)" in result
        assert "Env vars: FOO=new" in result

    @patch("oduflow.docker_ops.env_ops.update_environment")
    def test_update_hostname(self, mock_update):
        mock_update.return_value = dict(self._renamed_result(), hostname="qa")
        result = _call_tool("update_environment", env_name="main", hostname="qa")
        assert mock_update.call_args.kwargs["hostname_override"] == "qa"
        assert "Hostname: qa" in result

    @staticmethod
    def _renamed_result():
        return {
            "url": "http://localhost:50000",
            "env_name": "next",
            "renamed_from": "main",
            "odoo_container": "oduflow-1-next-odoo",
            "database": "oduflow_1_next",
            "workspace": "/tmp/ws",
            "image": "odoo:17.0",
            "image_updated": False,
            "env_vars": {},
        }

    @patch("oduflow.docker_ops.env_ops.update_environment")
    def test_a_new_name_renames_and_reports_the_moved_mcp_endpoint(self, mock_update):
        mock_update.return_value = self._renamed_result()

        result = _call_tool("update_environment", env_name="main", new_name="next")

        assert mock_update.call_args.kwargs["rename_to"] == "next"
        assert "Environment renamed from 'main' to 'next'" in result
        # An MCP client pointed at the old path stops working; say so.
        assert "/mcp/next" in result

    @patch("oduflow.docker_ops.env_ops.update_environment")
    def test_renaming_to_the_current_name_is_a_plain_update(self, mock_update):
        mock_update.return_value = {
            "url": "http://localhost:50000",
            "odoo_container": "oduflow-1-main-odoo",
            "database": "oduflow_1_main",
            "workspace": "/tmp/ws",
            "image": "odoo:17.0",
            "image_updated": False,
            "env_vars": {},
        }

        result = _call_tool("update_environment", env_name="main", new_name="main")

        assert mock_update.call_args.kwargs["rename_to"] is None
        assert "Environment updated successfully!" in result

    @staticmethod
    def _name_is_free(env_name: str) -> bool:
        import oduflow.server
        from oduflow.errors import BusyError

        locks = oduflow.server._locks
        try:
            locks.acquire_env(env_name, "1")
        except BusyError:
            return False
        locks.release_env(env_name)
        return True

    @patch("oduflow.docker_ops.env_ops.update_environment")
    def test_the_target_name_is_locked_for_the_rename_and_released_after(
        self, mock_update
    ):
        free_during: list[bool] = []

        def _check(*args, **kwargs):
            # A concurrent create_environment must not claim the name while the
            # rename is in flight; the tool decorator only holds env_name.
            free_during.append(self._name_is_free("next"))
            return self._renamed_result()

        mock_update.side_effect = _check
        _call_tool("update_environment", env_name="main", new_name="next")

        assert free_during == [False]
        # And it is free again afterwards, so the renamed environment is usable.
        assert self._name_is_free("next")

    @patch("oduflow.docker_ops.env_ops.update_environment")
    def test_the_target_name_is_released_when_the_rename_fails(self, mock_update):
        from oduflow.errors import ConflictError

        mock_update.side_effect = ConflictError("taken")

        with pytest.raises(ToolError):
            _call_tool("update_environment", env_name="main", new_name="next")

        assert self._name_is_free("next")


class TestDeleteEnvironmentTool:
    @patch("oduflow.docker_ops.env_ops.delete_environment")
    def test_delete(self, mock_delete):
        mock_delete.return_value = []
        result = _call_tool("delete_environment", env_name="main")
        assert "torn down" in result
        mock_delete.assert_called_once_with(TEST_SETTINGS, TEST_TEAM, "main")

    @patch("oduflow.docker_ops.env_ops.delete_environment")
    def test_delete_with_warnings(self, mock_delete):
        mock_delete.return_value = [
            'Failed to drop database "oduflow_main": connection refused'
        ]
        result = _call_tool("delete_environment", env_name="main")
        assert "torn down" in result
        assert "Warnings:" in result
        assert "Failed to drop database" in result

    @patch("oduflow.docker_ops.env_ops.delete_environment")
    def test_delete_missing_raises(self, mock_delete):
        from oduflow.errors import NotFoundError

        mock_delete.side_effect = NotFoundError(
            "Environment 'firewall' does not exist."
        )
        with pytest.raises(ToolError, match="does not exist"):
            _call_tool("delete_environment", env_name="firewall")


class TestListEnvironmentsTool:
    @patch("oduflow.docker_ops.env_ops.list_environments")
    def test_list(self, mock_list):
        mock_list.return_value = [
            {
                "env_name": "main",
                "status": "running",
                "url": "http://localhost:50000",
                "containers": [
                    {
                        "name": "oduflow-main-odoo",
                        "status": "running",
                        "image": "odoo:15.0",
                    }
                ],
            }
        ]
        result = _call_tool("list_environments")
        assert "main" in result
        assert "oduflow-main-odoo" in result

    @patch("oduflow.docker_ops.env_ops.list_environments")
    def test_list_empty(self, mock_list):
        mock_list.return_value = []
        result = _call_tool("list_environments")
        assert "No active" in result

    @patch("oduflow.docker_ops.env_ops.list_environments")
    def test_list_shows_live_mount_path(self, mock_list):
        mock_list.return_value = [
            {
                "env_name": "local",
                "status": "running",
                "url": "http://localhost:50000",
                "repo_url": "/Users/dev/addons",
                "local_path": "/Users/dev/addons",
                "containers": [],
            }
        ]

        result = _call_tool("list_environments")

        assert "Live-mount: /Users/dev/addons" in result
        assert "Repo:" not in result

    @patch("oduflow.docker_ops.env_ops.list_environments")
    def test_list_shows_reuse_metadata(self, mock_list):
        mock_list.return_value = [
            {
                "env_name": "dev1",
                "git_branch": "feature/finished",
                "status": "stopped",
                "url": None,
                "created_at": "2026-08-20T08:00:00+00:00",
                "last_activity": "2026-08-26T09:30:00+00:00",
                "stopped_at": "2026-08-27T09:30:00+00:00",
                "stopped_by": "auto",
                "protected": True,
                "note": "Reserved for QA\nDo not reuse",
                "stack": "demo",
                "containers": [],
            }
        ]

        result = _call_tool("list_environments")

        assert "Git Branch: feature/finished" in result
        assert "Created: 2026-08-20T08:00:00+00:00" in result
        assert "Last Activity: 2026-08-26T09:30:00+00:00" in result
        assert "Stopped At: 2026-08-27T09:30:00+00:00 (auto)" in result
        assert "Protected: yes" in result
        assert "Stack: demo" in result
        assert "Note: Reserved for QA" in result
        assert "Do not reuse" in result

    @patch("oduflow.docker_ops.env_ops.list_environments")
    def test_list_marks_missing_activity_as_unknown(self, mock_list):
        mock_list.return_value = [
            {
                "env_name": "legacy",
                "status": "stopped",
                "containers": [],
            }
        ]

        result = _call_tool("list_environments")

        assert "Git Branch: legacy" in result
        assert "Last Activity: unknown" in result
        assert "Stopped At: unknown (unknown)" in result
        assert "Protected: no" in result


class TestAgentInstructionsTool:
    def test_bundled_guide_is_compact_and_workflow_focused(self):
        import oduflow.server

        guide_path = (
            Path(oduflow.server.__file__).resolve().parent
            / "templates"
            / "agent_guides"
            / "agent_instructions.md"
        )
        guide = guide_path.read_text(encoding="utf-8")

        assert len(guide) < 16_000
        assert "Per-environment Odoo configuration" in guide
        assert "Version: 9" in guide
        assert "Never invent `#.` or `#:` metadata" in guide
        assert "An empty `gh pr list` result proves neither" in guide
        assert "git push -u origin HEAD" in guide
        assert "cannot see a local-only branch" in guide
        assert "db_maxconn" in guide
        assert "max_cron_threads" in guide
        assert "workers" in guide
        assert "Auxiliary Services" not in guide
        assert "### Volumes" not in guide
        assert "WAL-G" not in guide
        assert "Re-fetch this instruction document" not in guide
        assert "Self-Caching Instruction" not in guide

    @patch("oduflow.docker_ops.env_ops.list_environments")
    def test_instructions_preface_local_mode(self, mock_list):
        mock_list.return_value = [
            {
                "env_name": "test-local",
                "local_path": "/Users/dev/addons",
            }
        ]

        result = _call_tool("get_agent_instructions")

        assert result.startswith("## Current Code Delivery Mode")
        assert "Live-mount/local_path mode is active" in result
        assert 'upgrade="module"' in result
        assert "Git commits are optional" in result

    @patch("oduflow.docker_ops.env_ops.list_environments")
    def test_instructions_preface_repo_url_mode(self, mock_list):
        mock_list.return_value = [{"env_name": "test-git", "local_path": None}]

        result = _call_tool("get_agent_instructions")

        assert result.startswith("## Current Code Delivery Mode")
        assert "No live-mount/local_path environment was detected" in result
        assert "Before the first `create_environment`" in result
        assert "git push -u origin HEAD" in result
        assert "cannot clone a local-only branch" in result
        assert "commit, push, and call `pull_and_apply`" in result

    @patch("oduflow.docker_ops.env_ops.list_environments")
    def test_instructions_preface_before_first_environment(self, mock_list):
        mock_list.return_value = []

        result = _call_tool("get_agent_instructions")

        assert result.startswith("## Current Code Delivery Mode")
        assert "No environment was detected yet" in result
        assert "Choose the delivery mode" in result
        assert "For `repo_url`" in result
        assert "git push -u origin HEAD" in result
        assert "For `local_path`" in result
        assert "do not push" in result


class TestInfoTool:
    @patch("oduflow.docker_ops.env_ops.get_environment_info")
    def test_info(self, mock_info):
        mock_info.return_value = {
            "env_name": "main",
            "db_name": "oduflow_1_main",
            "workspace": "/srv/oduflow/instance_1/workspaces/main",
            "all_running": True,
            "url": "http://localhost:50000/web?debug=1",
            "repo_url": "https://github.com/example/repo.git",
            "odoo_image": "odoo:17.0",
            "template_name": "default",
            "created_at": "2026-08-20T08:00:00+00:00",
            "last_activity": "2026-08-27T10:00:00+00:00",
            "protected": True,
            "note": "Reserved for QA",
            "extra_addons": {},
            "git_user": "",
            "odoo": {"status": "running", "running": True},
            "db": {"status": "running", "running": True},
        }
        result = _call_tool("get_environment_info", env_name="main")
        assert "All containers running" in result
        assert "Git Branch: main" in result
        assert "Created: 2026-08-20T08:00:00+00:00" in result
        assert "Last Activity: 2026-08-27T10:00:00+00:00" in result
        assert "Protected: yes" in result
        assert "Note: Reserved for QA" in result
        assert "Database: oduflow_1_main" in result
        assert "DB (shared)" in result

    @patch("oduflow.docker_ops.env_ops.get_environment_info")
    def test_info_shows_live_mount(self, mock_info):
        mock_info.return_value = {
            "env_name": "main",
            "db_name": "oduflow_1_main",
            "workspace": "/srv/oduflow/instance_1/workspaces/main",
            "all_running": True,
            "repo_url": "/Users/dev/addons",
            "local_path": "/Users/dev/addons",
            "odoo_image": "odoo:17.0",
            "template_name": "default",
            "extra_addons": {},
            "git_user": "",
            "odoo": {"status": "running", "running": True},
            "db": {"status": "running", "running": True},
        }

        result = _call_tool("get_environment_info", env_name="main")

        assert "Code delivery: live-mount" in result
        assert "Live-mount: /Users/dev/addons" in result
        assert "Repo:" not in result


class TestErrorHandling:
    @patch("oduflow.docker_ops.env_ops.restart_environment")
    def test_flow_error_raises_value_error(self, mock_restart):
        from oduflow.errors import NotFoundError

        mock_restart.side_effect = NotFoundError("container not found")
        with pytest.raises(ToolError, match="container not found"):
            _call_tool("restart_environment", env_name="main")


def _compact_output_id(response: str) -> str:
    return response.rsplit("output_id=", 1)[1].rstrip("]")


class TestCompactCommandResponses:
    @patch("oduflow.docker_ops.env_ops.ensure_running", return_value=False)
    @patch("oduflow.docker_ops.odoo_ops.run_environment_tests")
    def test_run_odoo_tests_returns_only_the_last_aggregate_summary(
        self, mock_run, mock_ensure
    ):
        mock_run.return_value = "\n".join(
            [
                "2026-01-01 INFO 2 failed, 0 error(s) of 10 tests",
                *(f"2026-01-01 WARNING noisy line {i}" for i in range(2_000)),
                "unique full-log detail",
                "2026-01-01 INFO 1 failed, 2 error(s) of 30 tests when loading database",
            ]
        )

        result = _call_tool(
            "run_odoo_tests",
            env_name="main",
            modules="sale",
            summary_only=True,
        )

        assert result.startswith("1 failed, 2 error(s) of 30 tests [output_id=")
        assert "\n" not in result
        assert "noisy line" not in result

        output_id = _compact_output_id(result)
        cached = _call_tool(
            "read_output", output_id=output_id, mode="grep", grep="unique full-log"
        )
        assert "unique full-log detail" in cached

    @patch("oduflow.docker_ops.env_ops.ensure_running", return_value=False)
    @patch("oduflow.docker_ops.odoo_ops.run_environment_tests")
    def test_run_odoo_tests_has_a_short_fallback_when_odoo_aborts_early(
        self, mock_run, mock_ensure
    ):
        mock_run.return_value = "Traceback\nImportError: broken module"

        result = _call_tool(
            "run_odoo_tests",
            env_name="main",
            modules="sale",
            summary_only=True,
        )

        assert result.startswith("Test summary not found [output_id=")
        assert "Traceback" not in result
        output_id = _compact_output_id(result)
        assert "ImportError: broken module" in _call_tool(
            "read_output", output_id=output_id, mode="tail"
        )

    @patch(
        "oduflow.server._wake_for_work",
        return_value="Note: environment was stopped; started it for this call.\n",
    )
    @patch("oduflow.docker_ops.odoo_ops.run_environment_tests")
    def test_run_odoo_tests_summary_keeps_the_wake_note(self, mock_run, mock_wake):
        mock_run.return_value = "INFO 0 failed, 0 error(s) of 7 tests"

        result = _call_tool(
            "run_odoo_tests",
            env_name="main",
            modules="sale",
            summary_only=True,
        )

        assert result.startswith("Note: environment was stopped; started it for ")
        assert "0 failed, 0 error(s) of 7 tests [output_id=" in result
        assert "\n" not in result

    @patch("oduflow.docker_ops.env_ops.ensure_running", return_value=False)
    @patch("oduflow.docker_ops.odoo_ops.run_environment_tests")
    def test_run_odoo_tests_is_verbose_by_default(self, mock_run, mock_ensure):
        mock_run.return_value = "complete Odoo command output"

        result = _call_tool("run_odoo_tests", env_name="main", modules="sale")

        assert "Test Results for main:" in result
        assert "complete Odoo command output" in result

    @patch("oduflow.docker_ops.env_ops.ensure_running", return_value=False)
    @patch("oduflow.docker_ops.env_ops.pull_environment")
    def test_pull_and_apply_suppresses_logs_and_file_names(
        self, mock_pull, mock_ensure
    ):
        mock_pull.return_value = {
            "action": "upgrade",
            "message": "Upgraded modules: sale. Container restarted.",
            "modules_installed": [],
            "modules_upgraded": ["sale"],
            "changed_files": ["sale/models/order.py", "sale/security/rules.xml"],
            "warnings": ["restart is also\nrecommended"],
            "exit_code": 1,
            "output": "RAW ODOO LOG\nunique apply failure",
        }

        result = _call_tool(
            "pull_and_apply",
            env_name="main",
            upgrade="sale",
            summary_only=True,
        )

        assert "\n" not in result
        assert "Changed files: 2" in result
        assert "Status: failed (exit_code=1)" in result
        assert "Guardrail warnings: restart is also recommended" in result
        assert "sale/models/order.py" not in result
        assert "RAW ODOO LOG" not in result

        output_id = _compact_output_id(result)
        assert "unique apply failure" in _call_tool(
            "read_output", output_id=output_id, mode="tail"
        )

    @patch("oduflow.docker_ops.env_ops.ensure_running", return_value=False)
    @patch("oduflow.docker_ops.env_ops.pull_environment")
    def test_pull_and_apply_is_verbose_by_default(self, mock_pull, mock_ensure):
        mock_pull.return_value = {
            "action": "restart",
            "message": "Container restarted.",
            "changed_files": ["sale/models/order.py"],
            "exit_code": 0,
            "output": "complete apply output",
        }

        result = _call_tool("pull_and_apply", env_name="main", restart=True)

        assert "sale/models/order.py" in result
        assert "complete apply output" in result


class TestProductionFeatureGate:
    @pytest.mark.parametrize(
        "tool_name",
        [
            "create_production",
            "list_productions",
            "get_production_info",
            "production_logs",
            "start_production",
            "stop_production",
            "restart_production",
            "set_production_auto_update",
            "update_production",
            "rollback_production",
            "production_deploys",
            "snapshot_production",
            "list_production_snapshots",
            "restore_production",
            "production_backup_status",
            "set_production_backup_schedule",
            "prune_production_backups",
            "restore_cluster_pitr",
            "delete_production",
        ],
    )
    def test_every_production_tool_is_rejected_when_disabled(self, tool_name):
        with pytest.raises(ToolError, match="Production hosting is disabled"):
            _call_tool(tool_name)

    @patch("oduflow.docker_ops.production_ops.list_productions", return_value=[])
    def test_enabled_production_tool_runs(self, mock_list):
        import oduflow.server

        oduflow.server._settings = Settings(
            prod_enabled=True,
            teams={"1": TEST_TEAM},
        )
        assert _call_tool("list_productions") == (
            "No productions found. Use create_production to provision one."
        )
        mock_list.assert_called_once()


class TestRestoreProductionSource:
    @pytest.fixture(autouse=True)
    def _enable_production(self):
        import oduflow.server

        oduflow.server._settings = Settings(prod_enabled=True, teams={"1": TEST_TEAM})
        yield

    def test_requires_exactly_one_source(self):
        restore_tool = _get_tool_fn("restore_production")
        with pytest.raises(ToolError, match="exactly one"):
            restore_tool(name="erp", confirm="erp")
        with pytest.raises(ToolError, match="exactly one"):
            restore_tool(
                name="erp",
                snapshot_id="snap",
                from_environment="feature",
                confirm="erp",
            )

    def test_environment_source_dispatches_to_environment_restore(self):
        with patch(
            "oduflow.backup_ops.restore_production_from_environment",
            return_value={"healthy": True, "warning": "", "notes": ["a note"]},
        ) as restore:
            result = _get_tool_fn("restore_production")(
                name="erp",
                from_environment="feature",
                confirm="erp",
            )

        assert "restored from environment 'feature'" in result
        assert "NOTE: a note" in result
        args = restore.call_args
        assert args.args[2:] == ("erp", "feature")
        assert callable(args.kwargs["env_lock"])


def _get_tool_fn(tool_name: str):
    """Get a sync-callable wrapper for a registered MCP tool."""
    import asyncio
    import inspect

    from oduflow.server import mcp

    fn = mcp._tool_manager._tools[tool_name].fn

    def sync_wrapper(*args, **kwargs):
        result = fn(*args, **kwargs)
        if inspect.isawaitable(result):
            return asyncio.run(result)
        return result

    return sync_wrapper


def _call_managed_tool(test_mcp, tool_name: str, **arguments):
    """Call through FastMCP's manager so its masking policy is exercised."""
    import asyncio

    return asyncio.run(test_mcp._tool_manager.call_tool(tool_name, arguments))


class TestMCPErrorVisibility:
    def test_docker_error_remains_masked(self):
        """Daemon text quotes container names, IDs and host paths: never shown."""
        from fastmcp import FastMCP

        from oduflow.server import handle_errors

        error = docker.errors.APIError(
            "500 Server Error for "
            "http+docker://localhost/v1.52/containers/internal-id/restart",
            explanation='No such container: "internal-name" (/srv/private/path)',
        )

        test_mcp = FastMCP("error-test", mask_error_details=True)

        @test_mcp.tool()
        @handle_errors
        def restart_without_context(name: str) -> str:
            raise error

        with pytest.raises(ToolError) as exc_info:
            _call_managed_tool(test_mcp, "restart_without_context", name="redis")

        message = str(exc_info.value)
        assert message == "Error calling tool 'restart_without_context'"
        assert "internal-name" not in message
        assert "/srv/private/path" not in message

    def test_unexpected_error_remains_masked(self):
        from fastmcp import FastMCP

        from oduflow.server import handle_errors

        internal_detail = "/srv/oduflow/private/team-1/runtime-state"

        test_mcp = FastMCP("error-test", mask_error_details=True)

        @test_mcp.tool()
        @handle_errors
        def restart_without_context(name: str) -> str:
            raise RuntimeError(internal_detail)

        with pytest.raises(ToolError) as exc_info:
            _call_managed_tool(test_mcp, "restart_without_context", name="redis")

        assert str(exc_info.value) == "Error calling tool 'restart_without_context'"
        assert internal_detail not in str(exc_info.value)

    def test_flow_error_message_is_shown(self):
        from fastmcp import FastMCP

        from oduflow.errors import ConflictError
        from oduflow.server import handle_errors

        test_mcp = FastMCP("error-test", mask_error_details=True)

        @test_mcp.tool()
        @handle_errors
        def create_without_context(name: str) -> str:
            raise ConflictError("host port 8080 is already allocated")

        with pytest.raises(ToolError, match="host port 8080 is already allocated"):
            _call_managed_tool(test_mcp, "create_without_context", name="redis")


class TestCreateServiceTool:
    @patch("oduflow.docker_ops.service_ops.create_service")
    def test_pull_error_is_returned_as_safe_tool_error(self, mock_create):
        from oduflow.errors import PrerequisiteNotMetError

        message = (
            "Could not pull Docker image 'registry.example.com/redis:7'. Check "
            "Docker connectivity, registry availability, and registry credentials."
        )
        mock_create.side_effect = PrerequisiteNotMetError(message)

        with pytest.raises(ToolError, match="Could not pull Docker image") as exc_info:
            _get_tool_fn("create_service")(
                name="redis",
                image="registry.example.com/redis:7",
                port=6379,
            )

        assert message in str(exc_info.value)

    @patch("oduflow.docker_ops.service_ops.create_service")
    def test_create(self, mock_create):
        mock_create.return_value = {
            "name": "redis",
            "container_name": "oduflow-1-svc-redis",
            "url": "http://localhost:6379",
            "image": "redis:7",
        }
        result = _get_tool_fn("create_service")(
            name="redis", image="redis:7", port=6379
        )
        assert "Service created successfully!" in result
        assert "redis" in result
        assert "oduflow-1-svc-redis" in result
        assert "http://localhost:6379" in result
        # Bridge-mode services advertise the container name as the internal
        # hostname so agents connect to it instead of the external URL.
        assert "Internal hostname (from Odoo & other team services)" in result

    @patch("oduflow.docker_ops.service_ops.create_service")
    def test_create_host_mode_reports_host_docker_internal(self, mock_create):
        mock_create.return_value = {
            "name": "fs",
            "container_name": "oduflow-1-svc-fs",
            "url": "https://fs.example.com",
            "image": "oduist/freeswitch:latest",
            "host_mode": True,
        }
        result = _get_tool_fn("create_service")(
            name="fs",
            image="oduist/freeswitch:latest",
            port=8080,
            host_mode=True,
        )
        # Host-mode services are not on the team network — the internal-hostname
        # line must point to host.docker.internal, not the container name.
        assert "host.docker.internal" in result

    @patch("oduflow.docker_ops.service_ops.create_service")
    def test_create_with_env_vars_parsing(self, mock_create):
        mock_create.return_value = {
            "name": "meili",
            "container_name": "oduflow-1-svc-meili",
            "url": "http://localhost:7700",
            "image": "getmeili/meilisearch:v1.6",
        }
        _get_tool_fn("create_service")(
            name="meili",
            image="getmeili/meilisearch:v1.6",
            port=7700,
            env_vars="MEILI_MASTER_KEY=abc,MEILI_ENV=production",
        )
        call_kwargs = mock_create.call_args
        parsed_env = call_kwargs[1]["env_vars"]
        assert parsed_env == {"MEILI_MASTER_KEY": "abc", "MEILI_ENV": "production"}

    @patch("oduflow.docker_ops.service_ops.create_service")
    def test_create_preserves_commas_inside_env_value(self, mock_create):
        mock_create.return_value = {
            "name": "connect-mcp-server",
            "container_name": "oduflow-1-svc-connect-mcp-server",
            "url": "http://localhost:8080",
            "image": "example/connect-mcp-server:latest",
        }

        _get_tool_fn("create_service")(
            name="connect-mcp-server",
            image="example/connect-mcp-server:latest",
            port=8080,
            env_vars=(
                "CONNECT_MCP_TOOL_GROUPS=write,collaboration,documents,LOG_LEVEL=info"
            ),
        )

        assert mock_create.call_args.kwargs["env_vars"] == {
            "CONNECT_MCP_TOOL_GROUPS": "write,collaboration,documents",
            "LOG_LEVEL": "info",
        }

    @patch("oduflow.docker_ops.service_ops.create_service")
    def test_create_empty_env_vars(self, mock_create):
        mock_create.return_value = {
            "name": "redis",
            "container_name": "oduflow-1-svc-redis",
            "url": "http://localhost:6379",
            "image": "redis:7",
        }
        _get_tool_fn("create_service")(
            name="redis", image="redis:7", port=6379, env_vars=""
        )
        call_kwargs = mock_create.call_args
        assert call_kwargs[1]["env_vars"] is None

    @patch("oduflow.docker_ops.service_ops.create_service")
    def test_create_with_routes(self, mock_create):
        routes = [{"path": "/RPC2", "port": 8080, "strip_prefix": False}]
        mock_create.return_value = {
            "name": "fs",
            "container_name": "oduflow-1-svc-fs",
            "url": "https://fs.example.com",
            "image": "fs:1",
            "routes": routes,
        }

        result = _get_tool_fn("create_service")(name="fs", image="fs:1", routes=routes)

        assert mock_create.call_args.args[4] is None
        assert mock_create.call_args.kwargs["routes"] == routes
        assert "- /RPC2 -> 8080" in result

    @patch("oduflow.docker_ops.service_ops.create_service")
    def test_create_splits_command_into_argv(self, mock_create):
        command = ["server", "/data", "--console-address", ":9001"]
        mock_create.return_value = {
            "name": "minio",
            "container_name": "oduflow-1-svc-minio",
            "url": "http://localhost:9000",
            "image": "minio/minio:latest",
            "command": command,
        }

        result = _get_tool_fn("create_service")(
            name="minio",
            image="minio/minio:latest",
            port=9000,
            command='server /data --console-address ":9001"',
        )

        assert mock_create.call_args.kwargs["command"] == command
        assert "Command: server /data --console-address :9001" in result

    @patch("oduflow.docker_ops.service_ops.create_service")
    def test_create_without_command_passes_none(self, mock_create):
        mock_create.return_value = {
            "name": "redis",
            "container_name": "oduflow-1-svc-redis",
            "url": "http://localhost:6379",
            "image": "redis:7",
        }

        _get_tool_fn("create_service")(name="redis", image="redis:7", port=6379)

        assert mock_create.call_args.kwargs["command"] is None


class TestUpdateServiceTool:
    @patch("oduflow.docker_ops.service_ops.update_service")
    def test_update(self, mock_update):
        mock_update.return_value = {
            "name": "redis",
            "container_name": "oduflow-1-svc-redis",
            "url": "http://localhost:6379",
            "image": "redis:7",
        }
        result = _get_tool_fn("update_service")(name="redis")
        assert "Service updated successfully!" in result
        assert "redis" in result
        assert "oduflow-1-svc-redis" in result
        mock_update.assert_called_once_with(
            TEST_SETTINGS,
            TEST_TEAM,
            "redis",
            env_override=None,
            image_override=None,
            port_override=None,
            hostname_override=None,
            host_mode_override=None,
            volume_override=None,
            cap_add_override=None,
            privileged_override=None,
            routes_override=None,
            command_override=None,
            runtime_override=None,
        )

    @patch("oduflow.docker_ops.service_ops.update_service")
    def test_update_host_mode_reports_host_docker_internal(self, mock_update):
        mock_update.return_value = {
            "name": "fs",
            "container_name": "oduflow-1-svc-fs",
            "url": "https://fs.example.com",
            "image": "oduist/freeswitch:latest",
            "host_mode": True,
            "image_updated": False,
            "config_updated": False,
        }

        result = _get_tool_fn("update_service")(name="fs")

        assert (
            "Internal hostname: host network — reach it via host.docker.internal"
            in result
        )

    @patch("oduflow.docker_ops.service_ops.update_service")
    def test_update_preserves_commas_inside_env_value(self, mock_update):
        mock_update.return_value = {
            "name": "connect-mcp-server",
            "container_name": "oduflow-1-svc-connect-mcp-server",
            "url": "http://localhost:8080",
            "image": "example/connect-mcp-server:latest",
            "image_updated": False,
            "config_updated": True,
        }

        _get_tool_fn("update_service")(
            name="connect-mcp-server",
            env_vars="CONNECT_MCP_TOOL_GROUPS=write,collaboration,documents",
        )

        assert mock_update.call_args.kwargs["env_override"] == {
            "CONNECT_MCP_TOOL_GROUPS": "write,collaboration,documents"
        }

    @patch("oduflow.docker_ops.service_ops.update_service")
    def test_update_routes_mapping(self, mock_update):
        mock_update.return_value = {
            "name": "fs",
            "container_name": "oduflow-1-svc-fs",
            "url": "https://fs.example.com",
            "image": "fs:1",
        }
        routes = [{"path": "/RPC2", "port": 8080, "strip_prefix": False}]
        _get_tool_fn("update_service")(name="fs", routes=routes)
        assert mock_update.call_args.kwargs["routes_override"] == routes

    @patch("oduflow.docker_ops.service_ops.update_service")
    def test_update_command_string_is_split(self, mock_update):
        mock_update.return_value = {
            "name": "minio",
            "container_name": "oduflow-1-svc-minio",
            "url": "http://localhost:9000",
            "image": "minio/minio:latest",
            "command": ["server", "/data"],
            "config_updated": True,
        }

        result = _get_tool_fn("update_service")(name="minio", command="server /data")

        assert mock_update.call_args.kwargs["command_override"] == ["server", "/data"]
        assert "Command: server /data" in result

    @patch("oduflow.docker_ops.service_ops.update_service")
    def test_update_empty_command_clears_the_override(self, mock_update):
        """An empty string means "drop it", unlike env_vars where it means "keep"."""
        mock_update.return_value = {
            "name": "minio",
            "container_name": "oduflow-1-svc-minio",
            "url": "http://localhost:9000",
            "image": "minio/minio:latest",
            "config_updated": True,
        }

        _get_tool_fn("update_service")(name="minio", command="")

        assert mock_update.call_args.kwargs["command_override"] == []

    @patch("oduflow.docker_ops.service_ops.update_service")
    def test_update_net_admin_and_privileged_mapping(self, mock_update):
        mock_update.return_value = {
            "name": "vpn",
            "container_name": "oduflow-1-svc-vpn",
            "url": "http://localhost:1194",
            "image": "vpn:latest",
        }
        _get_tool_fn("update_service")(name="vpn", net_admin=True, privileged=False)
        _, kwargs = mock_update.call_args
        assert kwargs["cap_add_override"] == ["NET_ADMIN"]
        assert kwargs["privileged_override"] is False

    @patch("oduflow.docker_ops.service_ops.update_service")
    def test_update_net_admin_false_clears_cap(self, mock_update):
        mock_update.return_value = {
            "name": "vpn",
            "container_name": "oduflow-1-svc-vpn",
            "url": "http://localhost:1194",
            "image": "vpn:latest",
        }
        _get_tool_fn("update_service")(name="vpn", net_admin=False)
        _, kwargs = mock_update.call_args
        assert kwargs["cap_add_override"] == []
        assert kwargs["privileged_override"] is None

    @patch("oduflow.docker_ops.service_ops.update_service")
    def test_update_not_found(self, mock_update):
        from oduflow.errors import NotFoundError

        mock_update.side_effect = NotFoundError("Service 'redis' not found")
        with pytest.raises(ToolError, match="Service 'redis' not found"):
            _get_tool_fn("update_service")(name="redis")


class TestDeleteServiceTool:
    @patch("oduflow.docker_ops.service_ops.delete_service")
    def test_delete(self, mock_delete):
        mock_delete.return_value = {
            "name": "redis",
            "container_name": "oduflow-1-svc-redis",
        }
        result = _get_tool_fn("delete_service")(name="redis")
        assert "deleted" in result
        assert "redis" in result
        mock_delete.assert_called_once_with(TEST_SETTINGS, TEST_TEAM, "redis")


class TestListServicesTool:
    @patch("oduflow.docker_ops.service_ops.list_services")
    def test_list(self, mock_list):
        mock_list.return_value = [
            {
                "name": "redis",
                "container_name": "oduflow-1-svc-redis",
                "image": "redis:7",
                "status": "running",
                "port": 6379,
                "url": "http://localhost:6379",
                "env_vars": {"REDIS_PASSWORD": "secret"},
            }
        ]
        result = _call_tool("list_services")
        assert "redis" in result
        assert "oduflow-1-svc-redis" in result
        assert "redis:7" in result
        assert "6379" in result
        assert "REDIS_PASSWORD=secret" in result

    @patch("oduflow.docker_ops.service_ops.list_services")
    def test_list_empty(self, mock_list):
        mock_list.return_value = []
        result = _call_tool("list_services")
        assert "No active services" in result


class TestGetServiceInfoTool:
    @patch("oduflow.docker_ops.service_ops.get_service_info")
    def test_system_acme_mount_is_not_reported_as_replayable_volume(self, mock_info):
        mock_info.return_value = {
            "name": "fs",
            "container_name": "oduflow-1-svc-fs",
            "status": "running",
            "image": "oduist/freeswitch:latest",
            "image_digest": "sha256:abc123",
            "port": 8080,
            "url": "https://fs.example.com",
            "host_mode": True,
            "volumes": [
                {"volume": "fs-sounds", "mount_path": "/sounds", "mode": "rw"},
                {
                    "volume": "oduflow-traefik-acme",
                    "mount_path": "/etc/traefik",
                    "mode": "ro",
                },
            ],
            "has_preset": True,
        }

        result = _get_tool_fn("get_service_info")(name="fs")

        assert "Volumes: fs-sounds:/sounds:rw" in result
        assert "Volumes: fs-sounds:/sounds:rw, oduflow-traefik-acme" not in result
        assert (
            "System mounts: oduflow-traefik-acme:/etc/traefik:ro "
            "(implicit; do not pass as volumes)"
        ) in result


class TestGetServiceLogsTool:
    @patch("oduflow.docker_ops.service_ops.get_service_logs")
    def test_logs(self, mock_logs):
        mock_logs.return_value = "2025-01-01 log line 1\n2025-01-01 log line 2"
        result = _get_tool_fn("get_service_logs")(name="redis", n_lines=50)
        assert "log line 1" in result
        assert "service 'redis'" in result
        mock_logs.assert_called_once_with(TEST_SETTINGS, TEST_TEAM, "redis", 50)

    @patch("oduflow.docker_ops.service_ops.get_service_logs")
    def test_logs_error(self, mock_logs):
        from oduflow.errors import NotFoundError

        mock_logs.side_effect = NotFoundError("Service 'redis' not found")
        with pytest.raises(ToolError, match="Service 'redis' not found"):
            _get_tool_fn("get_service_logs")(name="redis")


class TestResetAdminPasswordTool:
    @patch("oduflow.docker_ops.env_ops.ensure_running", return_value=False)
    @patch("oduflow.docker_ops.odoo_ops.reset_admin_password")
    def test_reset_default_password(self, mock_reset, mock_ensure):
        mock_reset.return_value = {
            "status": "ok",
            "login": "admin",
            "psql_output": "UPDATE 1",
        }
        result = _get_tool_fn("reset_admin_password")(env_name="main")
        assert "Admin password has been reset successfully" in result
        assert "Login: admin" in result
        assert "New password: test" in result
        mock_reset.assert_called_once_with(TEST_SETTINGS, TEST_TEAM, "main", "test")

    @patch("oduflow.docker_ops.env_ops.ensure_running", return_value=False)
    @patch("oduflow.docker_ops.odoo_ops.reset_admin_password")
    def test_reset_custom_password(self, mock_reset, mock_ensure):
        mock_reset.return_value = {
            "status": "ok",
            "login": "admin",
            "psql_output": "UPDATE 1",
        }
        result = _get_tool_fn("reset_admin_password")(
            env_name="main", new_password="s3cret"
        )
        assert "New password: s3cret" in result
        mock_reset.assert_called_once_with(TEST_SETTINGS, TEST_TEAM, "main", "s3cret")

    @patch("oduflow.docker_ops.odoo_ops.reset_admin_password")
    def test_reset_not_found(self, mock_reset):
        from oduflow.errors import NotFoundError

        mock_reset.side_effect = NotFoundError("Environment 'xyz' does not exist.")
        with pytest.raises(ToolError, match="does not exist"):
            _get_tool_fn("reset_admin_password")(env_name="xyz")


class TestConnectAsUserTool:
    _RESULT = {
        "sid": "deadbeef" * 8,
        "login": "jane@acme.com",
        "uid": "7",
        "base_url": "https://feature.example.com",
        "cookie_domain": "feature.example.com",
        "url": "https://feature.example.com/web",
        "expires_at": "2026-07-16T00:00:00Z",
    }

    @patch("oduflow.docker_ops.env_ops.ensure_running", return_value=False)
    @patch("oduflow.docker_ops.odoo_ops.connect_as_user")
    def test_connect_returns_cookie(self, mock_connect, mock_ensure):
        mock_connect.return_value = dict(self._RESULT)
        result = _get_tool_fn("connect_as_user")(
            env_name="feature", user="jane@acme.com"
        )
        assert "session_id" in result
        assert "deadbeef" * 8 in result  # the raw sid is handed back verbatim
        assert "feature.example.com" in result  # cookie domain
        assert "https://feature.example.com/web" in result  # target URL
        assert "jane@acme.com" in result
        assert "context.add_cookies" in result  # Playwright hint
        mock_connect.assert_called_once_with(
            TEST_SETTINGS, TEST_TEAM, "feature", "jane@acme.com"
        )

    @patch("oduflow.docker_ops.env_ops.ensure_running", return_value=False)
    @patch("oduflow.docker_ops.odoo_ops.connect_as_user")
    def test_connect_by_numeric_id(self, mock_connect, mock_ensure):
        mock_connect.return_value = dict(self._RESULT)
        _get_tool_fn("connect_as_user")(env_name="feature", user="7")
        mock_connect.assert_called_once_with(TEST_SETTINGS, TEST_TEAM, "feature", "7")

    @patch("oduflow.docker_ops.env_ops.ensure_running", return_value=False)
    @patch("oduflow.docker_ops.odoo_ops.connect_as_user")
    def test_connect_user_not_found(self, mock_connect, mock_ensure):
        from oduflow.errors import NotFoundError

        mock_connect.side_effect = NotFoundError(
            "User 'ghost' not found in environment 'feature'."
        )
        with pytest.raises(ToolError, match="not found"):
            _get_tool_fn("connect_as_user")(env_name="feature", user="ghost")

    def test_connect_in_scoped_allowlist(self):
        from oduflow.scoped_access import SCOPED_ALLOWLIST

        # Reachable by per-env scoped tokens, symmetric with run_odoo_shell.
        assert "connect_as_user" in SCOPED_ALLOWLIST


class TestConnectSentinelParsing:
    """The mint script frames values in sentinels because `odoo shell` merges its
    banner/logging with `print()` output; parsing must survive that noise."""

    def test_extract_sentinel_from_noisy_output(self):
        from oduflow.docker_ops.odoo_ops import _extract_sentinel

        noisy = (
            "2026-07-09 10:00:00,123 1 INFO feature odoo: Odoo version 19.0\n"
            "2026-07-09 10:00:00,456 1 INFO feature odoo.modules.loading: "
            "loading 42 modules...\n"
            "__ODUFLOW_SID__abc123def456__END__\n"
            "__ODUFLOW_LOGIN__jane@acme.com__END__\n"
            "__ODUFLOW_UID__7__END__\n"
            "2026-07-09 10:00:01,000 1 INFO feature odoo: Initiating shutdown\n"
        )
        assert _extract_sentinel(noisy, "SID") == "abc123def456"
        assert _extract_sentinel(noisy, "LOGIN") == "jane@acme.com"
        assert _extract_sentinel(noisy, "UID") == "7"
        assert _extract_sentinel(noisy, "TTL") is None

    def test_extract_sentinel_multiline_traceback(self):
        from oduflow.docker_ops.odoo_ops import _extract_sentinel

        out = (
            "some log line\n"
            "__ODUFLOW_ERR__Traceback (most recent call last):\n"
            '  File "<stdin>", line 3\n'
            "AttributeError: no session_store__END__\n"
            "trailing log\n"
        )
        err = _extract_sentinel(out, "ERR")
        assert err is not None
        assert err.startswith("Traceback")
        assert "AttributeError" in err


class TestEnvLock:
    @patch("oduflow.docker_ops.env_ops.create_environment")
    def test_busy_raises_tool_error(self, mock_create):
        import oduflow.server

        _locks = oduflow.server._locks
        _locks.acquire_env("main")
        try:
            with pytest.raises(ToolError, match="Another operation"):
                _call_tool(
                    "create_environment",
                    branch="main",
                    repo_url="https://x.git",
                    odoo_image="odoo:17.0",
                )
        finally:
            _locks.release_env("main")


class TestResourceLocks:
    """Service/volume tools lock the resource, not the whole team."""

    @staticmethod
    def _locks():
        import oduflow.server

        return oduflow.server._locks

    @patch("oduflow.docker_ops.service_ops.restart_service")
    def test_restart_service_waits_for_a_delete_of_the_same_service(self, mock_restart):
        # restart_service used to take no lock at all and could run against a
        # container delete_service was in the middle of removing.
        from oduflow.locking import service_lock_key

        locks = self._locks()
        key = service_lock_key("1", "redis")
        locks.acquire_env(key, operation="delete_service")
        try:
            with pytest.raises(ToolError, match="delete_service"):
                _get_tool_fn("restart_service")(name="redis")
        finally:
            locks.release_env(key)
        mock_restart.assert_not_called()

    @patch("oduflow.docker_ops.service_ops.run_command_in_service")
    def test_run_service_command_waits_for_the_same_service(self, mock_run):
        from oduflow.locking import service_lock_key

        locks = self._locks()
        key = service_lock_key("1", "redis")
        locks.acquire_env(key, operation="update_service")
        try:
            with pytest.raises(ToolError, match="update_service"):
                _get_tool_fn("run_service_command")(name="redis", command="ping")
        finally:
            locks.release_env(key)
        mock_run.assert_not_called()

    @patch("oduflow.docker_ops.service_ops.restart_service")
    def test_another_service_is_unaffected(self, mock_restart):
        from oduflow.locking import service_lock_key

        mock_restart.return_value = {"name": "meili", "container_name": "c"}
        locks = self._locks()
        key = service_lock_key("1", "redis")
        locks.acquire_env(key, operation="delete_service")
        try:
            _get_tool_fn("restart_service")(name="meili")
        finally:
            locks.release_env(key)
        mock_restart.assert_called_once()

    @patch("oduflow.docker_ops.volume_ops.create_volume")
    def test_volumes_run_during_a_team_operation(self, mock_create):
        # A template publish holds the team lock for minutes; unrelated volume
        # work must not queue behind it.
        mock_create.return_value = {
            "name": "data",
            "docker_name": "oduflow-vol-1-data",
            "description": "",
        }
        locks = self._locks()
        locks.acquire_team("1", operation="save_as_template")
        try:
            _get_tool_fn("create_volume")(name="data")
        finally:
            locks.release_team("1")
        mock_create.assert_called_once()

    @patch("oduflow.docker_ops.service_ops.delete_service")
    def test_a_service_operation_does_not_block_the_team(self, mock_delete):
        from oduflow.locking import service_lock_key

        locks = self._locks()
        key = service_lock_key("1", "redis")
        locks.acquire_env(key, operation="delete_service")
        try:
            locks.acquire_team("1")  # must not raise
            locks.release_team("1")
        finally:
            locks.release_env(key)


class TestProductionBackupLocks:
    """Prune must exclude snapshot and restore, which the team lock never did:
    productions lock in their own `prod:` keyspace."""

    @staticmethod
    def _prod_settings():
        return replace(TEST_SETTINGS, prod_enabled=True)

    def test_prune_waits_for_a_running_snapshot(self):
        import oduflow.server
        from oduflow.locking import prod_backups_lock_key

        locks = oduflow.server._locks
        key = prod_backups_lock_key("1")
        locks.acquire_env(key, operation="snapshot_production")
        with patch.object(oduflow.server, "_settings", self._prod_settings()):
            try:
                with pytest.raises(ToolError, match="snapshot_production"):
                    _get_tool_fn("prune_production_backups")()
            finally:
                locks.release_env(key)

    def test_snapshot_waits_for_a_running_prune(self):
        import oduflow.server
        from oduflow.locking import prod_backups_lock_key

        locks = oduflow.server._locks
        key = prod_backups_lock_key("1")
        locks.acquire_env(key, operation="prune_production_backups")
        with patch.object(oduflow.server, "_settings", self._prod_settings()):
            try:
                with pytest.raises(ToolError, match="prune_production_backups"):
                    _get_tool_fn("snapshot_production")(name="erp")
            finally:
                locks.release_env(key)
        # The production's own lock was handed back, not leaked.
        locks.acquire_env(oduflow.server.prod_lock_key("1", "erp"))
        locks.release_env(oduflow.server.prod_lock_key("1", "erp"))

    def test_cluster_pitr_excludes_a_running_production_operation(self):
        import oduflow.server

        locks = oduflow.server._locks
        key = oduflow.server.prod_lock_key("1", "erp")
        locks.acquire_env(key, operation="update_production")
        with patch.object(oduflow.server, "_settings", self._prod_settings()):
            try:
                with pytest.raises(ToolError, match="update_production"):
                    _get_tool_fn("restore_cluster_pitr")(confirm="RESTORE-CLUSTER")
            finally:
                locks.release_env(key)


class TestClusterPitrSafety:
    """P-H11: a failed cluster PITR must not leave every production stopped,
    and a missing [backup] section must fail before anything is stopped."""

    @staticmethod
    def _settings_with_backup():
        from oduflow.settings import BackupSettings

        return replace(
            TEST_SETTINGS,
            prod_enabled=True,
            backup=BackupSettings(bucket="b", access_key="k", secret_key="s"),
        )

    def test_failed_restore_restarts_stopped_productions(self):
        from unittest.mock import MagicMock

        import oduflow.server
        from oduflow.errors import PrerequisiteNotMetError

        container = MagicMock()
        container.status = "running"
        restarted: list[str] = []

        with (
            patch.object(oduflow.server, "_settings", self._settings_with_backup()),
            patch("oduflow.docker_ops.client.get_client", return_value=MagicMock()),
            patch(
                "oduflow.production_registry.list_productions",
                return_value={"erp": {}},
            ),
            patch(
                "oduflow.docker_ops.production_ops._get_container",
                return_value=container,
            ),
            patch(
                "oduflow.docker_ops.production_ops.start_production",
                side_effect=lambda s, t, n: restarted.append(n),
            ),
            patch(
                "oduflow.walg.pitr_restore_cluster",
                side_effect=PrerequisiteNotMetError("wal-g exploded"),
            ),
        ):
            with pytest.raises(ToolError, match="wal-g exploded"):
                _get_tool_fn("restore_cluster_pitr")(confirm="RESTORE-CLUSTER")

        # The production we stopped was brought back up before the error
        # surfaced, instead of every team being left offline.
        container.stop.assert_called_once()
        assert restarted == ["erp"]

    def test_failed_stop_loop_restarts_already_stopped_productions(self):
        from unittest.mock import MagicMock

        import oduflow.server

        # Two productions: the first stops cleanly, the second fails to stop.
        # The failure happens before the restore ever runs (PostgreSQL is
        # untouched), so the first production must be brought back up instead
        # of being left offline.
        ok = MagicMock()
        ok.status = "running"
        broken = MagicMock()
        broken.status = "running"
        broken.stop.side_effect = RuntimeError("stop exploded")
        restarted: list[str] = []

        with (
            patch.object(oduflow.server, "_settings", self._settings_with_backup()),
            patch("oduflow.docker_ops.client.get_client", return_value=MagicMock()),
            patch(
                "oduflow.production_registry.list_productions",
                return_value={"erp": {}, "crm": {}},
            ),
            patch(
                "oduflow.docker_ops.production_ops._get_container",
                side_effect=[ok, broken],
            ),
            patch(
                "oduflow.docker_ops.production_ops.start_production",
                side_effect=lambda s, t, n: restarted.append(n),
            ),
            patch("oduflow.walg.pitr_restore_cluster") as restore,
        ):
            with pytest.raises(RuntimeError, match="stop exploded"):
                _get_tool_fn("restore_cluster_pitr")(confirm="RESTORE-CLUSTER")

        restore.assert_not_called()
        ok.stop.assert_called_once()
        assert restarted == ["erp"]

    def test_missing_backup_section_fails_before_stopping(self):
        from unittest.mock import MagicMock

        import oduflow.server

        container = MagicMock()
        container.status = "running"

        with (
            patch.object(
                oduflow.server,
                "_settings",
                replace(TEST_SETTINGS, prod_enabled=True, backup=None),
            ),
            patch(
                "oduflow.production_registry.list_productions",
                return_value={"erp": {}},
            ) as list_prods,
            patch(
                "oduflow.docker_ops.production_ops._get_container",
                return_value=container,
            ),
        ):
            with pytest.raises(ToolError, match=r"\[backup\]"):
                _get_tool_fn("restore_cluster_pitr")(confirm="RESTORE-CLUSTER")

        # Validation happened before the stop loop even enumerated productions.
        list_prods.assert_not_called()
        container.stop.assert_not_called()


class TestOdooRpcToolsAreLockFree:
    """XML-RPC against a live Odoo is arbitrated by PostgreSQL, not by us."""

    @pytest.mark.parametrize(
        "tool,kwargs",
        [
            ("odoo_search_read", {"model": "res.partner"}),
            ("odoo_create", {"model": "res.partner", "values": '{"name": "x"}'}),
            ("odoo_write", {"model": "res.partner", "ids": "1", "values": "{}"}),
            ("odoo_unlink", {"model": "res.partner", "ids": "1"}),
            ("odoo_call", {"model": "res.partner", "method": "name_search"}),
            ("odoo_schema", {}),
        ],
    )
    @patch("oduflow.docker_ops.env_ops.ensure_running", return_value=False)
    @patch("oduflow.docker_ops.odoo_rpc.call_kw")
    def test_they_run_while_the_environment_lock_is_held(
        self, mock_call, mock_ensure, tool, kwargs
    ):
        import oduflow.server
        from oduflow.docker_ops.odoo_rpc import RpcResult

        mock_call.return_value = RpcResult(ok=True, value=[], login="admin", uid=2)
        locks = oduflow.server._locks
        locks.acquire_env("main", "1", operation="run_odoo_tests")
        try:
            _get_tool_fn(tool)(env_name="main", **kwargs)
        finally:
            locks.release_env("main")
        mock_call.assert_called_once()


class TestTemplateListLock:
    @patch("oduflow.docker_ops.system_ops.list_templates")
    def test_listing_needs_no_lock(self, mock_list):
        # A pure read must not bounce off a template publish that legitimately
        # holds the team lock for minutes.
        import oduflow.server

        mock_list.return_value = []
        locks = oduflow.server._locks
        locks.acquire_team("1")
        try:
            assert "No template profiles found." in _call_tool("list_templates")
        finally:
            locks.release_team("1")
        mock_list.assert_called_once()


class TestListServicePresetsTool:
    @patch("oduflow.docker_ops.service_presets.list_presets")
    def test_list_shows_all_fields(self, mock_list):
        mock_list.return_value = [
            {
                "name": "wg",
                "image": "linuxserver/wireguard:latest",
                "port": 51820,
                "hostname": "wg",
                "env_vars": {"PUID": "1000"},
                "host_mode": True,
                "privileged": True,
                "cap_add": ["NET_ADMIN", "SYS_MODULE"],
                "volumes": [
                    {"volume": "wg-data", "mount_path": "/config", "mode": "rw"},
                ],
            }
        ]
        result = _call_tool("list_service_presets")
        assert "wg" in result
        assert "image=linuxserver/wireguard:latest" in result
        assert "port=51820" in result
        assert "hostname=wg" in result
        assert "env=[PUID=1000]" in result
        assert "host_mode=true" in result
        assert "privileged=true" in result
        assert "cap_add=[NET_ADMIN,SYS_MODULE]" in result
        assert "volumes=[wg-data:/config:rw]" in result

    @patch("oduflow.docker_ops.service_presets.list_presets")
    def test_list_omits_absent_fields(self, mock_list):
        mock_list.return_value = [
            {
                "name": "redis",
                "image": "redis:7",
                "port": 6379,
                "hostname": "",
                "env_vars": {},
            }
        ]
        result = _call_tool("list_service_presets")
        assert "redis" in result
        assert "host_mode" not in result
        assert "privileged" not in result
        assert "cap_add" not in result
        assert "volumes" not in result


class TestRestoreServiceTool:
    @patch("oduflow.docker_ops.service_ops.create_service")
    @patch("oduflow.docker_ops.service_presets.get_preset")
    def test_restore_propagates_capabilities(self, mock_get, mock_create):
        mock_get.return_value = {
            "name": "wg",
            "image": "linuxserver/wireguard:latest",
            "port": 51820,
            "hostname": "wg",
            "env_vars": {"PUID": "1000"},
            "host_mode": True,
            "privileged": False,
            "cap_add": ["NET_ADMIN"],
            "volumes": [
                {"volume": "wg-data", "mount_path": "/config", "mode": "rw"},
            ],
        }
        mock_create.return_value = {
            "name": "wg",
            "container_name": "oduflow-1-svc-wg",
            "image": "linuxserver/wireguard:latest",
            "url": "http://localhost:51820",
        }
        result = _get_tool_fn("restore_service")(name="wg")

        kwargs = mock_create.call_args.kwargs
        assert kwargs["cap_add"] == ["NET_ADMIN"]
        assert kwargs["privileged"] is False
        assert kwargs["host_mode"] is True
        assert kwargs["volumes"] == [
            {"volume": "wg-data", "mount_path": "/config", "mode": "rw"},
        ]
        assert kwargs["env_vars"] == {"PUID": "1000"}
        assert "Service restored from preset!" in result
        assert "Capabilities: NET_ADMIN" in result
        assert "Volumes: wg-data:/config:rw" in result

    @patch("oduflow.docker_ops.service_ops.create_service")
    @patch("oduflow.docker_ops.service_presets.get_preset")
    def test_restore_propagates_privileged(self, mock_get, mock_create):
        mock_get.return_value = {
            "name": "dind",
            "image": "docker:dind",
            "port": 2375,
            "hostname": "",
            "env_vars": {},
            "privileged": True,
        }
        mock_create.return_value = {
            "name": "dind",
            "container_name": "oduflow-1-svc-dind",
            "image": "docker:dind",
            "url": "http://localhost:2375",
        }
        result = _get_tool_fn("restore_service")(name="dind")

        kwargs = mock_create.call_args.kwargs
        assert kwargs["privileged"] is True
        assert kwargs["cap_add"] is None
        assert "Privileged: true" in result

    @patch("oduflow.docker_ops.service_ops.create_service")
    @patch("oduflow.docker_ops.service_presets.get_preset")
    def test_restore_minimal_preset(self, mock_get, mock_create):
        mock_get.return_value = {
            "name": "redis",
            "image": "redis:7",
            "port": 6379,
            "hostname": "",
            "env_vars": {},
        }
        mock_create.return_value = {
            "name": "redis",
            "container_name": "oduflow-1-svc-redis",
            "image": "redis:7",
            "url": "http://localhost:6379",
        }
        _get_tool_fn("restore_service")(name="redis")

        kwargs = mock_create.call_args.kwargs
        assert kwargs["cap_add"] is None
        assert kwargs["privileged"] is False
        assert kwargs["host_mode"] is False
        assert kwargs["volumes"] is None


class TestHttpFailClosed:
    def test_build_auth_enables_host_relative_oauth_in_port_mode(self, tmp_path):
        from oduflow import server
        from oduflow.oauth_provider import OduflowOAuthProvider

        settings = Settings(
            base_data_dir=str(tmp_path),
            routing_mode="port",
            teams={
                "1": TeamSettings(
                    team_id="1",
                    hostname="oduflow.example.com",
                    auth_token="secret",
                )
            },
        )

        auth = server._build_auth(settings)

        assert isinstance(auth, OduflowOAuthProvider)

    def test_live_mount_security_warning_when_enabled(self, caplog):
        from oduflow import server

        settings = Settings(allow_local_path=True)

        with caplog.at_level(logging.WARNING, logger="oduflow"):
            server._warn_local_path_security(settings)

        assert "local_path live-mount mode is ENABLED" in caplog.text
        assert "read/write access" in caplog.text
        assert "allow_local_path = false" in caplog.text

    def test_live_mount_security_warning_skipped_when_disabled(self, caplog):
        from oduflow import server

        settings = Settings(allow_local_path=False)

        with caplog.at_level(logging.WARNING, logger="oduflow"):
            server._warn_local_path_security(settings)

        assert "local_path live-mount mode is ENABLED" not in caplog.text

    def test_start_http_refuses_without_auth(self):
        # Issue #37: HTTP transport must not serve /mcp unauthenticated.
        from oduflow import server
        from oduflow.errors import PrerequisiteNotMetError

        settings = Settings(
            bind_host="127.0.0.1",
            teams={"1": TeamSettings(team_id="1")},
            allow_insecure_http=False,
        )
        with (
            patch.object(server, "_get_settings", return_value=settings),
            patch.object(server, "_build_auth", return_value=None),
        ):
            with pytest.raises(PrerequisiteNotMetError, match="HTTP transport"):
                server._start_http()

    def test_start_http_allows_insecure_with_flag(self):
        from oduflow import server

        settings = Settings(
            bind_host="127.0.0.1",
            teams={"1": TeamSettings(team_id="1")},
            allow_insecure_http=True,
        )
        with (
            patch.object(server, "_get_settings", return_value=settings),
            patch.object(server, "_build_auth", return_value=None),
            patch("fastmcp.server.http.create_streamable_http_app"),
            patch("oduflow.web_ui.mount_web_ui"),
            patch("oduflow.reaper.start_reaper"),
            patch("uvicorn.run") as mock_uvicorn,
        ):
            server._start_http()
            mock_uvicorn.assert_called_once()

    def test_start_http_refuses_empty_team_map(self):
        from oduflow import server
        from oduflow.errors import PrerequisiteNotMetError

        settings = Settings(
            bind_host="127.0.0.1",
            teams={},
            allow_insecure_http=False,
        )
        with (
            patch.object(server, "_get_settings", return_value=settings),
            patch.object(server, "_ensure_web_ui_password", return_value=settings),
            patch.object(server, "_build_auth", return_value=object()),
        ):
            with pytest.raises(PrerequisiteNotMetError, match="web dashboard"):
                server._start_http()

    def test_port_mode_does_not_trust_forwarded_ips(self):
        # In port mode the TCP peer is the real client; trusting X-Forwarded-For
        # would let it spoof its IP, so uvicorn keeps the default trust list.
        from oduflow import server

        settings = Settings(
            bind_host="127.0.0.1",
            teams={"1": TeamSettings(team_id="1")},
            allow_insecure_http=True,
            routing_mode="port",
        )
        with (
            patch.object(server, "_get_settings", return_value=settings),
            patch.object(server, "_build_auth", return_value=None),
            patch("fastmcp.server.http.create_streamable_http_app"),
            patch("oduflow.web_ui.mount_web_ui"),
            patch("oduflow.reaper.start_reaper"),
            patch("uvicorn.run") as mock_uvicorn,
        ):
            server._start_http()
            _, kwargs = mock_uvicorn.call_args
            assert kwargs["proxy_headers"] is True
            assert kwargs["forwarded_allow_ips"] is None

    def test_traefik_mode_trusts_forwarded_ips(self):
        # Behind Traefik the peer is always the proxy, so uvicorn must read the
        # real client IP from X-Forwarded-For for the access log and login
        # rate-limiter.
        from oduflow import server

        settings = Settings(
            bind_host="0.0.0.0",
            teams={"1": TeamSettings(team_id="1", hostname="t1.example.com")},
            allow_insecure_http=True,
            routing_mode="traefik",
            routing_tls=False,
        )
        with (
            patch.object(server, "_get_settings", return_value=settings),
            patch.object(server, "_build_auth", return_value=None),
            patch("fastmcp.server.http.create_streamable_http_app"),
            patch("oduflow.web_ui.mount_web_ui"),
            patch("oduflow.reaper.start_reaper"),
            patch.object(
                server,
                "_traefik_forwarded_allow_ips",
                return_value=["127.0.0.1", "172.18.0.0/16"],
            ),
            patch("uvicorn.run") as mock_uvicorn,
        ):
            server._start_http()
            _, kwargs = mock_uvicorn.call_args
            assert kwargs["proxy_headers"] is True
            assert kwargs["forwarded_allow_ips"] == ["127.0.0.1", "172.18.0.0/16"]

    def test_traefik_proxy_trust_uses_stable_network_cidrs(self):
        from oduflow import server

        settings = Settings(shared_network="oduflow-net")
        network = type(
            "Network",
            (),
            {
                "attrs": {
                    "IPAM": {
                        "Config": [
                            {"Subnet": "172.18.0.0/16"},
                            {"Subnet": "fd00::/64"},
                        ]
                    }
                },
                "reload": lambda self: None,
            },
        )()
        client = type(
            "Client",
            (),
            {"networks": type("Networks", (), {"get": lambda self, name: network})()},
        )()

        with patch("oduflow.docker_ops.client.get_client", return_value=client):
            trusted = server._traefik_forwarded_allow_ips(settings)

        assert trusted == ["127.0.0.1", "172.18.0.0/16", "::1", "fd00::/64"]
        assert "*" not in trusted

    def test_traefik_proxy_trust_fails_closed_without_network(self):
        from oduflow import server
        from oduflow.errors import PrerequisiteNotMetError

        client = type(
            "Client",
            (),
            {
                "networks": type(
                    "Networks",
                    (),
                    {"get": lambda self, name: (_ for _ in ()).throw(OSError("down"))},
                )()
            },
        )()
        with (
            patch("oduflow.docker_ops.client.get_client", return_value=client),
            pytest.raises(PrerequisiteNotMetError, match="trusted proxy network"),
        ):
            server._traefik_forwarded_allow_ips(Settings(shared_network="oduflow-net"))


class TestOdooRpcTools:
    """The six execute_kw-equivalent tools: argument shaping and rendering."""

    @staticmethod
    def _result(value, ok=True, **kwargs):
        from oduflow.docker_ops.odoo_rpc import RpcResult

        return RpcResult(ok=ok, value=value, login="admin", uid=2, **kwargs)

    @patch("oduflow.docker_ops.env_ops.ensure_running", return_value=False)
    @patch("oduflow.docker_ops.odoo_rpc.call_kw")
    def test_search_read_shapes_args_and_renders_rows(self, mock_call, mock_ensure):
        mock_call.return_value = self._result([{"id": 1, "name": "Acme"}])

        result = _get_tool_fn("odoo_search_read")(
            env_name="main",
            model="res.partner",
            domain='[["customer_rank", ">", 0]]',
            fields="name,email",
            limit=5,
        )

        args = mock_call.call_args
        assert args[0][3:6] == (
            "res.partner",
            "search_read",
            [[["customer_rank", ">", 0]], ["name", "email"]],
        )
        assert args[0][6] == {"limit": 5, "offset": 0}
        assert "res.partner: 1 rows (as admin, limit 5)." in result
        assert '{"id":1,"name":"Acme"}' in result

    @patch("oduflow.docker_ops.env_ops.ensure_running", return_value=False)
    @patch("oduflow.docker_ops.odoo_rpc.call_kw")
    def test_search_read_bare_leaf_domain_is_wrapped(self, mock_call, mock_ensure):
        mock_call.return_value = self._result([])

        _get_tool_fn("odoo_search_read")(
            env_name="main", model="res.partner", domain="['name', '=', 'x']"
        )

        assert mock_call.call_args[0][5][0] == [["name", "=", "x"]]

    @patch("oduflow.docker_ops.env_ops.ensure_running", return_value=False)
    @patch("oduflow.docker_ops.odoo_rpc.call_kw")
    def test_count_only_uses_search_count(self, mock_call, mock_ensure):
        mock_call.return_value = self._result(1284)

        result = _get_tool_fn("odoo_search_read")(
            env_name="main", model="res.partner", count_only=True
        )

        assert mock_call.call_args[0][4] == "search_count"
        assert "1284 records match" in result

    @patch("oduflow.docker_ops.env_ops.ensure_running", return_value=False)
    @patch("oduflow.docker_ops.odoo_rpc.call_kw")
    def test_write_sends_ids_then_values(self, mock_call, mock_ensure):
        mock_call.return_value = self._result(True)

        result = _get_tool_fn("odoo_write")(
            env_name="main",
            model="res.partner",
            ids="1,2",
            values='{"comment": "x"}',
        )

        assert mock_call.call_args[0][5] == [[1, 2], {"comment": "x"}]
        assert "Committed." in result

    @patch("oduflow.docker_ops.env_ops.ensure_running", return_value=False)
    @patch("oduflow.docker_ops.odoo_rpc.call_kw")
    def test_unlink_warns_it_is_not_recoverable(self, mock_call, mock_ensure):
        mock_call.return_value = self._result(True)

        result = _get_tool_fn("odoo_unlink")(
            env_name="main", model="res.partner", ids="[7]"
        )

        assert mock_call.call_args[0][4] == "unlink"
        assert mock_call.call_args[0][5] == [[7]]
        assert "not recoverable" in result

    @patch("oduflow.docker_ops.env_ops.ensure_running", return_value=False)
    @patch("oduflow.docker_ops.odoo_rpc.call_kw")
    def test_create_accepts_several_records(self, mock_call, mock_ensure):
        mock_call.return_value = self._result([42, 43])

        result = _get_tool_fn("odoo_create")(
            env_name="main",
            model="res.partner",
            values='[{"name": "a"}, {"name": "b"}]',
        )

        assert mock_call.call_args[0][5] == [[{"name": "a"}, {"name": "b"}]]
        assert "2 record(s)" in result
        assert "[42, 43]" in result

    @patch("oduflow.docker_ops.env_ops.ensure_running", return_value=False)
    @patch("oduflow.docker_ops.odoo_rpc.call_kw")
    def test_call_prepends_ids_to_args(self, mock_call, mock_ensure):
        mock_call.return_value = self._result(True)

        _get_tool_fn("odoo_call")(
            env_name="main",
            model="sale.order",
            method="action_confirm",
            ids="42",
        )

        assert mock_call.call_args[0][5] == [[42]]

    @patch("oduflow.docker_ops.env_ops.ensure_running", return_value=False)
    @patch("oduflow.docker_ops.odoo_rpc.call_kw")
    def test_call_merges_context_into_kwargs(self, mock_call, mock_ensure):
        mock_call.return_value = self._result(True)

        _get_tool_fn("odoo_call")(
            env_name="main",
            model="res.partner",
            method="name_search",
            kwargs='{"name": "Acme"}',
            context='{"lang": "fr_FR"}',
        )

        assert mock_call.call_args[0][6] == {
            "name": "Acme",
            "context": {"lang": "fr_FR"},
        }

    @pytest.mark.parametrize("method", ["create", "write", "unlink"])
    @patch("oduflow.docker_ops.env_ops.ensure_running", return_value=False)
    @patch("oduflow.docker_ops.odoo_rpc.call_kw")
    def test_call_rejects_dedicated_mutations(self, mock_call, mock_ensure, method):
        with pytest.raises(ToolError, match=f"dedicated odoo_{method} tool"):
            _get_tool_fn("odoo_call")(
                env_name="main", model="res.partner", method=method, ids="1"
            )

        mock_ensure.assert_not_called()
        mock_call.assert_not_called()

    @patch("oduflow.docker_ops.env_ops.ensure_running", return_value=False)
    @patch("oduflow.docker_ops.odoo_rpc.call_kw")
    def test_schema_lists_models_when_model_is_empty(self, mock_call, mock_ensure):
        mock_call.return_value = self._result(
            [{"model": "sale.order", "name": "Sales Order", "transient": False}]
        )

        result = _get_tool_fn("odoo_schema")(
            env_name="main", name_filter="sale", limit=50, offset=100
        )

        assert mock_call.call_args[0][3:5] == ("ir.model", "search_read")
        assert mock_call.call_args[0][6] == {
            "limit": 50,
            "offset": 100,
            "order": "model",
        }
        assert "Models matching 'sale': 1" in result

    @patch("oduflow.docker_ops.env_ops.ensure_running", return_value=False)
    @patch("oduflow.docker_ops.odoo_rpc.call_kw")
    def test_schema_reports_a_full_page_as_possibly_truncated(
        self, mock_call, mock_ensure
    ):
        mock_call.return_value = self._result(
            [
                {"model": f"x.model.{index}", "name": str(index), "transient": False}
                for index in range(2)
            ]
        )

        result = _get_tool_fn("odoo_schema")(env_name="main", limit=2, offset=4)

        assert "there may be more" in result
        assert "offset=6" in result

    @patch("oduflow.docker_ops.env_ops.ensure_running", return_value=False)
    @patch("oduflow.docker_ops.odoo_rpc.call_kw")
    def test_schema_filters_fields_by_name(self, mock_call, mock_ensure):
        mock_call.return_value = self._result(
            {"name": {"type": "char"}, "email": {"type": "char"}}
        )

        result = _get_tool_fn("odoo_schema")(
            env_name="main", model="res.partner", name_filter="mail"
        )

        assert mock_call.call_args[0][4] == "fields_get"
        assert "res.partner: 1 fields" in result
        assert "email" in result and '"name"' not in result

    @patch("oduflow.docker_ops.env_ops.ensure_running", return_value=False)
    @patch("oduflow.docker_ops.odoo_rpc.call_kw")
    def test_odoo_error_is_returned_not_raised(self, mock_call, mock_ensure):
        mock_call.return_value = self._result(
            None,
            ok=False,
            error_name="odoo.exceptions.AccessError",
            error_message="not allowed",
            error_debug="Traceback: line 1",
        )

        result = _get_tool_fn("odoo_write")(
            env_name="main",
            model="res.partner",
            ids="1",
            values='{"comment": "x"}',
            as_user="portal@example.com",
        )

        assert "Error (as admin)." in result
        assert "AccessError: not allowed" in result
        assert "Traceback: line 1" in result

    @patch("oduflow.docker_ops.env_ops.ensure_running", return_value=False)
    @patch("oduflow.docker_ops.odoo_rpc.call_kw")
    def test_cold_mint_is_announced(self, mock_call, mock_ensure):
        mock_call.return_value = self._result([], minted=True)

        result = _get_tool_fn("odoo_search_read")(env_name="main", model="res.partner")

        assert "minted a new Odoo session for admin" in result

    @pytest.mark.parametrize("superuser", ["1", "__system__"])
    @patch("oduflow.docker_ops.env_ops.ensure_running", return_value=False)
    @patch("oduflow.docker_ops.odoo_rpc.call_kw")
    def test_superuser_is_rejected(self, mock_call, mock_ensure, superuser):
        with pytest.raises(ToolError, match="run_odoo_shell"):
            _get_tool_fn("odoo_search_read")(
                env_name="main", model="res.partner", as_user=superuser
            )
        mock_call.assert_not_called()

    @patch("oduflow.docker_ops.env_ops.ensure_running", return_value=False)
    @patch("oduflow.docker_ops.odoo_rpc.call_kw")
    def test_private_method_points_at_run_odoo_shell(self, mock_call, mock_ensure):
        mock_call.side_effect = ValueError(
            "Method '_compute' is private and is not callable over RPC "
            "(Odoo 19 rejects it server-side too). Use run_odoo_shell for "
            "private methods and registry internals."
        )

        with pytest.raises(ToolError, match="run_odoo_shell"):
            _get_tool_fn("odoo_call")(
                env_name="main", model="res.partner", method="_compute"
            )

    def test_tools_are_reachable_from_a_scoped_env_token(self):
        from oduflow.scoped_access import SCOPED_ALLOWLIST

        for name in (
            "odoo_search_read",
            "odoo_create",
            "odoo_write",
            "odoo_unlink",
            "odoo_call",
            "odoo_schema",
        ):
            assert name in SCOPED_ALLOWLIST


class TestResetAdminPasswordDropsSessions:
    @patch("oduflow.docker_ops.env_ops.ensure_running", return_value=False)
    @patch("oduflow.docker_ops.odoo_ops.reset_admin_password")
    def test_cached_sessions_are_invalidated(self, mock_reset, mock_ensure):
        from oduflow.docker_ops import odoo_rpc

        mock_reset.return_value = {"status": "ok", "login": "admin", "psql_output": ""}
        odoo_rpc._SESSIONS[("1", "main", "")] = odoo_rpc._CachedSession(
            "sid", "admin", 2, time.time() + 1000
        )

        _get_tool_fn("reset_admin_password")(env_name="main")

        assert ("1", "main", "") not in odoo_rpc._SESSIONS


def _po_entry(msgid: str, reference: str = "") -> PoEntry:
    return PoEntry(
        msgid=msgid,
        msgstr="",
        module="mymod",
        occurrences=(reference,) if reference else (),
    )


class TestTranslationTools:
    """The two i18n tools, at the layer that turns backend data into a report."""

    _SUMMARY = PoSummary(
        entries=311,
        translated=301,
        untranslated=10,
        by_type={"model": 161, "model_terms": 82, "code": 68},
        no_reference=0,
        no_module_comment=0,
        untranslated_terms=[_po_entry("Active"), _po_entry("Company")],
    )

    def _lang(self, lang="pl_PL", *, active=True, missing=(), stale=(), **entry):
        """One backend language record, verdict included as the backend adds it."""
        record = {"lang": lang, "active": active, "file_path": "", **entry}
        if record["file_path"]:
            record["diff"] = {"missing": list(missing), "stale": list(stale)}
            record.setdefault("metadata_template_path", "")
        record["status"] = po_tools.diagnose(
            self._SUMMARY,
            active=active,
            database=record.get("database"),
            file=record.get("file"),
            effective=record.get("import_effective"),
            missing=len(missing),
        )
        return record

    def _status_result(self, *langs):
        return {
            "module": "mymod",
            "module_dir": "/mnt/extra-addons/mymod",
            "template": self._SUMMARY,
            "active_langs": ["en_US", "pl_PL"],
            "langs": list(langs),
        }

    def _export_result(self, **overrides):
        base = {
            "module": "mymod",
            "lang": "",
            "filename": "mymod.pot",
            "content": 'msgid "Budget Ceiling"\nmsgstr ""\n',
            "summary": self._SUMMARY,
            "module_dir": "/mnt/extra-addons/mymod",
            "written_path": "/mnt/extra-addons/mymod/i18n/mymod.pot",
            "host_path": "/home/dev/veles/mymod/i18n/mymod.pot",
            "read_only_mount": False,
        }
        base.update(overrides)
        return base

    @patch("oduflow.docker_ops.env_ops.ensure_running", return_value=False)
    @patch("oduflow.docker_ops.odoo_ops.export_module_translations")
    def test_export_reports_counts_and_where_the_file_landed(
        self, mock_export, mock_ensure
    ):
        mock_export.return_value = self._export_result()
        result = _get_tool_fn("export_module_translations")(
            env_name="main", module="mymod"
        )

        assert "Terms: 311" in result
        assert "model_terms" in result and "82" in result
        assert "code" in result and "68" in result
        assert "/mnt/extra-addons/mymod/i18n/mymod.pot" in result
        assert "/home/dev/veles/mymod/i18n/mymod.pot" in result
        # The catalogue itself must never be inlined into the response.
        assert "Budget Ceiling" not in result
        mock_export.assert_called_once_with(
            TEST_SETTINGS, TEST_TEAM, "main", "mymod", ""
        )

    @patch("oduflow.docker_ops.env_ops.ensure_running", return_value=False)
    @patch("oduflow.docker_ops.odoo_ops.export_module_translations")
    def test_export_offers_a_download_url_when_the_web_ui_is_up(
        self, mock_export, mock_ensure
    ):
        import oduflow.server

        mock_export.return_value = self._export_result()
        oduflow.server._web_bind = ("0.0.0.0", 8000)
        try:
            result = _get_tool_fn("export_module_translations")(
                env_name="main", module="mymod"
            )
        finally:
            oduflow.server._web_bind = None

        # 0.0.0.0 is not a name anything can dial.
        assert "http://localhost:8000/oduflow-artifact?token=" in result

    def test_port_mode_download_uses_the_configured_public_hostname(self):
        import oduflow.server

        remote_team = replace(TEST_TEAM, hostname="oduflow.example.com")
        oduflow.server._web_bind = ("0.0.0.0", 8000)
        try:
            url = oduflow.server._artifact_url(
                TEST_SETTINGS, remote_team, "download-token"
            )
        finally:
            oduflow.server._web_bind = None

        assert url == (
            "http://oduflow.example.com:8000/oduflow-artifact?token=download-token"
        )

    @patch("oduflow.docker_ops.env_ops.ensure_running", return_value=False)
    @patch("oduflow.docker_ops.odoo_ops.export_module_translations")
    def test_export_without_web_ui_falls_back_to_the_host_path(
        self, mock_export, mock_ensure
    ):
        # Under stdio there is no server to download from — but Oduflow and the
        # agent share a machine there, so the host path is the better answer.
        mock_export.return_value = self._export_result()
        result = _get_tool_fn("export_module_translations")(
            env_name="main", module="mymod"
        )
        assert "oduflow-artifact" not in result
        assert "Host path: /home/dev/veles/mymod/i18n/mymod.pot" in result

    @patch("oduflow.docker_ops.env_ops.ensure_running", return_value=False)
    @patch("oduflow.docker_ops.odoo_ops.export_module_translations")
    def test_export_warns_when_no_code_terms_were_found(self, mock_export, mock_ensure):
        # The exporter finds these by reading the module's sources off
        # addons_path, so an empty count points at an addons_path problem
        # rather than at a module with no messages.
        summary = replace(self._SUMMARY, by_type={"model": 161})
        mock_export.return_value = self._export_result(summary=summary)
        result = _get_tool_fn("export_module_translations")(
            env_name="main", module="mymod"
        )
        assert "no `code:` terms were exported" in result
        assert "addons_path" in result

    @patch("oduflow.docker_ops.env_ops.ensure_running", return_value=False)
    @patch("oduflow.docker_ops.odoo_ops.export_module_translations")
    def test_export_explains_a_read_only_extra_addons_mount(
        self, mock_export, mock_ensure
    ):
        mock_export.return_value = self._export_result(
            module_dir="/mnt/extra-addons-enterprise/mymod",
            written_path="",
            host_path="",
            read_only_mount=True,
        )
        result = _get_tool_fn("export_module_translations")(
            env_name="main", module="mymod"
        )
        assert "mounted read-only" in result
        assert "Host path:" in result
        path = result.split("Host path: ", 1)[1].split("  ", 1)[0]
        with open(path, "rb") as artifact:
            assert artifact.read() == b'msgid "Budget Ceiling"\nmsgstr ""\n'

    @patch("oduflow.docker_ops.env_ops.ensure_running", return_value=False)
    @patch("oduflow.docker_ops.odoo_ops.export_module_translations")
    def test_export_materializes_core_module_artifact_under_stdio(
        self, mock_export, mock_ensure
    ):
        mock_export.return_value = self._export_result(
            module_dir="",
            written_path="",
            host_path="",
            read_only_mount=False,
        )

        result = _get_tool_fn("export_module_translations")(
            env_name="main", module="mymod"
        )

        assert "Core Odoo modules" in result
        assert "Host path:" in result

    @patch("oduflow.docker_ops.env_ops.ensure_running", return_value=False)
    @patch("oduflow.docker_ops.odoo_ops.export_module_translations")
    def test_export_surfaces_backend_errors(self, mock_export, mock_ensure):
        from oduflow.errors import PrerequisiteNotMetError

        mock_export.side_effect = PrerequisiteNotMetError(
            "Module 'mymod' is 'uninstalled', not installed."
        )
        with pytest.raises(ToolError, match="not installed"):
            _get_tool_fn("export_module_translations")(env_name="main", module="mymod")

    @patch("oduflow.docker_ops.env_ops.ensure_running", return_value=False)
    @patch("oduflow.docker_ops.odoo_ops.translation_status")
    def test_status_shouts_about_a_po_that_imports_to_nothing(
        self, mock_status, mock_ensure
    ):
        empty = PoSummary(0, 0, 0, {}, 0, 0, [])
        broken = PoSummary(
            entries=311,
            translated=311,
            untranslated=0,
            by_type={},
            no_reference=311,
            no_module_comment=4,
            untranslated_terms=[],
        )
        mock_status.return_value = self._status_result(
            self._lang(
                database=empty,
                file_path="/mnt/extra-addons/mymod/i18n/pl_PL.po",
                file=broken,
                import_effective=broken,
                missing=[_po_entry("Active")],
                stale=[_po_entry("Old string")],
            )
        )
        result = _get_tool_fn("translation_status")(env_name="main", module="mymod")

        assert "IMPORT SILENTLY DROPPED" in result
        assert "311 entries have no '#:' reference" in result
        assert "ZERO translations" in result
        assert "4 entries have no '#. module:' comment" in result
        assert "database 0/311 translated (0%)" in result
        assert "Active" in result
        assert "Old string" in result
        # The fix is the sibling template, not another upgrade on its own.
        assert 'Next: export_module_translations("main", "mymod") writes' in result
        mock_status.assert_called_once_with(
            TEST_SETTINGS, TEST_TEAM, "main", "mymod", None
        )

    @patch("oduflow.docker_ops.env_ops.ensure_running", return_value=False)
    @patch("oduflow.docker_ops.odoo_ops.translation_status")
    def test_status_does_not_warn_when_sibling_pot_supplies_metadata(
        self, mock_status, mock_ensure
    ):
        raw = PoSummary(311, 311, 0, {}, 311, 311, [])
        effective = PoSummary(311, 311, 0, {"model": 311}, 0, 0, [])
        mock_status.return_value = self._status_result(
            self._lang(
                database=PoSummary(311, 311, 0, {"model": 311}, 0, 0, []),
                file_path="/mnt/extra-addons/mymod/i18n/pl.po",
                file=raw,
                import_effective=effective,
                metadata_template_path="/mnt/extra-addons/mymod/i18n/mymod.pot",
            )
        )

        result = _get_tool_fn("translation_status")(env_name="main", module="mymod")

        assert "Import metadata: merged from" in result
        assert "imports these as ZERO" not in result
        assert "abort the import" not in result
        # Nothing left to do, so nothing is prescribed.
        assert "--- pl_PL ---  OK" in result
        assert "Next:" not in result

    @patch("oduflow.docker_ops.env_ops.ensure_running", return_value=False)
    @patch("oduflow.docker_ops.odoo_ops.translation_status")
    def test_status_says_so_when_a_language_is_not_activated(
        self, mock_status, mock_ensure
    ):
        mock_status.return_value = self._status_result(
            self._lang("ru_RU", active=False)
        )
        result = _get_tool_fn("translation_status")(
            env_name="main", module="mymod", langs="ru_RU"
        )

        assert "NOT activated" in result
        assert "Next: activate ru_RU in Odoo" in result
        # A language with no catalogue must never be counted as fully covered.
        assert "file 0/311 module terms" in result
        mock_status.assert_called_once_with(
            TEST_SETTINGS, TEST_TEAM, "main", "mymod", ["ru_RU"]
        )

    @patch("oduflow.docker_ops.env_ops.ensure_running", return_value=False)
    @patch("oduflow.docker_ops.odoo_ops.translation_status")
    def test_status_answers_an_untranslated_language_in_a_few_lines(
        self, mock_status, mock_ensure
    ):
        # A file covering 3 of 311 terms says "nobody translated this", and the
        # other 308 msgids say it again, at 100x the length.
        tiny = PoSummary(3, 3, 0, {}, 0, 0, [])
        mock_status.return_value = self._status_result(
            self._lang(
                database=PoSummary(311, 20, 291, {}, 0, 0, []),
                file_path="/mnt/extra-addons/mymod/i18n/ru.po",
                file=tiny,
                import_effective=tiny,
                missing=[_po_entry(f"Term {i}") for i in range(308)],
            )
        )

        result = _get_tool_fn("translation_status")(env_name="main", module="mymod")

        assert "NOT TRANSLATED" in result
        assert "file 3/311 module terms, database 20/311 translated (6%)" in result
        assert "Missing from the file (308) — too many to list" in result
        assert "Term 42" not in result
        assert "and 293 more" not in result
        assert "Next: export_module_translations" in result
        # The whole language fits well inside what the old preview alone cost.
        assert len(result.splitlines()) < 20

    @patch("oduflow.docker_ops.env_ops.ensure_running", return_value=False)
    @patch("oduflow.docker_ops.odoo_ops.translation_status")
    def test_status_lists_a_short_diff_by_reference_on_one_line(
        self, mock_status, mock_ensure
    ):
        # A report term's msgid is a page of markup; the "#:" reference is the
        # part that names the view to open.
        arch = _po_entry(
            "Place / Place\n     On / On\n     Signature and stamp of the "
            "consignee, plus a great deal more boilerplate",
            "model_terms:ir.ui.view,arch_db:mymod.report_cmr",
        )
        loaded = PoSummary(310, 310, 0, {}, 0, 0, [])
        mock_status.return_value = self._status_result(
            self._lang(
                database=PoSummary(311, 310, 1, {}, 0, 0, []),
                file_path="/mnt/extra-addons/mymod/i18n/pl.po",
                file=loaded,
                import_effective=loaded,
                missing=[arch],
            )
        )

        result = _get_tool_fn("translation_status")(env_name="main", module="mymod")

        (line,) = [ln for ln in result.splitlines() if "report_cmr" in ln]
        assert line.startswith("    - model_terms:ir.ui.view,arch_db:mymod.report_cmr")
        # One line, a readable snippet, and the rest of the markup left behind.
        assert line.endswith('…"')
        assert "boilerplate" not in result

    @patch("oduflow.docker_ops.env_ops.ensure_running", return_value=False)
    @patch("oduflow.docker_ops.odoo_ops.translation_status")
    def test_status_counts_what_the_file_holds_but_the_database_does_not(
        self, mock_status, mock_ensure
    ):
        # The gap between a good file and the database is the whole reason to
        # run this tool a second time; it must not be left for the reader to
        # subtract.
        file = PoSummary(304, 304, 0, {}, 0, 0, [])
        mock_status.return_value = self._status_result(
            self._lang(
                database=PoSummary(311, 248, 63, {}, 0, 0, []),
                file_path="/mnt/extra-addons/mymod/i18n/pl.po",
                file=file,
                import_effective=file,
                missing=[_po_entry(f"Term {i}") for i in range(7)],
            )
        )

        result = _get_tool_fn("translation_status")(env_name="main", module="mymod")

        assert "--- pl_PL ---  PARTIAL" in result
        assert "! 56 translated entries in the file are not in the database." in result
        assert 'Next: upgrade_odoo_modules("main", "mymod") re-reads' in result

    def test_translation_tools_in_scoped_allowlist(self):
        from oduflow.scoped_access import SCOPED_ALLOWLIST

        # Part of the per-environment dev loop, like install/upgrade_odoo_modules.
        assert "export_module_translations" in SCOPED_ALLOWLIST
        assert "translation_status" in SCOPED_ALLOWLIST
