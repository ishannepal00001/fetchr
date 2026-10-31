"""
Request/response schemas for v1 downloads. These validate what goes in and out
over HTTP; the DB table lives in app/modules/downloads/model.py.
"""

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, HttpUrl

from app.modules.downloads.model import DownloadStatus


class CodeEnvelope(BaseModel):
    """Every JSON success body carries the client's next code (also in X-Fetchr-Next-Code)."""

    next_code: str


class DownloadCreate(BaseModel):
    url: HttpUrl = Field(max_length=2048)
    format: str | None = Field(default=None, max_length=200, description="yt-dlp format selector")


class DownloadRead(BaseModel):
    """Public view of a download. Internal fields (client_id, b2_key, b2_bucket, sha256) are left out."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    source_url: str
    status: DownloadStatus
    title: str | None
    filename: str | None
    content_type: str | None
    filesize: int | None
    thumbnail: str | None  # bare file name at downloads/<client_id>/<id>/<thumbnail> in B2; set on completion
    error: str | None
    created_at: datetime
    updated_at: datetime


class DownloadResponse(DownloadRead, CodeEnvelope):
    pass


class DownloadList(CodeEnvelope):
    items: list[DownloadRead]
    offset: int
    limit: int


class FileLink(CodeEnvelope):
    url: str
    expires_in: int  # seconds
