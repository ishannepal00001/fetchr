"""
ffmpeg renderer. Implements the Renderer protocol from modules/downloads/services.py.

One source video in, one MP4 out, rendered a clip at a time so memory stays flat however
many clips there are (one decoder and one encoder alive at once: about 280 MB at 1080p
with a caption, so it fits a 512 MB instance next to the app):
1. ffprobe: duration, display size (rotation applied), whether there is audio.
2. Every clip must end within the video (END_TOLERANCE), else "Clip ends after the video does".
3. If there's a caption, the captioner draws it as a PNG as wide as the video.
4. One ffmpeg run per clip (clip_command), re-encoded so cuts are frame accurate, into
   part-NN.mkv:
   - the clip is read with -ss/-t, so ffmpeg seeks to it, then trim/atrim +
     setpts/asetpts=PTS-STARTPTS
   - a source without audio gets silence (anullsrc), so every part has an audio track
   - odd dimensions are cropped down to even (yuv420p)
   - the caption runs on the output's clock, not the clip's: it is fully visible for
     `hold` seconds from t=0 of the output, then its alpha fades out over `fade` seconds.
     A clip starting `offset` seconds into the output overlays the caption's timeline
     from `offset` on (trim=start), and clips after hold + fade get no caption at all.
     The PNG is read once and repeated inside the filter graph (loop + fps): looping it
     as an input (-loop 1) queues decoded frames and costs about 90 MB more at 1080p.
   - decoding, filtering and encoding use `threads` threads (1 by default): ffmpeg
     otherwise sizes its buffers by the host's cores, which in a container are not the
     container's, and runs out of memory
   - audio is kept as PCM in the parts, so it is encoded to AAC once, in the join;
     AAC per part would add encoder priming at every cut
5. One join run (join_command): the concat demuxer copies the parts' video as is and
   encodes the audio to AAC into clip.mp4. The parts are deleted afterwards.
6. The output's first frame is saved as the poster, which the thumbnailer turns into
   thumbnail.jpg (best effort).

`timeout` covers the whole render, all runs together. Failures raise RenderFailed with a
short message for the client; the ffmpeg command and the tail of its stderr are logged.
"""

import json
import logging
import shlex
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from app.modules.downloads.services import RENDER_FILENAME, Caption, Clip, FetchedMedia, RenderFailed, RenderSpec

logger = logging.getLogger(__name__)

END_TOLERANCE = 0.1  # seconds a clip may end past the probed duration
POSTER_FILENAME = "first-frame.png"
PART_FILENAME = "part-{:02d}.mkv"
CAPTION_FPS = 30  # caption frames per second, for a smooth fade
PARTS_LIST = "parts.txt"
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
        threads: int = 1,
    ) -> None:
        self._captioner = captioner
        self._ffmpeg = ffmpeg
        self._ffprobe = ffprobe
        self._timeout = timeout
        self._threads = threads

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

        deadline = time.monotonic() + self._timeout
        parts, offset = [], 0.0
        for i, clip in enumerate(spec.clips):
            part = dest_dir / PART_FILENAME.format(i)
            self._run(self.clip_command(source, clip, probe, spec.caption, caption_png, offset, part), deadline)
            parts.append(part)
            offset += min(clip.end, probe.duration) - clip.start

        out = dest_dir / RENDER_FILENAME
        parts_list = dest_dir / PARTS_LIST
        parts_list.write_text("".join(f"file '{part.name}'\n" for part in parts))
        try:
            self._run(self.join_command(parts_list, out), deadline)
        finally:
            for path in (*parts, parts_list):
                path.unlink(missing_ok=True)
        if not out.is_file() or out.stat().st_size == 0:
            raise RenderFailed("ffmpeg produced no video")
        return FetchedMedia(path=out, content_type="video/mp4", poster=self._first_frame(out, dest_dir))

    def probe(self, source: Path) -> Probe:
        cmd = [self._ffprobe, "-v", "error", "-show_format", "-show_streams", "-of", "json", str(source)]
        result = self._run(cmd, time.monotonic() + PROBE_TIMEOUT, failure="The source isn't a readable video")
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

    def clip_command(
        self,
        source: Path,
        clip: Clip,
        probe: Probe,
        caption: Caption | None,
        caption_png: Path | None,
        offset: float,
        out: Path,
    ) -> list[str]:
        """The ffmpeg command for one clip, starting `offset` seconds into the output (see the module docstring)."""
        threads = str(self._threads)
        length = _seconds(min(clip.end, probe.duration) - clip.start)
        cmd = [self._ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error", "-y", "-filter_complex_threads", threads]
        cmd += ["-threads", threads, "-ss", _seconds(clip.start), "-t", length, "-i", str(source)]

        graph = [f"[0:v:0]trim=duration={length},setpts=PTS-STARTPTS[v]"]
        if probe.has_audio:
            graph.append(f"[0:a:0]atrim=duration={length},asetpts=PTS-STARTPTS[aout]")
        else:
            graph.append(f"anullsrc=channel_layout=stereo:sample_rate=48000,atrim=duration={length}[aout]")

        video = "[v]"
        if probe.width % 2 or probe.height % 2:
            graph.append(f"{video}crop={probe.width - probe.width % 2}:{probe.height - probe.height % 2}:0:0[even]")
            video = "[even]"
        shown = caption.hold + caption.fade if caption is not None else 0
        if caption is not None and caption_png is not None and offset < shown:
            cmd += ["-i", str(caption_png)]
            frames = f"loop=loop=-1:size=1,fps={CAPTION_FPS},trim=duration={_seconds(shown)}"
            fade = f",fade=t=out:st={_seconds(caption.hold)}:d={_seconds(caption.fade)}:alpha=1" if caption.fade else ""
            graph.append(f"[1:v]format=rgba,{frames}{fade},trim=start={_seconds(offset)},setpts=PTS-STARTPTS[caption]")
            graph.append(f"{video}[caption]overlay=0:H-h:eof_action=pass[vout]")
        else:
            graph.append(f"{video}null[vout]")

        cmd += ["-filter_complex", ";".join(graph), "-map", "[vout]", "-map", "[aout]"]
        cmd += ["-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p", "-threads", threads]
        cmd += ["-c:a", "pcm_s16le", "-ar", "48000", "-ac", "2", str(out)]
        return cmd

    def join_command(self, parts_list: Path, out: Path) -> list[str]:
        """The ffmpeg command that joins the parts listed in parts_list into the output MP4."""
        cmd = [self._ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error", "-y", "-threads", str(self._threads)]
        cmd += ["-f", "concat", "-safe", "0", "-i", str(parts_list)]
        cmd += ["-c:v", "copy", "-c:a", "aac", "-b:a", "160k", "-movflags", "+faststart", str(out)]
        return cmd

    def _first_frame(self, video: Path, dest_dir: Path) -> Path | None:
        """The output's first frame as a PNG, for the thumbnail. None (logged) if it can't be grabbed."""
        out = dest_dir / POSTER_FILENAME
        cmd = [self._ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error", "-y"]
        cmd += ["-i", str(video), "-frames:v", "1", "-update", "1", str(out)]
        try:
            self._run(cmd, time.monotonic() + PROBE_TIMEOUT)
        except RenderFailed:
            return None  # the thumbnailer falls back to a frame of the video
        return out if out.is_file() else None

    def _run(
        self, cmd: list[str], deadline: float, failure: str = "ffmpeg could not render the video"
    ) -> subprocess.CompletedProcess:
        """Run cmd until `deadline` (time.monotonic()); on any failure log it with its stderr tail and raise RenderFailed(failure)."""
        try:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise subprocess.TimeoutExpired(cmd, 0)
            result = subprocess.run(cmd, capture_output=True, text=True, errors="replace", timeout=remaining)
        except subprocess.TimeoutExpired as exc:
            logger.error("timed out: %s\n%s", shlex.join(cmd), _tail(exc.stderr))
            raise RenderFailed(f"ffmpeg took longer than {self._timeout:g}s") from exc
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
