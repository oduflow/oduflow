import io
import json
import os
import ssl
import subprocess
import tarfile
from unittest.mock import MagicMock, patch

import pytest

from oduflow import walg
from oduflow.errors import ExternalCommandError, PrerequisiteNotMetError
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
        assert cmd.startswith("/bin/sh /opt/oduflow-bin/wal-archive.sh ")
        assert '120 "%p" "%f"' in cmd


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
        assert cfg["WALG_S3_CA_CERT_FILE"] == walg.WALG_CA_BUNDLE
        bundle = tmp_path / "walg" / "ca-certificates.crt"
        assert bundle.stat().st_mode & 0o777 == 0o644
        assert ssl.create_default_context(cafile=str(bundle)).get_ca_certs()

    def test_existing_mount_receives_updated_bundle(self, tmp_path, monkeypatch):
        from botocore.httpsession import get_cert_path

        original = open(get_cert_path(True), "rb").read()
        source = tmp_path / "custom-ca.pem"
        source.write_bytes(original)
        monkeypatch.setenv("AWS_CA_BUNDLE", str(source))
        settings = _settings(tmp_path, BACKUP)
        walg.write_walg_config(settings)
        directory = tmp_path / "walg"
        inode = directory.stat().st_ino
        source.write_bytes(original + b"\n")
        walg.write_walg_config(settings)
        assert directory.stat().st_ino == inode
        assert (directory / "ca-certificates.crt").read_bytes() == original + b"\n"

    def test_invalid_override_preserves_working_bundle_and_config(
        self, tmp_path, monkeypatch
    ):
        settings = _settings(tmp_path, BACKUP)
        path = walg.write_walg_config(settings)
        original_config = open(path, "rb").read()
        bundle = tmp_path / "walg" / "ca-certificates.crt"
        original_bundle = bundle.read_bytes()
        invalid = tmp_path / "bad-ca.pem"
        invalid.write_text("not a certificate")
        monkeypatch.setenv("AWS_CA_BUNDLE", str(invalid))
        with pytest.raises(ssl.SSLError):
            walg.write_walg_config(settings)
        assert bundle.read_bytes() == original_bundle
        assert open(path, "rb").read() == original_config

    def test_ssl_cert_file_override(self, tmp_path, monkeypatch):
        from botocore.httpsession import get_cert_path

        source = get_cert_path(True)
        monkeypatch.delenv("AWS_CA_BUNDLE", raising=False)
        monkeypatch.setenv("SSL_CERT_FILE", source)
        walg.write_walg_config(_settings(tmp_path, BACKUP))
        assert (tmp_path / "walg" / "ca-certificates.crt").read_bytes() == open(
            source, "rb"
        ).read()

    def test_bundle_readable_with_restrictive_umask(self, tmp_path):
        previous = os.umask(0o077)
        try:
            walg.write_walg_config(_settings(tmp_path, BACKUP))
        finally:
            os.umask(previous)
        assert (tmp_path / "walg").stat().st_mode & 0o777 == 0o755
        assert (
            tmp_path / "walg" / "ca-certificates.crt"
        ).stat().st_mode & 0o777 == 0o644
        assert (tmp_path / "walg" / "walg.json").stat().st_mode & 0o777 == 0o600

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

        def _fake_exec(_c, _s, sql):
            issued.append(sql)
            return ""

        client = MagicMock()
        with patch.object(walg, "_pg_probe", _fake_exec):
            walg.apply_archive_command(client, settings, enabled=True)

        assert any("ALTER SYSTEM SET archive_command" in s for s in issued)
        assert any('wal-archive.sh 120 "%p" "%f"' in s for s in issued)
        assert any("pg_reload_conf" in s for s in issued)


class TestStorageDiagnostics:
    def test_probe_runs_inside_pg_as_postgres(self, tmp_path):
        client = MagicMock()
        container = client.containers.get.return_value
        container.exec_run.return_value = (0, b"[]")
        settings = _settings(tmp_path, BACKUP)
        assert walg.storage_status(client, settings)["status"] == "ok"
        client.containers.get.assert_called_once_with(settings.prod_db_container)
        cmd = container.exec_run.call_args.args[0]
        assert cmd[:3] == ["timeout", "--kill-after=2s", "5s"]
        assert cmd[3:6] == [walg.WALG_BIN, "--config", walg.WALG_CONF]
        assert cmd[6:] == ["st", "ls"]
        assert container.exec_run.call_args.kwargs["user"] == "postgres"
        assert container.exec_run.call_args.kwargs["environment"] == {
            "WALG_S3_MAX_RETRIES": "0"
        }

    @pytest.mark.parametrize("code", [124, 137])
    def test_timeout_is_not_an_empty_inventory(self, tmp_path, code):
        client = MagicMock()
        client.containers.get.return_value.exec_run.return_value = (code, b"")
        with pytest.raises(ExternalCommandError, match="Timed out"):
            walg.backup_list(client, _settings(tmp_path, BACKUP))
        result = walg.storage_status(client, _settings(tmp_path, BACKUP))
        assert result == {"status": "error", "detail": "WAL-G storage probe timed out"}

    @pytest.mark.parametrize("output", [b"", b"garbage", b"{}", b"[1]"])
    def test_invalid_inventory_is_not_empty(self, tmp_path, output):
        client = MagicMock()
        client.containers.get.return_value.exec_run.return_value = (0, output)
        with pytest.raises(PrerequisiteNotMetError, match="invalid backup inventory"):
            walg.backup_list(client, _settings(tmp_path, BACKUP))

    @pytest.mark.parametrize("code", [0, 1])
    def test_empty_archive_requires_successful_storage_listing(self, tmp_path, code):
        client = MagicMock()
        client.containers.get.return_value.exec_run.side_effect = [
            (code, b"INFO: No backups found"),
            (0, b"type size last modified name"),
        ]
        assert walg.backup_list(client, _settings(tmp_path, BACKUP)) == []

    def test_upstream_empty_inventory_message_does_not_hide_storage_error(
        self, tmp_path
    ):
        client = MagicMock()
        client.containers.get.return_value.exec_run.side_effect = [
            (0, b"INFO: No backups found"),
            (1, b"AccessDenied"),
        ]
        with pytest.raises(ExternalCommandError, match="AccessDenied"):
            walg.backup_list(client, _settings(tmp_path, BACKUP))

    def test_tls_failure_is_public_without_raw_diagnostics(self, tmp_path):
        client = MagicMock()
        client.containers.get.return_value.exec_run.return_value = (
            1,
            b"https://private-bucket.example: x509: certificate signed by unknown authority",
        )
        result = walg.storage_status(client, _settings(tmp_path, BACKUP))
        assert result["status"] == "error"
        assert "TLS" in result["detail"]
        assert "private-bucket" not in result["detail"]

    def test_timeout_terminates_unresponsive_command(self, tmp_path, monkeypatch):
        # Exercise the generated command with a real process that ignores TERM.
        binary = tmp_path / "wal-g"
        binary.write_text("#!/bin/sh\ntrap '' TERM\nwhile :; do sleep 1; done\n")
        binary.chmod(0o755)
        monkeypatch.setattr(walg, "WALG_BIN", str(binary))
        client = MagicMock()

        def local_exec(cmd, **kwargs):
            result = subprocess.run(cmd, capture_output=True, timeout=6)
            return result.returncode, result.stdout + result.stderr

        client.containers.get.return_value.exec_run.side_effect = local_exec
        with pytest.raises(ExternalCommandError):
            walg._exec_walg(client, _settings(tmp_path), ["backup-list"], timeout=1)


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
