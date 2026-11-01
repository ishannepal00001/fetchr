"""
Download(SQLModel, table=True): one row per job a client asked for. Usually a URL to
fetch; a render (clips cut from a video already in B2) is the same row with render_spec
set, so both share the status lifecycle, cancel/restart handling and upload pipeline.
"""

import uuid
from datetime import UTC, datetime
from enum import StrEnum

from sqlmodel import Field, SQLModel


def utcnow() -> datetime:
    return datetime.now(UTC)


class DownloadStatus(StrEnum):
    QUEUED = "queued"
    DOWNLOADING = "downloading"
    UPLOADING = "uploading"
    VERIFYING = "verifying"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


# Work is still happening (or about to): the only states a download can be cancelled in.
ACTIVE_STATUSES = (
    DownloadStatus.QUEUED,
    DownloadStatus.DOWNLOADING,
    DownloadStatus.UPLOADING,
    DownloadStatus.VERIFYING,
)


class Download(SQLModel, table=True):
    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    client_id: str = Field(index=True)  # who requested it
    source_url: str
    format: str | None = None  # yt-dlp format selector; None = yt-dlp default
    render_spec: str | None = None  # JSON RenderSpec for a render job (source_url is then its B2 key); None = a download
    status: DownloadStatus = Field(default=DownloadStatus.QUEUED)

    # Filled in once yt-dlp has fetched the file.
    title: str | None = None
    filename: str | None = None
    content_type: str | None = None
    filesize: int | None = None  # bytes
    sha256: str | None = None  # hash of the local file, checked against B2 after upload

    # Set only once the upload has been verified.
    b2_bucket: str | None = None
    b2_key: str | None = None  # downloads/<client_id>/<id>/<filename> (renders/... for a render)
    thumbnail: str | None = None  # bare name (thumbnail.jpg), next to the video in B2; None if none could be made

    error: str | None = None  # set when status == failed
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)
