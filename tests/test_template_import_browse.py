"""The S3 object browser behind the dashboard's import dialog.

A prefix typed blind is the one import step nothing can verify before it runs,
so the dialog walks the bucket a level at a time. These tests pin what one
level returns, what it hides, and that browsing an existing S3 template signs
with that template's stored secret without ever echoing it back.
"""

import datetime

import pytest

from oduflow import secret_store, template_import
from oduflow.errors import NotFoundError, PrerequisiteNotMetError
from oduflow.settings import Settings, TeamSettings

_WHEN = datetime.datetime(2026, 10, 1, 3, 0, tzinfo=datetime.timezone.utc)
_OLDER = datetime.datetime(2026, 9, 1, 3, 0, tzinfo=datetime.timezone.utc)


@pytest.fixture
def env(tmp_path):
    team = TeamSettings(
        team_id="1",
        data_dir=str(tmp_path),
        port_registry_path=str(tmp_path / "ports.json"),
    )
    settings = Settings(base_data_dir=str(tmp_path), teams={"1": team})
    return team, settings


class _FakeClient:
    def __init__(self, **responses):
        self.responses = responses
        self.calls: list[tuple[str, dict]] = []

    def _respond(self, name, kwargs):
        self.calls.append((name, kwargs))
        value = self.responses[name]
        if isinstance(value, Exception):
            raise value
        return value

    def list_buckets(self, **kwargs):
        return self._respond("list_buckets", kwargs)

    def list_objects_v2(self, **kwargs):
        return self._respond("list_objects_v2", kwargs)


def _with_client(monkeypatch, client):
    seen: dict[str, str] = {}

    def make(settings, bucket, *, endpoint, access_key, secret_key, region):
        seen.update(
            bucket=bucket,
            endpoint=endpoint,
            access_key=access_key,
            secret_key=secret_key,
            region=region,
        )
        return client

    monkeypatch.setattr(template_import, "_make_source_client", make)
    return seen


def test_empty_bucket_lists_the_buckets(monkeypatch, env):
    team, settings = env
    client = _FakeClient(
        list_buckets={"Buckets": [{"Name": "backups"}, {"Name": "archive"}, {}]}
    )
    _with_client(monkeypatch, client)

    result = template_import.browse_source(settings, team)

    assert result["buckets"] == ["backups", "archive"]
    assert result["folders"] == [] and result["files"] == []
    assert result["bucket"] == "" and result["truncated"] is False


def test_one_level_splits_folders_from_files_and_hides_the_marker(monkeypatch, env):
    team, settings = env
    client = _FakeClient(
        list_objects_v2={
            "CommonPrefixes": [{"Prefix": "backups/acme/"}, {}],
            "Contents": [
                # The zero-byte object standing for the folder itself.
                {"Key": "backups/", "Size": 0, "LastModified": _WHEN},
                {
                    "Key": "backups/dump.pgdump",
                    "Size": 42,
                    "LastModified": _OLDER,
                    "ETag": '"e1"',
                },
                {"Key": "backups/notes.txt", "Size": 7, "LastModified": _WHEN},
            ],
        }
    )
    _with_client(monkeypatch, client)

    result = template_import.browse_source(
        settings, team, bucket="store", prefix="backups"
    )

    assert client.calls[0][1]["Delimiter"] == "/"
    assert client.calls[0][1]["Prefix"] == "backups/"
    assert result["folders"] == ["backups/acme/"]
    # Newest first, and the folder marker is gone.
    assert [f["key"] for f in result["files"]] == [
        "backups/notes.txt",
        "backups/dump.pgdump",
    ]
    assert result["files"][1] == {
        "key": "backups/dump.pgdump",
        "size": 42,
        "last_modified": _OLDER.isoformat(),
        "etag": "e1",
    }


@pytest.mark.parametrize(
    "given,expected",
    [("", ""), ("a/b", "a/b/"), ("/a/b/", "a/b/"), ("a/b/", "a/b/")],
)
def test_prefix_is_normalized_to_one_folder_level(monkeypatch, env, given, expected):
    team, settings = env
    client = _FakeClient(list_objects_v2={})
    _with_client(monkeypatch, client)

    result = template_import.browse_source(settings, team, bucket="store", prefix=given)

    assert client.calls[0][1]["Prefix"] == expected
    assert result["prefix"] == expected


def test_truncated_listing_reports_its_continuation_token(monkeypatch, env):
    team, settings = env
    client = _FakeClient(
        list_objects_v2={
            "Contents": [],
            "IsTruncated": True,
            "NextContinuationToken": "tok2",
        }
    )
    _with_client(monkeypatch, client)

    result = template_import.browse_source(
        settings, team, bucket="store", continuation_token="tok1"
    )

    assert client.calls[0][1]["ContinuationToken"] == "tok1"
    assert result["truncated"] is True
    assert result["next_token"] == "tok2"


def test_first_page_sends_no_continuation_token(monkeypatch, env):
    team, settings = env
    client = _FakeClient(list_objects_v2={})
    _with_client(monkeypatch, client)

    template_import.browse_source(settings, team, bucket="store")

    assert "ContinuationToken" not in client.calls[0][1]


@pytest.mark.parametrize("bucket", ["../etc", "has space", "-leading", "a" * 256, "/"])
def test_invalid_bucket_names_are_refused(monkeypatch, env, bucket):
    team, settings = env
    _with_client(monkeypatch, _FakeClient(list_objects_v2={}))

    with pytest.raises(ValueError, match="Invalid S3 bucket name"):
        template_import.browse_source(settings, team, bucket=bucket)


def test_loopback_endpoint_is_refused(env):
    """The browser reaches the network on operator-supplied input, so it must
    pass the same SSRF gate as an import (no client is patched here)."""
    from oduflow.url_safety import BlockedURLError

    team, settings = env
    with pytest.raises(BlockedURLError):
        template_import.browse_source(
            settings, team, bucket="store", endpoint="http://127.0.0.1:9000"
        )


def test_browsing_a_template_signs_with_its_stored_secret(monkeypatch, env, tmp_path):
    team, settings = env
    secret_store.set_secret(team, "s3-template-acme", "RAW")
    import json
    import os

    os.makedirs(team.get_template_dir("acme"), exist_ok=True)
    with open(team.get_template_metadata_path("acme"), "w") as f:
        json.dump(
            {
                "source_url": "s3://store/backups/acme",
                "source_access_key": "AK",
                "source_secret_key": "secret:s3-template-acme",
                "source_secret_managed": True,
                "source_endpoint": "",
            },
            f,
        )
    seen = _with_client(monkeypatch, _FakeClient(list_objects_v2={}))

    result = template_import.browse_source(
        settings, team, bucket="store", template_name="acme"
    )

    assert (seen["access_key"], seen["secret_key"]) == ("AK", "RAW")
    # The listing itself says nothing about the credentials.
    assert "RAW" not in str(result)


def test_browsing_a_template_at_another_endpoint_demands_the_key(monkeypatch, env):
    team, settings = env
    secret_store.set_secret(team, "s3-template-acme", "RAW")
    import json
    import os

    os.makedirs(team.get_template_dir("acme"), exist_ok=True)
    with open(team.get_template_metadata_path("acme"), "w") as f:
        json.dump(
            {
                "source_url": "s3://store/backups/acme",
                "source_access_key": "AK",
                "source_secret_key": "secret:s3-template-acme",
                "source_secret_managed": True,
                "source_endpoint": "",
            },
            f,
        )
    _with_client(monkeypatch, _FakeClient(list_objects_v2={}))

    with pytest.raises(PrerequisiteNotMetError, match="Re-enter s3_secret_key"):
        template_import.browse_source(
            settings,
            team,
            bucket="store",
            template_name="acme",
            endpoint="https://minio.example.com",
        )


def _client_error(code, status):
    from botocore.exceptions import ClientError

    return ClientError(
        {
            "Error": {"Code": code, "Message": f"{code} happened"},
            "ResponseMetadata": {"HTTPStatusCode": status},
        },
        "ListObjectsV2",
    )


def test_a_missing_bucket_reads_as_not_found(monkeypatch, env):
    team, settings = env
    _with_client(
        monkeypatch, _FakeClient(list_objects_v2=_client_error("NoSuchBucket", 404))
    )

    with pytest.raises(NotFoundError, match="not found"):
        template_import.browse_source(settings, team, bucket="store")


def test_a_rejected_key_keeps_the_store_s_own_wording(monkeypatch, env):
    team, settings = env
    _with_client(
        monkeypatch,
        _FakeClient(list_objects_v2=_client_error("InvalidAccessKeyId", 403)),
    )

    # The operator needs to tell a typo from a permissions problem, so the
    # store's message is passed through rather than flattened.
    with pytest.raises(PrerequisiteNotMetError, match="InvalidAccessKeyId happened"):
        template_import.browse_source(settings, team, bucket="store")
