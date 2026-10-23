"""
Download(SQLModel, table=True): one row per URL a client asked us to fetch.

Planned fields:
- id: UUID, primary key
- client_id: str, indexed        who requested it (from request.state)
- source_url: str
- status: DownloadStatus enum    queued -> downloading -> uploading -> verifying -> completed | failed
- title: str | None              from yt-dlp metadata
- filename: str | None
- content_type: str | None
- filesize: int | None           bytes
- sha256: str | None             hash of the local file, checked against B2 after upload
- b2_bucket: str | None
- b2_key: str | None             downloads/<client_id>/<id>/<filename>
- error: str | None              set when status == failed
- created_at / updated_at: datetime
"""
