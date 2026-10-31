"""
ffmpeg thumbnailer. Implements the Thumbnailer protocol from modules/downloads/services.py.

Turns the video's poster image (saved by yt-dlp next to the video) into a 640px-wide
JPEG. If there is no poster, or ffmpeg can't read it, a frame is taken from the video
itself at ~1s (0s for clips shorter than that). Needs ffmpeg on the PATH.
"""

import logging
import subprocess
from pathlib import Path

logger = logging.getLogger(__name__)

THUMBNAIL_FILENAME = "thumbnail.jpg"
WIDTH = 640
JPEG_QUALITY = 5  # ffmpeg's -q:v scale, 2 (best) to 31 (worst); 5 is roughly "quality 80"
FRAME_AT = 1.0  # seconds into the video for the fallback frame


class ThumbnailError(Exception):
    """Neither the poster nor a video frame could be turned into a thumbnail."""


class FfmpegThumbnailer:
    def __init__(self, ffmpeg: str = "ffmpeg", timeout: float = 60) -> None:
        self._ffmpeg = ffmpeg
        self._timeout = timeout

    def make(self, video: Path, poster: Path | None, out_dir: Path) -> Path:
        """Write out_dir/thumbnail.jpg and return its path. Raises ThumbnailError on failure."""
        out = out_dir / THUMBNAIL_FILENAME
        if poster is not None:
            try:
                return self._convert(poster, out)
            except ThumbnailError as exc:
                logger.warning("poster %s unusable, falling back to a video frame: %s", poster.name, exc)
        try:
            return self._convert(video, out, seek=FRAME_AT)
        except ThumbnailError:
            return self._convert(video, out, seek=0)  # shorter than FRAME_AT: nothing to grab there

    def _convert(self, source: Path, out: Path, seek: float | None = None) -> Path:
        """One frame of `source` (an image or a video at `seek` seconds) -> scaled JPEG at `out`."""
        out.unlink(missing_ok=True)
        cmd = [self._ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error", "-y"]
        if seek is not None:
            cmd += ["-ss", str(seek)]
        cmd += ["-i", str(source), "-frames:v", "1", "-vf", f"scale={WIDTH}:-2", "-q:v", str(JPEG_QUALITY)]
        cmd += ["-update", "1", str(out)]
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=self._timeout)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ThumbnailError(f"ffmpeg could not run: {exc}") from exc
        if result.returncode != 0 or not out.is_file() or out.stat().st_size == 0:
            out.unlink(missing_ok=True)
            detail = result.stderr.strip().splitlines()[-1:] or ["no output"]
            raise ThumbnailError(f"ffmpeg failed on {source.name}: {detail[0]}")
        return out
