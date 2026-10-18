"""Remembered S3 source credentials for template imports.

The secret key for a private bucket lives in the team secret store and the
template metadata records only a ``secret:<name>`` reference, so an
``overwrite`` re-sync needs no key on the command line. These tests pin the
resolution precedence, the refusal to sign a different endpoint or key pair
with a remembered secret, and the lifetime of a secret Oduflow created.
"""

import datetime
import hashlib
import json
import os
from contextlib import contextmanager
from unittest.mock import MagicMock

import pytest

from oduflow import secret_store, template_import
from oduflow.docker_ops import system_ops
from oduflow.errors import PrerequisiteNotMetError
from oduflow.naming import validate_secret_name
from oduflow.settings import Settings, TeamSettings

_LAST_MODIFIED = datetime.datetime(2026, 10, 1, 3, 0, tzinfo=datetime.timezone.utc)


@pytest.fixture
def env(tmp_path):
    team = TeamSettings(
        team_id="1",
        data_dir=str(tmp_path),
        port_registry_path=str(tmp_path / "ports.json"),
    )
    settings = Settings(base_data_dir=str(tmp_path), teams={"1": team})
    return team, settings


def _names(team):
    return {s["name"] for s in secret_store.list_secrets(team)}


# --- managed secret names ----------------------------------------------------


def test_plain_template_name_becomes_a_readable_secret_name():
    assert template_import.managed_secret_name("acme") == "s3-template-acme"


@pytest.mark.parametrize(
    "template_name",
    ["Acme", "team/prod-copy", "x" * 80, "Mixed/Case-And-Length" + "y" * 60],
)
def test_names_needing_normalization_stay_valid_and_distinct(template_name):
    name = template_import.managed_secret_name(template_name)
    # Must survive the secret store's own validation, including the 64 cap.
    assert validate_secret_name(name) == name
    assert name.startswith("s3-template-")


def test_names_that_normalize_alike_do_not_collide():
    assert template_import.managed_secret_name(
        "Acme"
    ) != template_import.managed_secret_name("acme")


# --- resolution precedence ---------------------------------------------------


def test_raw_key_is_stored_under_the_managed_name(env):
    team, _settings = env
    creds = template_import.resolve_source_credentials(
        team, "acme", access_key="AK", secret_key="RAW"
    )
    assert creds.secret_key == "RAW"
    assert creds.secret_ref == "secret:s3-template-acme"
    assert creds.secret_managed is True
    assert creds.store_value == "RAW"
    # Nothing is written until the caller persists it.
    assert _names(team) == set()
    template_import.store_source_secret(team, creds)
    assert _names(team) == {"s3-template-acme"}


def test_metadata_records_the_reference_and_never_the_value(env):
    team, _settings = env
    creds = template_import.resolve_source_credentials(
        team, "acme", access_key="AK", secret_key="RAW"
    )
    recorded = template_import.credential_metadata(creds)
    assert recorded == {
        "source_access_key": "AK",
        "source_secret_key": "secret:s3-template-acme",
        "source_secret_managed": True,
    }
    assert "RAW" not in json.dumps(recorded)


def test_operator_reference_is_used_as_is_and_not_adopted(env):
    team, _settings = env
    secret_store.set_secret(team, "shared-s3", "SHARED")
    creds = template_import.resolve_source_credentials(
        team, "acme", access_key="AK", secret_key="secret:shared-s3"
    )
    assert creds.secret_key == "SHARED"
    assert creds.secret_ref == "secret:shared-s3"
    # Not managed: Oduflow did not create it, so it must not delete it either.
    assert creds.secret_managed is False
    assert creds.store_value == ""


def test_dangling_reference_is_refused(env):
    team, _settings = env
    with pytest.raises(PrerequisiteNotMetError, match="Undefined secret"):
        template_import.resolve_source_credentials(
            team, "acme", access_key="AK", secret_key="secret:absent"
        )


def test_empty_key_reuses_the_remembered_secret_and_access_key(env):
    team, _settings = env
    secret_store.set_secret(team, "s3-template-acme", "RAW")
    carried = {
        "source_access_key": "AK",
        "source_secret_key": "secret:s3-template-acme",
        "source_secret_managed": True,
        "source_endpoint": "",
    }
    creds = template_import.resolve_source_credentials(team, "acme", carried=carried)
    assert (creds.access_key, creds.secret_key) == ("AK", "RAW")
    assert creds.secret_managed is True
    # Nothing new to write: the secret is already stored.
    assert creds.store_value == ""


def test_no_keys_and_nothing_remembered_stays_anonymous(env):
    team, _settings = env
    creds = template_import.resolve_source_credentials(team, "acme")
    assert creds == template_import.SourceCredentials()


@pytest.mark.parametrize(
    "override",
    [
        {"endpoint": "https://minio.example.com"},
        {"access_key": "OTHER"},
    ],
)
def test_remembered_secret_is_never_sent_to_a_different_target(env, override):
    team, _settings = env
    secret_store.set_secret(team, "s3-template-acme", "RAW")
    carried = {
        "source_access_key": "AK",
        "source_secret_key": "secret:s3-template-acme",
        "source_secret_managed": True,
        "source_endpoint": "",
    }
    with pytest.raises(PrerequisiteNotMetError, match="Re-enter s3_secret_key"):
        template_import.resolve_source_credentials(
            team, "acme", carried=carried, **override
        )


def test_rotating_the_key_reuses_the_same_managed_secret(env):
    team, _settings = env
    carried = {
        "source_access_key": "AK",
        "source_secret_key": "secret:s3-template-acme",
        "source_secret_managed": True,
    }
    creds = template_import.resolve_source_credentials(
        team, "acme", access_key="AK", secret_key="ROTATED", carried=carried
    )
    assert creds.secret_ref == "secret:s3-template-acme"
    template_import.store_source_secret(team, creds)
    template_import.release_replaced_secret(team, creds, carried)
    # One secret, holding the new value — no orphan left behind.
    assert _names(team) == {"s3-template-acme"}
    assert (
        secret_store.resolve_env_secrets(team, {"K": "secret:s3-template-acme"})["K"]
        == "ROTATED"
    )


def test_switching_to_an_operator_secret_releases_the_managed_one(env):
    team, _settings = env
    secret_store.set_secret(team, "s3-template-acme", "RAW")
    secret_store.set_secret(team, "shared-s3", "SHARED")
    carried = {
        "source_access_key": "AK",
        "source_secret_key": "secret:s3-template-acme",
        "source_secret_managed": True,
    }
    creds = template_import.resolve_source_credentials(
        team, "acme", access_key="AK", secret_key="secret:shared-s3", carried=carried
    )
    template_import.release_replaced_secret(team, creds, carried)
    assert _names(team) == {"shared-s3"}


def test_an_unmanaged_secret_is_never_deleted(env):
    team, _settings = env
    secret_store.set_secret(team, "shared-s3", "SHARED")
    carried = {
        "source_secret_key": "secret:shared-s3",
        "source_secret_managed": False,
    }
    creds = template_import.resolve_source_credentials(
        team, "acme", access_key="AK", secret_key="RAW"
    )
    template_import.release_replaced_secret(team, creds, carried)
    assert _names(team) == {"shared-s3"}


# --- lifetime inside a real import ------------------------------------------


class _FakeS3Client:
    def __init__(self, objects):
        self.objects = objects

    def get_paginator(self, name):
        assert name == "list_objects_v2"
        client = self

        class _P:
            def paginate(self, Bucket, Prefix=""):
                yield {
                    "Contents": [
                        {
                            "Key": key,
                            "Size": len(body),
                            "ETag": f'"{hashlib.md5(body).hexdigest()}"',
                            "LastModified": _LAST_MODIFIED,
                        }
                        for key, body in sorted(client.objects.items())
                        if key.startswith(Prefix)
                    ]
                }

        return _P()

    def download_file(self, bucket, key, filename):
        with open(filename, "wb") as f:
            f.write(self.objects[key])


@contextmanager
def _remount():
    remount = MagicMock()
    remount.affected = []
    remount.failures = []
    yield remount


def _patch_import(monkeypatch, *, reload_fails=False):
    """Patch everything below the credential layer, keeping it exercised."""
    seen: dict[str, str] = {}
    fake = _FakeS3Client({"acme/dump.pgdump": b"PGDMP fake dump bytes"})

    def capture(settings, bucket, *, endpoint, access_key, secret_key, region):
        seen.update(endpoint=endpoint, access_key=access_key, secret_key=secret_key)
        return fake

    monkeypatch.setattr(template_import, "_make_source_client", capture)
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
    if reload_fails:
        monkeypatch.setattr(
            system_ops,
            "reload_template",
            MagicMock(side_effect=RuntimeError("restore blew up")),
        )
    else:
        monkeypatch.setattr(
            system_ops,
            "reload_template",
            MagicMock(
                return_value={"template_db": "tpl", "restore_seconds": 0.1},
            ),
        )
    monkeypatch.setattr(
        system_ops,
        "_read_template_manifest_from_db",
        lambda *a, **k: {"version": "18.0.1.0", "major_version": "18.0"},
    )
    return seen


def test_import_stores_the_secret_and_records_only_its_reference(monkeypatch, env):
    team, settings = env
    seen = _patch_import(monkeypatch)

    template_import.import_from_s3_prefix(
        settings,
        team,
        url="s3://bucket/acme",
        template_name="acme",
        access_key="AK",
        secret_key="RAW",
    )

    assert seen["access_key"] == "AK" and seen["secret_key"] == "RAW"
    assert _names(team) == {"s3-template-acme"}
    with open(team.get_template_metadata_path("acme")) as f:
        metadata = json.load(f)
    assert metadata["source_secret_key"] == "secret:s3-template-acme"
    assert metadata["source_access_key"] == "AK"
    assert metadata["source_secret_managed"] is True
    assert "RAW" not in json.dumps(metadata)


def test_resync_signs_with_the_remembered_secret(monkeypatch, env):
    team, settings = env
    seen = _patch_import(monkeypatch)
    template_import.import_from_s3_prefix(
        settings,
        team,
        url="s3://bucket/acme",
        template_name="acme",
        access_key="AK",
        secret_key="RAW",
    )

    seen.clear()
    template_import.import_from_s3_prefix(
        settings, team, url="s3://bucket/acme", template_name="acme", overwrite=True
    )

    # No key passed this time, yet the request is still signed.
    assert seen["access_key"] == "AK" and seen["secret_key"] == "RAW"


def test_failed_fresh_import_leaves_no_orphan_secret(monkeypatch, env):
    team, settings = env
    _patch_import(monkeypatch, reload_fails=True)

    with pytest.raises(RuntimeError, match="restore blew up"):
        template_import.import_from_s3_prefix(
            settings,
            team,
            url="s3://bucket/acme",
            template_name="acme",
            access_key="AK",
            secret_key="RAW",
        )

    assert _names(team) == set()


def test_failed_resync_keeps_the_secret_the_template_still_needs(monkeypatch, env):
    team, settings = env
    _patch_import(monkeypatch)
    template_import.import_from_s3_prefix(
        settings,
        team,
        url="s3://bucket/acme",
        template_name="acme",
        access_key="AK",
        secret_key="RAW",
    )

    _patch_import(monkeypatch, reload_fails=True)
    with pytest.raises(RuntimeError, match="restore blew up"):
        template_import.import_from_s3_prefix(
            settings,
            team,
            url="s3://bucket/acme",
            template_name="acme",
            overwrite=True,
            access_key="AK",
            secret_key="ROTATED",
        )

    assert _names(team) == {"s3-template-acme"}


def test_deleting_the_template_deletes_its_managed_secret(monkeypatch, env):
    team, settings = env
    _patch_import(monkeypatch)
    template_import.import_from_s3_prefix(
        settings,
        team,
        url="s3://bucket/acme",
        template_name="acme",
        access_key="AK",
        secret_key="RAW",
    )
    assert _names(team) == {"s3-template-acme"}

    client = MagicMock()
    client.containers.list.return_value = []
    monkeypatch.setattr(system_ops, "get_client", lambda: client)
    monkeypatch.setattr(system_ops, "_db_exists", lambda *a, **k: False)
    system_ops.delete_template(settings, team, "acme")

    assert _names(team) == set()


def test_publishing_over_an_s3_template_releases_its_secret(monkeypatch, env):
    team, settings = env
    _patch_import(monkeypatch)
    template_import.import_from_s3_prefix(
        settings,
        team,
        url="s3://bucket/acme",
        template_name="acme",
        access_key="AK",
        secret_key="RAW",
    )
    assert _names(team) == {"s3-template-acme"}

    # Publishing from an environment rebuilds metadata with no S3 lineage.
    system_ops._finalize_template_metadata(
        settings, team, "acme", {"source_production": "prod"}
    )

    assert _names(team) == set()
    with open(team.get_template_metadata_path("acme")) as f:
        assert "source_secret_key" not in json.load(f)


def test_a_non_s3_publish_does_not_touch_an_unrelated_secret(monkeypatch, env):
    team, settings = env
    secret_store.set_secret(team, "shared-s3", "SHARED")
    os.makedirs(team.get_template_dir("plain"), exist_ok=True)

    system_ops._finalize_template_metadata(settings, team, "plain", {})

    assert _names(team) == {"shared-s3"}
