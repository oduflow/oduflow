"""Thin boto3 wrapper for the backup subsystem.

One S3 client shape for everything backup-related: the chunkstore's S3
backend (thousands of small HEAD/PUT/CopyObject calls — the reason this is
boto3 and not an ``aws`` CLI shell-out), streaming multipart uploads for
pg_dump snapshots, and the health check's HeadBucket.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from typing import Any

from oduflow.settings import BackupSettings

logger = logging.getLogger("oduflow")

# S3 multipart minimum part size is 5 MiB; use 16 MiB parts.
_PART_SIZE = 16 * 1024 * 1024


def make_client(backup: BackupSettings) -> Any:
    import boto3
    from botocore.config import Config

    kwargs: dict[str, Any] = {
        "aws_access_key_id": backup.access_key,
        "aws_secret_access_key": backup.secret_key,
        "config": Config(
            retries={"max_attempts": 3, "mode": "standard"},
            # Parallel chunk uploads must not starve on the connection
            # pool (botocore's default is 10 < upload_threads).
            max_pool_connections=max(10, backup.upload_threads + 4),
            s3={"addressing_style": "path"} if backup.endpoint else {},
        ),
    }
    if backup.region:
        kwargs["region_name"] = backup.region
    if backup.endpoint:
        kwargs["endpoint_url"] = backup.endpoint
    return boto3.client("s3", **kwargs)


def check_s3(backup: BackupSettings) -> dict[str, Any]:
    """HeadBucket health probe. Returns {"ok": bool, "error": str}."""
    try:
        make_client(backup).head_bucket(Bucket=backup.bucket)
        return {"ok": True, "error": ""}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


class S3Storage:
    """chunkstore Storage protocol over an S3 bucket prefix."""

    def __init__(self, backup: BackupSettings, prefix: str, client: Any = None):
        self.bucket = backup.bucket
        self.prefix = prefix.strip("/")
        self.client = client or make_client(backup)

    def _key(self, key: str) -> str:
        return f"{self.prefix}/{key}" if self.prefix else key

    def exists(self, key: str) -> bool:
        try:
            self.client.head_object(Bucket=self.bucket, Key=self._key(key))
            return True
        except self.client.exceptions.ClientError as exc:
            code = exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
            if code in (403, 404):
                return False
            raise

    def get(self, key: str) -> bytes:
        resp = self.client.get_object(Bucket=self.bucket, Key=self._key(key))
        data: bytes = resp["Body"].read()
        return data

    def put(self, key: str, data: bytes) -> None:
        self.client.put_object(Bucket=self.bucket, Key=self._key(key), Body=data)

    def list(self, prefix: str) -> list[str]:
        full_prefix = self._key(prefix)
        keys: list[str] = []
        paginator = self.client.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=self.bucket, Prefix=full_prefix):
            for obj in page.get("Contents", []):
                key = obj["Key"]
                if self.prefix:
                    key = key[len(self.prefix) + 1 :]
                keys.append(key)
        return sorted(keys)

    def rename(self, src: str, dst: str) -> None:
        # S3 has no rename: CopyObject + DeleteObject (fossilization path).
        self.client.copy_object(
            Bucket=self.bucket,
            Key=self._key(dst),
            CopySource={"Bucket": self.bucket, "Key": self._key(src)},
        )
        self.client.delete_object(Bucket=self.bucket, Key=self._key(src))

    def delete(self, key: str) -> None:
        self.client.delete_object(Bucket=self.bucket, Key=self._key(key))


def multipart_upload_stream(
    client: Any,
    bucket: str,
    key: str,
    frames: Iterator[bytes],
) -> int:
    """Stream an unbounded byte iterator into S3 as a multipart upload.

    Buffers frames into ~16 MiB parts (no temp disk). Returns total bytes.
    Aborts the multipart upload on any failure so no orphaned parts accrue
    storage costs.
    """
    upload = client.create_multipart_upload(Bucket=bucket, Key=key)
    upload_id = upload["UploadId"]
    parts: list[dict[str, Any]] = []
    total = 0
    buffer = bytearray()
    try:
        part_number = 1

        def _flush_part() -> None:
            nonlocal part_number
            if not buffer:
                return
            resp = client.upload_part(
                Bucket=bucket,
                Key=key,
                PartNumber=part_number,
                UploadId=upload_id,
                Body=bytes(buffer),
            )
            parts.append({"ETag": resp["ETag"], "PartNumber": part_number})
            part_number += 1
            buffer.clear()

        for frame in frames:
            if not frame:
                continue
            buffer.extend(frame)
            total += len(frame)
            if len(buffer) >= _PART_SIZE:
                _flush_part()
        _flush_part()
        if not parts:
            # Zero-byte stream: complete_multipart_upload needs >= 1 part.
            client.abort_multipart_upload(Bucket=bucket, Key=key, UploadId=upload_id)
            client.put_object(Bucket=bucket, Key=key, Body=b"")
            return 0
        client.complete_multipart_upload(
            Bucket=bucket,
            Key=key,
            UploadId=upload_id,
            MultipartUpload={"Parts": parts},
        )
        return total
    except BaseException:
        try:
            client.abort_multipart_upload(Bucket=bucket, Key=key, UploadId=upload_id)
        except Exception:
            logger.warning("Could not abort multipart upload %s", key)
        raise
