"""
Download services. Framework agnostic: no HTTP, no FastAPI, no task runner.

Everything the service needs is injected through the constructor:
- session_factory: opens a DB session (each call uses its own, so process_download
  can run outside a request)
- downloader / storage: anything matching the Downloader / Storage protocols below
  (yt-dlp and Backblaze B2 in production, fakes in tests)
- tmp_dir, upload_retries: plain config values

process_download() is synchronous and blocking. The caller decides how to run it
(FastAPI BackgroundTasks, a thread, a worker queue, a CLI).
Errors the caller should handle are raised as the domain exceptions below.
"""

import hashlib
import shutil
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from sqlmodel import Session, col, select

from app.modules.downloads.model import Download, DownloadStatus, utcnow


@dataclass(frozen=True)
class FetchedMedia:
    path: Path  # the downloaded file, somewhere inside dest_dir
    title: str | None = None
    content_type: str | None = None


class Downloader(Protocol):
    def download(self, url: str, dest_dir: Path, format: str | None = None) -> FetchedMedia:
        """Fetch `url` into `dest_dir`. Raise on any failure."""
        ...


class Storage(Protocol):
    bucket: str

    def upload_file(self, local_path: Path, key: str, sha256: str, content_type: str | None) -> None: ...

    def verify_upload(self, key: str, expected_size: int, expected_sha256: str) -> bool: ...

    def presigned_url(self, key: str) -> str: ...

    def delete_object(self, key: str) -> None: ...


class DownloadNotFound(Exception):
    """No download with that id for this client."""


class DownloadNotReady(Exception):
    """The download hasn't completed, so there is no file to link to."""


class DownloadService:
    def __init__(
        self,
        session_factory: Callable[[], Session],
        downloader: Downloader,
        storage: Storage,
        tmp_dir: Path,
        upload_retries: int = 3,
    ) -> None:
        self._session_factory = session_factory
        self._downloader = downloader
        self._storage = storage
        self._tmp_dir = tmp_dir
        self._upload_retries = max(1, upload_retries)

    def create_download(self, client_id: str, url: str, format: str | None = None) -> Download:
        """Insert a queued row. Call process_download(download.id) afterwards."""
        download = Download(client_id=client_id, source_url=url, format=format)
        with self._session_factory() as session:
            session.add(download)
            session.commit()
            session.refresh(download)
        return download

    def get_download(self, client_id: str, download_id: uuid.UUID) -> Download:
        """Scoped to client_id: another client's download counts as not found."""
        with self._session_factory() as session:
            download = session.get(Download, download_id)
        if download is None or download.client_id != client_id:
            raise DownloadNotFound(download_id)
        return download

    def list_downloads(self, client_id: str, offset: int = 0, limit: int = 50) -> list[Download]:
        """Newest first."""
        statement = (
            select(Download)
            .where(Download.client_id == client_id)
            .order_by(col(Download.created_at).desc())
            .offset(offset)
            .limit(limit)
        )
        with self._session_factory() as session:
            return list(session.exec(statement).all())

    def file_link(self, client_id: str, download_id: uuid.UUID) -> str:
        """A presigned B2 URL. Raises DownloadNotReady unless status == completed."""
        download = self.get_download(client_id, download_id)
        if download.status != DownloadStatus.COMPLETED or download.b2_key is None:
            raise DownloadNotReady(download_id)
        return self._storage.presigned_url(download.b2_key)

    def process_download(self, download_id: uuid.UUID) -> None:
        """
        queued -> downloading -> uploading -> verifying -> completed | failed.

        Only a queued download is processed, so calling this twice is harmless.
        tmp/<id>/ is deleted after a verified upload or a failed fetch; it is KEPT
        when the upload can't be verified, so the video isn't lost.
        """
        with self._session_factory() as session:
            download = session.get(Download, download_id)
            if download is None or download.status != DownloadStatus.QUEUED:
                return
            work_dir = self._tmp_dir / str(download.id)

            _update(session, download, status=DownloadStatus.DOWNLOADING)
            try:
                media = self._downloader.download(download.source_url, work_dir, download.format)
            except Exception as exc:
                shutil.rmtree(work_dir, ignore_errors=True)
                _update(session, download, status=DownloadStatus.FAILED, error=f"download failed: {exc}")
                return

            sha256, size = _hash_file(media.path)
            key = f"downloads/{download.client_id}/{download.id}/{media.path.name}"
            _update(
                session,
                download,
                title=media.title,
                filename=media.path.name,
                content_type=media.content_type,
                filesize=size,
                sha256=sha256,
            )

            last_error = "upload could not be verified"
            for _ in range(self._upload_retries):
                _update(session, download, status=DownloadStatus.UPLOADING)
                try:
                    self._storage.upload_file(media.path, key, sha256, media.content_type)
                except Exception as exc:
                    last_error = f"upload failed: {exc}"
                    continue

                _update(session, download, status=DownloadStatus.VERIFYING)
                if self._storage.verify_upload(key, size, sha256):
                    _update(
                        session,
                        download,
                        status=DownloadStatus.COMPLETED,
                        b2_bucket=self._storage.bucket,
                        b2_key=key,
                    )
                    shutil.rmtree(work_dir, ignore_errors=True)
                    return
                last_error = "upload could not be verified"

            try:
                self._storage.delete_object(key)
            except Exception:
                pass  # the row is marked failed either way; a stray object is harmless
            _update(
                session,
                download,
                status=DownloadStatus.FAILED,
                error=f"{last_error} after {self._upload_retries} attempt(s); file kept at {media.path}",
            )


def _update(session: Session, download: Download, **fields: object) -> None:
    """Set fields and commit right away, so pollers see each step."""
    for name, value in fields.items():
        setattr(download, name, value)
    download.updated_at = utcnow()
    session.add(download)
    session.commit()


def _hash_file(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as f:
        while chunk := f.read(1024 * 1024):
            digest.update(chunk)
            size += len(chunk)
    return digest.hexdigest(), size
