"""
yt-dlp downloader. Implements the Downloader protocol from modules/downloads/services.py.

Config (cookies file, default format) is passed in by the app setup; this module
doesn't read settings. The default format merges separate video + audio streams,
which needs ffmpeg on the PATH.
"""

import mimetypes
from pathlib import Path

from yt_dlp import YoutubeDL
from yt_dlp.utils import DownloadError

from app.modules.downloads.services import FetchedMedia

DEFAULT_FORMAT = "bv*+ba/b"  # best video + best audio, else best single file


class DownloaderError(Exception):
    """yt-dlp couldn't produce a single media file for the URL."""


class YtDlpDownloader:
    def __init__(self, cookies_file: Path | None = None, default_format: str = DEFAULT_FORMAT) -> None:
        self._cookies_file = cookies_file
        self._default_format = default_format

    def download(self, url: str, dest_dir: Path, format: str | None = None) -> FetchedMedia:
        dest_dir.mkdir(parents=True, exist_ok=True)

        with YoutubeDL(self._options(dest_dir, format)) as ydl:
            try:
                info = ydl.extract_info(url, download=True)
            except DownloadError as exc:
                raise DownloaderError(_clean(exc)) from exc
            if info is None:
                raise DownloaderError("yt-dlp returned no information for this URL")
            if info.get("_type") == "playlist" or "entries" in info:
                raise DownloaderError("URL is a playlist, not a single video")

            requested = info.get("requested_downloads") or [{}]
            path = Path(requested[0].get("filepath") or ydl.prepare_filename(info))

        if not path.is_file():
            raise DownloaderError("yt-dlp reported success but no file was written")

        return FetchedMedia(
            path=path,
            title=info.get("title"),
            content_type=mimetypes.guess_type(path.name)[0],
        )

    def _options(self, dest_dir: Path, format: str | None) -> dict:
        opts = {
            "outtmpl": str(dest_dir / "%(id)s.%(ext)s"),  # id-based: safe, collision-free names
            "format": format or self._default_format,
            "merge_output_format": "mp4",
            "noplaylist": True,
            "quiet": True,
            "no_warnings": True,
            "noprogress": True,
            "retries": 3,
            "fragment_retries": 3,
        }
        if self._cookies_file:
            opts["cookiefile"] = str(self._cookies_file)
        return opts


def _clean(exc: DownloadError) -> str:
    return str(exc.msg or exc).removeprefix("ERROR: ").strip()
