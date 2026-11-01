"""
ffmpeg renderer. Implements the Renderer protocol from modules/downloads/services.py.

One source video in, one MP4 out:
1. ffprobe: duration, display size (rotation applied), whether there is audio.
2. Every clip must end within the video (END_TOLERANCE), else "Clip ends after the video does".
3. If there's a caption, the captioner draws it as a PNG as wide as the video.
4. One ffmpeg run, re-encoded so cuts are frame accurate:
   - each clip is its own input (-ss/-t on the same file), so ffmpeg seeks to it instead
     of decoding the whole source and buffering out-of-order clips in memory; then
     trim/atrim + setpts/asetpts=PTS-STARTPTS
   - a source without audio gets silence (anullsrc) per clip, so concat and the MP4
     always have an audio track
   - concat in the requested order; odd dimensions are cropped down to even (yuv420p)
   - the caption PNG is overlaid at the bottom from t=0 of the output, fully visible for
     `hold` seconds, then its alpha fades out over `fade` seconds
   ffmpeg applies the source's rotation metadata itself (autorotate) before the filters.
5. The output's first frame is saved as the poster, which the thumbnailer turns into
   thumbnail.jpg (best effort).

Failures raise RenderFailed with a short message for the client; the ffmpeg command and
the tail of its stderr are logged.
"""

import json
import logging
import shlex
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from app.modules.downloads.services import RENDER_FILENAME, Caption, FetchedMedia, RenderFailed, RenderSpec

logger = logging.getLogger(__name__)

END_TOLERANCE = 0.1  # seconds a clip may end past the probed duration
POSTER_FILENAME = "first-frame.png"
STDERR_TAIL = 20  # lines of ffmpeg stderr logged on failure
PROBE_TIMEOUT = 60  # seconds, for ffprobe and the first-frame grab


class Captioner(Protocol):
    def make(self, caption: Caption, width: int, height: int, out_dir: Path) -> Path:
        """Write the caption as a transparent PNG `width` px wide into out_dir and return its path. Raise on failure."""
        ...


@dataclass(frozen=True)
class Probe:
    duration: float  # seconds
    width: int  # as displayed, i.e. after rotation
    height: int
    has_audio: bool


class FfmpegRenderer:
    def __init__(
        self,
        captioner: Captioner,
        ffmpeg: str = "ffmpeg",
        ffprobe: str = "ffprobe",
        timeout: float = 1800,
    ) -> None:
        self._captioner = captioner
        self._ffmpeg = ffmpeg
        self._ffprobe = ffprobe
        self._timeout = timeout

    def render(self, source: Path, spec: RenderSpec, dest_dir: Path) -> FetchedMedia:
        probe = self.probe(source)
        if any(clip.end > probe.duration + END_TOLERANCE for clip in spec.clips):
            raise RenderFailed("Clip ends after the video does")

        width, height = probe.width - probe.width % 2, probe.height - probe.height % 2
        caption_png = None
        if spec.caption is not None:
            try:
                caption_png = self._captioner.make(spec.caption, width, height, dest_dir)
            except Exception as exc:
                logger.error("caption for %s failed: %s", source.name, exc)
                raise RenderFailed("Could not draw the caption") from exc

        out = dest_dir / RENDER_FILENAME
        self._run(self.command(source, spec, probe, caption_png, out), self._timeout)
        if not out.is_file() or out.stat().st_size == 0:
            raise RenderFailed("ffmpeg produced no video")
        return FetchedMedia(path=out, content_type="video/mp4", poster=self._first_frame(out, dest_dir))

    def probe(self, source: Path) -> Probe:
        cmd = [self._ffprobe, "-v", "error", "-show_format", "-show_streams", "-of", "json", str(source)]
        result = self._run(cmd, PROBE_TIMEOUT, failure="The source isn't a readable video")
        try:
            info = json.loads(result.stdout)
            streams = info.get("streams", [])
            video = next(
                s for s in streams if s.get("codec_type") == "video" and not s.get("disposition", {}).get("attached_pic")
            )
            duration = float(info.get("format", {}).get("duration") or video["duration"])
            width, height = int(video["width"]), int(video["height"])
        except (ValueError, KeyError, TypeError, StopIteration):
            logger.error("ffprobe output for %s has no usable video stream: %s", source.name, result.stdout[-2000:])
            raise RenderFailed("The source isn't a readable video") from None
        if _rotation(video) % 180 == 90:
            width, height = height, width
        has_audio = any(s.get("codec_type") == "audio" for s in streams)
        return Probe(duration=duration, width=width, height=height, has_audio=has_audio)

    def command(self, source: Path, spec: RenderSpec, probe: Probe, caption_png: Path | None, out: Path) -> list[str]:
        """The full ffmpeg command for a render (see the module docstring)."""
        cmd = [self._ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error", "-y"]
        graph = []
        for i, clip in enumerate(spec.clips):
            length = _seconds(min(clip.end, probe.duration) - clip.start)
            cmd += ["-ss", _seconds(clip.start), "-t", length, "-i", str(source)]
            graph.append(f"[{i}:v:0]trim=duration={length},setpts=PTS-STARTPTS[v{i}]")
            if probe.has_audio:
                graph.append(f"[{i}:a:0]atrim=duration={length},asetpts=PTS-STARTPTS[a{i}]")
            else:
                graph.append(f"anullsrc=channel_layout=stereo:sample_rate=48000,atrim=duration={length}[a{i}]")
        n = len(spec.clips)
        graph.append("".join(f"[v{i}][a{i}]" for i in range(n)) + f"concat=n={n}:v=1:a=1[joined][aout]")

        video = "[joined]"
        if probe.width % 2 or probe.height % 2:
            graph.append(f"{video}crop={probe.width - probe.width % 2}:{probe.height - probe.height % 2}:0:0[even]")
            video = "[even]"
        if caption_png is not None and spec.caption is not None:
            caption = spec.caption
            cmd += ["-loop", "1", "-t", _seconds(caption.hold + caption.fade), "-i", str(caption_png)]
            fade = f",fade=t=out:st={_seconds(caption.hold)}:d={_seconds(caption.fade)}:alpha=1" if caption.fade else ""
            graph.append(f"[{n}:v]format=rgba{fade}[caption]")
            graph.append(f"{video}[caption]overlay=0:H-h:eof_action=pass[vout]")
        else:
            graph.append(f"{video}null[vout]")

        cmd += ["-filter_complex", ";".join(graph), "-map", "[vout]", "-map", "[aout]"]
        cmd += ["-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p"]
        cmd += ["-c:a", "aac", "-b:a", "160k", "-movflags", "+faststart", str(out)]
        return cmd

    def _first_frame(self, video: Path, dest_dir: Path) -> Path | None:
        """The output's first frame as a PNG, for the thumbnail. None (logged) if it can't be grabbed."""
        out = dest_dir / POSTER_FILENAME
        cmd = [self._ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error", "-y"]
        cmd += ["-i", str(video), "-frames:v", "1", "-update", "1", str(out)]
        try:
            self._run(cmd, PROBE_TIMEOUT)
        except RenderFailed:
            return None  # the thumbnailer falls back to a frame of the video
        return out if out.is_file() else None

    def _run(
        self, cmd: list[str], timeout: float, failure: str = "ffmpeg could not render the video"
    ) -> subprocess.CompletedProcess:
        """Run cmd; on any failure log it with its stderr tail and raise RenderFailed(failure)."""
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, errors="replace", timeout=timeout)
        except subprocess.TimeoutExpired as exc:
            logger.error("timed out after %ss: %s\n%s", timeout, shlex.join(cmd), _tail(exc.stderr))
            raise RenderFailed(f"ffmpeg took longer than {timeout:g}s") from exc
        except OSError as exc:
            logger.error("could not run %s: %s", shlex.join(cmd), exc)
            raise RenderFailed(failure) from exc
        if result.returncode != 0:
            logger.error("exit %s: %s\n%s", result.returncode, shlex.join(cmd), _tail(result.stderr))
            raise RenderFailed(failure)
        return result


def _rotation(stream: dict) -> int:
    """Degrees the player rotates this stream: display matrix side data (newer ffmpeg) or the old rotate tag."""
    for side_data in stream.get("side_data_list") or []:
        if "rotation" in side_data:
            return round(float(side_data["rotation"]))
    return round(float(stream.get("tags", {}).get("rotate", 0) or 0))


def _seconds(value: float) -> str:
    return f"{max(value, 0):.3f}"


def _tail(stderr: str | bytes | None) -> str:
    if isinstance(stderr, bytes):
        stderr = stderr.decode(errors="replace")
    return "\n".join((stderr or "").strip().splitlines()[-STDERR_TAIL:])
