"""modules/downloads/services tests for renders, with fake storage, renderer and thumbnailer."""

import uuid
from pathlib import Path

import pytest

from app.modules.downloads.model import Download, DownloadStatus
from app.modules.downloads.services import (
    Caption,
    Clip,
    DownloadNotFound,
    DownloadService,
    FetchedMedia,
    InvalidRender,
    RenderFailed,
    RenderSpec,
)

SOURCE_KEY = "downloads/alice/0b9d/123.mp4"
SPEC = RenderSpec(clips=(Clip(12.4, 31.0), Clip(1, 2)), caption=Caption("Headline", "Details\nmore", hold=6, fade=1.5))


class FakeStorage:
    bucket = "test-bucket"

    def __init__(self, verify_ok: bool = True, download_error: Exception | None = None) -> None:
        self.verify_ok = verify_ok
        self.download_error = download_error
        self.objects: dict[str, tuple[int, str]] = {}
        self.sources: dict[str, bytes] = {SOURCE_KEY: b"source video bytes"}
        self.downloaded: list[tuple[str, Path]] = []

    def download_file(self, key: str, local_path: Path, should_stop=None) -> None:
        self.downloaded.append((key, local_path))
        if self.download_error:
            raise self.download_error
        if key not in self.sources:
            raise FileNotFoundError(key)
        local_path.write_bytes(self.sources[key])

    def upload_file(self, local_path: Path, key: str, sha256: str, content_type: str | None, should_stop=None) -> None:
        self.objects[key] = (local_path.stat().st_size, sha256)

    def verify_upload(self, key: str, expected_size: int, expected_sha256: str) -> bool:
        return self.verify_ok and self.objects.get(key) == (expected_size, expected_sha256)

    def presigned_url(self, key: str) -> str:
        return f"https://b2.example/{key}?sig=abc"

    def delete_object(self, key: str) -> None:
        self.objects.pop(key, None)


class FakeRenderer:
    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.calls: list[tuple[Path, RenderSpec, Path]] = []

    def render(self, source: Path, spec: RenderSpec, dest_dir: Path) -> FetchedMedia:
        self.calls.append((source, spec, dest_dir))
        assert source.read_bytes() == b"source video bytes"  # fetched before rendering
        if self.error:
            raise self.error
        path = dest_dir / "clip.mp4"
        path.write_bytes(b"rendered mp4")
        poster = dest_dir / "first-frame.png"
        poster.write_bytes(b"png")
        return FetchedMedia(path=path, content_type="video/mp4", poster=poster)


class FakeThumbnailer:
    def __init__(self) -> None:
        self.posters: list[Path | None] = []

    def make(self, video: Path, poster: Path | None, out_dir: Path) -> Path:
        self.posters.append(poster)
        path = out_dir / "thumbnail.jpg"
        path.write_bytes(b"jpeg")
        return path


def make_service(session_factory, tmp_path, storage=None, renderer=None, thumbnailer=None, max_seconds=600):
    return DownloadService(
        session_factory=session_factory,
        downloader=None,  # renders never touch yt-dlp
        storage=storage or FakeStorage(),
        tmp_dir=tmp_path,
        stop_check_interval=0,
        thumbnailer=thumbnailer,
        renderer=renderer if renderer is not None else FakeRenderer(),
        render_max_seconds=max_seconds,
    )


# --- create_render validation ---


@pytest.mark.parametrize(
    "key",
    [
        "downloads/bob/0b9d/123.mp4",  # another client's file
        "downloads/alice",  # no file under the prefix
        "downloads/alice/",
        "downloads/alicex/0b9d/123.mp4",  # prefix of another client id
        "renders/alice/0b9d/clip.mp4",  # only downloads can be rendered
        "downloads/alice/../bob/0b9d/123.mp4",
        "downloads/alice/0b9d//123.mp4",
        "/downloads/alice/0b9d/123.mp4",
    ],
)
def test_source_key_must_be_one_of_this_clients_downloads(session_factory, tmp_path, key):
    service = make_service(session_factory, tmp_path)

    with pytest.raises(InvalidRender, match="downloads/alice/"):
        service.create_render("alice", key, SPEC)


def test_total_length_is_limited(session_factory, tmp_path):
    service = make_service(session_factory, tmp_path, max_seconds=20)

    with pytest.raises(InvalidRender, match="20s"):
        service.create_render("alice", SOURCE_KEY, RenderSpec(clips=(Clip(0, 15), Clip(10, 15.5))))
    service.create_render("alice", SOURCE_KEY, RenderSpec(clips=(Clip(0, 15), Clip(10, 15))))  # exactly 20s is fine


def test_create_render_stores_a_queued_row_with_its_spec(session_factory, tmp_path):
    service = make_service(session_factory, tmp_path)

    render = service.create_render("alice", SOURCE_KEY, SPEC)

    assert render.status == DownloadStatus.QUEUED
    assert render.source_url == SOURCE_KEY
    assert RenderSpec.from_json(service.get_render("alice", render.id).render_spec) == SPEC


def test_spec_without_caption_round_trips():
    spec = RenderSpec(clips=(Clip(0, 1),))

    assert RenderSpec.from_json(spec.to_json()) == spec


# --- process_download on a render ---


def test_render_completes_and_uploads_under_renders_prefix(session_factory, tmp_path):
    storage, renderer, thumbnailer = FakeStorage(), FakeRenderer(), FakeThumbnailer()
    service = make_service(session_factory, tmp_path, storage, renderer, thumbnailer)
    render = service.create_render("alice", SOURCE_KEY, SPEC)

    service.process_download(render.id)

    done = service.get_render("alice", render.id)
    assert done.status == DownloadStatus.COMPLETED
    assert (done.filename, done.content_type, done.filesize) == ("clip.mp4", "video/mp4", len(b"rendered mp4"))
    assert done.thumbnail == "thumbnail.jpg"
    assert done.error is None
    assert done.b2_key == f"renders/alice/{render.id}/clip.mp4"
    assert set(storage.objects) == {f"renders/alice/{render.id}/clip.mp4", f"renders/alice/{render.id}/thumbnail.jpg"}
    assert storage.downloaded == [(SOURCE_KEY, tmp_path / str(render.id) / "source.mp4")]
    assert renderer.calls[0][1] == SPEC
    assert thumbnailer.posters == [tmp_path / str(render.id) / "first-frame.png"]  # thumbnail = first output frame
    assert not (tmp_path / str(render.id)).exists()


def test_render_status_is_downloading_while_fetching_and_rendering(session_factory, tmp_path):
    seen = []

    class WatchingRenderer(FakeRenderer):
        def render(self, source, spec, dest_dir):
            seen.append(service.get_render("alice", render.id).status)
            return super().render(source, spec, dest_dir)

    service = make_service(session_factory, tmp_path, renderer=WatchingRenderer())
    render = service.create_render("alice", SOURCE_KEY, SPEC)

    service.process_download(render.id)

    assert seen == [DownloadStatus.DOWNLOADING]


def test_render_failure_stores_its_message_and_cleans_tmp(session_factory, tmp_path):
    storage = FakeStorage()
    service = make_service(session_factory, tmp_path, storage, FakeRenderer(RenderFailed("Clip ends after the video does")))
    render = service.create_render("alice", SOURCE_KEY, SPEC)

    service.process_download(render.id)

    failed = service.get_render("alice", render.id)
    assert failed.status == DownloadStatus.FAILED
    assert failed.error == "Clip ends after the video does"
    assert storage.objects == {}
    assert not (tmp_path / str(render.id)).exists()


@pytest.mark.parametrize(
    "storage, error",
    [
        (FakeStorage(), "Source video not found"),
        (FakeStorage(download_error=ConnectionError("B2 unreachable")), "Could not fetch the source video"),
    ],
)
def test_source_fetch_failure(session_factory, tmp_path, storage, error):
    storage.sources.clear()
    renderer = FakeRenderer()
    service = make_service(session_factory, tmp_path, storage, renderer)
    render = service.create_render("alice", SOURCE_KEY, SPEC)

    service.process_download(render.id)

    failed = service.get_render("alice", render.id)
    assert (failed.status, failed.error) == (DownloadStatus.FAILED, error)
    assert renderer.calls == []
    assert not (tmp_path / str(render.id)).exists()


def test_unexpected_renderer_error_is_logged_and_reported_briefly(session_factory, tmp_path, caplog):
    service = make_service(session_factory, tmp_path, renderer=FakeRenderer(ValueError("boom: a long traceback")))
    render = service.create_render("alice", SOURCE_KEY, SPEC)

    service.process_download(render.id)

    assert service.get_render("alice", render.id).error == "Render failed unexpectedly"
    assert "boom" in caplog.text


def test_unverified_upload_fails_and_still_deletes_tmp(session_factory, tmp_path):
    storage = FakeStorage(verify_ok=False)
    service = make_service(session_factory, tmp_path, storage, thumbnailer=FakeThumbnailer())
    render = service.create_render("alice", SOURCE_KEY, SPEC)

    service.process_download(render.id)

    failed = service.get_render("alice", render.id)
    assert failed.status == DownloadStatus.FAILED
    assert failed.error == "upload could not be verified after 3 attempt(s)"
    assert storage.objects == {}
    assert not (tmp_path / str(render.id)).exists()  # unlike a download: the source is still in B2


def test_without_a_renderer_renders_fail(session_factory, tmp_path):
    service = DownloadService(session_factory, None, FakeStorage(), tmp_path)
    render = service.create_render("alice", SOURCE_KEY, SPEC)

    service.process_download(render.id)

    assert service.get_render("alice", render.id).error == "Rendering is not available on this server"


# --- renders and downloads stay apart ---


def test_renders_and_downloads_dont_see_each_other(session_factory, tmp_path):
    service = make_service(session_factory, tmp_path)
    render = service.create_render("alice", SOURCE_KEY, SPEC)
    download = service.create_download("alice", "https://facebook.com/watch?v=1")

    with pytest.raises(DownloadNotFound):
        service.get_download("alice", render.id)
    with pytest.raises(DownloadNotFound):
        service.cancel_download("alice", render.id)
    with pytest.raises(DownloadNotFound):
        service.get_render("alice", download.id)
    with pytest.raises(DownloadNotFound):
        service.get_render("bob", render.id)
    with pytest.raises(DownloadNotFound):
        service.get_render("alice", uuid.uuid4())
    assert [d.id for d in service.list_downloads("alice")] == [download.id]


def test_fail_interrupted_also_fails_unfinished_renders(session_factory, tmp_path):
    service = make_service(session_factory, tmp_path)
    render = service.create_render("alice", SOURCE_KEY, SPEC)
    with session_factory() as session:
        row = session.get(Download, render.id)
        row.status = DownloadStatus.DOWNLOADING
        session.add(row)
        session.commit()

    assert service.fail_interrupted() == 1
    assert service.get_render("alice", render.id).status == DownloadStatus.FAILED
