"""
ffmpeg caption maker: draws a render's news-style caption as a transparent PNG, as wide
as the video, which the renderer overlays on the bottom of the output.

    ┌──────────────────────────────────────────┐
    │ HEADLINE, bold white on red              │  headline bar
    ├──────────────────────────────────────────┤
    │ details, white on translucent black      │  details bar (omitted if empty)
    └──────────────────────────────────────────┘

Text is laid out here (font size from the video width, greedy word wrap on estimated
glyph widths) and drawn with ffmpeg's drawtext, one call per line. Each line goes
through a text file, so captions need no escaping. If the caption would cover more than
MAX_HEIGHT_RATIO of the video, the fonts shrink, and as a last resort trailing detail
lines are cut. Widths are estimates for Latin sans fonts, so wide scripts may wrap late;
fonts come from settings (a file path or a fontconfig pattern).
"""

import logging
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

from app.modules.downloads.services import Caption

logger = logging.getLogger(__name__)

CAPTION_FILENAME = "caption.png"
MAX_HEIGHT_RATIO = 0.5  # of the video height
HEADLINE_RATIO = 0.045  # headline font size, as a fraction of the video width
DETAILS_RATIO = 0.032
MIN_FONT_SIZE = 10
LINE_HEIGHT = 1.3  # x font size
HEADLINE_BG = "0xC8102E@0.95"
DETAILS_BG = "black@0.7"
TEXT_COLOR = "white"

# Rough advance widths in em for a sans font (DejaVu/Liberation), erring wide so lines wrap early, not off-screen.
_NARROW = set("il.,:;'|!ijtfrI()[] ")
_WIDE = set("mwMW@%")
BOLD_FACTOR = 1.08


class CaptionError(Exception):
    """ffmpeg couldn't draw the caption."""


@dataclass(frozen=True)
class CaptionLayout:
    width: int
    headline_size: int
    details_size: int
    pad: int  # left/right margin and the space above and below each bar's text
    headline: tuple[str, ...]  # wrapped lines
    details: tuple[str, ...]  # wrapped lines; "" is a blank line

    @property
    def headline_bar(self) -> int:
        return 2 * self.pad + _lines_height(len(self.headline), self.headline_size)

    @property
    def details_bar(self) -> int:
        return 2 * self.pad + _lines_height(len(self.details), self.details_size) if self.details else 0

    @property
    def height(self) -> int:
        return self.headline_bar + self.details_bar


def layout(caption: Caption, width: int, max_height: int) -> CaptionLayout:
    """Font sizes and wrapped lines for a caption `width` px wide and at most `max_height` tall."""
    scale = 1.0
    while True:
        headline_size = max(MIN_FONT_SIZE, round(width * HEADLINE_RATIO * scale))
        details_size = max(MIN_FONT_SIZE, round(width * DETAILS_RATIO * scale))
        pad = max(4, round(headline_size * 0.4))
        text_width = width - 2 * pad
        result = CaptionLayout(
            width=width,
            headline_size=headline_size,
            details_size=details_size,
            pad=pad,
            headline=tuple(wrap(" ".join(caption.headline.split()), text_width, headline_size, bold=True)),
            details=tuple(_wrap_paragraphs(caption.details, text_width, details_size)),
        )
        if result.height <= max_height or (headline_size == details_size == MIN_FONT_SIZE):
            break
        scale *= 0.85
    return _truncate(result, max_height)


def _truncate(result: CaptionLayout, max_height: int) -> CaptionLayout:
    """Drop trailing detail lines (marking the cut with an ellipsis) until the caption fits."""
    details = list(result.details)
    while details and result.height > max_height:
        details.pop()
        while details and not details[-1]:
            details.pop()
        if details:
            details[-1] = details[-1].rstrip(" .") + "…"
        result = CaptionLayout(**{**result.__dict__, "details": tuple(details)})
    return result


def _wrap_paragraphs(text: str, max_width: int, size: int) -> list[str]:
    paragraphs = text.replace("\r\n", "\n").strip().split("\n")
    lines: list[str] = []
    for paragraph in paragraphs:
        lines += wrap(" ".join(paragraph.split()), max_width, size) or [""]
    return lines


def wrap(text: str, max_width: int, size: int, bold: bool = False) -> list[str]:
    """Greedy word wrap on estimated widths; a word longer than a line is split."""
    lines: list[str] = []
    line = ""
    for word in text.split(" "):
        if not word:
            continue
        candidate = f"{line} {word}" if line else word
        if text_width(candidate, size, bold) <= max_width:
            line = candidate
            continue
        if line:
            lines.append(line)
        while text_width(word, size, bold) > max_width and len(word) > 1:
            cut = len(word) - 1
            while cut > 1 and text_width(word[:cut], size, bold) > max_width:
                cut -= 1
            lines.append(word[:cut])
            word = word[cut:]
        line = word
    if line:
        lines.append(line)
    return lines


def text_width(text: str, size: int, bold: bool = False) -> float:
    em = 0.0
    for char in text:
        if char in _NARROW:
            em += 0.32
        elif char in _WIDE:
            em += 1.0
        elif char.isascii() and (char.isupper() or char.isdigit()):
            em += 0.75
        elif char.isascii():
            em += 0.62
        else:
            em += 0.8
    return em * size * (BOLD_FACTOR if bold else 1.0)


def _lines_height(count: int, size: int) -> int:
    return round(count * size * LINE_HEIGHT)


class FfmpegCaptioner:
    def __init__(
        self,
        ffmpeg: str = "ffmpeg",
        font: str = "Sans",
        bold_font: str = "Sans:bold",
        timeout: float = 60,
    ) -> None:
        self._ffmpeg = ffmpeg
        self._font = font
        self._bold_font = bold_font
        self._timeout = timeout

    def make(self, caption: Caption, width: int, height: int, out_dir: Path) -> Path:
        """Write out_dir/caption.png, `width` px wide, for a video `height` px tall. Raises CaptionError."""
        result = layout(caption, width, int(height * MAX_HEIGHT_RATIO))
        out = out_dir / CAPTION_FILENAME
        out.unlink(missing_ok=True)
        with tempfile.TemporaryDirectory(dir=out_dir, prefix="caption-") as text_dir:
            graph = self._graph(result, Path(text_dir))
            cmd = [self._ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error", "-y"]
            cmd += ["-filter_complex", graph, "-map", "[out]", "-frames:v", "1", "-update", "1", str(out.resolve())]
            try:
                # cwd = text_dir: drawtext reads each line from a bare relative file name, so nothing needs escaping
                run = subprocess.run(cmd, cwd=text_dir, capture_output=True, text=True, errors="replace", timeout=self._timeout)
            except (OSError, subprocess.TimeoutExpired) as exc:
                raise CaptionError(f"ffmpeg could not run: {exc}") from exc
        if run.returncode != 0 or not out.is_file():
            logger.error("caption ffmpeg failed (exit %s): %s\n%s", run.returncode, cmd, run.stderr.strip()[-2000:])
            raise CaptionError("ffmpeg could not draw the caption")
        return out

    def _graph(self, result: CaptionLayout, text_dir: Path) -> str:
        """The filtergraph: one color source per bar, stacked, then a drawtext per line. Writes the line files."""
        w = result.width
        bars = [f"color=c={HEADLINE_BG}:s={w}x{result.headline_bar}:d=1,format=rgba"]
        if result.details:
            bars.append(f"color=c={DETAILS_BG}:s={w}x{result.details_bar}:d=1,format=rgba")
        graph = ";".join(f"{bar}[bar{i}]" for i, bar in enumerate(bars)) + ";"
        graph += "".join(f"[bar{i}]" for i in range(len(bars)))
        graph += f"vstack=inputs={len(bars)}" if len(bars) > 1 else "null"

        draws = []
        y = result.pad
        for i, line in enumerate(result.headline):
            draws.append(self._drawtext(text_dir, f"h{i}.txt", line, self._bold_font, result.headline_size, result.pad, y))
            y += round(result.headline_size * LINE_HEIGHT)
        y = result.headline_bar + result.pad
        for i, line in enumerate(result.details):
            if line:
                draws.append(self._drawtext(text_dir, f"d{i}.txt", line, self._font, result.details_size, result.pad, y))
            y += round(result.details_size * LINE_HEIGHT)
        return graph + "".join(f",{draw}" for draw in draws) + "[out]"

    @staticmethod
    def _drawtext(text_dir: Path, name: str, line: str, font: str, size: int, x: int, y: int) -> str:
        (text_dir / name).write_text(line, encoding="utf-8")
        # fontfile: drawtext tries it as a path, then as a fontconfig pattern ("Sans:bold")
        return (
            f"drawtext=textfile={name}:expansion=none:fontfile={_escape(font)}"
            f":fontsize={size}:fontcolor={TEXT_COLOR}:x={x}:y={y}"
        )


def _escape(value: str) -> str:
    """Escape a value for a filter option inside -filter_complex (both of ffmpeg's escaping levels)."""
    for char in "\\':":
        value = value.replace(char, "\\" + char)  # option level
    for char in "\\'[],;":
        value = value.replace(char, "\\" + char)  # filtergraph level
    return value
