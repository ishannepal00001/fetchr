"""
Backblaze B2 storage through B2's S3-compatible API (boto3).
Implements the Storage protocol from modules/downloads/services.py.

Config is passed in by main.py; this module doesn't read settings.
"""

from pathlib import Path
from urllib.parse import urlparse

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError

from app.modules.downloads.services import StopCheck

DEFAULT_REGION = "us-east-1"


class UploadStopped(Exception):
    """should_stop() turned true, so the upload was aborted part way."""


def region_from_endpoint(endpoint_url: str) -> str:
    """'https://s3.us-west-004.backblazeb2.com' -> 'us-west-004' (needed to sign presigned URLs)."""
    parts = (urlparse(endpoint_url).hostname or "").split(".")
    if len(parts) >= 3 and parts[0] == "s3":
        return parts[1]
    return DEFAULT_REGION


class B2Storage:
    def __init__(
        self,
        endpoint_url: str,
        key_id: str,
        application_key: str,
        bucket: str,
        presign_expiry: int = 3600,
    ) -> None:
        self.bucket = bucket
        self._presign_expiry = presign_expiry
        self._client = boto3.client(
            "s3",
            endpoint_url=endpoint_url,
            region_name=region_from_endpoint(endpoint_url),
            aws_access_key_id=key_id,
            aws_secret_access_key=application_key,
            config=Config(
                signature_version="s3v4",
                retries={"mode": "standard", "max_attempts": 3},
                # Newer boto3 adds CRC checksum headers to every upload by default;
                # B2's S3 API hasn't always accepted them, so only send when required.
                request_checksum_calculation="when_required",
                response_checksum_validation="when_required",
            ),
        )

    def upload_file(
        self,
        local_path: Path,
        key: str,
        sha256: str,
        content_type: str | None,
        should_stop: StopCheck | None = None,
    ) -> None:
        """
        Raises on failure; process_download retries. Large files go up as multipart automatically.
        boto3 calls Callback as bytes go out; raising there aborts the transfer (and an
        unfinished multipart upload), and the error comes back out of upload_file. A
        small single-PUT file may already be stored by then, so the caller deletes the key.
        """

        def stop_callback(_bytes_sent: int) -> None:
            if should_stop():
                raise UploadStopped("stopped on request")

        self._client.upload_file(
            str(local_path),
            self.bucket,
            key,
            ExtraArgs={
                "Metadata": {"sha256": sha256},
                "ContentType": content_type or "application/octet-stream",
            },
            Callback=stop_callback if should_stop else None,
        )

    def verify_upload(self, key: str, expected_size: int, expected_sha256: str) -> bool:
        """
        True only if the object exists with the expected size and sha256 metadata.
        Never raises: process_download doesn't catch errors here, and a False just
        means another upload attempt.
        """
        try:
            head = self._client.head_object(Bucket=self.bucket, Key=key)
        except (ClientError, BotoCoreError):
            return False
        return head.get("ContentLength") == expected_size and head.get("Metadata", {}).get("sha256") == expected_sha256

    def presigned_url(self, key: str) -> str:
        """A temporary download link straight to B2. Signed locally; no request is made."""
        filename = key.rsplit("/", 1)[-1].replace('"', "")
        return self._client.generate_presigned_url(
            "get_object",
            Params={
                "Bucket": self.bucket,
                "Key": key,
                "ResponseContentDisposition": f'attachment; filename="{filename}"',
            },
            ExpiresIn=self._presign_expiry,
        )

    def delete_object(self, key: str) -> None:
        """
        Remove every version of `key`. B2 keeps all versions by default, and a plain
        S3 delete only hides the file (still stored, still billed).
        """
        paginator = self._client.get_paginator("list_object_versions")
        for page in paginator.paginate(Bucket=self.bucket, Prefix=key):
            for entry in page.get("Versions", []) + page.get("DeleteMarkers", []):
                if entry["Key"] == key:  # Prefix also matches longer keys
                    self._client.delete_object(Bucket=self.bucket, Key=key, VersionId=entry["VersionId"])
