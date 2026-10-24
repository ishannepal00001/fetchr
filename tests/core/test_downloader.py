"""core/downloader tests. A fake YoutubeDL stands in for yt-dlp, so no network is used."""

import os
from pathlib import Path

import pytest
from yt_dlp.utils import DownloadError

from app.core import downloader as downloader_module
from app.core.downloader import DEFAULT_FORMAT, DownloaderError, YtDlpDownloader


class FakeYoutubeDL:
    """Records the options it was built with and returns a canned info dict."""

    last_opts: dict | None = None

    def __init__(self, opts: dict) -> None:
        FakeYoutubeDL.last_opts = opts
        self.outdir = Path(opts["outtmpl"]).parent

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def prepare_filename(self, info: dict) -> str:
        return str(self.outdir / f"{info['id']}.{info['ext']}")

    def extract_info(self, url: str, download: bool = True) -> dict:
        path = self.outdir / "123.mp4"
        path.write_bytes(b"merged video")
        return {
            "id": "123",
            "ext": "mp4",
            "title": "My video",
            "requested_downloads": [{"filepath": str(path)}],
        }


@pytest.fixture
def fake_ydl(monkeypatch):
    monkeypatch.setattr(downloader_module, "YoutubeDL", FakeYoutubeDL)
    return FakeYoutubeDL


def test_download_returns_merged_file_and_metadata(fake_ydl, tmp_path):
    dest = tmp_path / "abc"

    media = YtDlpDownloader().download("https://facebook.com/watch?v=123", dest)

    assert media.path == dest / "123.mp4"
    assert media.path.read_bytes() == b"merged video"
    assert media.title == "My video"
    assert media.content_type == "video/mp4"


def test_options_default_format_and_no_cookies(fake_ydl, tmp_path):
    YtDlpDownloader().download("https://facebook.com/watch?v=123", tmp_path)

    opts = fake_ydl.last_opts
    assert opts["format"] == DEFAULT_FORMAT
    assert opts["outtmpl"] == str(tmp_path / "%(id)s.%(ext)s")
    assert opts["merge_output_format"] == "mp4"
    assert opts["noplaylist"] is True
    assert "cookiefile" not in opts


def test_options_custom_format_and_cookies(fake_ydl, tmp_path):
    cookies = tmp_path / "cookies.txt"

    YtDlpDownloader(cookies_file=cookies).download("https://facebook.com/watch?v=123", tmp_path, format="b")

    assert fake_ydl.last_opts["format"] == "b"
    assert fake_ydl.last_opts["cookiefile"] == str(cookies)


def test_falls_back_to_prepare_filename(fake_ydl, monkeypatch, tmp_path):
    def extract_info(self, url, download=True):
        (self.outdir / "123.mp4").write_bytes(b"x")
        return {"id": "123", "ext": "mp4", "title": "t"}  # no requested_downloads

    monkeypatch.setattr(FakeYoutubeDL, "extract_info", extract_info)

    media = YtDlpDownloader().download("https://facebook.com/watch?v=123", tmp_path)

    assert media.path == tmp_path / "123.mp4"


def test_yt_dlp_error_becomes_clean_downloader_error(fake_ydl, monkeypatch, tmp_path):
    def extract_info(self, url, download=True):
        raise DownloadError("ERROR: [facebook] 123: Cannot parse data")

    monkeypatch.setattr(FakeYoutubeDL, "extract_info", extract_info)

    with pytest.raises(DownloaderError, match=r"^\[facebook\] 123: Cannot parse data$"):
        YtDlpDownloader().download("https://facebook.com/watch?v=123", tmp_path)


def test_playlist_is_rejected(fake_ydl, monkeypatch, tmp_path):
    monkeypatch.setattr(
        FakeYoutubeDL, "extract_info", lambda self, url, download=True: {"_type": "playlist", "entries": []}
    )

    with pytest.raises(DownloaderError, match="playlist"):
        YtDlpDownloader().download("https://facebook.com/some/page", tmp_path)


def test_missing_file_is_an_error(fake_ydl, monkeypatch, tmp_path):
    monkeypatch.setattr(
        FakeYoutubeDL,
        "extract_info",
        lambda self, url, download=True: {"id": "1", "ext": "mp4", "requested_downloads": [{"filepath": "/nope.mp4"}]},
    )

    with pytest.raises(DownloaderError, match="no file was written"):
        YtDlpDownloader().download("https://facebook.com/watch?v=1", tmp_path)


@pytest.mark.skipif(not os.environ.get("FETCHR_TEST_URL"), reason="set FETCHR_TEST_URL to run against the real network")
def test_real_download(tmp_path):
    cookies = os.environ.get("FETCHR_COOKIES_FILE")
    downloader = YtDlpDownloader(cookies_file=Path(cookies) if cookies else None)

    media = downloader.download(os.environ["FETCHR_TEST_URL"], tmp_path)

    assert media.path.is_file()
    assert media.path.stat().st_size > 0
