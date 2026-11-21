import json
import os
import shutil
import zipfile
from contextlib import contextmanager
from unittest.mock import MagicMock

import pytest

from oduflow.docker_ops import system_ops
from oduflow.errors import PrerequisiteNotMetError
from oduflow.settings import Settings, TeamSettings

HASH_60 = "609e7ca59cc05bf0de7233c6781a381b742a2931"
HASH_61 = "61dde68eefc2d6823b1243e482f522de8f9a8f32"


def _team_and_settings(tmp_path):
    team = TeamSettings(
        team_id="1",
        data_dir=str(tmp_path),
        port_registry_path=str(tmp_path / "ports.json"),
    )
    settings = Settings(base_data_dir=str(tmp_path), teams={"1": team})
    return team, settings


@contextmanager
def _remount():
    remount = MagicMock()
    remount.affected = []
    remount.failures = []
    yield remount


def _patch_attach_dependencies(monkeypatch):
    monkeypatch.setattr(system_ops, "get_client", lambda: MagicMock())
    monkeypatch.setattr(system_ops, "_db_exists", lambda *a, **k: True)
    monkeypatch.setattr(
        "oduflow.docker_ops.env_ops.remount_template_overlays",
        lambda *a, **k: _remount(),
    )
    monkeypatch.setattr(system_ops, "get_odoo_uid_gid", lambda *a, **k: "100:100")
    monkeypatch.setattr(system_ops, "chown_recursive", lambda *a, **k: None)


def _write_metadata(team, template_name="prod"):
    tpl_dir = team.get_template_dir(template_name)
    os.makedirs(tpl_dir, exist_ok=True)
    with open(team.get_template_metadata_path(template_name), "w") as f:
        json.dump({"odoo_image": "odoo:19.0", "includes_filestore": False}, f)


def _zip(path, members):
    with zipfile.ZipFile(path, "w") as zf:
        for name, body in members.items():
            zf.writestr(name, body)


def test_attach_filestore_zip_auto_strips_database_prefix(monkeypatch, tmp_path):
    team, settings = _team_and_settings(tmp_path)
    _write_metadata(team)
    _patch_attach_dependencies(monkeypatch)
    archive = tmp_path / "filestore.zip"
    _zip(
        archive,
        {
            f"odoo19-mirage/60/{HASH_60}": b"one",
            f"odoo19-mirage/61/{HASH_61}": b"two",
            f"odoo19-mirage/checklist/60/{HASH_60}": b"",
            f"odoo19-mirage/checklist/61/{HASH_61}": b"",
            "odoo19-mirage/checklist/ab/" + "ab" * 20: b"",
        },
    )

    result = system_ops.attach_filestore(settings, team, "prod", str(archive))

    fs = team.get_template_filestore_path("prod")
    assert result["status"] == "attached"
    assert result["strip_prefix"] == "odoo19-mirage"
    assert result["filestore_files"] == 2
    assert open(os.path.join(fs, "60", HASH_60), "rb").read() == b"one"
    assert not os.path.exists(os.path.join(fs, "odoo19-mirage"))
    with open(team.get_template_metadata_path("prod")) as f:
        metadata = json.load(f)
    assert metadata["includes_filestore"] is True
    assert metadata["filestore_size_mb"] >= 0.0


def test_attach_filestore_zip_without_prefix(monkeypatch, tmp_path):
    team, settings = _team_and_settings(tmp_path)
    _write_metadata(team)
    _patch_attach_dependencies(monkeypatch)
    archive = tmp_path / "filestore.zip"
    _zip(archive, {f"60/{HASH_60}": b"one"})

    result = system_ops.attach_filestore(settings, team, "prod", str(archive))

    assert result["strip_prefix"] == ""
    assert os.path.isfile(
        os.path.join(team.get_template_filestore_path("prod"), "60", HASH_60)
    )


def test_attach_filestore_ambiguous_archive_prefix_fails(monkeypatch, tmp_path):
    team, settings = _team_and_settings(tmp_path)
    _write_metadata(team)
    _patch_attach_dependencies(monkeypatch)
    archive = tmp_path / "filestore.zip"
    _zip(
        archive,
        {
            f"db-a/60/{HASH_60}": b"one",
            f"db-b/61/{HASH_61}": b"two",
        },
    )

    with pytest.raises(PrerequisiteNotMetError, match="unique filestore prefix"):
        system_ops.attach_filestore(settings, team, "prod", str(archive))


def test_attach_filestore_rsync_source_uses_rsync_and_normalizes(monkeypatch, tmp_path):
    team, settings = _team_and_settings(tmp_path)
    _write_metadata(team)
    _patch_attach_dependencies(monkeypatch)
    source = tmp_path / "source"
    os.makedirs(source / "odoo19-mirage" / "60")
    (source / "odoo19-mirage" / "60" / HASH_60).write_bytes(b"one")
    calls = []

    def fake_run(cmd, check, capture_output):
        calls.append(cmd)
        src = cmd[-2].rstrip("/")
        dest = cmd[-1].rstrip("/")
        shutil.copytree(src, dest, dirs_exist_ok=True)

    monkeypatch.setattr(system_ops.subprocess, "run", fake_run)

    result = system_ops.attach_filestore(settings, team, "prod", str(source))

    assert calls[0][:3] == ["rsync", "-a", "--delete"]
    assert result["source_kind"] == "rsync"
    assert result["strip_prefix"] == "odoo19-mirage"
    assert os.path.isfile(
        os.path.join(team.get_template_filestore_path("prod"), "60", HASH_60)
    )


def test_attach_filestore_remote_source_builds_rsync_command(monkeypatch, tmp_path):
    team, settings = _team_and_settings(tmp_path)
    _write_metadata(team)
    _patch_attach_dependencies(monkeypatch)
    calls = []

    def fake_run(cmd, check, capture_output):
        calls.append(cmd)
        dest = cmd[-1].rstrip("/")
        os.makedirs(os.path.join(dest, "60"))
        with open(os.path.join(dest, "60", HASH_60), "wb") as f:
            f.write(b"one")

    monkeypatch.setattr(system_ops.subprocess, "run", fake_run)

    system_ops.attach_filestore(
        settings, team, "prod", "odoo@example.com:/srv/filestore/odoo19-mirage"
    )

    assert calls[0][-2] == "odoo@example.com:/srv/filestore/odoo19-mirage/"


def test_attach_filestore_restores_previous_on_replace_failure(monkeypatch, tmp_path):
    team, settings = _team_and_settings(tmp_path)
    _write_metadata(team)
    _patch_attach_dependencies(monkeypatch)
    target = team.get_template_filestore_path("prod")
    os.makedirs(os.path.join(target, "60"))
    old_file = os.path.join(target, "60", HASH_60)
    with open(old_file, "wb") as f:
        f.write(b"old")
    archive = tmp_path / "filestore.zip"
    _zip(archive, {f"61/{HASH_61}": b"new"})

    real_replace = os.replace
    prepared_replace_seen = False

    def fail_prepared_replace(src, dst):
        nonlocal prepared_replace_seen
        if os.path.basename(src) == "prepared":
            prepared_replace_seen = True
            raise OSError("replace failed")
        return real_replace(src, dst)

    monkeypatch.setattr(system_ops.os, "replace", fail_prepared_replace)

    with pytest.raises(OSError, match="replace failed"):
        system_ops.attach_filestore(settings, team, "prod", str(archive))

    assert prepared_replace_seen
    assert os.path.isfile(old_file)
    assert open(old_file, "rb").read() == b"old"
    assert not os.path.exists(os.path.join(target, "61", HASH_61))
