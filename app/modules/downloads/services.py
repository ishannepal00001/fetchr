"""
Download services. Framework agnostic: no HTTP, no FastAPI, no task runner.

Everything the service needs is injected through the constructor:
- session_factory: opens a DB session (each call uses its own, so process_download
  can run outside a request)
- downloader / storage / thumbnailer: anything matching the protocols below
  (yt-dlp, Backblaze B2 and ffmpeg in production, fakes in tests). The thumbnailer is
  optional; without one, downloads simply have no thumbnail.
- tmp_dir, upload_retries: plain config values

process_download() is synchronous and blocking. The caller decides how to run it
(FastAPI BackgroundTasks, a thread, a worker queue, a CLI).
Errors the caller should handle are raised as the domain exceptions below.

Cancelling: cancel_download() only flips the row to cancelled. The worker notices it
through `should_stop` (polled by the downloader and storage while they work) and
through its own status writes, which only apply while the row is still active, so a
cancel can't be overwritten. The worker then cleans up tmp/<id>/ and any B2 objects.

Thumbnails are best effort: any error making or uploading one is logged and the
download completes with thumbnail = None.
"""

import hashlib
import logging
import shutil
import tempfile
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from sqlmodel import Session, col, select, update

from app.modules.downloads.model import ACTIVE_STATUSES, Download, DownloadStatus, utcnow

logger = logging.getLogger(__name__)

StopCheck = Callable[[], bool]


@dataclass(frozen=True)
class FetchedMedia:
    path: Path  # the downloaded file, somewhere inside dest_dir
    title: str | None = None
    content_type: str | None = None
    poster: Path | None = None  # the video's own poster image, downloaded next to it


class Downloader(Protocol):
    def download(
        self, url: str, dest_dir: Path, format: str | None = None, should_stop: StopCheck | None = None
    ) -> FetchedMedia:
        """Fetch `url` into `dest_dir`. Raise on any failure, and abort (raise) once should_stop() is true."""
        ...


class Thumbnailer(Protocol):
    def make(self, video: Path, poster: Path | None, out_dir: Path) -> Path:
        """Write a JPEG thumbnail into out_dir (from the poster, else a video frame) and return its path. Raise on failure."""
        ...


class Storage(Protocol):
    bucket: str

    def upload_file(
        self,
        local_path: Path,
        key: str,
        sha256: str,
        content_type: str | None,
        should_stop: StopCheck | None = None,
    ) -> None:
        """Raise on failure, and abort (raise) once should_stop() is true."""
        ...

    def verify_upload(self, key: str, expected_size: int, expected_sha256: str) -> bool: ...

    def presigned_url(self, key: str) -> str: ...

    def delete_object(self, key: str) -> None: ...


class DownloadNotFound(Exception):
    """No download with that id for this client."""


class DownloadNotReady(Exception):
    """The download hasn't completed, so there is no file to link to."""


class DownloadNotCancellable(Exception):
    """The download already finished (completed or failed), so there is nothing to cancel."""


class DownloadService:
    def __init__(
        self,
        session_factory: Callable[[], Session],
        downloader: Downloader,
        storage: Storage,
        tmp_dir: Path,
        upload_retries: int = 3,
        stop_check_interval: float = 1.0,
        thumbnailer: Thumbnailer | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._downloader = downloader
        self._storage = storage
        self._tmp_dir = tmp_dir
        self._upload_retries = max(1, upload_retries)
        self._stop_check_interval = stop_check_interval  # seconds between DB reads in should_stop
        self._thumbnailer = thumbnailer

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

    def cancel_download(self, client_id: str, download_id: uuid.UUID) -> Download:
        """
        Mark an active download cancelled and return it. Cancelling an already cancelled
        download is a no-op; a completed or failed one raises DownloadNotCancellable.
        A running worker stops at its next check and cleans up after itself.
        """
        self.get_download(client_id, download_id)  # raises DownloadNotFound for other clients
        if not self._set(download_id, status=DownloadStatus.CANCELLED):
            download = self.get_download(client_id, download_id)
            if download.status != DownloadStatus.CANCELLED:
                raise DownloadNotCancellable(download_id)
        return self.get_download(client_id, download_id)

    def fail_interrupted(self) -> int:
        """
        Call once at startup, before any work is accepted. Background work dies with the
        process, so anything not completed/failed was interrupted (redeploy, crash, Render
        spin-down) and would otherwise sit in its status forever. Returns how many rows
        were marked failed. Their tmp/<id>/ files are left alone, like a failed upload.
        """
        unfinished = [
            DownloadStatus.QUEUED,
            DownloadStatus.DOWNLOADING,
            DownloadStatus.UPLOADING,
            DownloadStatus.VERIFYING,
        ]
        with self._session_factory() as session:
            rows = session.exec(select(Download).where(col(Download.status).in_(unfinished))).all()
            for download in rows:
                _update(
                    session,
                    download,
                    status=DownloadStatus.FAILED,
                    error=f"interrupted by a server restart while {download.status.value}; submit it again",
                )
        return len(rows)

    def process_download(self, download_id: uuid.UUID) -> None:
        """
        queued -> downloading -> uploading -> verifying -> completed | failed | cancelled.

        The thumbnail is made and uploaded after the fetch, before the video upload, and
        only recorded on the row together with `completed`. A failed or cancelled job
        deletes it from B2 along with the video.

        Only a queued download is processed, so calling this twice is harmless.
        tmp/<id>/ is deleted after a verified upload, a failed fetch, or a cancel; it is
        KEPT when the upload can't be verified, so the video isn't lost.
        """
        with self._session_factory() as session:
            download = session.get(Download, download_id)
        if download is None:
            return
        work_dir = self._tmp_dir / str(download.id)
        should_stop = self._stop_check(download_id)

        if not self._set(download_id, only_if=(DownloadStatus.QUEUED,), status=DownloadStatus.DOWNLOADING):
            return  # not queued: already processed, or cancelled before it started
        try:
            media = self._downloader.download(download.source_url, work_dir, download.format, should_stop)
        except Exception as exc:
            shutil.rmtree(work_dir, ignore_errors=True)
            self._set(download_id, status=DownloadStatus.FAILED, error=f"download failed: {exc}")
            return  # if it was cancelled, _set did nothing and the row stays cancelled

        sha256, size = _hash_file(media.path)
        key = f"downloads/{download.client_id}/{download.id}/{media.path.name}"
        if not self._set(
            download_id,
            title=media.title,
            filename=media.path.name,
            content_type=media.content_type,
            filesize=size,
            sha256=sha256,
        ):
            shutil.rmtree(work_dir, ignore_errors=True)
            return

        thumbnail_key = self._upload_thumbnail(download, media, work_dir, should_stop)
        last_error = "upload could not be verified"
        for _ in range(self._upload_retries):
            if not self._set(download_id, status=DownloadStatus.UPLOADING):
                break
            try:
                self._storage.upload_file(media.path, key, sha256, media.content_type, should_stop)
            except Exception as exc:
                last_error = f"upload failed: {exc}"
                continue

            if not self._set(download_id, status=DownloadStatus.VERIFYING):
                break
            if self._storage.verify_upload(key, size, sha256):
                if self._set(
                    download_id,
                    status=DownloadStatus.COMPLETED,
                    b2_bucket=self._storage.bucket,
                    b2_key=key,
                    thumbnail=thumbnail_key.rsplit("/", 1)[-1] if thumbnail_key else None,
                ):
                    shutil.rmtree(work_dir, ignore_errors=True)
                    return
                break  # cancelled at the last moment: undo the upload below
            last_error = "upload could not be verified"

        self._delete_object(key)
        if thumbnail_key:
            self._delete_object(thumbnail_key)
        if self._set(
            download_id,
            status=DownloadStatus.FAILED,
            error=f"{last_error} after {self._upload_retries} attempt(s); file kept at {media.path}",
        ):
            return
        shutil.rmtree(work_dir, ignore_errors=True)  # cancelled: nothing worth keeping

    def _upload_thumbnail(
        self, download: Download, media: FetchedMedia, work_dir: Path, should_stop: StopCheck
    ) -> str | None:
        """
        Make the thumbnail in a temp dir and upload it next to the video. Returns its B2
        key, or None (after logging why) if anything went wrong. Never raises.
        """
        if self._thumbnailer is None:
            return None
        key = None
        try:
            with tempfile.TemporaryDirectory(dir=work_dir, prefix="thumbnail-") as out_dir:
                path = self._thumbnailer.make(media.path, media.poster, Path(out_dir))
                sha256, size = _hash_file(path)
                key = f"downloads/{download.client_id}/{download.id}/{path.name}"
                self._storage.upload_file(path, key, sha256, "image/jpeg", should_stop)
                if not self._storage.verify_upload(key, size, sha256):
                    raise RuntimeError("upload could not be verified")
            return key
        except Exception as exc:
            logger.warning("download %s: no thumbnail: %s", download.id, exc)
            if key:
                self._delete_object(key)
            return None

    def _set(
        self,
        download_id: uuid.UUID,
        only_if: tuple[DownloadStatus, ...] = ACTIVE_STATUSES,
        **fields: object,
    ) -> bool:
        """
        Set fields and commit right away (so pollers see each step), but only while the
        row's status is in `only_if`. A single conditional UPDATE, so it can't race with
        a cancel. Returns False when nothing changed (cancelled, finished, or gone).
        """
        statement = (
            update(Download)
            .where(col(Download.id) == download_id, col(Download.status).in_(only_if))
            .values(**fields, updated_at=utcnow())
        )
        with self._session_factory() as session:
            result = session.exec(statement)
            session.commit()
        return result.rowcount == 1

    def _stop_check(self, download_id: uuid.UUID) -> StopCheck:
        """
        should_stop for the downloader and storage: true once the row is cancelled.
        They call it on every progress tick, so the DB is read at most once per interval.
        """
        last_check = float("-inf")
        cancelled = False

        def should_stop() -> bool:
            nonlocal last_check, cancelled
            now = time.monotonic()
            if not cancelled and now - last_check >= self._stop_check_interval:
                last_check = now
                with self._session_factory() as session:
                    download = session.get(Download, download_id)
                cancelled = download is None or download.status == DownloadStatus.CANCELLED
            return cancelled

        return should_stop

    def _delete_object(self, key: str) -> None:
        try:
            self._storage.delete_object(key)
        except Exception:
            pass  # the row is failed/cancelled either way; a stray object is harmless


def _update(session: Session, download: Download, **fields: object) -> None:
    """Set fields on a loaded row and commit right away (unconditionally, unlike _set)."""
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
