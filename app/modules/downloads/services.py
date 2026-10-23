"""
Download services. No HTTP knowledge.

Planned contents:
- create_download(session, client_id, url, format=None) -> Download
    Insert a row with status=queued and return it.

- get_download(session, client_id, download_id) -> Download | None
    Scoped to client_id, so clients only see their own downloads.

- list_downloads(session, client_id, offset, limit) -> list[Download]

- process_download(download_id) -> None
    Runs as a background task after the POST response has been sent:
    1. status=downloading; run yt-dlp in a worker thread (asyncio.to_thread),
       output into FETCHR_TMP_DIR/<id>/, using the cookies file if configured.
       On a yt-dlp error: status=failed + error, remove tmp/<id>/ (no finished video), stop.
    2. compute the local file's sha256 + size; save title/filename/content_type.
    3. status=uploading; storage.upload_file(path, key, sha256, content_type).
    4. status=verifying; storage.verify_upload(key, size, sha256).
         - passed -> save b2_bucket, b2_key, filesize, sha256, status=completed,
                     THEN delete tmp/<id>/ (only once verification succeeded)
         - failed -> retry upload+verify up to FETCHR_UPLOAD_RETRIES; if it still fails:
                     storage.delete_object(key), status=failed + error, and KEEP
                     tmp/<id>/ so the video isn't lost.

- file_link(download) -> str
    storage.presigned_url(download.b2_key); only valid when status == completed.
"""
