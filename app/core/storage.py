"""
Backblaze B2 client, using B2's S3-compatible API through boto3.

Planned contents:
- client: boto3.client("s3", endpoint_url=B2_ENDPOINT_URL, credentials from settings)
- upload_file(local_path, key, sha256, content_type) -> None
    Upload to B2_BUCKET under `key`, storing the sha256 in object metadata.
- verify_upload(key, expected_size, expected_sha256) -> bool
    head_object(key); True only if ContentLength == expected_size and the
    metadata sha256 == expected_sha256. The caller deletes the local temp file
    only after this returns True.
- presigned_url(key, expires=B2_PRESIGN_EXPIRY) -> str
    A temporary download link, so the client downloads straight from B2.
- delete_object(key) -> None
    Removes a partial/failed upload.
"""
