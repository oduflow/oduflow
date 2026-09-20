import io
import json
import os
import zipfile
from contextlib import contextmanager
from unittest.mock import MagicMock

import pytest

from oduflow.docker_ops import system_ops
from oduflow.errors import ConflictError
from oduflow.settings import Settings, TeamSettings


def _team_and_settings(tmp_path):
    team = TeamSettings(
        team_id="1",
        data_dir=str(tmp_path),
        port_registry_path=str(tmp_path / "ports.json"),
    )
    settings = Settings(base_data_dir=str(tmp_path), teams={"1": team})
    return team, settings


def _zip_backup() -> bytes:
    payload = io.BytesIO()
    with zipfile.ZipFile(payload, "w") as zf:
        zf.writestr(
            "manifest.json",
            json.dumps(
                {
                    "major_version": "19.0",
                    "version": "19.0+e",
                    "pg_version": "16",
                    "modules": {"base": "19.0.1.0"},
                }
            ),
        )
        zf.writestr("dump.sql", "SELECT 1;")
        zf.writestr("filestore/ab/abcdef", b"file")
    return payload.getvalue()


def _dump_backup() -> bytes:
    return b"PGDMP custom dump bytes"


class _Response:
    def __init__(self, body: bytes, content_type: str = "application/octet-stream"):
        self._body = io.BytesIO(body)
        self.headers = {"Content-Type": content_type}

    def read(self, size: int = -1) -> bytes:
        return self._body.read(size)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


@contextmanager
def _remount():
    remount = MagicMock()
    remount.affected = []
    remount.failures = []
    yield remount


def _patch_import_dependencies(monkeypatch, backup_requests, *, patch_url_safety=True):
    if patch_url_safety:
        monkeypatch.setattr(
            "oduflow.url_safety.assert_allowed_url", lambda *a, **k: None
        )
    monkeypatch.setattr(system_ops, "get_client", lambda: MagicMock())
    monkeypatch.setattr(system_ops, "_db_exists", lambda *a, **k: False)
    monkeypatch.setattr(system_ops, "check_db_quota", lambda *a, **k: None)
    monkeypatch.setattr(
        "oduflow.docker_ops.env_ops.remount_template_overlays",
        lambda *a, **k: _remount(),
    )
    monkeypatch.setattr(system_ops, "get_odoo_uid_gid", lambda *a, **k: "100:100")
    monkeypatch.setattr(system_ops, "chown_recursive", lambda *a, **k: None)
    monkeypatch.setattr(
        system_ops,
        "reload_template",
        lambda *a, **k: {
            "template_db": "oduflow_template_1_imported",
            "restore_seconds": 0.1,
        },
    )

    def fake_exec_sql(_client, _settings, sql, db="postgres"):
        if "name='base'" in sql:
            return "18.0.1.0"
        if "SHOW server_version" in sql:
            return "16"
        if "json_object_agg" in sql:
            return json.dumps({"base": "18.0.1.0", "web": "18.0.1.0"})
        raise AssertionError(f"Unexpected SQL against {db}: {sql}")

    monkeypatch.setattr(system_ops, "_exec_sql", fake_exec_sql)

    def fake_urlopen(req, timeout=0):
        if req.full_url.endswith("/web/database/backup"):
            body = req.data.decode("utf-8")
            backup_requests.append(body)
            if "dump" in body:
                return _Response(_dump_backup())
            return _Response(_zip_backup(), "application/zip")
        raise AssertionError(f"Unexpected request: {req.full_url}")

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)


def test_import_from_odoo_default_zip_omits_filestore_field(monkeypatch, tmp_path):
    team, settings = _team_and_settings(tmp_path)
    backup_requests = []
    _patch_import_dependencies(monkeypatch, backup_requests)

    result = system_ops.import_from_odoo(
        settings,
        team,
        odoo_url="https://odoo.example.com",
        master_pwd="master",
        db_name="prod",
        template_name="imported",
    )

    assert result["includes_filestore"] is True
    assert 'name="backup_format"' in backup_requests[0]
    assert "zip" in backup_requests[0]
    assert 'name="filestore"' not in backup_requests[0]
    assert os.path.isfile(team.get_template_sql_path("imported"))
    assert os.path.isfile(
        os.path.join(team.get_template_filestore_path("imported"), "ab", "abcdef")
    )


def test_import_from_odoo_allows_http_source(monkeypatch, tmp_path):
    team, settings = _team_and_settings(tmp_path)
    backup_requests = []
    _patch_import_dependencies(monkeypatch, backup_requests, patch_url_safety=False)

    result = system_ops.import_from_odoo(
        settings,
        team,
        odoo_url="http://8.8.8.8",
        master_pwd="master",
        db_name="prod",
        template_name="http-import",
    )

    assert result["source_url"] == "http://8.8.8.8"
    assert result["template_name"] == "http-import"


def test_import_from_odoo_without_filestore_uses_custom_dump_and_skips_files(
    monkeypatch, tmp_path
):
    team, settings = _team_and_settings(tmp_path)
    backup_requests = []
    _patch_import_dependencies(monkeypatch, backup_requests)

    result = system_ops.import_from_odoo(
        settings,
        team,
        odoo_url="https://odoo.example.com",
        master_pwd="master",
        db_name="prod",
        template_name="dbonly",
        without_filestore=True,
    )

    assert result["includes_filestore"] is False
    assert 'name="backup_format"' in backup_requests[0]
    assert "dump" in backup_requests[0]
    assert 'name="filestore"' not in backup_requests[0]
    assert os.path.isfile(team.get_template_sql_path("dbonly"))
    assert os.path.basename(team.get_template_sql_path("dbonly")) == "dump.pgdump"
    assert not os.path.exists(team.get_template_filestore_path("dbonly"))
    with open(team.get_template_metadata_path("dbonly")) as f:
        metadata = json.load(f)
    assert metadata["includes_filestore"] is False
    assert metadata["filestore_size_mb"] == 0.0
    assert metadata["odoo_image"] == "odoo:18.0"
    assert metadata["modules"] == {"base": "18.0.1.0", "web": "18.0.1.0"}


def test_import_from_odoo_existing_template_dir_fails_before_network(
    monkeypatch, tmp_path
):
    team, settings = _team_and_settings(tmp_path)
    os.makedirs(team.get_template_dir("existing"))
    urlopen = MagicMock()
    monkeypatch.setattr("oduflow.url_safety.assert_allowed_url", lambda *a, **k: None)
    monkeypatch.setattr("urllib.request.urlopen", urlopen)
    get_client = MagicMock()
    monkeypatch.setattr(system_ops, "get_client", get_client)

    with pytest.raises(ConflictError, match="Template directory already exists"):
        system_ops.import_from_odoo(
            settings,
            team,
            odoo_url="https://odoo.example.com",
            master_pwd="master",
            db_name="prod",
            template_name="existing",
        )

    urlopen.assert_not_called()
    get_client.assert_not_called()


def test_import_from_odoo_existing_template_db_fails_before_network(
    monkeypatch, tmp_path
):
    team, settings = _team_and_settings(tmp_path)
    urlopen = MagicMock()
    quota = MagicMock()
    monkeypatch.setattr("oduflow.url_safety.assert_allowed_url", lambda *a, **k: None)
    monkeypatch.setattr("urllib.request.urlopen", urlopen)
    monkeypatch.setattr(system_ops, "get_client", lambda: MagicMock())
    monkeypatch.setattr(system_ops, "_db_exists", lambda *a, **k: True)
    monkeypatch.setattr(system_ops, "check_db_quota", quota)

    with pytest.raises(ConflictError, match="Template database already exists"):
        system_ops.import_from_odoo(
            settings,
            team,
            odoo_url="https://odoo.example.com",
            master_pwd="master",
            db_name="prod",
            template_name="existingdb",
        )

    urlopen.assert_not_called()
    quota.assert_not_called()


def _tmp_backups(tmp_path) -> list[str]:
    return [p for p in os.listdir(tmp_path) if p.startswith("tmp_odoo_backup.")]


def test_a_failure_after_the_download_leaves_no_orphan(monkeypatch, tmp_path):
    """The scratch path is unique per call, so nothing ever overwrites a
    leftover: a multi-gigabyte download followed by an out-of-space makedirs
    would strand it on disk forever."""
    team, settings = _team_and_settings(tmp_path)
    _patch_import_dependencies(monkeypatch, [])

    real_makedirs = os.makedirs

    def failing_makedirs(path, *args, **kwargs):
        if path == team.get_template_dir("imported"):
            raise OSError(28, "No space left on device")
        return real_makedirs(path, *args, **kwargs)

    monkeypatch.setattr(os, "makedirs", failing_makedirs)

    with pytest.raises(OSError):
        system_ops.import_from_odoo(
            settings,
            team,
            odoo_url="https://odoo.example.com",
            master_pwd="master",
            db_name="prod",
            template_name="imported",
        )

    assert _tmp_backups(tmp_path) == []


def test_a_failed_download_leaves_no_orphan(monkeypatch, tmp_path):
    team, settings = _team_and_settings(tmp_path)
    _patch_import_dependencies(monkeypatch, [])

    class _DyingResponse(_Response):
        def read(self, size: int = -1) -> bytes:
            raise OSError("connection reset mid-download")

    monkeypatch.setattr(
        "urllib.request.urlopen",
        lambda req, timeout=0: _DyingResponse(b"", "application/zip"),
    )

    with pytest.raises(OSError):
        system_ops.import_from_odoo(
            settings,
            team,
            odoo_url="https://odoo.example.com",
            master_pwd="master",
            db_name="prod",
            template_name="imported",
        )

    assert _tmp_backups(tmp_path) == []
