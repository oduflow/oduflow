"""Unit tests for the S3-prefix template import (oduflow.template_import)."""

import datetime
import hashlib
import json
import os
from contextlib import contextmanager
from unittest.mock import MagicMock

import pytest

from oduflow import template_import
from oduflow.docker_ops import system_ops
from oduflow.errors import ConflictError, PrerequisiteNotMetError
from oduflow.settings import BackupSettings, Settings, TeamSettings

_LAST_MODIFIED = datetime.datetime(2026, 9, 1, 3, 0, tzinfo=datetime.timezone.utc)


def _team_and_settings(tmp_path, backup=None):
    team = TeamSettings(
        team_id="1",
        data_dir=str(tmp_path),
        port_registry_path=str(tmp_path / "ports.json"),
    )
    settings = Settings(base_data_dir=str(tmp_path), teams={"1": team}, backup=backup)
    return team, settings


class _FakePaginator:
    def __init__(self, client):
        self._client = client

    def paginate(self, Bucket, Prefix=""):
        contents = [
            {
                "Key": key,
                "Size": len(body),
                "ETag": f'"{hashlib.md5(body).hexdigest()}"',
                "LastModified": _LAST_MODIFIED,
            }
            for key, body in sorted(self._client.objects.items())
            if key.startswith(Prefix)
        ]
        yield {"Contents": contents}


class _FakeS3Client:
    """Minimal stand-in for the boto3 client surface s3_import uses."""

    def __init__(self, objects):
        self.objects = objects  # key -> bytes
        self.downloads: list[str] = []

    def get_paginator(self, name):
        assert name == "list_objects_v2"
        return _FakePaginator(self)

    def download_file(self, bucket, key, filename):
        self.downloads.append(key)
        with open(filename, "wb") as f:
            f.write(self.objects[key])


@contextmanager
def _remount():
    remount = MagicMock()
    remount.affected = []
    remount.failures = []
    yield remount


def _source_objects():
    return {
        "backups/acme/dump.pgdump": b"PGDMP fake dump bytes",
        "backups/acme/filestore/ab/abcdef01": b"file-one",
        "backups/acme/filestore/cd/cdef2345": b"file-two-longer",
    }


def _patch_s3_import(monkeypatch, objects, *, db_exists=False):
    fake = _FakeS3Client(objects)
    monkeypatch.setattr(template_import, "_make_source_client", lambda *a, **k: fake)
    monkeypatch.setattr("oduflow.docker_ops.client.get_client", lambda: MagicMock())
    monkeypatch.setattr(system_ops, "_db_exists", lambda *a, **k: db_exists)
    monkeypatch.setattr(system_ops, "check_db_quota", lambda *a, **k: None)
    monkeypatch.setattr(
        "oduflow.docker_ops.env_ops.remount_template_overlays",
        lambda *a, **k: _remount(),
    )
    monkeypatch.setattr(
        "oduflow.docker_ops.client.get_odoo_uid_gid", lambda *a, **k: "100:100"
    )
    monkeypatch.setattr(
        "oduflow.docker_ops.client.chown_recursive", lambda *a, **k: None
    )
    reload_mock = MagicMock(
        return_value={
            "template_db": "oduflow_template_1_s3",
            "restore_seconds": 0.2,
        }
    )
    monkeypatch.setattr(system_ops, "reload_template", reload_mock)
    monkeypatch.setattr(
        system_ops,
        "_read_template_manifest_from_db",
        lambda *a, **k: {
            "version": "18.0.1.0",
            "major_version": "18.0",
            "pg_version": "16",
            "modules": {"base": "18.0.1.0"},
        },
    )
    return fake, reload_mock


def _etag(body: bytes) -> str:
    return hashlib.md5(body).hexdigest()


def test_fresh_import_downloads_dump_and_filestore(monkeypatch, tmp_path):
    team, settings = _team_and_settings(tmp_path)
    objects = _source_objects()
    fake, reload_mock = _patch_s3_import(monkeypatch, objects)

    result = template_import.import_from_s3_prefix(
        settings, team, url="s3://bucket/backups/acme", template_name="acme"
    )

    assert result["status"] == "imported"
    assert result["dump_reloaded"] is True
    assert result["downloaded_files"] == 3
    assert result["includes_filestore"] is True
    assert sorted(fake.downloads) == sorted(objects)
    reload_mock.assert_called_once()
    fs = team.get_template_filestore_path("acme")
    assert open(os.path.join(fs, "ab", "abcdef01"), "rb").read() == b"file-one"
    assert os.path.basename(team.get_template_sql_path("acme")) == "dump.pgdump"
    with open(team.get_template_metadata_path("acme")) as f:
        metadata = json.load(f)
    assert metadata["source_url"] == "s3://bucket/backups/acme"
    assert metadata["source_db"] == "dump.pgdump"
    assert metadata["odoo_image"] == "odoo:18.0"
    assert metadata["modules"] == {"base": "18.0.1.0"}
    assert metadata["source_dump_etag"] == _etag(objects["backups/acme/dump.pgdump"])
    assert metadata["snapshot_at"] == _LAST_MODIFIED.isoformat()
    # Staging is gone after a successful promote.
    assert not os.path.exists(team.get_import_staging_dir("acme"))


def test_resume_skips_already_staged_files(monkeypatch, tmp_path):
    team, settings = _team_and_settings(tmp_path)
    objects = _source_objects()
    fake, _reload = _patch_s3_import(monkeypatch, objects)

    # A previous interrupted run staged one filestore file and the dump.
    staging = team.get_import_staging_dir("acme")
    staged_file = os.path.join(staging, "filestore", "ab", "abcdef01")
    os.makedirs(os.path.dirname(staged_file))
    with open(staged_file, "wb") as f:
        f.write(b"file-one")
    with open(os.path.join(staging, "dump.pgdump"), "wb") as f:
        f.write(objects["backups/acme/dump.pgdump"])

    result = template_import.import_from_s3_prefix(
        settings, team, url="s3://bucket/backups/acme", template_name="acme"
    )

    assert fake.downloads == ["backups/acme/filestore/cd/cdef2345"]
    assert result["downloaded_files"] == 1
    assert result["reused_files"] == 1
    assert result["dump_reloaded"] is True


def test_overwrite_syncs_incrementally_and_skips_unchanged_dump(monkeypatch, tmp_path):
    team, settings = _team_and_settings(tmp_path)
    objects = _source_objects()
    fake, reload_mock = _patch_s3_import(monkeypatch, objects, db_exists=True)

    # Existing template from a previous sync of the same prefix: one file
    # unchanged, one stale (different size), one live-only (deleted on S3).
    fs = team.get_template_filestore_path("acme")
    os.makedirs(os.path.join(fs, "ab"))
    os.makedirs(os.path.join(fs, "cd"))
    os.makedirs(os.path.join(fs, "ee"))
    with open(os.path.join(fs, "ab", "abcdef01"), "wb") as f:
        f.write(b"file-one")
    with open(os.path.join(fs, "cd", "cdef2345"), "wb") as f:
        f.write(b"stale")
    with open(os.path.join(fs, "ee", "ee990011"), "wb") as f:
        f.write(b"gone-from-s3")
    with open(os.path.join(team.get_template_dir("acme"), "dump.pgdump"), "wb") as f:
        f.write(objects["backups/acme/dump.pgdump"])
    metadata = {
        "odoo_image": "odoo:18.0",
        "odoo_version": "18.0.1.0",
        "modules": {"base": "18.0.1.0"},
        "extra_addons": {"repo": "18.0"},
        "source_dump_etag": _etag(objects["backups/acme/dump.pgdump"]),
        "source_dump_bytes": len(objects["backups/acme/dump.pgdump"]),
    }
    with open(team.get_template_metadata_path("acme"), "w") as f:
        json.dump(metadata, f)

    result = template_import.import_from_s3_prefix(
        settings,
        team,
        url="s3://bucket/backups/acme",
        template_name="acme",
        overwrite=True,
    )

    assert result["status"] == "synced"
    assert result["dump_reloaded"] is False
    reload_mock.assert_not_called()
    # Only the changed filestore file was fetched — not the dump.
    assert fake.downloads == ["backups/acme/filestore/cd/cdef2345"]
    assert result["reused_files"] == 1  # hardlinked ab/abcdef01
    assert open(os.path.join(fs, "cd", "cdef2345"), "rb").read() == b"file-two-longer"
    # Deleted on S3 → gone from the template after the swap.
    assert not os.path.exists(os.path.join(fs, "ee", "ee990011"))
    with open(team.get_template_metadata_path("acme")) as f:
        merged = json.load(f)
    # Unrelated metadata keys survive the re-sync.
    assert merged["extra_addons"] == {"repo": "18.0"}
    assert merged["odoo_image"] == "odoo:18.0"


def test_overwrite_reloads_db_when_dump_changed(monkeypatch, tmp_path):
    team, settings = _team_and_settings(tmp_path)
    objects = _source_objects()
    fake, reload_mock = _patch_s3_import(monkeypatch, objects, db_exists=True)

    os.makedirs(team.get_template_dir("acme"))
    with open(os.path.join(team.get_template_dir("acme"), "dump.pgdump"), "wb") as f:
        f.write(b"OLD dump bytes")
    with open(team.get_template_metadata_path("acme"), "w") as f:
        json.dump({"source_dump_etag": "stale-etag", "source_dump_bytes": 14}, f)

    result = template_import.import_from_s3_prefix(
        settings,
        team,
        url="s3://bucket/backups/acme",
        template_name="acme",
        overwrite=True,
    )

    assert result["dump_reloaded"] is True
    reload_mock.assert_called_once()
    assert "backups/acme/dump.pgdump" in fake.downloads


def test_without_filestore_skips_filestore_tree(monkeypatch, tmp_path):
    team, settings = _team_and_settings(tmp_path)
    fake, _reload = _patch_s3_import(monkeypatch, _source_objects())

    result = template_import.import_from_s3_prefix(
        settings,
        team,
        url="s3://bucket/backups/acme",
        template_name="acme",
        without_filestore=True,
    )

    assert fake.downloads == ["backups/acme/dump.pgdump"]
    assert result["includes_filestore"] is False


def test_unsafe_filestore_keys_are_skipped(monkeypatch, tmp_path):
    team, settings = _team_and_settings(tmp_path)
    objects = {
        "backups/acme/dump.pgdump": b"PGDMP fake dump bytes",
        "backups/acme/filestore/../evil": b"escape",
        "backups/acme/filestore/ok/file01": b"fine",
    }
    _patch_s3_import(monkeypatch, objects)

    template_import.import_from_s3_prefix(
        settings, team, url="s3://bucket/backups/acme", template_name="acme"
    )

    fs = team.get_template_filestore_path("acme")
    assert os.path.isfile(os.path.join(fs, "ok", "file01"))
    assert not os.path.exists(os.path.join(tmp_path, "templates", "evil"))


def test_fresh_import_conflicts_with_existing_template(monkeypatch, tmp_path):
    team, settings = _team_and_settings(tmp_path)
    os.makedirs(team.get_template_dir("acme"))
    monkeypatch.setattr("oduflow.docker_ops.client.get_client", lambda: MagicMock())

    with pytest.raises(ConflictError, match="overwrite=true"):
        template_import.import_from_s3_prefix(
            settings, team, url="s3://bucket/backups/acme", template_name="acme"
        )


def test_missing_dump_is_rejected(monkeypatch, tmp_path):
    team, settings = _team_and_settings(tmp_path)
    _patch_s3_import(monkeypatch, {"backups/acme/filestore/ab/abcdef01": b"file-one"})

    with pytest.raises(PrerequisiteNotMetError, match="No database dump found"):
        template_import.import_from_s3_prefix(
            settings, team, url="s3://bucket/backups/acme", template_name="acme"
        )


def test_canonical_dump_outranks_db_dump(monkeypatch, tmp_path):
    # Same precedence as get_template_sql_path: a mirrored template dir may
    # carry a hand-placed db.dump next to the canonical dump Oduflow wrote.
    team, settings = _team_and_settings(tmp_path)
    fake, _reload = _patch_s3_import(
        monkeypatch,
        {
            "backups/acme/dump.pgdump": b"canonical",
            "backups/acme/db.dump": b"hand-placed leftover",
        },
    )

    result = template_import.import_from_s3_prefix(
        settings, team, url="s3://bucket/backups/acme", template_name="acme"
    )

    assert result["source_db"] == "dump.pgdump"
    assert "backups/acme/db.dump" not in fake.downloads


def test_db_dump_source_installs_under_canonical_name(monkeypatch, tmp_path):
    team, settings = _team_and_settings(tmp_path)
    _patch_s3_import(
        monkeypatch,
        {
            "backups/acme/db.dump": b"PGDMP hand-made dump",
            "backups/acme/filestore/ab/abcdef01": b"file-one",
        },
    )

    result = template_import.import_from_s3_prefix(
        settings, team, url="s3://bucket/backups/acme", template_name="acme"
    )

    # Recorded as it was named at the source, installed canonically.
    assert result["source_db"] == "db.dump"
    assert os.path.basename(team.get_template_sql_path("acme")) == "dump.pgdump"


def test_db_dump_gz_keeps_gz_suffix(monkeypatch, tmp_path):
    team, settings = _team_and_settings(tmp_path)
    _patch_s3_import(monkeypatch, {"backups/acme/db.dump.gz": b"\x1f\x8bgz"})

    template_import.import_from_s3_prefix(
        settings, team, url="s3://bucket/backups/acme", template_name="acme"
    )

    assert os.path.basename(team.get_template_sql_path("acme")) == "dump.pgdump.gz"


class TestSourceClient:
    def _capture_boto3(self, monkeypatch):
        import boto3

        calls = {}

        def fake_client(service, **kwargs):
            calls["service"] = service
            calls["kwargs"] = kwargs
            return MagicMock()

        monkeypatch.setattr(boto3, "client", fake_client)
        return calls

    def test_explicit_credentials_win(self, monkeypatch, tmp_path):
        backup = BackupSettings(bucket="bucket", access_key="bk", secret_key="bs")
        _team, settings = _team_and_settings(tmp_path, backup=backup)
        calls = self._capture_boto3(monkeypatch)

        template_import._make_source_client(
            settings,
            "bucket",
            endpoint="",
            access_key="ak",
            secret_key="sk",
            region="eu-west-1",
        )

        assert calls["kwargs"]["aws_access_key_id"] == "ak"
        assert calls["kwargs"]["region_name"] == "eu-west-1"

    def test_backup_settings_reused_for_matching_bucket(self, monkeypatch, tmp_path):
        backup = BackupSettings(bucket="bucket", access_key="bk", secret_key="bs")
        _team, settings = _team_and_settings(tmp_path, backup=backup)
        calls = self._capture_boto3(monkeypatch)

        template_import._make_source_client(
            settings, "bucket", endpoint="", access_key="", secret_key="", region=""
        )

        assert calls["kwargs"]["aws_access_key_id"] == "bk"

    def test_anonymous_for_foreign_bucket(self, monkeypatch, tmp_path):
        from botocore import UNSIGNED

        _team, settings = _team_and_settings(tmp_path)
        calls = self._capture_boto3(monkeypatch)

        template_import._make_source_client(
            settings, "public", endpoint="", access_key="", secret_key="", region=""
        )

        assert "aws_access_key_id" not in calls["kwargs"]
        assert calls["kwargs"]["config"].signature_version is UNSIGNED

    def test_key_without_secret_rejected(self, tmp_path):
        _team, settings = _team_and_settings(tmp_path)
        with pytest.raises(PrerequisiteNotMetError, match="together"):
            template_import._make_source_client(
                settings, "b", endpoint="", access_key="ak", secret_key="", region=""
            )


class TestImportTemplateDispatch:
    def test_s3_url_with_master_pwd_rejected(self, tmp_path):
        team, settings = _team_and_settings(tmp_path)
        with pytest.raises(PrerequisiteNotMetError, match="master_pwd is only used"):
            system_ops.import_template(
                settings,
                team,
                source="s3://bucket/prefix",
                master_pwd="secret",
                template_name="acme",
            )

    def test_http_without_master_pwd_rejected(self, tmp_path):
        team, settings = _team_and_settings(tmp_path)
        with pytest.raises(PrerequisiteNotMetError, match="master_pwd is required"):
            system_ops.import_template(
                settings,
                team,
                source="https://odoo.example.com",
                template_name="acme",
            )

    def test_overwrite_with_http_rejected(self, tmp_path):
        team, settings = _team_and_settings(tmp_path)
        with pytest.raises(PrerequisiteNotMetError, match="s3:// and local-path"):
            system_ops.import_template(
                settings,
                team,
                source="https://odoo.example.com",
                master_pwd="secret",
                template_name="acme",
                overwrite=True,
            )

    def test_no_source_without_refresh_rejected(self, tmp_path):
        team, settings = _team_and_settings(tmp_path)
        with pytest.raises(PrerequisiteNotMetError, match="source is required"):
            system_ops.import_template(settings, team, template_name="acme")

    def test_refresh_with_source_rejected(self, tmp_path):
        team, settings = _team_and_settings(tmp_path)
        with pytest.raises(PrerequisiteNotMetError, match="do not pass a source"):
            system_ops.import_template(
                settings,
                team,
                source="s3://bucket/prefix",
                template_name="acme",
                refresh=True,
            )

    def test_s3_url_dispatches_to_s3_import(self, monkeypatch, tmp_path):
        team, settings = _team_and_settings(tmp_path)
        called = {}

        def fake_import(*args, **kwargs):
            called.update(kwargs)
            return {"status": "imported"}

        monkeypatch.setattr(
            "oduflow.template_import.import_from_s3_prefix", fake_import
        )

        result = system_ops.import_template(
            settings,
            team,
            source="s3://bucket/backups/acme",
            template_name="acme",
            overwrite=True,
            s3_endpoint="https://minio.lan:9000",
            s3_access_key="ak",
            s3_secret_key="sk",
            s3_region="us-east-1",
        )

        assert result == {"status": "imported"}
        assert called["url"] == "s3://bucket/backups/acme"
        assert called["overwrite"] is True
        assert called["endpoint"] == "https://minio.lan:9000"
        assert called["access_key"] == "ak"

    def test_local_path_dispatches_to_local_import(self, monkeypatch, tmp_path):
        team, settings = _team_and_settings(tmp_path)
        called = {}

        def fake_import(*args, **kwargs):
            called.update(kwargs)
            return {"status": "imported"}

        monkeypatch.setattr(
            "oduflow.template_import.import_from_local_path", fake_import
        )

        system_ops.import_template(
            settings,
            team,
            source="/backups/acme",
            template_name="acme",
            overwrite=True,
        )

        assert called["path"] == "/backups/acme"
        assert called["overwrite"] is True

    def test_refresh_dispatches_to_refresh(self, monkeypatch, tmp_path):
        team, settings = _team_and_settings(tmp_path)
        called = {}

        def fake_refresh(*args, **kwargs):
            called.update(kwargs)
            return {"status": "refreshed"}

        monkeypatch.setattr(
            "oduflow.template_import.refresh_from_own_files", fake_refresh
        )

        result = system_ops.import_template(
            settings, team, template_name="acme", refresh=True
        )

        assert result == {"status": "refreshed"}
        assert called["template_name"] == "acme"


def _make_local_source(tmp_path):
    src = tmp_path / "drop"
    (src / "filestore" / "ab").mkdir(parents=True)
    (src / "filestore" / "ab" / "abcdef01").write_bytes(b"file-one")
    (src / "dump.pgdump").write_bytes(b"PGDMP fake dump bytes")
    return str(src)


def test_local_dir_import_hardlinks_files(monkeypatch, tmp_path):
    team, settings = _team_and_settings(tmp_path / "data")
    (tmp_path / "data").mkdir()
    source = _make_local_source(tmp_path)
    _patch_s3_import(monkeypatch, {})

    result = template_import.import_from_local_path(
        settings, team, path=source, template_name="acme"
    )

    assert result["status"] == "imported"
    assert result["dump_reloaded"] is True
    fs_file = os.path.join(team.get_template_filestore_path("acme"), "ab", "abcdef01")
    assert open(fs_file, "rb").read() == b"file-one"
    # Same filesystem: the file arrived via hardlink, not a copy.
    assert os.stat(fs_file).st_nlink >= 2
    with open(team.get_template_metadata_path("acme")) as f:
        metadata = json.load(f)
    assert metadata["source_url"] == source
    assert metadata["source_dump_etag"].startswith("local:")
    assert not os.path.exists(team.get_import_staging_dir("acme"))


def test_local_single_dump_file_is_db_only(monkeypatch, tmp_path):
    team, settings = _team_and_settings(tmp_path / "data")
    (tmp_path / "data").mkdir()
    dump = tmp_path / "nightly.dump"
    dump.write_bytes(b"PGDMP fake custom dump")
    _patch_s3_import(monkeypatch, {})

    result = template_import.import_from_local_path(
        settings, team, path=str(dump), template_name="acme"
    )

    assert result["includes_filestore"] is False
    assert result["source_db"] == "dump.pgdump"
    assert os.path.basename(team.get_template_sql_path("acme")) == "dump.pgdump"


def test_local_missing_path_rejected(monkeypatch, tmp_path):
    team, settings = _team_and_settings(tmp_path)
    _patch_s3_import(monkeypatch, {})
    from oduflow.errors import NotFoundError

    with pytest.raises(NotFoundError, match="does not exist"):
        template_import.import_from_local_path(
            settings, team, path=str(tmp_path / "nope"), template_name="acme"
        )


def test_refresh_from_own_files(monkeypatch, tmp_path):
    team, settings = _team_and_settings(tmp_path)
    _fake, reload_mock = _patch_s3_import(monkeypatch, {})

    # An external process dropped new files straight into the template dir.
    fs = team.get_template_filestore_path("acme")
    os.makedirs(os.path.join(fs, "ab"))
    with open(os.path.join(fs, "ab", "abcdef01"), "wb") as f:
        f.write(b"file-one")
    with open(os.path.join(team.get_template_dir("acme"), "dump.pgdump"), "wb") as f:
        f.write(b"PGDMP new dump bytes")
    with open(team.get_template_metadata_path("acme"), "w") as f:
        json.dump({"odoo_image": "odoo:18.0", "extra_addons": {"repo": "18.0"}}, f)

    result = template_import.refresh_from_own_files(
        settings, team, template_name="acme"
    )

    assert result["status"] == "refreshed"
    assert result["dump_reloaded"] is True
    reload_mock.assert_called_once()
    with open(team.get_template_metadata_path("acme")) as f:
        metadata = json.load(f)
    assert metadata["odoo_version"] == "18.0.1.0"
    assert metadata["extra_addons"] == {"repo": "18.0"}
    assert metadata["source_dump_etag"].startswith("local:")
    assert metadata["includes_filestore"] is True
    assert "filestore_size_mb" in metadata


def test_refresh_without_template_rejected(monkeypatch, tmp_path):
    team, settings = _team_and_settings(tmp_path)
    _patch_s3_import(monkeypatch, {})
    from oduflow.errors import NotFoundError

    with pytest.raises(NotFoundError, match="does not exist"):
        template_import.refresh_from_own_files(settings, team, template_name="acme")
