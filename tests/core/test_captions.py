"""core/captions tests: the pure layout, and the real ffmpeg drawing the PNG."""

import json
import shutil
import subprocess

import pytest

from app.core.captions import FfmpegCaptioner, _escape, layout, text_width, wrap
from app.modules.downloads.services import Caption

needs_ffmpeg = pytest.mark.skipif(not shutil.which("ffmpeg") or not shutil.which("ffprobe"), reason="needs ffmpeg")


def test_wrap_keeps_every_line_within_the_width():
    text = "Kathmandu flood waters recede after three days of heavy monsoon rain across the valley"

    lines = wrap(text, 300, 20, bold=True)

    assert len(lines) > 1
    assert " ".join(lines) == text
    assert all(text_width(line, 20, bold=True) <= 300 for line in lines)


def test_wrap_splits_a_word_longer_than_a_line():
    lines = wrap("x" * 80, 100, 20)

    assert "".join(lines) == "x" * 80
    assert all(text_width(line, 20) <= 100 for line in lines)


def test_layout_scales_with_the_video_width():
    caption = Caption("Headline", "Details")

    small, large = layout(caption, 640, 1000), layout(caption, 1920, 1000)

    assert large.headline_size == pytest.approx(3 * small.headline_size, abs=2)
    assert large.width == 1920


def test_details_keep_their_line_breaks_and_blank_lines():
    result = layout(Caption("Headline", "First\n\nThird"), 1280, 1000)

    assert result.details == ("First", "", "Third")


def test_long_caption_shrinks_to_fit_half_the_video():
    caption = Caption("H" * 200, "word " * 120)

    result = layout(caption, 640, 180)

    assert result.height <= 180
    assert result.headline_size < round(640 * 0.045)


def test_caption_that_cannot_fit_is_cut_with_an_ellipsis():
    caption = Caption("Headline", "\n".join(f"line {i}" for i in range(60)))

    result = layout(caption, 320, 120)

    assert result.height <= 120
    assert result.details[-1].endswith("…")


def test_escape_handles_both_filtergraph_levels():
    assert _escape("Sans:bold") == r"Sans\\:bold"  # graph level unescapes to Sans\:bold, the option level to Sans:bold
    assert _escape("/fonts/a,b[1].ttf") == r"/fonts/a\,b\[1\].ttf"


@needs_ffmpeg
def test_make_draws_a_transparent_png_as_wide_as_the_video(tmp_path):
    caption = Caption("Breaking: 'quotes', colons: and %{pts} stay literal", "Line one\nLine two")

    png = FfmpegCaptioner().make(caption, 640, 360, tmp_path)

    assert png == tmp_path / "caption.png"
    probe = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "stream=width,height,pix_fmt", "-of", "json", str(png)],
        capture_output=True,
        text=True,
        check=True,
    )
    stream = json.loads(probe.stdout)["streams"][0]
    assert stream["width"] == 640
    assert stream["height"] == layout(caption, 640, 180).height
    assert stream["pix_fmt"] == "rgba"
    assert [p.name for p in tmp_path.iterdir()] == ["caption.png"]  # line files cleaned up


@needs_ffmpeg
def test_make_raises_when_ffmpeg_fails(tmp_path):
    from app.core.captions import CaptionError

    with pytest.raises(CaptionError):
        FfmpegCaptioner(ffmpeg="/nonexistent/ffmpeg").make(Caption("H", "D"), 640, 360, tmp_path)
