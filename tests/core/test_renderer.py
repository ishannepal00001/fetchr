"""core/renderer tests against the real ffmpeg, on tiny sources it generates itself."""

import json
import logging
import shutil
import subprocess
from pathlib import Path

import pytest

from app.core.captions import PillowCaptioner
from app.core.renderer import FfmpegRenderer, Probe
from app.modules.downloads.services import Caption, Clip, RenderFailed, RenderSpec

pytestmark = pytest.mark.skipif(not shutil.which("ffmpeg") or not shutil.which("ffprobe"), reason="needs ffmpeg")

RED, GREEN, BLUE = (255, 0, 0), (0, 255, 0), (0, 0, 255)


def ffmpeg(*args: str) -> None:
    subprocess.run(["ffmpeg", "-nostdin", "-loglevel", "error", "-y", *args], check=True)


def make_source(out: Path, size: str = "160x120", audio: bool = True, pix_fmt: str = "yuv420p") -> Path:
    """6s: red for 0-2s, green for 2-4s, blue for 4-6s (so a frame tells where it came from); a tone if audio."""
    inputs = []
    for color in ("red", "lime", "blue"):
        inputs += ["-f", "lavfi", "-i", f"color=c={color}:size={size}:duration=2:rate=25"]
    graph = "[0:v][1:v][2:v]concat=n=3:v=1:a=0[v]"
    maps = ["-map", "[v]"]
    if audio:
        inputs += ["-f", "lavfi", "-i", "sine=frequency=440:duration=6"]
        maps += ["-map", "3:a"]
    ffmpeg(*inputs, "-filter_complex", graph, *maps, "-pix_fmt", pix_fmt, str(out))
    return out


def probe(video: Path) -> dict:
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-show_format", "-show_streams", "-of", "json", str(video)],
        capture_output=True,
        text=True,
        check=True,
    )
    info = json.loads(result.stdout)
    streams = {s["codec_type"]: s for s in info["streams"]}
    return {"duration": float(info["format"]["duration"]), **streams}


def color_at(video: Path, t: float, y: float = 0.25) -> tuple[int, int, int]:
    """Average RGB of a small patch in the middle column, `y` of the way down, at t seconds."""
    result = subprocess.run(
        ["ffmpeg", "-nostdin", "-loglevel", "error", "-ss", str(t), "-i", str(video), "-frames:v", "1",
         "-vf", f"crop=8:8:(iw-8)/2:(ih-8)*{y},scale=1:1:flags=area", "-pix_fmt", "rgb24", "-f", "rawvideo", "-"],
        capture_output=True,
        check=True,
    )
    return tuple(result.stdout[:3])


def near(color: tuple[int, ...], expected: tuple[int, ...], tolerance: int = 40) -> bool:
    return all(abs(a - b) <= tolerance for a, b in zip(color, expected))


@pytest.fixture
def source(tmp_path) -> Path:
    return make_source(tmp_path / "source.mp4")


@pytest.fixture
def out_dir(tmp_path) -> Path:
    path = tmp_path / "out"
    path.mkdir()
    return path


@pytest.fixture
def renderer() -> FfmpegRenderer:
    return FfmpegRenderer(PillowCaptioner())


def spec(*clips: tuple[float, float], caption: Caption | None = None) -> RenderSpec:
    return RenderSpec(clips=tuple(Clip(start, end) for start, end in clips), caption=caption)


def test_one_clip(renderer, source, out_dir):
    media = renderer.render(source, spec((0.5, 2.0)), out_dir)

    assert media.path == out_dir / "clip.mp4"
    assert media.content_type == "video/mp4"
    info = probe(media.path)
    assert info["duration"] == pytest.approx(1.5, abs=0.1)
    assert (info["video"]["codec_name"], info["video"]["pix_fmt"]) == ("h264", "yuv420p")
    assert (info["video"]["width"], info["video"]["height"]) == (160, 120)
    assert info["audio"]["codec_name"] == "aac"
    assert near(color_at(media.path, 0.5), RED)


def test_several_clips_are_joined_in_request_order(renderer, source, out_dir):
    media = renderer.render(source, spec((4.2, 5.2), (0.2, 1.2), (2.2, 3.2)), out_dir)

    assert probe(media.path)["duration"] == pytest.approx(3.0, abs=0.1)
    assert near(color_at(media.path, 0.5), BLUE)
    assert near(color_at(media.path, 1.5), RED)
    assert near(color_at(media.path, 2.5), GREEN)


def test_overlapping_clips_both_play_in_full(renderer, source, out_dir):
    media = renderer.render(source, spec((1.0, 3.0), (1.5, 3.5)), out_dir)

    assert probe(media.path)["duration"] == pytest.approx(4.0, abs=0.1)
    assert near(color_at(media.path, 0.5), RED)  # source 1.5
    assert near(color_at(media.path, 1.5), GREEN)  # source 2.5
    assert near(color_at(media.path, 2.2), RED)  # second clip, source 1.7
    assert near(color_at(media.path, 3.5), GREEN)  # source 3.0


def test_source_without_audio_gets_a_silent_track(renderer, tmp_path, out_dir):
    silent = make_source(tmp_path / "silent.mp4", audio=False)

    media = renderer.render(silent, spec((0, 1), (3, 4)), out_dir)

    info = probe(media.path)
    assert info["audio"]["codec_name"] == "aac"
    assert float(info["audio"]["duration"]) == pytest.approx(2.0, abs=0.15)


def test_clip_past_the_end_fails(renderer, source, out_dir):
    with pytest.raises(RenderFailed, match="^Clip ends after the video does$"):
        renderer.render(source, spec((0, 1), (5.5, 6.2)), out_dir)
    assert not (out_dir / "clip.mp4").exists()


def test_clip_ending_within_the_tolerance_is_cut_at_the_end(renderer, source, out_dir):
    media = renderer.render(source, spec((5.0, 6.08)), out_dir)

    assert probe(media.path)["duration"] == pytest.approx(1.0, abs=0.1)


def test_caption_is_burned_in_at_the_bottom_then_fades_out(renderer, source, out_dir):
    caption = Caption(headline="Breaking news", details="Line one\nLine two", hold=0.5, fade=0.5)

    media = renderer.render(source, spec((0, 2), caption=caption), out_dir)

    bottom = color_at(media.path, 0.3, y=0.95)
    assert not near(bottom, RED)  # covered by the caption's translucent black details bar
    assert near(color_at(media.path, 0.3, y=0.1), RED)  # top of the frame untouched
    assert not near(color_at(media.path, 0.75, y=0.95), color_at(media.path, 0.3, y=0.95), tolerance=10)  # fading
    assert near(color_at(media.path, 1.5, y=0.95), RED)  # after hold + fade: gone
    assert (out_dir / "caption.png").is_file()


def test_without_caption_the_bottom_is_untouched(renderer, source, out_dir):
    media = renderer.render(source, spec((0, 1.5)), out_dir)

    assert near(color_at(media.path, 0.3, y=0.95), RED)
    assert not (out_dir / "caption.png").exists()


def test_caption_shows_from_the_start_of_the_output_not_each_clip(renderer, source, out_dir):
    caption = Caption(headline="Headline", details="Details", hold=0.5, fade=0.2)

    media = renderer.render(source, spec((0, 1.5), (0, 1.5)), out_dir)  # no caption: reference
    plain = color_at(media.path, 1.8, y=0.95)
    media = renderer.render(source, spec((0, 1.5), (0, 1.5), caption=caption), out_dir)

    assert not near(color_at(media.path, 0.2, y=0.95), RED)
    assert color_at(media.path, 1.8, y=0.95) == pytest.approx(plain, abs=10)  # second clip: no caption again


def test_odd_dimensions_are_cropped_to_even(renderer, tmp_path, out_dir):
    odd = make_source(tmp_path / "odd.mp4", size="161x121", pix_fmt="yuv444p")  # 4:2:0 can't be odd-sized

    media = renderer.render(odd, spec((0, 1), caption=Caption("Headline", "Details")), out_dir)

    info = probe(media.path)
    assert (info["video"]["width"], info["video"]["height"]) == (160, 120)


def test_rotation_metadata_is_applied(renderer, source, tmp_path, out_dir):
    rotated = tmp_path / "rotated.mp4"
    try:
        ffmpeg("-display_rotation:v", "90", "-i", str(source), "-c", "copy", str(rotated))
    except subprocess.CalledProcessError:
        pytest.skip("this ffmpeg can't write display rotation")

    assert renderer.probe(rotated).width == 120
    media = renderer.render(rotated, spec((0, 1)), out_dir)

    info = probe(media.path)
    assert (info["video"]["width"], info["video"]["height"]) == (120, 160)


def test_first_frame_is_returned_as_the_poster(renderer, source, out_dir):
    media = renderer.render(source, spec((4.5, 5.5), (0, 1)), out_dir)

    assert media.poster == out_dir / "first-frame.png"
    assert near(color_at(media.poster, 0), BLUE)


def test_probe_reads_size_duration_and_audio(renderer, source, tmp_path):
    assert renderer.probe(source) == Probe(duration=pytest.approx(6.0, abs=0.05), width=160, height=120, has_audio=True)
    assert renderer.probe(make_source(tmp_path / "silent.mp4", audio=False)).has_audio is False


def test_unreadable_source_fails_cleanly(renderer, tmp_path, out_dir):
    junk = tmp_path / "junk.mp4"
    junk.write_bytes(b"not a video")

    with pytest.raises(RenderFailed, match="^The source isn't a readable video$"):
        renderer.render(junk, spec((0, 1)), out_dir)


def test_ffmpeg_failure_logs_the_command_and_stderr_tail(renderer, tmp_path, out_dir, caplog, monkeypatch):
    silent = make_source(tmp_path / "silent.mp4", audio=False)
    real_probe = renderer.probe
    monkeypatch.setattr(renderer, "probe", lambda source: Probe(**{**real_probe(source).__dict__, "has_audio": True}))

    with caplog.at_level(logging.ERROR), pytest.raises(RenderFailed, match="^ffmpeg could not render the video$"):
        renderer.render(silent, spec((0, 1)), out_dir)  # [0:a:0] matches no stream

    command, *stderr = caplog.records[-1].getMessage().split("\n")
    assert "-filter_complex" in command and "concat=n=1" in command
    assert any(line.strip() for line in stderr)  # ffmpeg's own error follows the command


def test_caption_failure_fails_the_render(source, out_dir):
    class FailingCaptioner:
        def make(self, caption, width, height, out_dir):
            raise RuntimeError("no fonts")

    with pytest.raises(RenderFailed, match="^Could not draw the caption$"):
        FfmpegRenderer(FailingCaptioner()).render(source, spec((0, 1), caption=Caption("H", "D")), out_dir)


def test_ffmpeg_and_ffprobe_paths_are_configurable(source, out_dir):
    renderer = FfmpegRenderer(PillowCaptioner(), ffmpeg="/nonexistent/ffmpeg", ffprobe="/nonexistent/ffprobe")

    with pytest.raises(RenderFailed):
        renderer.render(source, spec((0, 1)), out_dir)

    command = renderer.command(source, spec((0, 1)), Probe(6, 160, 120, True), None, out_dir / "clip.mp4")
    assert command[0] == "/nonexistent/ffmpeg"


def test_command_shape():
    renderer = FfmpegRenderer(PillowCaptioner())
    caption = Caption("Headline", "Details", hold=6, fade=1.5)
    command = renderer.command(
        Path("src.mp4"), spec((12.4, 31), (1, 2.5), caption=caption), Probe(60, 1280, 720, False), Path("cap.png"), Path("out.mp4")
    )
    joined = " ".join(command)
    graph = command[command.index("-filter_complex") + 1]

    assert "-ss 12.400 -t 18.600 -i src.mp4 -ss 1.000 -t 1.500 -i src.mp4" in joined
    assert "-loop 1 -t 7.500 -i cap.png" in joined
    assert "[0:v:0]trim=duration=18.600,setpts=PTS-STARTPTS[v0]" in graph
    assert graph.count("anullsrc") == 2  # no audio in the source
    assert "[v0][a0][v1][a1]concat=n=2:v=1:a=1" in graph
    assert "format=rgba,fade=t=out:st=6.000:d=1.500:alpha=1" in graph
    assert "overlay=0:H-h:eof_action=pass" in graph
    assert "crop" not in graph  # already even
    assert "-c:v libx264 -preset veryfast -crf 20 -pix_fmt yuv420p -c:a aac -b:a 160k -movflags +faststart out.mp4" in joined
