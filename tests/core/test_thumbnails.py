"""core/thumbnails tests against the real ffmpeg, on tiny clips and images it generates itself."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from app.core.thumbnails import FfmpegThumbnailer, ThumbnailError

pytestmark = pytest.mark.skipif(not shutil.which("ffmpeg") or not shutil.which("ffprobe"), reason="needs ffmpeg")


def generate(out: Path, source: str, *args: str) -> Path:
    """Render an ffmpeg lavfi test source (e.g. 'testsrc=size=320x240:duration=3') to `out`."""
    subprocess.run(
        ["ffmpeg", "-nostdin", "-loglevel", "error", "-y", "-f", "lavfi", "-i", source, *args, str(out)], check=True
    )
    return out


def dimensions(image: Path) -> tuple[int, int]:
    probe = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "stream=width,height,codec_name", "-of", "json", str(image)],
        capture_output=True,
        text=True,
        check=True,
    )
    stream = json.loads(probe.stdout)["streams"][0]
    assert stream["codec_name"] == "mjpeg"
    return stream["width"], stream["height"]


@pytest.fixture
def video(tmp_path) -> Path:
    """3s, 4:3, so a frame from it comes out 640x480."""
    return generate(tmp_path / "123.mp4", "testsrc=size=320x240:duration=3:rate=10", "-pix_fmt", "yuv420p")


@pytest.fixture
def poster(tmp_path) -> Path:
    """16:9, so a thumbnail made from it comes out 640x360 (tells the two sources apart)."""
    return generate(tmp_path / "123.webp", "color=c=red:size=1280x720", "-frames:v", "1")


@pytest.fixture
def out_dir(tmp_path) -> Path:
    path = tmp_path / "out"
    path.mkdir()
    return path


def test_poster_is_used_when_available(video, poster, out_dir):
    thumbnail = FfmpegThumbnailer().make(video, poster, out_dir)

    assert thumbnail == out_dir / "thumbnail.jpg"
    assert dimensions(thumbnail) == (640, 360)  # the poster's aspect ratio, scaled to 640 wide
    assert 0 < thumbnail.stat().st_size < 100_000


def test_falls_back_to_a_video_frame_without_a_poster(video, out_dir):
    thumbnail = FfmpegThumbnailer().make(video, None, out_dir)

    assert dimensions(thumbnail) == (640, 480)


def test_falls_back_to_a_video_frame_when_the_poster_is_broken(video, out_dir, tmp_path):
    broken = tmp_path / "123.jpg"
    broken.write_bytes(b"<html>not an image</html>")

    thumbnail = FfmpegThumbnailer().make(video, broken, out_dir)

    assert dimensions(thumbnail) == (640, 480)


def test_video_shorter_than_a_second_uses_its_first_frame(out_dir, tmp_path):
    short = generate(tmp_path / "short.mp4", "testsrc=size=320x240:duration=0.3:rate=10", "-pix_fmt", "yuv420p")

    thumbnail = FfmpegThumbnailer().make(short, None, out_dir)

    assert dimensions(thumbnail) == (640, 480)


def test_unreadable_video_without_poster_raises(out_dir, tmp_path):
    broken = tmp_path / "broken.mp4"
    broken.write_bytes(b"not a video")

    with pytest.raises(ThumbnailError):
        FfmpegThumbnailer().make(broken, None, out_dir)
    assert not (out_dir / "thumbnail.jpg").exists()


def test_missing_ffmpeg_raises_thumbnail_error(video, poster, out_dir):
    with pytest.raises(ThumbnailError, match="could not run"):
        FfmpegThumbnailer(ffmpeg="/nonexistent/ffmpeg").make(video, poster, out_dir)
