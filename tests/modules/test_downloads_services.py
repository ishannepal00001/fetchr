"""modules/downloads/services tests, using a fake downloader and fake storage."""

import uuid
from pathlib import Path

import pytest

from app.modules.downloads.model import Download, DownloadStatus
from app.modules.downloads.services import (
    DownloadNotCancellable,
    DownloadNotFound,
    DownloadNotReady,
    DownloadService,
    FetchedMedia,
)


class FakeDownloader:
    def __init__(self, fail: bool = False, during=None) -> None:
        self.fail = fail
        self.during = during  # called mid-download, e.g. to cancel it
        self.stopped = False

    def download(self, url: str, dest_dir: Path, format: str | None = None, should_stop=None) -> FetchedMedia:
        dest_dir.mkdir(parents=True, exist_ok=True)
        path = dest_dir / "video.mp4"
        path.write_bytes(b"fake video bytes")  # partial file, as yt-dlp might leave behind
        if self.during:
            self.during()
        if should_stop and should_stop():
            self.stopped = True
            raise RuntimeError("stopped on request")
        if self.fail:
            raise RuntimeError("Unsupported URL")
        return FetchedMedia(path=path, title="A video", content_type="video/mp4")


class FakeStorage:
    bucket = "test-bucket"

    def __init__(self, verify_ok: bool = True, upload_errors: int = 0, during_upload=None, during_verify=None) -> None:
        self.verify_ok = verify_ok
        self.upload_errors = upload_errors  # how many uploads raise before succeeding
        self.during_upload = during_upload
        self.during_verify = during_verify
        self.objects: dict[str, tuple[int, str]] = {}
        self.upload_calls = 0

    def upload_file(self, local_path: Path, key: str, sha256: str, content_type: str | None, should_stop=None) -> None:
        self.upload_calls += 1
        if self.during_upload:
            self.during_upload()
        if should_stop and should_stop():
            raise RuntimeError("stopped on request")
        if self.upload_errors:
            self.upload_errors -= 1
            raise ConnectionError("B2 unreachable")
        self.objects[key] = (local_path.stat().st_size, sha256)

    def verify_upload(self, key: str, expected_size: int, expected_sha256: str) -> bool:
        if self.during_verify:
            self.during_verify()
        return self.verify_ok and self.objects.get(key) == (expected_size, expected_sha256)

    def presigned_url(self, key: str) -> str:
        return f"https://b2.example/{key}?sig=abc"

    def delete_object(self, key: str) -> None:
        self.objects.pop(key, None)


def make_service(session_factory, tmp_path, downloader=None, storage=None, retries=3):
    return DownloadService(
        session_factory=session_factory,
        downloader=downloader or FakeDownloader(),
        storage=storage or FakeStorage(),
        tmp_dir=tmp_path,
        upload_retries=retries,
        stop_check_interval=0,  # read the DB on every check, so tests see a cancel at once
    )


def test_happy_path_completes_uploads_and_cleans_tmp(session_factory, tmp_path):
    storage = FakeStorage()
    service = make_service(session_factory, tmp_path, storage=storage)
    created = service.create_download("alice", "https://facebook.com/watch?v=1")
    assert created.status == DownloadStatus.QUEUED

    service.process_download(created.id)

    download = service.get_download("alice", created.id)
    assert download.status == DownloadStatus.COMPLETED
    assert download.title == "A video"
    assert download.filename == "video.mp4"
    assert download.filesize == len(b"fake video bytes")
    assert download.b2_bucket == "test-bucket"
    assert download.b2_key == f"downloads/alice/{created.id}/video.mp4"
    assert download.b2_key in storage.objects
    assert download.error is None
    assert not (tmp_path / str(created.id)).exists()


def test_verification_mismatch_retries_then_fails_and_keeps_tmp(session_factory, tmp_path):
    storage = FakeStorage(verify_ok=False)
    service = make_service(session_factory, tmp_path, storage=storage, retries=3)
    created = service.create_download("alice", "https://facebook.com/watch?v=1")

    service.process_download(created.id)

    download = service.get_download("alice", created.id)
    assert download.status == DownloadStatus.FAILED
    assert "could not be verified after 3 attempt(s)" in download.error
    assert storage.upload_calls == 3
    assert storage.objects == {}  # partial upload removed
    assert (tmp_path / str(created.id) / "video.mp4").exists()  # video not lost
    assert download.b2_key is None


def test_upload_error_is_retried(session_factory, tmp_path):
    storage = FakeStorage(upload_errors=2)
    service = make_service(session_factory, tmp_path, storage=storage, retries=3)
    created = service.create_download("alice", "https://facebook.com/watch?v=1")

    service.process_download(created.id)

    assert service.get_download("alice", created.id).status == DownloadStatus.COMPLETED
    assert storage.upload_calls == 3


def test_downloader_error_fails_and_removes_tmp(session_factory, tmp_path):
    service = make_service(session_factory, tmp_path, downloader=FakeDownloader(fail=True))
    created = service.create_download("alice", "https://example.com/nope")

    service.process_download(created.id)

    download = service.get_download("alice", created.id)
    assert download.status == DownloadStatus.FAILED
    assert download.error == "download failed: Unsupported URL"
    assert not (tmp_path / str(created.id)).exists()


def test_process_only_runs_on_queued_downloads(session_factory, tmp_path):
    storage = FakeStorage()
    service = make_service(session_factory, tmp_path, storage=storage)
    created = service.create_download("alice", "https://facebook.com/watch?v=1")

    service.process_download(created.id)
    service.process_download(created.id)
    service.process_download(uuid.uuid4())  # unknown id is ignored

    assert storage.upload_calls == 1


def test_get_and_list_are_scoped_to_client(session_factory, tmp_path):
    service = make_service(session_factory, tmp_path)
    first = service.create_download("alice", "https://facebook.com/watch?v=1")
    second = service.create_download("alice", "https://facebook.com/watch?v=2")
    bobs = service.create_download("bob", "https://facebook.com/watch?v=3")

    with pytest.raises(DownloadNotFound):
        service.get_download("alice", bobs.id)

    listed = service.list_downloads("alice")
    assert [d.id for d in listed] == [second.id, first.id]  # newest first
    assert [d.id for d in service.list_downloads("alice", offset=1, limit=1)] == [first.id]


def test_file_link_only_for_completed_downloads(session_factory, tmp_path):
    service = make_service(session_factory, tmp_path)
    created = service.create_download("alice", "https://facebook.com/watch?v=1")

    with pytest.raises(DownloadNotReady):
        service.file_link("alice", created.id)

    service.process_download(created.id)

    url = service.file_link("alice", created.id)
    assert url == f"https://b2.example/downloads/alice/{created.id}/video.mp4?sig=abc"
    with pytest.raises(DownloadNotFound):
        service.file_link("bob", created.id)


def test_fail_interrupted_marks_unfinished_downloads_failed(session_factory, tmp_path):
    service = make_service(session_factory, tmp_path)
    done = service.create_download("alice", "https://facebook.com/watch?v=1")
    service.process_download(done.id)
    queued = service.create_download("alice", "https://facebook.com/watch?v=2")
    stuck = service.create_download("alice", "https://facebook.com/watch?v=3")
    with session_factory() as session:  # simulate a process killed mid-upload
        row = session.get(Download, stuck.id)
        row.status = DownloadStatus.UPLOADING
        session.add(row)
        session.commit()

    assert service.fail_interrupted() == 2

    assert service.get_download("alice", done.id).status == DownloadStatus.COMPLETED
    for download_id, was in [(queued.id, "queued"), (stuck.id, "uploading")]:
        download = service.get_download("alice", download_id)
        assert download.status == DownloadStatus.FAILED
        assert download.error == f"interrupted by a server restart while {was}; submit it again"
    assert service.fail_interrupted() == 0  # nothing left to recover


def cancel_hook(service, holder):
    """A `during` callback that cancels the download whose id is put in holder[0]."""
    return lambda: service.cancel_download("alice", holder[0])


def test_cancel_queued_download_never_runs(session_factory, tmp_path):
    storage = FakeStorage()
    service = make_service(session_factory, tmp_path, storage=storage)
    created = service.create_download("alice", "https://facebook.com/watch?v=1")

    cancelled = service.cancel_download("alice", created.id)
    service.process_download(created.id)

    assert cancelled.status == DownloadStatus.CANCELLED
    assert service.get_download("alice", created.id).status == DownloadStatus.CANCELLED
    assert storage.upload_calls == 0
    assert not (tmp_path / str(created.id)).exists()


def test_cancel_during_download_stops_it_and_cleans_tmp(session_factory, tmp_path):
    holder = []
    downloader = FakeDownloader()
    storage = FakeStorage()
    service = make_service(session_factory, tmp_path, downloader=downloader, storage=storage)
    downloader.during = cancel_hook(service, holder)
    holder.append(service.create_download("alice", "https://facebook.com/watch?v=1").id)

    service.process_download(holder[0])

    download = service.get_download("alice", holder[0])
    assert downloader.stopped
    assert download.status == DownloadStatus.CANCELLED
    assert download.error is None
    assert storage.upload_calls == 0
    assert not (tmp_path / str(holder[0])).exists()


@pytest.mark.parametrize("stage", ["during_upload", "during_verify"])
def test_cancel_during_upload_or_verify_removes_object_and_tmp(session_factory, tmp_path, stage):
    holder = []
    storage = FakeStorage()
    service = make_service(session_factory, tmp_path, storage=storage)
    setattr(storage, stage, cancel_hook(service, holder))
    holder.append(service.create_download("alice", "https://facebook.com/watch?v=1").id)

    service.process_download(holder[0])

    download = service.get_download("alice", holder[0])
    assert download.status == DownloadStatus.CANCELLED
    assert download.b2_key is None
    assert storage.upload_calls == 1  # a stop isn't retried
    assert storage.objects == {}
    assert not (tmp_path / str(holder[0])).exists()


def test_cancel_is_idempotent_and_finished_downloads_cant_be_cancelled(session_factory, tmp_path):
    service = make_service(session_factory, tmp_path)
    done = service.create_download("alice", "https://facebook.com/watch?v=1")
    service.process_download(done.id)
    failed = service.create_download("alice", "https://example.com/nope")
    service._downloader.fail = True
    service.process_download(failed.id)
    queued = service.create_download("alice", "https://facebook.com/watch?v=2")

    service.cancel_download("alice", queued.id)
    assert service.cancel_download("alice", queued.id).status == DownloadStatus.CANCELLED
    for download in (done, failed):
        with pytest.raises(DownloadNotCancellable):
            service.cancel_download("alice", download.id)
    with pytest.raises(DownloadNotFound):
        service.cancel_download("bob", queued.id)
    assert service.get_download("alice", done.id).status == DownloadStatus.COMPLETED


def test_should_stop_reads_db_at_most_once_per_interval(session_factory, tmp_path):
    service = DownloadService(session_factory, FakeDownloader(), FakeStorage(), tmp_path, stop_check_interval=60)
    created = service.create_download("alice", "https://facebook.com/watch?v=1")
    should_stop = service._stop_check(created.id)

    assert should_stop() is False  # first call reads the DB
    service.cancel_download("alice", created.id)
    assert should_stop() is False  # inside the interval: cached answer

    assert service._stop_check(created.id)() is True  # a fresh check sees the cancel
