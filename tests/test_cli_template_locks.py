"""Direct CLI template commands share the server's file locks without
freezing on every environment operation the server runs in the team."""

import os
from unittest.mock import Mock

import pytest

from oduflow import server
from oduflow.docker_ops import system_ops
from oduflow.errors import BusyError
from oduflow.locking import LockManager, template_lock_key
from oduflow.settings import Settings, TeamSettings


@pytest.fixture
def cli(tmp_path, monkeypatch):
    team = TeamSettings(team_id="1", data_dir=str(tmp_path / "team"))
    settings = Settings(base_data_dir=str(tmp_path), teams={"1": team})
    importer = Mock(return_value={})
    monkeypatch.setattr(system_ops, "import_template", importer)
    monkeypatch.setattr(server, "_format_template_import_result", lambda r: "done")
    monkeypatch.setattr(server, "ATTACH_SWAP_LOCK_TIMEOUT", 0.05)
    # The running server, mid-way through an environment operation.
    live = LockManager(os.path.join(settings.base_data_dir, ".locks"))
    live.acquire_env("dev", "1", operation="run_odoo_tests")
    yield settings, team, importer, live
    live.release_env("dev")


def test_new_template_import_needs_only_its_key(cli):
    settings, team, importer, _ = cli
    server._run_import_template(settings, team, source="/backup", template_name="new")
    importer.assert_called_once()


def test_replacing_import_waits_for_the_team(cli):
    settings, team, importer, _ = cli
    with pytest.raises(BusyError):
        server._run_import_template(
            settings, team, source="/backup", template_name="prod", overwrite=True
        )
    importer.assert_not_called()


def test_template_key_still_excludes_the_server(cli):
    settings, team, importer, live = cli
    with live.env_lock(template_lock_key("1", "new")):
        with pytest.raises(BusyError):
            server._run_import_template(
                settings, team, source="/backup", template_name="new"
            )
    importer.assert_not_called()


def test_attach_takes_the_team_only_for_its_swap(cli, monkeypatch):
    settings, team, _, live = cli
    seen = {}

    def attach(settings, team, template_name, source, **kwargs):
        seen["lock"] = kwargs["lock"]
        return {
            "template_name": template_name,
            "source": source,
            "source_kind": "directory",
            "filestore_files": 0,
            "filestore_size_mb": 0,
        }

    monkeypatch.setattr(system_ops, "attach_filestore", attach)
    # Staging runs while the server works on an environment.
    server._run_attach_filestore(settings, team, "prod", "/files")
    with pytest.raises(BusyError):
        with seen["lock"]():
            pass
    live.release_env("dev")
    with seen["lock"]():
        pass
    live.acquire_env("dev", "1")


def test_pinned_environments_are_named_in_the_output():
    assert server._pinned_envs_lines({}) == []
    (line,) = server._pinned_envs_lines({"pinned_envs": ["a", "b"]})
    assert line.endswith(": a, b")
