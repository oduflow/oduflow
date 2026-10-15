"""Importing a template from an Odoo backup `.zip` held in a bucket.

The raw dump + filestore layout stays the efficient path for a large source;
a zip is accepted because operators already hold one. These tests pin the
unpacking (including the zip-slip guard shared with the pull-from-Odoo
import), the ETag skip that makes a re-sync of an unchanged archive free, and
the fact that a failed import leaves nothing behind.
"""

from __future__ import annotations

import datetime
import io
import json
import os
import zipfile
from contextlib import contextmanager
from unittest.mock import MagicMock

import pytest

from oduflow import secret_store, template_import
from oduflow.docker_ops import system_ops
from oduflow.errors import PrerequisiteNotMetError
from oduflow.settings import Settings, TeamSettings

_WHEN = datetime.datetime(2026, 10, 1, 3, 0, tzinfo=datetime.timezone.utc)


@pytest.fixture
def env(tmp_path):
    team = TeamSettings(
        team_id="1",
        data_dir=str(tmp_path),
        port_registry_path=str(tmp_path / "ports.json"),
    )
    settings = Settings(base_data_dir=str(tmp_path), teams={"1": team})
    return team, settings


def _backup_zip(members=None, *, dump=b"-- SQL dump\n", manifest=None) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        if dump is not None:
            zf.writestr("dump.sql", dump)
        if manifest is not None:
            zf.writestr("manifest.json", json.dumps(manifest))
        for name, body in (members or {}).items():
            zf.writestr(name, body)
    return buf.getvalue()


_DEFAULT_MEMBERS = {
    "filestore/ab/abcdef01": b"file-one",
    "filestore/cd/cdef2345": b"file-two-longer",
    "filestore/checklist/0-ignored": b"skip me",
}


# --- key classification ------------------------------------------------------


@pytest.mark.parametrize(
    "url,is_archive",
    [
        ("s3://b/backups/odoo.zip", True),
        ("s3://b/backups/ODOO.ZIP", True),
        ("s3://b/backups/mydb/", False),
        ("s3://b/backups/mydb", False),
        ("s3://b/backups/dump.pgdump", False),
        ("s3://b/zipped/mydb", False),
    ],
)
def test_only_a_zip_key_reads_as_an_archive(url, is_archive):
    assert template_import.is_archive_key(url) is is_archive


# --- unpacking ---------------------------------------------------------------


def test_extract_writes_the_dump_and_the_filestore(tmp_path):
    archive = tmp_path / "backup.zip"
    archive.write_bytes(_backup_zip(_DEFAULT_MEMBERS, manifest={"version": "18.0"}))
    dump = tmp_path / "staged" / "dump.sql"
    fs = tmp_path / "staged" / "filestore"

    manifest = template_import.extract_odoo_backup_zip(str(archive), str(dump), str(fs))

    assert manifest == {"version": "18.0"}
    assert dump.read_bytes() == b"-- SQL dump\n"
    assert (fs / "ab" / "abcdef01").read_bytes() == b"file-one"
    # checklist/ holds symlink-like bookkeeping entries, never attachments.
    assert not (fs / "checklist").exists()


def test_extract_without_a_manifest_still_works(tmp_path):
    archive = tmp_path / "backup.zip"
    archive.write_bytes(_backup_zip(_DEFAULT_MEMBERS))
    dump = tmp_path / "dump.sql"
    fs = tmp_path / "filestore"

    assert (
        template_import.extract_odoo_backup_zip(str(archive), str(dump), str(fs)) == {}
    )
    assert dump.is_file()


def test_an_archive_without_a_dump_is_refused(tmp_path):
    archive = tmp_path / "backup.zip"
    archive.write_bytes(_backup_zip({"filestore/ab/x": b"x"}, dump=None))

    with pytest.raises(PrerequisiteNotMetError, match="not an Odoo backup"):
        template_import.extract_odoo_backup_zip(
            str(archive), str(tmp_path / "d.sql"), str(tmp_path / "fs")
        )


@pytest.mark.parametrize(
    "member", ["filestore/../../escaped", "filestore/a/../../../escaped"]
)
def test_members_escaping_the_filestore_are_skipped(tmp_path, member):
    archive = tmp_path / "backup.zip"
    archive.write_bytes(_backup_zip({member: b"pwned", **_DEFAULT_MEMBERS}))
    fs = tmp_path / "out" / "filestore"

    template_import.extract_odoo_backup_zip(
        str(archive), str(tmp_path / "out" / "dump.sql"), str(fs)
    )

    assert not (tmp_path / "escaped").exists()
    assert not (tmp_path / "out" / "escaped").exists()
    # The safe members are still there.
    assert (fs / "ab" / "abcdef01").is_file()


def test_extract_replaces_a_previous_filestore(tmp_path):
    fs = tmp_path / "filestore"
    (fs / "old").mkdir(parents=True)
    (fs / "old" / "stale").write_bytes(b"gone")
    archive = tmp_path / "backup.zip"
    archive.write_bytes(_backup_zip(_DEFAULT_MEMBERS))

    template_import.extract_odoo_backup_zip(
        str(archive), str(tmp_path / "dump.sql"), str(fs)
    )

    assert not (fs / "old").exists()
    assert (fs / "ab" / "abcdef01").is_file()


def test_uncompressed_size_counts_every_member(tmp_path):
    archive = tmp_path / "backup.zip"
    archive.write_bytes(_backup_zip(_DEFAULT_MEMBERS))

    expected = len(b"-- SQL dump\n") + sum(len(v) for v in _DEFAULT_MEMBERS.values())
    assert template_import.zip_uncompressed_bytes(str(archive)) == expected


# --- importing from a bucket -------------------------------------------------


class _FakeS3:
    def __init__(self, body: bytes, etag: str = "e1"):
        self.body = body
        self.etag = etag
        self.heads = 0
        self.downloads: list[str] = []

    def head_object(self, Bucket, Key):
        self.heads += 1
        return {
            "ContentLength": len(self.body),
            "ETag": f'"{self.etag}"',
            "LastModified": _WHEN,
        }

    def download_file(self, bucket, key, filename):
        self.downloads.append(key)
        with open(filename, "wb") as f:
            f.write(self.body)


@contextmanager
def _remount():
    remount = MagicMock()
    remount.affected = []
    remount.failures = []
    yield remount


def _patch(monkeypatch, fake, *, reload_fails=False):
    monkeypatch.setattr(template_import, "_make_source_client", lambda *a, **k: fake)
    monkeypatch.setattr("oduflow.docker_ops.client.get_client", lambda: MagicMock())
    monkeypatch.setattr(system_ops, "_db_exists", lambda *a, **k: False)
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
    monkeypatch.setattr(
        system_ops,
        "reload_template",
        MagicMock(
            side_effect=RuntimeError("restore blew up") if reload_fails else None,
            return_value={"template_db": "tpl", "restore_seconds": 0.3},
        ),
    )
    monkeypatch.setattr(
        system_ops,
        "_read_template_manifest_from_db",
        lambda *a, **k: {"version": "18.0.1.0", "major_version": "18.0"},
    )


def test_a_zip_import_restores_the_dump_and_the_filestore(monkeypatch, env):
    team, settings = env
    fake = _FakeS3(_backup_zip(_DEFAULT_MEMBERS))
    _patch(monkeypatch, fake)

    result = template_import.import_from_s3_archive(
        settings, team, url="s3://backups/odoo/acme.zip", template_name="acme"
    )

    assert result["status"] == "imported"
    assert result["dump_reloaded"] is True
    assert result["includes_filestore"] is True
    assert result["downloaded_files"] == 1
    assert fake.downloads == ["odoo/acme.zip"]
    fs = team.get_template_filestore_path("acme")
    assert open(os.path.join(fs, "ab", "abcdef01"), "rb").read() == b"file-one"
    assert os.path.basename(team.get_template_sql_path("acme")) == "dump.sql"
    with open(team.get_template_metadata_path("acme")) as f:
        metadata = json.load(f)
    assert metadata["source_url"] == "s3://backups/odoo/acme.zip"
    assert metadata["source_dump_etag"] == "e1"
    # Freshness describes the data, not the import.
    assert metadata["snapshot_at"] == _WHEN.isoformat()
    # Nothing is resumable here, so staging does not survive.
    assert not os.path.exists(team.get_import_staging_dir("acme"))


def test_database_only_never_unpacks_the_attachments(monkeypatch, env):
    team, settings = env
    _patch(monkeypatch, _FakeS3(_backup_zip(_DEFAULT_MEMBERS)))

    result = template_import.import_from_s3_archive(
        settings,
        team,
        url="s3://backups/odoo/acme.zip",
        template_name="acme",
        without_filestore=True,
    )

    assert result["includes_filestore"] is False
    # Unpacking a filestore only to discard it would cost the disk and the
    # minutes the flag exists to save.
    fs = team.get_template_filestore_path("acme")
    assert not os.path.isdir(fs) or os.listdir(fs) == []
    assert os.path.basename(team.get_template_sql_path("acme")) == "dump.sql"


def test_database_only_extraction_reads_just_the_dump(tmp_path):
    archive = tmp_path / "backup.zip"
    archive.write_bytes(_backup_zip(_DEFAULT_MEMBERS))
    fs = tmp_path / "filestore"

    template_import.extract_odoo_backup_zip(
        str(archive), str(tmp_path / "dump.sql"), str(fs), with_filestore=False
    )

    assert (tmp_path / "dump.sql").is_file()
    assert not fs.exists()


def test_an_unchanged_archive_costs_one_head(monkeypatch, env):
    team, settings = env
    fake = _FakeS3(_backup_zip(_DEFAULT_MEMBERS))
    _patch(monkeypatch, fake)
    template_import.import_from_s3_archive(
        settings, team, url="s3://backups/odoo/acme.zip", template_name="acme"
    )
    monkeypatch.setattr(system_ops, "_db_exists", lambda *a, **k: True)
    fake.downloads.clear()

    result = template_import.import_from_s3_archive(
        settings,
        team,
        url="s3://backups/odoo/acme.zip",
        template_name="acme",
        overwrite=True,
    )

    assert result["status"] == "unchanged"
    assert result["dump_reloaded"] is False
    assert fake.downloads == []


def test_a_changed_archive_is_re_fetched(monkeypatch, env):
    team, settings = env
    fake = _FakeS3(_backup_zip(_DEFAULT_MEMBERS))
    _patch(monkeypatch, fake)
    template_import.import_from_s3_archive(
        settings, team, url="s3://backups/odoo/acme.zip", template_name="acme"
    )

    fake.etag = "e2"
    fake.body = _backup_zip({"filestore/ef/ef678901": b"brand-new"})
    fake.downloads.clear()
    monkeypatch.setattr(system_ops, "_db_exists", lambda *a, **k: True)

    result = template_import.import_from_s3_archive(
        settings,
        team,
        url="s3://backups/odoo/acme.zip",
        template_name="acme",
        overwrite=True,
    )

    assert result["dump_reloaded"] is True
    assert fake.downloads == ["odoo/acme.zip"]
    fs = team.get_template_filestore_path("acme")
    assert os.path.isfile(os.path.join(fs, "ef", "ef678901"))
    # The replaced tree is gone, not merged.
    assert not os.path.exists(os.path.join(fs, "ab", "abcdef01"))


def test_a_corrupt_archive_leaves_no_staging_or_secret(monkeypatch, env):
    team, settings = env
    _patch(monkeypatch, _FakeS3(b"not a zip at all"))

    with pytest.raises(Exception):
        template_import.import_from_s3_archive(
            settings,
            team,
            url="s3://backups/odoo/acme.zip",
            template_name="acme",
            access_key="AK",
            secret_key="RAW",
        )

    assert not os.path.exists(team.get_import_staging_dir("acme"))
    assert secret_store.list_secrets(team) == []


def test_a_zip_import_remembers_its_credentials(monkeypatch, env):
    team, settings = env
    _patch(monkeypatch, _FakeS3(_backup_zip(_DEFAULT_MEMBERS)))

    template_import.import_from_s3_archive(
        settings,
        team,
        url="s3://backups/odoo/acme.zip",
        template_name="acme",
        access_key="AK",
        secret_key="RAW",
        region="eu-central-1",
    )

    with open(team.get_template_metadata_path("acme")) as f:
        metadata = json.load(f)
    assert metadata["source_secret_key"] == "secret:s3-template-acme"
    assert metadata["source_region"] == "eu-central-1"
    assert "RAW" not in json.dumps(metadata)
    assert template_import.public_source_view(metadata)["has_secret"] is True


# --- dispatch ----------------------------------------------------------------


@pytest.mark.parametrize(
    "source,expected",
    [
        ("s3://b/backups/acme.zip", "import_from_s3_archive"),
        ("s3://b/backups/acme/", "import_from_s3_prefix"),
    ],
)
def test_import_template_routes_on_the_key(monkeypatch, env, source, expected):
    team, settings = env
    called = []
    for name in ("import_from_s3_archive", "import_from_s3_prefix"):
        monkeypatch.setattr(
            template_import,
            name,
            lambda *a, _n=name, **k: called.append(_n) or {"status": "imported"},
        )

    system_ops.import_template(settings, team, source=source, template_name="acme")

    assert called == [expected]
