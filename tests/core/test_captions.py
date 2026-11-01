"""core/captions tests: script runs, clusters, wrapping and layout on the bundled fonts, and the drawn PNG."""

import pytest
from PIL import Image

from app.core.captions import (
    BAR_COLOR,
    DETAILS_COLOR,
    HEADLINE_SIZE,
    CaptionError,
    FontBook,
    PillowCaptioner,
    clusters,
    layout,
    script_runs,
    wrap,
)
from app.modules.downloads.services import Caption

NEPALI_HEADLINE = "काठमाडौंमा भूकम्प"
NEPALI_DETAILS = "सुरक्षित स्थानमा जानुहोस्।\nथप जानकारी छिट्टै।"


@pytest.fixture(scope="module")
def fonts() -> FontBook:
    return FontBook()


def near(pixel: tuple[int, ...], color: tuple[int, ...], tolerance: int = 12) -> bool:
    return all(abs(a - b) <= tolerance for a, b in zip(pixel[:3], color[:3]))


def test_script_runs_split_latin_and_devanagari_and_keep_spaces_in_the_run():
    assert script_runs("Breaking काठमाडौं news") == [
        ("latin", "Breaking "),
        ("devanagari", "काठमाडौं "),
        ("latin", "news"),
    ]


def test_clusters_keep_marks_and_conjuncts_together():
    assert clusters("क्षत्रि") == ["क्ष", "त्रि"]
    assert clusters("भूकम्प") == ["भू", "क", "म्प"]
    assert clusters("abc") == ["a", "b", "c"]


def test_wrap_keeps_every_line_within_the_width(fonts):
    face = fonts.face("headline", 40)
    text = "Kathmandu flood waters recede after three days of heavy monsoon rain across the valley"

    lines = wrap(text, 500, face)

    assert len(lines) > 1
    assert " ".join(lines) == text
    assert all(face.length(line) <= 500 for line in lines)


def test_wrap_splits_a_long_word_between_clusters_only(fonts):
    face = fonts.face("headline", 40)
    word = "स्वास्थ्य" * 12

    lines = wrap(word, 300, face)

    assert "".join(lines) == word
    assert all(not line.startswith(("्", "ा", "्य")) for line in lines[1:])
    assert all(face.length(line) <= 300 for line in lines)


def test_layout_scales_with_the_video_width(fonts):
    caption = Caption("Headline", "Details")

    small, large = layout(caption, 640, 1000, fonts), layout(caption, 1920, 1000, fonts)

    assert small.headline.size == round(640 * HEADLINE_SIZE)
    assert large.headline.size == pytest.approx(3 * small.headline.size, abs=2)


def test_layout_floors_font_sizes_on_tiny_videos(fonts):
    result = layout(Caption("Headline", "Details"), 160, 1000, fonts)

    assert (result.headline.size, result.details.size) == (16, 12)


def test_headline_collapses_whitespace_and_details_keep_line_breaks(fonts):
    result = layout(Caption("  Big   news\ntoday ", "First\n\nThird"), 1280, 1000, fonts)

    assert result.headline_lines == ("Big news today",)
    assert result.details_lines == ("First", "", "Third")


def test_a_long_caption_shrinks_to_fit_and_keeps_every_word(fonts):
    caption = Caption("Headline " * 20, "word " * 110)

    result = layout(caption, 640, 180, fonts)

    assert result.height <= 180
    assert " ".join(result.headline_lines) == " ".join(caption.headline.split())
    assert " ".join(result.details_lines) == " ".join(caption.details.split())


def test_make_draws_the_caption_as_wide_as_the_video(tmp_path):
    caption = Caption("Breaking news in the valley", "Line one\nLine two")

    png = PillowCaptioner().make(caption, 1080, 1920, tmp_path)

    assert png == tmp_path / "caption.png"
    image = Image.open(png)
    assert image.mode == "RGBA" and image.width == 1080
    assert image.getpixel((2, 2))[3] == 153  # the panel: black at 60%
    pixels = list(image.get_flattened_data())
    assert any(near(p, BAR_COLOR, 2) and p[3] == 255 for p in pixels)  # a gold headline bar
    assert any(near(p, DETAILS_COLOR, 2) and p[3] == 255 for p in pixels)  # cream details text


def test_each_headline_line_gets_its_own_bar_as_wide_as_the_line(tmp_path, fonts):
    caption = Caption("Short line then a much much much longer second line", "Details")
    result = layout(caption, 640, 1000, fonts)
    assert len(result.headline_lines) >= 2

    image = Image.open(PillowCaptioner().make(caption, 640, 1000, tmp_path))

    widths = []
    for i in range(len(result.headline_lines)):
        y = result.pad_y + i * result.headline_pitch + result.headline_pitch // 2
        row = [x for x in range(image.width) if near(image.getpixel((x, y)), BAR_COLOR, 2)]
        widths.append(row[-1] - row[0] if row else 0)
    assert all(widths) and len(set(widths)) > 1


def test_nepali_caption_renders(tmp_path):
    png = PillowCaptioner().make(Caption(NEPALI_HEADLINE, NEPALI_DETAILS), 1920, 1080, tmp_path)

    image = Image.open(png)
    assert image.width == 1920
    assert any(near(p, DETAILS_COLOR, 2) for p in image.get_flattened_data())


def test_missing_fonts_fail_at_startup(tmp_path):
    with pytest.raises(CaptionError, match="fonts missing"):
        PillowCaptioner(font_dir=tmp_path)


def test_make_raises_caption_error_on_failure(tmp_path):
    with pytest.raises(CaptionError):
        PillowCaptioner().make(Caption("H", "D"), 640, 360, tmp_path / "missing-dir")
