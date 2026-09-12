"""Disk admission control for environment creation.

Creation must be refused while the disk still has room to recover — once
PostgreSQL hits 0 bytes free even deleting environments fails. The check
estimates the bytes each target filesystem will receive (template DB size via
pg_database_size, filestore copy vs overlay, git clone budget), groups targets
sharing a device, and keeps a max(5 GiB, min(5%, 10 GiB)) reserve free on
every device.
"""

from __future__ import annotations

import json
import os
from collections import namedtuple
from unittest.mock import MagicMock, patch

import pytest

from oduflow.docker_ops import system_ops
from oduflow.docker_ops.system_ops import (
    _CLONE_BUDGET_BYTES,
    _GREENFIELD_DB_BYTES,
    _OVERLAY_HEADROOM_BYTES,
    _estimate_template_filestore_bytes,
    _existing_anchor,
    _reserve_bytes,
    check_db_quota,
    check_disk_space,
    estimate_new_db_bytes,
    pg_clone_strategy_clause,
)
from oduflow.errors import PrerequisiteNotMetError
from oduflow.settings import Settings, TeamSettings

GB = 1024**3
MB = 1024**2

_usage = namedtuple("_usage", "total used free")


def _make_env(tmp_path):
    team = TeamSettings(team_id="1", data_dir=str(tmp_path / "team_1"))
    settings = Settings(base_data_dir=str(tmp_path), teams={"1": team})
    os.makedirs(team.workspaces_dir, exist_ok=True)
    return settings, team


def _client_without_pgdata():
    """Docker client whose volume/container lookups fail: the PGDATA leg of
    the check degrades to a logged skip, isolating the host-path assertions."""
    client = MagicMock()
    client.volumes.get.side_effect = RuntimeError("no docker")
    client.containers.get.side_effect = RuntimeError("no docker")
    return client


def _write_template(team, name, metadata, *, filestore=True):
    tpl_dir = team.get_template_dir(name)
    os.makedirs(tpl_dir, exist_ok=True)
    if filestore:
        os.makedirs(os.path.join(tpl_dir, "filestore"), exist_ok=True)
    with open(team.get_template_metadata_path(name), "w") as f:
        json.dump(metadata, f)


# --- estimate_new_db_bytes -------------------------------------------------


class TestEstimateNewDbBytes:
    def test_template_uses_pg_database_size(self, tmp_path):
        settings, team = _make_env(tmp_path)
        with patch.object(system_ops, "_exec_sql", return_value=str(3 * GB)) as sql:
            assert estimate_new_db_bytes(None, settings, team, "prod") == 3 * GB
        assert "pg_database_size('oduflow_template_1_prod')" in sql.call_args.args[2]

    def test_no_template_uses_greenfield_budget(self, tmp_path):
        settings, team = _make_env(tmp_path)
        with patch.object(system_ops, "_exec_sql") as sql:
            assert estimate_new_db_bytes(None, settings, team, None) == (
                _GREENFIELD_DB_BYTES
            )
        sql.assert_not_called()

    def test_measurement_failure_falls_back(self, tmp_path):
        settings, team = _make_env(tmp_path)
        with patch.object(system_ops, "_exec_sql", side_effect=RuntimeError("down")):
            assert estimate_new_db_bytes(None, settings, team, "prod") == (
                _GREENFIELD_DB_BYTES
            )


# --- filestore estimate ----------------------------------------------------


class TestFilestoreEstimate:
    def test_overlay_on_linux_needs_only_headroom(self, tmp_path):
        settings, team = _make_env(tmp_path)
        _write_template(team, "t", {"use_overlay": True, "filestore_size_mb": 4096})
        with patch.object(system_ops.sys, "platform", "linux"):
            assert _estimate_template_filestore_bytes(settings, team, "t") == (
                _OVERLAY_HEADROOM_BYTES
            )

    def test_copy_mode_needs_full_size(self, tmp_path):
        settings, team = _make_env(tmp_path)
        _write_template(team, "t", {"use_overlay": False, "filestore_size_mb": 4096})
        assert _estimate_template_filestore_bytes(settings, team, "t") == 4096 * MB

    def test_overlay_falls_back_to_copy_off_linux(self, tmp_path):
        # _mount_filestore forces a plain copy on non-Linux platforms; the
        # estimate must budget the full size there too.
        settings, team = _make_env(tmp_path)
        _write_template(team, "t", {"use_overlay": True, "filestore_size_mb": 4096})
        with patch.object(system_ops.sys, "platform", "darwin"):
            assert _estimate_template_filestore_bytes(settings, team, "t") == 4096 * MB

    def test_missing_metadata_scans_filestore(self, tmp_path):
        settings, team = _make_env(tmp_path)
        _write_template(team, "t", {})
        with (
            patch("oduflow.docker_ops.env_ops._dir_size_mb", return_value=10.0) as scan,
            patch.object(system_ops.sys, "platform", "linux"),
        ):
            # 10 MB is under the overlay threshold (50), so copy mode: 10 MB.
            assert _estimate_template_filestore_bytes(settings, team, "t") == 10 * MB
        scan.assert_called_once()

    def test_no_filestore_dir_is_zero(self, tmp_path):
        settings, team = _make_env(tmp_path)
        _write_template(team, "t", {"filestore_size_mb": 4096}, filestore=False)
        assert _estimate_template_filestore_bytes(settings, team, "t") == 0


# --- anchor resolution -----------------------------------------------------


def test_anchor_climbs_to_existing_parent(tmp_path):
    missing = tmp_path / "a" / "b" / "c"
    assert _existing_anchor(str(missing)) == str(tmp_path)


# --- reserve formula -------------------------------------------------------


def test_reserve_is_capped_on_large_disks():
    assert _reserve_bytes(60 * GB) == 5 * GB  # 5% = 3 GB, floor wins
    assert _reserve_bytes(100 * GB) == 5 * GB  # 5% = floor
    assert _reserve_bytes(1000 * GB) == 10 * GB  # 5% = 50 GB, cap wins


# --- check_disk_space ------------------------------------------------------


class TestCheckDiskSpace:
    def _check(self, settings, team, template_name, db_bytes, usage, **kwargs):
        with patch.object(system_ops.shutil, "disk_usage", return_value=usage):
            check_disk_space(
                _client_without_pgdata(),
                settings,
                team,
                template_name,
                estimated_db_bytes=db_bytes,
                env_name="feature-x",
                **kwargs,
            )

    def test_enough_space_passes(self, tmp_path):
        settings, team = _make_env(tmp_path)
        self._check(settings, team, None, 2 * GB, _usage(100 * GB, 90 * GB, 10 * GB))

    def test_insufficient_space_raises(self, tmp_path):
        settings, team = _make_env(tmp_path)
        with pytest.raises(PrerequisiteNotMetError, match="Not enough free disk"):
            self._check(settings, team, None, 2 * GB, _usage(100 * GB, 96 * GB, 4 * GB))

    def test_reserve_cannot_be_consumed(self, tmp_path):
        # free (6 GB) covers the estimate (2 GB * 1.2 + clone budget) but the
        # remainder dips under the 5 GiB floor: refuse.
        settings, team = _make_env(tmp_path)
        with pytest.raises(PrerequisiteNotMetError, match="must stay free"):
            self._check(settings, team, None, 2 * GB, _usage(100 * GB, 94 * GB, 6 * GB))

    def test_same_device_requirements_are_summed(self, tmp_path):
        # DB (4 GB) or filestore copy (4 GB) alone fits in 11 GB free with the
        # 5 GiB reserve; together (8 GB * 1.2 margin) they do not.
        settings, team = _make_env(tmp_path)
        _write_template(team, "t", {"use_overlay": False, "filestore_size_mb": 4096})
        with pytest.raises(PrerequisiteNotMetError, match="Not enough free disk"):
            self._check(settings, team, "t", 4 * GB, _usage(100 * GB, 89 * GB, 11 * GB))

    def test_local_mount_skips_clone_budget(self, tmp_path):
        # 2 GB * 1.2 = 2.4 GB; free 7.5 GB leaves 5.1 GB > 5 GiB reserve only
        # because no clone budget is added for a live-mount.
        settings, team = _make_env(tmp_path)
        self._check(
            settings,
            team,
            None,
            2 * GB,
            _usage(100 * GB, int(92.5 * GB), int(7.5 * GB)),
            local_mount=True,
        )
        clone_extra = _CLONE_BUDGET_BYTES * 1.2 / GB
        assert clone_extra > 0.1  # the same free space fails with the budget
        with pytest.raises(PrerequisiteNotMetError):
            self._check(
                settings,
                team,
                None,
                2 * GB,
                _usage(100 * GB, int(92.5 * GB), int(7.5 * GB)),
            )

    def test_separate_devices_checked_independently(self, tmp_path):
        # Workspace disk is huge; the tablespace disk alone is too full. The
        # refusal must name the database tablespace component.
        settings, team = _make_env(tmp_path)
        tablespace_root = str(tmp_path / "pg_tablespaces")
        os.makedirs(tablespace_root, exist_ok=True)
        real_stat = os.stat

        def fake_stat(path, *args, **kwargs):
            result = real_stat(path, *args, **kwargs)
            if str(path).startswith(tablespace_root):
                return _FakeStat(st_dev=result.st_dev + 1)
            return result

        def fake_usage(path):
            if str(path).startswith(tablespace_root):
                return _usage(100 * GB, 97 * GB, 3 * GB)
            return _usage(100 * GB, 10 * GB, 90 * GB)

        with (
            patch.object(system_ops.os, "stat", side_effect=fake_stat),
            patch.object(system_ops.shutil, "disk_usage", side_effect=fake_usage),
        ):
            with pytest.raises(PrerequisiteNotMetError, match="database tablespace"):
                check_disk_space(
                    _client_without_pgdata(),
                    settings,
                    team,
                    None,
                    estimated_db_bytes=1 * GB,
                    env_name="feature-x",
                )

    def test_measurement_errors_never_block_creation(self, tmp_path):
        settings, team = _make_env(tmp_path)
        with patch.object(system_ops.shutil, "disk_usage", side_effect=OSError("gone")):
            check_disk_space(
                _client_without_pgdata(),
                settings,
                team,
                None,
                estimated_db_bytes=1 * GB,
                env_name="feature-x",
            )

    def test_pgdata_df_fallback_enforces_reserve(self, tmp_path):
        settings, team = _make_env(tmp_path)
        client = MagicMock()
        client.volumes.get.side_effect = RuntimeError("no mountpoint")
        df_output = (
            "Filesystem 1024-blocks Used Available Capacity Mounted on\n"
            f"overlay {100 * GB // 1024} {98 * GB // 1024} {2 * GB // 1024} 98% "
            "/var/lib/postgresql/data\n"
        )
        client.containers.get.return_value.exec_run.return_value = (
            0,
            df_output.encode(),
        )
        with patch.object(
            system_ops.shutil,
            "disk_usage",
            return_value=_usage(100 * GB, 10 * GB, 90 * GB),
        ):
            with pytest.raises(PrerequisiteNotMetError, match="data volume"):
                check_disk_space(
                    client,
                    settings,
                    team,
                    None,
                    estimated_db_bytes=1 * GB,
                    env_name="feature-x",
                )


class _FakeStat:
    def __init__(self, st_dev):
        self.st_dev = st_dev


# --- predictive db quota ---------------------------------------------------


class TestPredictiveDbQuota:
    _ROWS = f"oduflow_1_main|{6 * GB}"

    def _team(self, quota_gb):
        return TeamSettings(team_id="1", db_quota_gb=quota_gb)

    def test_projected_over_quota_raises(self):
        with patch.object(system_ops, "_exec_sql", return_value=self._ROWS):
            with pytest.raises(PrerequisiteNotMetError, match="quota exceeded"):
                check_db_quota(
                    None, Settings(), self._team(8), estimated_new_db_bytes=3 * GB
                )

    def test_projected_under_quota_passes(self):
        with patch.object(system_ops, "_exec_sql", return_value=self._ROWS):
            check_db_quota(
                None, Settings(), self._team(10), estimated_new_db_bytes=3 * GB
            )


# --- CREATE DATABASE strategy ----------------------------------------------


class TestCloneStrategy:
    def test_pg15_uses_file_copy(self):
        with patch.object(system_ops, "_exec_sql", return_value="150004"):
            assert pg_clone_strategy_clause(None, Settings()) == " STRATEGY FILE_COPY"

    def test_pg14_uses_default(self):
        with patch.object(system_ops, "_exec_sql", return_value="140007"):
            assert pg_clone_strategy_clause(None, Settings()) == ""

    def test_version_probe_failure_is_safe(self):
        with patch.object(system_ops, "_exec_sql", side_effect=RuntimeError("down")):
            assert pg_clone_strategy_clause(None, Settings()) == ""


# --- recreate checks before deleting ---------------------------------------


class TestRecreateChecksFirst:
    @patch("oduflow.docker_ops.env_ops.create_environment")
    @patch("oduflow.docker_ops.env_ops.delete_environment")
    @patch("oduflow.docker_ops.client.get_client")
    def test_recreate_refuses_before_delete(
        self, mock_client, mock_delete, mock_create, tmp_path
    ):
        from starlette.applications import Starlette
        from starlette.testclient import TestClient

        from oduflow.locking import LockManager
        from oduflow.web_ui import mount_web_ui

        settings, team = _make_env(tmp_path)
        container = MagicMock()
        container.labels = {
            settings.repo_label: "https://github.com/x/y.git",
            settings.image_label: "odoo:19.0",
            "oduflow.template": "none",
        }
        mock_client.return_value.containers.get.return_value = container

        app = Starlette()
        mount_web_ui(app, lambda: settings, LockManager())
        with patch.object(
            system_ops,
            "check_disk_space",
            side_effect=PrerequisiteNotMetError("Not enough free disk space"),
        ):
            resp = TestClient(app).post("/api/environments/main/recreate")

        assert resp.status_code == 400
        assert "Not enough free disk space" in resp.json()["error"]
        mock_delete.assert_not_called()
        mock_create.assert_not_called()

    @patch("oduflow.docker_ops.env_ops.create_environment")
    @patch("oduflow.docker_ops.env_ops.delete_environment")
    @patch("oduflow.docker_ops.client.get_client")
    def test_recreate_refuses_on_dangling_secret_before_delete(
        self, mock_client, mock_delete, mock_create, tmp_path
    ):
        """A dangling secret:<name> reference must refuse the recreate while
        the working environment is still intact — create_environment would
        only re-check after delete_environment has destroyed it."""
        from starlette.applications import Starlette
        from starlette.testclient import TestClient

        from oduflow.locking import LockManager
        from oduflow.web_ui import mount_web_ui

        settings, team = _make_env(tmp_path)
        container = MagicMock()
        container.labels = {
            settings.repo_label: "https://github.com/x/y.git",
            settings.image_label: "odoo:19.0",
            "oduflow.template": "none",
            "oduflow.env_vars": json.dumps({"KEY": "secret:gone"}),
        }
        mock_client.return_value.containers.get.return_value = container

        app = Starlette()
        mount_web_ui(app, lambda: settings, LockManager())
        with (
            patch.object(system_ops, "estimate_new_db_bytes", return_value=0),
            patch.object(system_ops, "check_disk_space"),
        ):
            resp = TestClient(app).post("/api/environments/main/recreate")

        assert resp.status_code == 400
        assert "Undefined secret(s): gone" in resp.json()["error"]
        mock_delete.assert_not_called()
        mock_create.assert_not_called()
