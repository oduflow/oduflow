import io
import json
import os
import tarfile
from unittest.mock import MagicMock, patch

import pytest

from oduflow import walg
from oduflow.errors import PrerequisiteNotMetError
from oduflow.settings import BackupSettings, Settings, TeamSettings


def _settings(tmp_path, backup=None):
    return Settings(
        base_data_dir=str(tmp_path),
        backup=backup,
        teams={"1": TeamSettings(team_id="1", data_dir=str(tmp_path / "team_1"))},
    )


BACKUP = BackupSettings(
    bucket="bkt",
    access_key="AK",
    secret_key="SK",
    endpoint="http://minio:9000",
    region="us-east-1",
)


class TestArchiveCommand:
    def test_disabled_is_noop(self):
        assert walg.archive_command(False) == "/bin/true"

    def test_enabled_uses_mounted_paths(self):
        cmd = walg.archive_command(True)
        assert cmd.startswith("/opt/oduflow-bin/wal-g ")
        assert "--config /etc/walg/walg.json" in cmd
        assert cmd.endswith("wal-push %p")


class TestWalgConfig:
    def test_writes_private_json(self, tmp_path):
        settings = _settings(tmp_path, BACKUP)
        path = walg.write_walg_config(settings)
        assert path is not None
        assert os.stat(path).st_mode & 0o777 == 0o600
        cfg = json.load(open(path))
        assert cfg["WALG_S3_PREFIX"] == "s3://bkt/oduflow/walg"
        assert cfg["AWS_ACCESS_KEY_ID"] == "AK"
        assert cfg["AWS_ENDPOINT"] == "http://minio:9000"
        assert cfg["AWS_S3_FORCE_PATH_STYLE"] == "true"
        assert cfg["PGHOST"] == "/var/run/postgresql"

    def test_no_endpoint_omits_path_style(self, tmp_path):
        backup = BackupSettings(bucket="b", access_key="a", secret_key="s")
        cfg_path = walg.write_walg_config(_settings(tmp_path, backup))
        cfg = json.load(open(cfg_path))
        assert "AWS_ENDPOINT" not in cfg
        assert "AWS_S3_FORCE_PATH_STYLE" not in cfg

    def test_unconfigured_removes_stale_file(self, tmp_path):
        settings = _settings(tmp_path, BACKUP)
        path = walg.write_walg_config(settings)
        assert os.path.isfile(path)
        assert walg.write_walg_config(_settings(tmp_path, None)) is None
        assert not os.path.isfile(path)


def _tarball_bytes(inner_name: str, payload: bytes) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        info = tarfile.TarInfo(inner_name)
        info.size = len(payload)
        tar.addfile(info, io.BytesIO(payload))
    return buf.getvalue()


class TestEnsureWalg:
    def _fake_downloads(self, payload: bytes, sha: str):
        tarball = _tarball_bytes("wal-g-pg-ubuntu-20.04-amd64", payload)

        def _download(url, dest, timeout=120):
            with open(dest, "wb") as f:
                if url.endswith(".sha256"):
                    f.write(f"{sha}  asset\n".encode())
                else:
                    f.write(tarball)

        return _download

    def test_downloads_verifies_and_links(self, tmp_path):
        import hashlib

        payload = b"#!walg binary"
        tarball = _tarball_bytes("wal-g-pg-ubuntu-20.04-amd64", payload)
        sha = hashlib.sha256(tarball).hexdigest()
        settings = _settings(tmp_path)

        with (
            patch.object(walg, "_docker_arch", return_value="amd64"),
            patch.object(walg, "_download", self._fake_downloads(payload, sha)),
        ):
            path = walg.ensure_walg(settings)

        assert open(path, "rb").read() == payload
        assert os.access(path, os.X_OK)
        link = os.path.join(walg.bin_host_dir(settings), "wal-g")
        assert os.readlink(link) == os.path.basename(path)

    def test_checksum_mismatch_raises(self, tmp_path):
        settings = _settings(tmp_path)
        with (
            patch.object(walg, "_docker_arch", return_value="amd64"),
            patch.object(walg, "_download", self._fake_downloads(b"x", "0" * 64)),
            pytest.raises(PrerequisiteNotMetError, match="checksum mismatch"),
        ):
            walg.ensure_walg(settings)

    def test_existing_binary_short_circuits(self, tmp_path):
        settings = _settings(tmp_path)
        bin_dir = walg.bin_host_dir(settings)
        os.makedirs(bin_dir)
        versioned = os.path.join(bin_dir, f"wal-g-{walg.WALG_VERSION}")
        open(versioned, "wb").write(b"existing")

        with patch.object(walg, "_download") as dl:
            path = walg.ensure_walg(settings)

        dl.assert_not_called()
        assert path == versioned


class TestApplyArchiveCommand:
    def test_alter_system_and_reload(self, tmp_path):
        settings = _settings(tmp_path, BACKUP)
        issued: list[str] = []

        def _fake_exec(_c, _s, sql, db="postgres", container_name=None):
            assert container_name == settings.prod_db_container
            issued.append(sql)
            return ""

        client = MagicMock()
        with patch("oduflow.docker_ops.system_ops._exec_sql", _fake_exec):
            walg.apply_archive_command(client, settings, enabled=True)

        assert any("ALTER SYSTEM SET archive_command" in s for s in issued)
        assert any("wal-push %p" in s for s in issued)
        assert any("pg_reload_conf" in s for s in issued)


class TestParseWalgTime:
    def test_rfc3339_z(self):
        dt = walg._parse_walg_time("2026-08-29T03:30:00Z")
        assert dt is not None
        assert dt.year == 2026 and dt.month == 8 and dt.day == 29 and dt.hour == 3
        assert dt.utcoffset().total_seconds() == 0

    def test_postgres_space_and_short_offset(self):
        dt = walg._parse_walg_time("2026-08-01 00:00:00+00")
        assert dt is not None and dt.utcoffset().total_seconds() == 0

    def test_naive_is_assumed_utc(self):
        dt = walg._parse_walg_time("2026-08-01T12:00:00")
        assert dt is not None and dt.utcoffset().total_seconds() == 0

    def test_empty_or_garbage_is_none(self):
        assert walg._parse_walg_time("") is None
        assert walg._parse_walg_time("not-a-time") is None


class TestSelectPitrBaseBackup:
    _BACKUPS = [
        {"backup_name": "base_001", "finish_time": "2026-08-27T03:30:00Z"},
        {"backup_name": "base_002", "finish_time": "2026-08-28T03:30:00Z"},
        {"backup_name": "base_003", "finish_time": "2026-08-29T03:30:00Z"},
    ]

    def test_no_target_uses_latest(self):
        assert walg._select_pitr_base_backup(MagicMock(), MagicMock(), "") == "LATEST"

    def test_picks_newest_base_at_or_before_target(self):
        with patch.object(walg, "backup_list", return_value=list(self._BACKUPS)):
            # 28th afternoon: newest base at or before it is base_002 (28th 03:30),
            # never the LATEST base_003 (29th) that the old code always fetched.
            name = walg._select_pitr_base_backup(
                MagicMock(), MagicMock(), "2026-08-28 14:00:00+00"
            )
        assert name == "base_002"

    def test_target_before_all_bases_raises_before_destruction(self):
        with patch.object(walg, "backup_list", return_value=list(self._BACKUPS)):
            with pytest.raises(PrerequisiteNotMetError, match="No base backup"):
                walg._select_pitr_base_backup(
                    MagicMock(), MagicMock(), "2026-08-01 00:00:00+00"
                )

    def test_unparseable_target_raises_before_destruction(self):
        # Falling back to LATEST here restores to the wrong point (or FATALs
        # recovery) after PGDATA is already displaced — refuse up front.
        with patch.object(walg, "backup_list", return_value=list(self._BACKUPS)):
            with pytest.raises(PrerequisiteNotMetError, match="target_time"):
                walg._select_pitr_base_backup(MagicMock(), MagicMock(), "whenever")

    def test_unreadable_backup_times_raise_before_destruction(self):
        with patch.object(walg, "backup_list", return_value=[{"foo": "bar"}]):
            with pytest.raises(
                PrerequisiteNotMetError, match="no base backup can be matched"
            ):
                walg._select_pitr_base_backup(
                    MagicMock(), MagicMock(), "2026-08-28 14:00:00+00"
                )

    def test_empty_backup_inventory_raises_before_destruction(self):
        with patch.object(walg, "backup_list", return_value=[]):
            with pytest.raises(PrerequisiteNotMetError):
                walg._select_pitr_base_backup(
                    MagicMock(), MagicMock(), "2026-08-28 14:00:00+00"
                )
