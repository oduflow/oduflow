"""boto3 wrapper for the backup subsystem.

The module had no tests at all. The parts worth pinning are the ones whose
bugs are expensive rather than loud: key prefixing (a wrong prefix silently
splits a backup across two namespaces), the 403/404-vs-raise split in
``exists`` (swallowing a real error makes the chunkstore re-upload every
chunk), and multipart abort-on-failure (orphaned parts accrue storage cost
forever).
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from oduflow import s3_client
from oduflow.settings import BackupSettings


def _backup(**kwargs) -> BackupSettings:
    defaults = {
        "bucket": "backups",
        "access_key": "AK",
        "secret_key": "SK",
    }
    defaults.update(kwargs)
    return BackupSettings(**defaults)


def _client_error(status: int) -> type[Exception]:
    """A stand-in for botocore's ClientError carrying an HTTP status."""

    class ClientError(Exception):
        def __init__(self):
            super().__init__(f"HTTP {status}")
            self.response = {"ResponseMetadata": {"HTTPStatusCode": status}}

    return ClientError


class TestMakeClient:
    def test_credentials_and_retries_are_passed_through(self, monkeypatch):
        seen = {}

        def _boto_client(service, **kwargs):
            seen["service"] = service
            seen["kwargs"] = kwargs
            return "client"

        boto3 = MagicMock()
        boto3.client.side_effect = _boto_client
        monkeypatch.setitem(__import__("sys").modules, "boto3", boto3)

        assert s3_client.make_client(_backup()) == "client"
        assert seen["service"] == "s3"
        assert seen["kwargs"]["aws_access_key_id"] == "AK"
        assert seen["kwargs"]["aws_secret_access_key"] == "SK"
        assert seen["kwargs"]["config"].retries == {
            "max_attempts": 3,
            "mode": "standard",
        }

    def test_region_and_endpoint_are_only_sent_when_configured(self, monkeypatch):
        calls = []
        boto3 = MagicMock()
        boto3.client.side_effect = lambda service, **kw: calls.append(kw)
        monkeypatch.setitem(__import__("sys").modules, "boto3", boto3)

        s3_client.make_client(_backup())
        assert "region_name" not in calls[0]
        assert "endpoint_url" not in calls[0]

        s3_client.make_client(
            _backup(region="eu-central-1", endpoint="https://minio.example")
        )
        assert calls[1]["region_name"] == "eu-central-1"
        assert calls[1]["endpoint_url"] == "https://minio.example"

    def test_a_custom_endpoint_switches_to_path_addressing(self, monkeypatch):
        # Virtual-host addressing does not work against most S3-compatible
        # servers (MinIO, Ceph), so a configured endpoint must force paths.
        calls = []
        boto3 = MagicMock()
        boto3.client.side_effect = lambda service, **kw: calls.append(kw)
        monkeypatch.setitem(__import__("sys").modules, "boto3", boto3)

        s3_client.make_client(_backup())
        s3_client.make_client(_backup(endpoint="https://minio.example"))

        assert calls[0]["config"].s3 == {}
        assert calls[1]["config"].s3 == {"addressing_style": "path"}


class TestCheckS3:
    def test_reachable_bucket_is_ok(self, monkeypatch):
        client = MagicMock()
        monkeypatch.setattr(s3_client, "make_client", lambda backup: client)

        assert s3_client.check_s3(_backup()) == {"ok": True, "error": ""}
        client.head_bucket.assert_called_once_with(Bucket="backups")

    def test_failure_is_reported_not_raised(self, monkeypatch):
        client = MagicMock()
        client.head_bucket.side_effect = RuntimeError("no such bucket")
        monkeypatch.setattr(s3_client, "make_client", lambda backup: client)

        result = s3_client.check_s3(_backup())

        assert result["ok"] is False
        assert "no such bucket" in result["error"]


class TestS3StorageKeys:
    def _storage(self, prefix: str, client: MagicMock | None = None):
        return s3_client.S3Storage(_backup(), prefix, client=client or MagicMock())

    def test_the_prefix_is_prepended_to_every_key(self):
        client = MagicMock()
        storage = self._storage("team_1/prod", client)

        storage.put("chunks/ab/cd", b"x")

        assert client.put_object.call_args.kwargs["Key"] == "team_1/prod/chunks/ab/cd"

    def test_surrounding_slashes_in_the_prefix_are_stripped(self):
        client = MagicMock()
        storage = self._storage("/team_1/prod/", client)

        storage.put("k", b"x")

        assert client.put_object.call_args.kwargs["Key"] == "team_1/prod/k"

    def test_an_empty_prefix_leaves_keys_untouched(self):
        client = MagicMock()
        storage = self._storage("", client)

        storage.put("k", b"x")

        assert client.put_object.call_args.kwargs["Key"] == "k"

    def test_the_bucket_comes_from_the_backup_settings(self):
        storage = s3_client.S3Storage(_backup(bucket="other"), "p", client=MagicMock())
        assert storage.bucket == "other"

    def test_a_client_is_built_when_none_is_supplied(self, monkeypatch):
        monkeypatch.setattr(s3_client, "make_client", lambda backup: "made")
        assert s3_client.S3Storage(_backup(), "p").client == "made"


class TestS3StorageOperations:
    def _storage(self, client, prefix="p"):
        return s3_client.S3Storage(_backup(), prefix, client=client)

    def test_get_returns_the_body_bytes(self):
        client = MagicMock()
        client.get_object.return_value = {"Body": MagicMock(read=lambda: b"payload")}

        assert self._storage(client).get("k") == b"payload"
        assert client.get_object.call_args.kwargs["Key"] == "p/k"

    def test_put_sends_the_body(self):
        client = MagicMock()
        self._storage(client).put("k", b"payload")

        assert client.put_object.call_args.kwargs["Body"] == b"payload"

    def test_delete_targets_the_prefixed_key(self):
        client = MagicMock()
        self._storage(client).delete("k")

        assert client.delete_object.call_args.kwargs == {
            "Bucket": "backups",
            "Key": "p/k",
        }

    def test_exists_is_true_when_head_succeeds(self):
        client = MagicMock()
        assert self._storage(client).exists("k") is True
        assert client.head_object.call_args.kwargs["Key"] == "p/k"

    @pytest.mark.parametrize("status", [403, 404])
    def test_missing_or_forbidden_means_absent(self, status):
        # Buckets without ListBucket answer 403 for a missing key, so both
        # statuses have to read as "not there".
        client = MagicMock()
        error = _client_error(status)
        client.exceptions.ClientError = error
        client.head_object.side_effect = error()

        assert self._storage(client).exists("k") is False

    @pytest.mark.parametrize("status", [500, 503])
    def test_other_client_errors_propagate(self, status):
        # Swallowing a server error would make the chunkstore believe every
        # chunk is missing and re-upload the whole backup.
        client = MagicMock()
        error = _client_error(status)
        client.exceptions.ClientError = error
        client.head_object.side_effect = error()

        with pytest.raises(error):
            self._storage(client).exists("k")

    def test_list_strips_the_prefix_and_sorts(self):
        client = MagicMock()
        client.get_paginator.return_value.paginate.return_value = [
            {"Contents": [{"Key": "p/b"}, {"Key": "p/a"}]},
            {"Contents": [{"Key": "p/c"}]},
        ]

        assert self._storage(client).list("") == ["a", "b", "c"]

    def test_list_paginates_under_the_full_prefix(self):
        client = MagicMock()
        paginator = client.get_paginator.return_value
        paginator.paginate.return_value = []

        self._storage(client).list("chunks/")

        client.get_paginator.assert_called_once_with("list_objects_v2")
        assert paginator.paginate.call_args.kwargs == {
            "Bucket": "backups",
            "Prefix": "p/chunks/",
        }

    def test_list_without_a_prefix_returns_whole_keys(self):
        client = MagicMock()
        client.get_paginator.return_value.paginate.return_value = [
            {"Contents": [{"Key": "a/b"}]}
        ]

        assert s3_client.S3Storage(_backup(), "", client=client).list("") == ["a/b"]

    def test_an_empty_page_yields_nothing(self):
        client = MagicMock()
        client.get_paginator.return_value.paginate.return_value = [{}]

        assert self._storage(client).list("") == []

    def test_rename_copies_then_deletes_the_source(self):
        client = MagicMock()
        self._storage(client).rename("old", "new")

        assert client.copy_object.call_args.kwargs == {
            "Bucket": "backups",
            "Key": "p/new",
            "CopySource": {"Bucket": "backups", "Key": "p/old"},
        }
        assert client.delete_object.call_args.kwargs["Key"] == "p/old"

    def test_rename_does_not_delete_when_the_copy_fails(self):
        # Deleting after a failed copy would lose the object outright.
        client = MagicMock()
        client.copy_object.side_effect = RuntimeError("copy failed")

        with pytest.raises(RuntimeError):
            self._storage(client).rename("old", "new")

        client.delete_object.assert_not_called()


class TestMultipartUploadStream:
    def _client(self):
        client = MagicMock()
        client.create_multipart_upload.return_value = {"UploadId": "UP1"}
        client.upload_part.side_effect = lambda **kw: {
            "ETag": f"etag-{kw['PartNumber']}"
        }
        return client

    def test_a_small_stream_becomes_one_part(self):
        client = self._client()

        total = s3_client.multipart_upload_stream(
            client, "backups", "dump.sql", iter([b"abc", b"de"])
        )

        assert total == 5
        assert client.upload_part.call_count == 1
        assert client.upload_part.call_args.kwargs["Body"] == b"abcde"
        assert client.upload_part.call_args.kwargs["PartNumber"] == 1

    def test_parts_are_flushed_at_the_part_size_and_numbered_from_one(self):
        client = self._client()
        frames = [b"x" * s3_client._PART_SIZE, b"y" * 10]

        total = s3_client.multipart_upload_stream(
            client, "backups", "dump.sql", iter(frames)
        )

        assert total == s3_client._PART_SIZE + 10
        numbers = [c.kwargs["PartNumber"] for c in client.upload_part.call_args_list]
        assert numbers == [1, 2]
        completed = client.complete_multipart_upload.call_args.kwargs
        assert completed["MultipartUpload"]["Parts"] == [
            {"ETag": "etag-1", "PartNumber": 1},
            {"ETag": "etag-2", "PartNumber": 2},
        ]
        assert completed["UploadId"] == "UP1"

    def test_empty_frames_are_skipped(self):
        client = self._client()

        total = s3_client.multipart_upload_stream(
            client, "backups", "dump.sql", iter([b"", b"ab", b"", b"c"])
        )

        assert total == 3
        assert client.upload_part.call_args.kwargs["Body"] == b"abc"

    def test_a_zero_byte_stream_falls_back_to_put_object(self):
        # complete_multipart_upload rejects an upload with no parts.
        client = self._client()

        total = s3_client.multipart_upload_stream(
            client, "backups", "dump.sql", iter([])
        )

        assert total == 0
        client.complete_multipart_upload.assert_not_called()
        client.abort_multipart_upload.assert_called_once()
        assert client.put_object.call_args.kwargs == {
            "Bucket": "backups",
            "Key": "dump.sql",
            "Body": b"",
        }

    def test_a_failure_mid_stream_aborts_the_upload(self):
        # Orphaned parts are billed until a lifecycle rule reaps them.
        client = self._client()

        def _frames():
            yield b"ab"
            raise RuntimeError("pg_dump died")

        with pytest.raises(RuntimeError, match="pg_dump died"):
            s3_client.multipart_upload_stream(client, "backups", "dump.sql", _frames())

        client.abort_multipart_upload.assert_called_once_with(
            Bucket="backups", Key="dump.sql", UploadId="UP1"
        )
        client.complete_multipart_upload.assert_not_called()

    def test_a_failing_upload_part_aborts_the_upload(self):
        client = self._client()
        client.upload_part.side_effect = RuntimeError("network")

        with pytest.raises(RuntimeError):
            s3_client.multipart_upload_stream(
                client, "backups", "dump.sql", iter([b"ab"])
            )

        client.abort_multipart_upload.assert_called_once()

    def test_a_failing_abort_does_not_mask_the_original_error(self):
        client = self._client()
        client.upload_part.side_effect = RuntimeError("original")
        client.abort_multipart_upload.side_effect = RuntimeError("abort failed")

        with pytest.raises(RuntimeError, match="original"):
            s3_client.multipart_upload_stream(
                client, "backups", "dump.sql", iter([b"ab"])
            )


class TestS3PutIfAbsent:
    """P-M16: the store config must be created first-writer-wins, not
    exists-then-put — two concurrent first backups minted different chunker
    keys and the loser's revision was permanently unrestorable."""

    def _storage(self, client):
        return s3_client.S3Storage(_backup(), "p", client=client)

    def test_sends_conditional_put_and_reports_creation(self):
        client = MagicMock()
        assert self._storage(client).put_if_absent("config", b"x") is True
        kwargs = client.put_object.call_args.kwargs
        assert kwargs["IfNoneMatch"] == "*"
        assert kwargs["Key"] == "p/config"

    def test_precondition_failed_means_already_exists(self):
        client = MagicMock()
        err = _client_error(412)
        client.exceptions.ClientError = err
        client.put_object.side_effect = err()
        assert self._storage(client).put_if_absent("config", b"x") is False

    def test_conditional_conflict_means_a_concurrent_creator(self):
        client = MagicMock()
        err = _client_error(409)
        client.exceptions.ClientError = err
        client.put_object.side_effect = err()
        assert self._storage(client).put_if_absent("config", b"x") is False
