"""
Pillow caption maker: draws a render's news-style caption as a transparent PNG, as wide
as the video, which the renderer overlays on the bottom of the output. It matches
Bhandar's editor preview (frontend/src/editor/ClipCaption.tsx); every size below is a
fraction of the video width W, like the preview's cqw units.

    ┌──────────────────────────────────────────┐  full-width panel, black @ 60%
    │         ▐ Headline line one ▌            │  one gold bar per headline line,
    │      ▐ and its second, wider line ▌      │  as wide as that line, soft shadow
    │      details in bold cream, centred      │  no bar, soft text shadow
    └──────────────────────────────────────────┘

Fonts are bundled in app/core/fonts (SIL OFL): Montserrat for Latin and Noto Sans
Devanagari for Nepali, ExtraBold for the headline and Bold for the details. Pillow has
no font fallback, so each line is split into runs by script and each run is shaped and
drawn with its own font, on a shared baseline. Shaping goes through libraqm (HarfBuzz),
which Devanagari needs for conjuncts, matras and reph; PillowCaptioner refuses to start
without it. Pillow's wheels bundle it.

Wrapping uses the real advance widths. The headline collapses whitespace, like the
preview; the details keep their line breaks and blank lines. A word wider than a line is
split between grapheme clusters, never inside one. The text is never cut: if the caption
would cover more than MAX_HEIGHT_RATIO of the video, every size shrinks together until
it fits.
"""

import logging
import re
import unicodedata
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont, features

from app.modules.downloads.services import Caption

logger = logging.getLogger(__name__)

CAPTION_FILENAME = "caption.png"
FONT_DIR = Path(__file__).with_name("fonts")
FONT_FILES = {
    ("latin", "headline"): "Montserrat-ExtraBold.ttf",
    ("latin", "details"): "Montserrat-Bold.ttf",
    ("devanagari", "headline"): "NotoSansDevanagari-ExtraBold.ttf",
    ("devanagari", "details"): "NotoSansDevanagari-Bold.ttf",
}

MAX_HEIGHT_RATIO = 0.5  # of the video height
SHRINK_STEP = 0.9
MIN_SCALE = 0.05

# Fractions of the video width (the preview's cqw / 100)
PANEL_PAD_X = 0.04
PANEL_PAD_Y = 0.03
HEADLINE_SIZE = 0.065
DETAILS_SIZE = 0.036
BAR_PAD_X = 0.016
BAR_PAD_Y = 0.005
DETAILS_GAP = 0.025
BAR_SHADOW = (0.006, 0.016)  # y offset, blur
TEXT_SHADOW = (0.003, 0.01)

# Floors in px so small videos stay legible (the preview clamps at 16px / 12px)
MIN_HEADLINE_PX = 16
MIN_DETAILS_PX = 12
HEADLINE_LINE_HEIGHT = 1.6  # x font size
DETAILS_LINE_HEIGHT = 1.375

PANEL_COLOR = (0, 0, 0, 153)
BAR_COLOR = (0xB0, 0x8A, 0x3C, 255)
HEADLINE_COLOR = (0x0B, 0x23, 0x50, 255)
DETAILS_COLOR = (0xF3, 0xEF, 0xE6, 255)
BAR_SHADOW_COLOR = (0, 0, 0, 140)
TEXT_SHADOW_COLOR = (0, 0, 0, 204)

VIRAMA = "्"
JOINERS = "‌‍"


class CaptionError(Exception):
    """The caption couldn't be drawn."""


def is_devanagari(char: str) -> bool:
    code = ord(char)
    return 0x0900 <= code <= 0x097F or 0xA8E0 <= code <= 0xA8FF or 0x1CD0 <= code <= 0x1CFF


def script_runs(text: str) -> list[tuple[str, str]]:
    """Split text into (script, run) pairs. Spaces, joiners and marks stay with the run they're in."""
    runs: list[tuple[str, str]] = []
    for char in text:
        neutral = char.isspace() or char in JOINERS or unicodedata.combining(char)
        script = runs[-1][0] if neutral and runs else ("devanagari" if is_devanagari(char) else "latin")
        if runs and runs[-1][0] == script:
            runs[-1] = (script, runs[-1][1] + char)
        else:
            runs.append((script, char))
    return runs


def clusters(word: str) -> list[str]:
    """Grapheme-ish clusters: a base character with its marks, kept joined across a virama or joiner."""
    result: list[str] = []
    for char in word:
        attach = result and (
            unicodedata.category(char) in ("Mn", "Mc", "Me")
            or char in JOINERS
            or result[-1][-1] in (VIRAMA, *JOINERS)
        )
        if attach:
            result[-1] += char
        else:
            result.append(char)
    return result


@lru_cache(maxsize=64)
def _font(path: Path, size: int) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(str(path), size, layout_engine=ImageFont.Layout.RAQM)


@dataclass(frozen=True)
class Face:
    """One role (headline or details) at one size, in both scripts."""

    latin: ImageFont.FreeTypeFont
    devanagari: ImageFont.FreeTypeFont

    @property
    def size(self) -> int:
        return int(self.latin.size)

    def _for(self, script: str) -> ImageFont.FreeTypeFont:
        return self.devanagari if script == "devanagari" else self.latin

    def length(self, text: str) -> float:
        return sum(self._for(script).getlength(run) for script, run in script_runs(text))

    def metrics(self) -> tuple[int, int]:
        """Ascent and descent of the Latin font, which sets the bar's height, as in the browser."""
        return self.latin.getmetrics()

    def draw(self, draw: ImageDraw.ImageDraw, x: float, baseline: float, text: str, fill: tuple[int, ...]) -> None:
        for script, run in script_runs(text):
            font = self._for(script)
            draw.text((x, baseline), run, font=font, fill=fill, anchor="ls")
            x += font.getlength(run)


class FontBook:
    def __init__(self, font_dir: Path = FONT_DIR) -> None:
        self._paths = {key: font_dir / name for key, name in FONT_FILES.items()}
        missing = [str(path) for path in self._paths.values() if not path.is_file()]
        if missing:
            raise CaptionError(f"caption fonts missing: {', '.join(missing)}")

    def face(self, role: str, size: int) -> Face:
        size = max(1, size)
        return Face(_font(self._paths[("latin", role)], size), _font(self._paths[("devanagari", role)], size))


@dataclass(frozen=True)
class CaptionLayout:
    width: int
    headline: Face
    details: Face
    pad_x: int
    pad_y: int
    bar_pad_x: int
    bar_pad_y: int
    gap: int
    headline_lines: tuple[str, ...]  # wrapped
    details_lines: tuple[str, ...]  # wrapped; "" is a blank line

    @property
    def headline_pitch(self) -> int:
        return round(self.headline.size * HEADLINE_LINE_HEIGHT)

    @property
    def details_pitch(self) -> int:
        return round(self.details.size * DETAILS_LINE_HEIGHT)

    @property
    def details_top(self) -> int:
        return self.pad_y + len(self.headline_lines) * self.headline_pitch + self.gap

    @property
    def height(self) -> int:
        details = self.gap + len(self.details_lines) * self.details_pitch if self.details_lines else 0
        return 2 * self.pad_y + len(self.headline_lines) * self.headline_pitch + details


def wrap(text: str, max_width: float, face: Face) -> list[str]:
    """Greedy word wrap on real advance widths; a word wider than a line is split between clusters."""
    lines: list[str] = []
    line = ""
    for word in text.split(" "):
        if not word:
            continue
        candidate = f"{line} {word}" if line else word
        if face.length(candidate) <= max_width:
            line = candidate
            continue
        if line:
            lines.append(line)
        line = ""
        for cluster in clusters(word):
            if line and face.length(line + cluster) > max_width:
                lines.append(line)
                line = ""
            line += cluster
    if line:
        lines.append(line)
    return lines


def _collapse(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def _details_lines(details: str, max_width: float, face: Face) -> list[str]:
    paragraphs = details.replace("\r\n", "\n").strip("\n").split("\n")
    lines: list[str] = []
    for paragraph in paragraphs:
        lines += wrap(_collapse(paragraph), max_width, face) or [""]
    return lines if any(lines) else []


def layout(caption: Caption, width: int, max_height: int, fonts: FontBook) -> CaptionLayout:
    """Sizes and wrapped lines for a caption `width` px wide, shrunk until it's at most `max_height` tall."""
    scale = 1.0
    while True:
        headline = fonts.face("headline", round(max(width * HEADLINE_SIZE, MIN_HEADLINE_PX) * scale))
        details = fonts.face("details", round(max(width * DETAILS_SIZE, MIN_DETAILS_PX) * scale))
        pad_x = round(width * PANEL_PAD_X * scale)
        bar_pad_x = round(width * BAR_PAD_X * scale)
        text_width = width - 2 * pad_x
        result = CaptionLayout(
            width=width,
            headline=headline,
            details=details,
            pad_x=pad_x,
            pad_y=round(width * PANEL_PAD_Y * scale),
            bar_pad_x=bar_pad_x,
            bar_pad_y=round(width * BAR_PAD_Y * scale),
            gap=round(width * DETAILS_GAP * scale),
            headline_lines=tuple(wrap(_collapse(caption.headline), text_width - 2 * bar_pad_x, headline)),
            details_lines=tuple(_details_lines(caption.details, text_width, details)),
        )
        if result.height <= max_height or scale <= MIN_SCALE:
            return result
        scale *= SHRINK_STEP


def _layer(width: int, height: int) -> tuple[Image.Image, ImageDraw.ImageDraw]:
    image = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    return image, ImageDraw.Draw(image)


def draw(result: CaptionLayout) -> Image.Image:
    """The caption as an RGBA image: panel, bar shadows, bars, headline, details shadow, details."""
    width, height = result.width, result.height
    canvas = Image.new("RGBA", (width, height), PANEL_COLOR)
    bar_shadow, bar_shadow_draw = _layer(width, height)
    bars, bars_draw = _layer(width, height)
    headline_text, headline_draw = _layer(width, height)
    details_shadow, details_shadow_draw = _layer(width, height)
    details_text, details_draw = _layer(width, height)

    ascent, descent = result.headline.metrics()
    shadow_dy, shadow_blur = (round(width * value) for value in BAR_SHADOW)
    for i, line in enumerate(result.headline_lines):
        line_top = result.pad_y + i * result.headline_pitch
        content_top = line_top + (result.headline_pitch - ascent - descent) / 2
        text_width = result.headline.length(line)
        x = (width - text_width) / 2
        box = (
            x - result.bar_pad_x,
            content_top - result.bar_pad_y,
            x + text_width + result.bar_pad_x,
            content_top + ascent + descent + result.bar_pad_y,
        )
        bar_shadow_draw.rectangle((box[0], box[1] + shadow_dy, box[2], box[3] + shadow_dy), fill=BAR_SHADOW_COLOR)
        bars_draw.rectangle(box, fill=BAR_COLOR)
        result.headline.draw(headline_draw, x, content_top + ascent, line, HEADLINE_COLOR)

    ascent, descent = result.details.metrics()
    text_dy, text_blur = (round(width * value) for value in TEXT_SHADOW)
    for i, line in enumerate(result.details_lines):
        if not line:
            continue
        line_top = result.details_top + i * result.details_pitch
        baseline = line_top + (result.details_pitch - ascent - descent) / 2 + ascent
        x = (width - result.details.length(line)) / 2
        result.details.draw(details_shadow_draw, x, baseline + text_dy, line, TEXT_SHADOW_COLOR)
        result.details.draw(details_draw, x, baseline, line, DETAILS_COLOR)

    canvas.alpha_composite(bar_shadow.filter(ImageFilter.GaussianBlur(shadow_blur / 2)))
    canvas.alpha_composite(bars)
    canvas.alpha_composite(headline_text)
    canvas.alpha_composite(details_shadow.filter(ImageFilter.GaussianBlur(text_blur / 2)))
    canvas.alpha_composite(details_text)
    return canvas


class PillowCaptioner:
    def __init__(self, font_dir: Path = FONT_DIR) -> None:
        if not features.check("raqm"):
            raise CaptionError("Pillow was built without libraqm; Devanagari captions can't be shaped")
        self._fonts = FontBook(font_dir)

    def make(self, caption: Caption, width: int, height: int, out_dir: Path) -> Path:
        """Write out_dir/caption.png, `width` px wide, for a video `height` px tall. Raises CaptionError."""
        out = out_dir / CAPTION_FILENAME
        try:
            result = layout(caption, width, int(height * MAX_HEIGHT_RATIO), self._fonts)
            draw(result).save(out, format="PNG")
        except Exception as exc:
            logger.error("caption drawing failed: %s", exc)
            raise CaptionError("could not draw the caption") from exc
        return out
