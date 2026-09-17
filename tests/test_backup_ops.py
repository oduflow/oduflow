"""Unit tests for backup_ops helpers that need no Docker/S3."""

from __future__ import annotations

import hashlib
import os
from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from oduflow import backup_ops
from oduflow.docker_ops import production_ops
from oduflow.errors import ExternalCommandError, PrerequisiteNotMetError
from oduflow.naming import get_filestore_paths
from oduflow.settings import BackupSettings, Settings, TeamSettings


class _FakeApi:
    def __init__(self, frames, exit_code):
        self._frames = frames
        self._exit = exit_code

    def exec_create(self, container_id, cmd):
        return {"Id": "exec-1"}

    def exec_start(self, exec_id, stream, demux):
        return iter(self._frames)

    def exec_inspect(self, exec_id):
        return {"ExitCode": self._exit}


class _FakeContainer:
    def __init__(self, frames, exit_code):
        self.id = "cid"
        self.client = type("_C", (), {"api": _FakeApi(frames, exit_code)})()


class TestDumpStream:
    def test_yields_stdout_and_ignores_stderr_on_success(self):
        container = _FakeContainer(
            [(b"a", None), (None, b"NOTICE: ..."), (b"b", None)], exit_code=0
        )
        assert list(backup_ops._dump_stream(container, "odoo", "db")) == [b"a", b"b"]

    def test_raises_on_nonzero_exit_after_streaming(self):
        # pg_dump emits some output, then dies mid-dump (exit 1). The generator
        # must raise after the last frame so the snapshot fails before the
        # manifest is written (no truncated dump recorded as success).
        container = _FakeContainer([(b"partial", None)], exit_code=1)
        gen = backup_ops._dump_stream(container, "odoo", "db")
        assert next(gen) == b"partial"
        with pytest.raises(ExternalCommandError):
            next(gen)


class TestFilestoreRestore:
    def test_manifest_requires_explicit_revision(self):
        with pytest.raises(PrerequisiteNotMetError, match="no filestore revision"):
            backup_ops._filestore_revision_from_manifest({"filestore": {}})

    def test_revision_zero_is_valid(self):
        assert (
            backup_ops._filestore_revision_from_manifest({"filestore": {"revision": 0}})
            == 0
        )

    def test_empty_staged_filestore_replaces_old_files(self, tmp_path):
        live = tmp_path / "filestore"
        staged = tmp_path / "staged"
        old = tmp_path / "old"
        live.mkdir()
        staged.mkdir()
        (live / "attachment").write_text("old")

        had_previous = backup_ops._swap_restored_filestore(
            str(staged), str(live), str(old)
        )

        assert had_previous is True
        assert list(live.iterdir()) == []
        assert (old / "attachment").read_text() == "old"

    def test_failed_filestore_swap_restores_old_files(self, tmp_path, monkeypatch):
        live = tmp_path / "filestore"
        staged = tmp_path / "staged"
        old = tmp_path / "old"
        live.mkdir()
        staged.mkdir()
        (live / "attachment").write_text("old")
        (staged / "attachment").write_text("new")
        real_replace = os.replace

        def fail_staged(src, dst):
            if src == str(staged):
                raise OSError("swap failed")
            return real_replace(src, dst)

        monkeypatch.setattr(backup_ops.os, "replace", fail_staged)

        with pytest.raises(OSError, match="swap failed"):
            backup_ops._swap_restored_filestore(str(staged), str(live), str(old))

        assert (live / "attachment").read_text() == "old"
        assert (staged / "attachment").read_text() == "new"


def _restore_settings(tmp_path):
    team = TeamSettings(team_id="1", data_dir=str(tmp_path / "team"))
    settings = Settings(
        base_data_dir=str(tmp_path),
        db_user="odoo",
        db_password="secret",
        backup=BackupSettings(bucket="bucket", access_key="key", secret_key="secret"),
        teams={"1": team},
    )
    return settings, team


def _manifest(revision=1):
    dump = b"database dump"
    return {
        "id": "snap",
        "db": {
            "bytes": len(dump),
            "key": "db/snap.pgdump",
            "sha256": hashlib.sha256(dump).hexdigest(),
        },
        "filestore": {"revision": revision},
    }


def _restore_patches(manifest, *, container=None):
    client = MagicMock()
    pg = MagicMock()
    pg.exec_run.return_value = (0, b"")
    client.containers.get.return_value = pg
    s3 = MagicMock()

    def download(_bucket, _key, destination):
        with open(destination, "wb") as f:
            f.write(b"database dump")

    s3.download_file.side_effect = download
    return (
        client,
        pg,
        s3,
        (
            patch.object(
                backup_ops.production_registry, "get_production", return_value={}
            ),
            patch.object(backup_ops, "_load_manifest", return_value=manifest),
            patch.object(backup_ops, "get_client", return_value=client),
            patch.object(production_ops, "_get_container", return_value=container),
            patch.object(backup_ops.s3_client, "make_client", return_value=s3),
            patch.object(backup_ops, "_copy_file_to_container"),
            patch.object(
                backup_ops,
                "load_credentials",
                return_value={"pg_user": "prod_user", "pg_password": "pw"},
            ),
            patch.object(backup_ops, "reassign_db_ownership"),
            patch.object(backup_ops, "drop_signaling_sequences"),
            patch.object(backup_ops, "filestore_storage", return_value=MagicMock()),
        ),
    )


def test_chunk_restore_failure_happens_before_live_database_swap(tmp_path):
    settings, team = _restore_settings(tmp_path)
    manifest = _manifest()
    _client, _pg, _s3, patches = _restore_patches(manifest)
    with ExitStack() as stack:
        for patcher in patches:
            stack.enter_context(patcher)
        stack.enter_context(
            patch.object(
                backup_ops.chunkstore, "restore", side_effect=OSError("chunk failed")
            )
        )
        sql = stack.enter_context(patch.object(backup_ops, "_exec_sql"))
        with pytest.raises(OSError, match="chunk failed"):
            backup_ops.restore_production(settings, team, "erp", "snap")

    statements = [call.args[2] for call in sql.call_args_list]
    assert not any("ALTER DATABASE" in statement for statement in statements)


def test_filestore_preparation_failure_does_not_stop_production(tmp_path):
    settings, team = _restore_settings(tmp_path)
    container = MagicMock(status="running")
    manifest = _manifest()
    _client, _pg, _s3, patches = _restore_patches(manifest, container=container)

    with ExitStack() as stack:
        for patcher in patches:
            stack.enter_context(patcher)
        stack.enter_context(
            patch.object(
                backup_ops.chunkstore, "restore", side_effect=OSError("chunk failed")
            )
        )
        with pytest.raises(OSError, match="chunk failed"):
            backup_ops.restore_production(settings, team, "erp", "snap")

    container.stop.assert_not_called()
    container.start.assert_not_called()


def test_filestore_swap_failure_rolls_database_back(tmp_path):
    settings, team = _restore_settings(tmp_path)
    manifest = _manifest()
    _client, _pg, _s3, patches = _restore_patches(manifest)
    db_name = production_ops.prod_db_name(team, "erp")

    with ExitStack() as stack:
        for patcher in patches:
            stack.enter_context(patcher)
        stack.enter_context(patch.object(backup_ops.chunkstore, "restore"))
        stack.enter_context(
            patch.object(
                backup_ops,
                "_swap_restored_filestore",
                side_effect=OSError("filestore swap failed"),
            )
        )
        sql = stack.enter_context(patch.object(backup_ops, "_exec_sql"))
        with pytest.raises(OSError, match="filestore swap failed"):
            backup_ops.restore_production(settings, team, "erp", "snap")

    statements = [call.args[2] for call in sql.call_args_list]
    assert f'ALTER DATABASE "{db_name}" RENAME TO "{db_name}__old";' in statements
    assert f'ALTER DATABASE "{db_name}__restore" RENAME TO "{db_name}";' in statements
    assert f'ALTER DATABASE "{db_name}" RENAME TO "{db_name}__restore";' in statements
    assert f'ALTER DATABASE "{db_name}__old" RENAME TO "{db_name}";' in statements


def _env_restore_settings(tmp_path):
    """Settings WITHOUT [backup]: restore-from-environment needs no S3."""
    team = TeamSettings(team_id="1", data_dir=str(tmp_path / "team"))
    settings = Settings(
        base_data_dir=str(tmp_path),
        db_user="odoo",
        db_password="secret",
        teams={"1": team},
    )
    return settings, team


def _env_restore_patches(*, container=None, source_env=None, copy_result=None):
    client = MagicMock()
    pg = MagicMock()
    pg.exec_run.return_value = (0, b"")
    client.containers.get.return_value = pg
    if source_env is None:
        source_env = {
            "container": MagicMock(status="running"),
            "template": "",
            "head_commit": "",
        }
    return (
        client,
        source_env,
        (
            patch.object(
                backup_ops.production_registry, "get_production", return_value={}
            ),
            patch.object(backup_ops.production_registry, "update_production"),
            patch.object(backup_ops, "get_client", return_value=client),
            patch.object(production_ops, "_source_env_info", return_value=source_env),
            patch.object(production_ops, "_get_container", return_value=container),
            patch.object(
                production_ops,
                "_copy_env_data_into_production",
                return_value=copy_result or [],
            ),
            patch.object(production_ops, "wait_production_healthy", return_value=True),
            patch.object(production_ops, "append_deploy"),
            patch.object(
                backup_ops,
                "load_credentials",
                return_value={"pg_user": "prod_user", "pg_password": "pw"},
            ),
            patch.object(backup_ops, "reassign_db_ownership"),
            patch.object(backup_ops, "drop_signaling_sequences"),
        ),
    )


def test_env_restore_swaps_staged_pair_without_backup_config(tmp_path):
    settings, team = _env_restore_settings(tmp_path)
    prod_container = MagicMock(status="running")
    _client, _env, patches = _env_restore_patches(container=prod_container)
    db_name = production_ops.prod_db_name(team, "erp")

    with ExitStack() as stack:
        for patcher in patches:
            stack.enter_context(patcher)
        sql = stack.enter_context(patch.object(backup_ops, "_exec_sql"))
        result = backup_ops.restore_production_from_environment(
            settings, team, "erp", "feature-x"
        )

    statements = [call.args[2] for call in sql.call_args_list]
    assert f'CREATE DATABASE "{db_name}__restore";' in statements
    assert f'ALTER DATABASE "{db_name}" RENAME TO "{db_name}__old";' in statements
    assert f'ALTER DATABASE "{db_name}__restore" RENAME TO "{db_name}";' in statements
    prod_container.stop.assert_called_once_with()
    prod_container.start.assert_called_once_with()
    assert result["healthy"] is True
    assert result["source_environment"] == "feature-x"
    assert result["warning"] == ""


def test_env_restore_copy_failure_leaves_production_running(tmp_path):
    settings, team = _env_restore_settings(tmp_path)
    prod_container = MagicMock(status="running")
    _client, _env, patches = _env_restore_patches(container=prod_container)
    db_name = production_ops.prod_db_name(team, "erp")

    with ExitStack() as stack:
        for patcher in patches:
            stack.enter_context(patcher)
        stack.enter_context(
            patch.object(
                production_ops,
                "_copy_env_data_into_production",
                side_effect=OSError("copy failed"),
            )
        )
        sql = stack.enter_context(patch.object(backup_ops, "_exec_sql"))
        with pytest.raises(OSError, match="copy failed"):
            backup_ops.restore_production_from_environment(
                settings, team, "erp", "feature-x"
            )

    statements = [call.args[2] for call in sql.call_args_list]
    assert not any("ALTER DATABASE" in statement for statement in statements)
    # The staged database is cleaned, the live one untouched.
    assert f'DROP DATABASE IF EXISTS "{db_name}__restore" WITH (FORCE);' in statements
    prod_container.stop.assert_not_called()


def test_env_restore_does_not_probe_a_stopped_production(tmp_path):
    """A production stopped before the restore is left stopped on purpose, so
    probing it would burn the full 180 s timeout and then brand a successful
    restore as a failed deploy."""
    settings, team = _env_restore_settings(tmp_path)
    prod_container = MagicMock(status="exited")
    _client, _env, patches = _env_restore_patches(container=prod_container)

    with ExitStack() as stack:
        for patcher in patches:
            stack.enter_context(patcher)
        wait = stack.enter_context(
            patch.object(production_ops, "wait_production_healthy", return_value=False)
        )
        update = stack.enter_context(
            patch.object(backup_ops.production_registry, "update_production")
        )
        deploy = stack.enter_context(patch.object(production_ops, "append_deploy"))
        stack.enter_context(patch.object(backup_ops, "_exec_sql"))
        result = backup_ops.restore_production_from_environment(
            settings, team, "erp", "feature-x"
        )

    wait.assert_not_called()
    assert result["healthy"] is True
    # Not marked unhealthy, and the deploy record is a success.
    assert not any(
        call.args[2].get("unhealthy") for call in update.call_args_list if call.args[2:]
    )
    assert deploy.call_args[0][2]["status"] == "success"
    assert "stopped" in deploy.call_args[0][2]["note"]


def test_env_restore_still_probes_a_running_production(tmp_path):
    settings, team = _env_restore_settings(tmp_path)
    prod_container = MagicMock(status="running")
    _client, _env, patches = _env_restore_patches(container=prod_container)

    with ExitStack() as stack:
        for patcher in patches:
            stack.enter_context(patcher)
        wait = stack.enter_context(
            patch.object(production_ops, "wait_production_healthy", return_value=False)
        )
        deploy = stack.enter_context(patch.object(production_ops, "append_deploy"))
        stack.enter_context(patch.object(backup_ops, "_exec_sql"))
        result = backup_ops.restore_production_from_environment(
            settings, team, "erp", "feature-x"
        )

    wait.assert_called_once()
    assert result["healthy"] is False
    assert deploy.call_args[0][2]["status"] == "rollback_failed"


def test_env_restore_refuses_when_filestore_does_not_fit(tmp_path):
    """The staging directory shares a volume with the live production's
    filestore and PGDATA, so an oversized source must be refused up front."""
    settings, team = _env_restore_settings(tmp_path)
    _client, _env, patches = _env_restore_patches(container=MagicMock(status="running"))

    src = get_filestore_paths("feature-x", team.workspaces_dir)["merged"]
    os.makedirs(src, exist_ok=True)
    with open(os.path.join(src, "blob"), "wb") as fh:
        fh.write(b"x" * 4096)

    with ExitStack() as stack:
        for patcher in patches:
            stack.enter_context(patcher)
        stack.enter_context(
            patch.object(
                backup_ops.shutil,
                "disk_usage",
                return_value=SimpleNamespace(total=0, used=0, free=1),
            )
        )
        sql = stack.enter_context(patch.object(backup_ops, "_exec_sql"))
        with pytest.raises(PrerequisiteNotMetError, match="Not enough free disk"):
            backup_ops.restore_production_from_environment(
                settings, team, "erp", "feature-x"
            )

    # Refused before any database work.
    sql.assert_not_called()


def test_env_restore_warns_about_sanitized_provenance(tmp_path):
    settings, team = _env_restore_settings(tmp_path)
    source_env = {
        "container": MagicMock(status="running"),
        "template": "prod-erp",
        "head_commit": "",
    }
    _client, _env, patches = _env_restore_patches(source_env=source_env)

    with ExitStack() as stack:
        for patcher in patches:
            stack.enter_context(patcher)
        stack.enter_context(patch.object(backup_ops, "_exec_sql"))
        result = backup_ops.restore_production_from_environment(
            settings, team, "erp", "feature-x"
        )

    assert any("sanitized" in note for note in result["notes"])


def test_failed_database_compensation_keeps_production_stopped(tmp_path):
    settings, team = _restore_settings(tmp_path)
    container = MagicMock(status="running")
    manifest = _manifest()
    _client, _pg, _s3, patches = _restore_patches(manifest, container=container)

    with ExitStack() as stack:
        for patcher in patches:
            stack.enter_context(patcher)
        stack.enter_context(patch.object(backup_ops.chunkstore, "restore"))
        stack.enter_context(
            patch.object(
                backup_ops,
                "_swap_restored_filestore",
                side_effect=OSError("filestore swap failed"),
            )
        )
        stack.enter_context(
            patch.object(
                backup_ops,
                "_rollback_database_swap",
                side_effect=OSError("database rollback failed"),
            )
        )
        with pytest.raises(OSError, match="database rollback failed"):
            backup_ops.restore_production(settings, team, "erp", "snap")

    container.stop.assert_called_once_with()
    container.start.assert_not_called()
