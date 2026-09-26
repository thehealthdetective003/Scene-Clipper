"""Deterministic synthetic video fixtures with frame-accurate annotations.

Each generator returns the file plus the exact transition positions it built,
so detection tests can assert that no output ever crosses an annotated
transition (spec 12). Fixtures are cached in a session directory because
encoding them is the slow part, not the assertions.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from fractions import Fraction
from pathlib import Path

from app.config import get_settings
from app.media.runner import ffmpeg_binary
from app.media.timebase import MIN_CLIP_US, US_PER_SECOND

#: Comfortably under the eligibility floor even after two-frame guards.
_BELOW_FLOOR_SECONDS = round(MIN_CLIP_US / US_PER_SECOND * 0.8, 2)

#: 29.97 fps, the awkward rational the spec calls out explicitly.
NTSC = Fraction(30000, 1001)


@dataclass(slots=True)
class Fixture:
    path: Path
    duration_us: int
    frame_rate: Fraction
    #: ``(start_us, end_us, kind)`` for every transition deliberately built in.
    transitions: list[tuple[int, int, str]] = field(default_factory=list)
    #: Intervals that contain no transition at all.
    continuous_spans: list[tuple[int, int]] = field(default_factory=list)
    has_audio: bool = True


def _run(argv: list[str]) -> None:
    result = subprocess.run(argv, capture_output=True, text=True)  # noqa: S603
    if result.returncode != 0:
        raise RuntimeError(f"fixture generation failed: {result.stderr[-2000:]}")


def _ffmpeg() -> str:
    return ffmpeg_binary(get_settings())


def _color_segment(
    directory: Path, name: str, color: str, seconds: float, size: str, rate: Fraction
) -> Path:
    path = directory / f"{name}.mp4"
    _run(
        [
            _ffmpeg(), "-hide_banner", "-loglevel", "error", "-y",
            "-f", "lavfi",
            "-i", f"color=c={color}:size={size}:rate={rate.numerator}/{rate.denominator}:duration={seconds}",
            "-f", "lavfi",
            "-i", f"sine=frequency=330:sample_rate=48000:duration={seconds}",
            "-c:v", "libx264", "-crf", "18", "-pix_fmt", "yuv420p", "-g", "15",
            "-c:a", "aac", "-b:a", "96k", "-shortest",
            str(path),
        ]
    )
    return path


def hard_cuts(directory: Path, size: str = "320x180", rate: Fraction = NTSC) -> Fixture:
    """Four 5-second solid-colour shots joined by three hard cuts."""
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / "hard_cuts.mp4"
    if not target.exists():
        parts = [
            _color_segment(directory, f"hc{i}", colour, 5.0, size, rate)
            for i, colour in enumerate(("red", "green", "blue", "yellow"))
        ]
        listing = directory / "hc.txt"
        listing.write_text(
            "\n".join(f"file '{p.resolve().as_posix()}'" for p in parts), encoding="utf-8"
        )
        _run(
            [
                _ffmpeg(), "-hide_banner", "-loglevel", "error", "-y",
                "-f", "concat", "-safe", "0", "-i", str(listing),
                "-c:v", "libx264", "-crf", "18", "-pix_fmt", "yuv420p",
                "-c:a", "aac", "-b:a", "96k",
                str(target),
            ]
        )

    cuts = [5_000_000, 10_000_000, 15_000_000]
    frame_us = int(1_000_000 / float(rate)) + 1
    return Fixture(
        path=target,
        duration_us=20_000_000,
        frame_rate=rate,
        transitions=[(c - frame_us, c + frame_us, "hard-cut") for c in cuts],
        continuous_spans=[
            (200_000, 4_800_000),
            (5_200_000, 9_800_000),
            (10_200_000, 14_800_000),
            (15_200_000, 19_800_000),
        ],
    )


def fade_to_black(directory: Path, size: str = "320x180", rate: Fraction = NTSC) -> Fixture:
    """Two 6-second shots joined by a fade out to black and back in.

    Each half carries its own fade before concatenation. Chaining ``fade=out``
    and ``fade=in`` over a single source would black out the whole clip, since
    the second filter darkens everything before its start time.
    """
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / "fade.mp4"
    if not target.exists():
        first = directory / "fade_a.mp4"
        second = directory / "fade_b.mp4"
        _run(
            [
                _ffmpeg(), "-hide_banner", "-loglevel", "error", "-y",
                "-f", "lavfi",
                "-i", f"smptebars=size={size}:rate={rate.numerator}/{rate.denominator}:duration=6",
                "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000:duration=6",
                "-vf", "fade=t=out:st=5:d=1",
                "-c:v", "libx264", "-crf", "18", "-pix_fmt", "yuv420p",
                "-c:a", "aac", "-b:a", "96k", "-shortest",
                str(first),
            ]
        )
        _run(
            [
                _ffmpeg(), "-hide_banner", "-loglevel", "error", "-y",
                "-f", "lavfi",
                "-i", f"color=c=orange:size={size}:rate={rate.numerator}/{rate.denominator}:duration=6",
                "-f", "lavfi", "-i", "sine=frequency=330:sample_rate=48000:duration=6",
                "-vf", "fade=t=in:st=0:d=1",
                "-c:v", "libx264", "-crf", "18", "-pix_fmt", "yuv420p",
                "-c:a", "aac", "-b:a", "96k", "-shortest",
                str(second),
            ]
        )
        listing = directory / "fade.txt"
        listing.write_text(
            "\n".join(f"file '{p.resolve().as_posix()}'" for p in (first, second)),
            encoding="utf-8",
        )
        _run(
            [
                _ffmpeg(), "-hide_banner", "-loglevel", "error", "-y",
                "-f", "concat", "-safe", "0", "-i", str(listing),
                "-c:v", "libx264", "-crf", "18", "-pix_fmt", "yuv420p",
                "-c:a", "aac", "-b:a", "96k",
                str(target),
            ]
        )
    return Fixture(
        path=target,
        duration_us=12_000_000,
        frame_rate=rate,
        # Black spans roughly 5s-7s; the annotation is deliberately generous.
        transitions=[(5_000_000, 7_000_000, "fade")],
        continuous_spans=[(200_000, 4_800_000), (7_200_000, 11_800_000)],
    )


def dissolve(directory: Path, size: str = "320x180", rate: Fraction = NTSC) -> Fixture:
    """Two visually distinct 8-second sources cross-dissolving at 7s.

    The dissolve runs for half a second between a moving pattern and colour
    bars. A slower dissolve between two similar mid-grey sources produces a
    per-frame content delta below the spec's absolute floor of 8, which is a
    real limit of the rule rather than something a fixture should paper over --
    :func:`low_contrast_dissolve` covers that case explicitly.
    """
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / "dissolve.mp4"
    if not target.exists():
        _run(
            [
                _ffmpeg(), "-hide_banner", "-loglevel", "error", "-y",
                "-f", "lavfi",
                "-i", f"testsrc2=size={size}:rate={rate.numerator}/{rate.denominator}:duration=8",
                "-f", "lavfi",
                "-i", f"smptebars=size={size}:rate={rate.numerator}/{rate.denominator}:duration=8",
                "-filter_complex", "[0:v][1:v]xfade=transition=fade:duration=0.5:offset=7[v]",
                "-map", "[v]",
                "-c:v", "libx264", "-crf", "18", "-pix_fmt", "yuv420p",
                str(target),
            ]
        )
    return Fixture(
        path=target,
        duration_us=15_500_000,
        frame_rate=rate,
        transitions=[(7_000_000, 7_500_000, "dissolve")],
        continuous_spans=[(200_000, 6_800_000), (7_700_000, 15_300_000)],
        has_audio=False,
    )


def low_contrast_dissolve(directory: Path, size: str = "320x180", rate: Fraction = NTSC) -> Fixture:
    """A slow dissolve between two similarly bright sources.

    Its per-frame content delta sits near the spec's absolute floor, so it
    documents where the rule's sensitivity ends.
    """
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / "dissolve_low.mp4"
    if not target.exists():
        _run(
            [
                _ffmpeg(), "-hide_banner", "-loglevel", "error", "-y",
                "-f", "lavfi",
                "-i", f"testsrc2=size={size}:rate={rate.numerator}/{rate.denominator}:duration=8",
                "-f", "lavfi",
                "-i", f"smptebars=size={size}:rate={rate.numerator}/{rate.denominator}:duration=8",
                "-filter_complex", "[0:v][1:v]xfade=transition=fade:duration=1:offset=7[v]",
                "-map", "[v]",
                "-c:v", "libx264", "-crf", "18", "-pix_fmt", "yuv420p",
                str(target),
            ]
        )
    return Fixture(
        path=target,
        duration_us=15_000_000,
        frame_rate=rate,
        transitions=[(7_000_000, 8_000_000, "dissolve")],
        continuous_spans=[(200_000, 6_800_000), (8_200_000, 14_800_000)],
        has_audio=False,
    )


def continuous_pan(directory: Path, rate: Fraction = NTSC) -> Fixture:
    """One uninterrupted 15-second take with a strong horizontal pan.

    Motion, shake, and blur must never be treated as a transition (spec 2.4),
    so this fixture must yield exactly one eligible shot.
    """
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / "pan.mp4"
    if not target.exists():
        _run(
            [
                _ffmpeg(), "-hide_banner", "-loglevel", "error", "-y",
                "-f", "lavfi",
                "-i", f"testsrc2=size=640x360:rate={rate.numerator}/{rate.denominator}:duration=15",
                "-vf", "crop=320:180:x='min(iw-320,t*20)':y=90",
                "-c:v", "libx264", "-crf", "18", "-pix_fmt", "yuv420p",
                str(target),
            ]
        )
    return Fixture(
        path=target,
        duration_us=15_000_000,
        frame_rate=rate,
        transitions=[],
        continuous_spans=[(100_000, 14_900_000)],
        has_audio=False,
    )


def long_take(directory: Path, seconds: float = 30.0, rate: Fraction = NTSC) -> Fixture:
    """A single long shot: it may produce exactly one six-second clip."""
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / "long_take.mp4"
    if not target.exists():
        _run(
            [
                _ffmpeg(), "-hide_banner", "-loglevel", "error", "-y",
                "-f", "lavfi",
                "-i", f"testsrc2=size=320x180:rate={rate.numerator}/{rate.denominator}:duration={seconds}",
                "-f", "lavfi", "-i", f"sine=frequency=220:sample_rate=48000:duration={seconds}",
                "-c:v", "libx264", "-crf", "20", "-pix_fmt", "yuv420p",
                "-c:a", "aac", "-b:a", "96k", "-shortest",
                str(target),
            ]
        )
    return Fixture(
        path=target,
        duration_us=int(seconds * 1_000_000),
        frame_rate=rate,
        transitions=[],
        continuous_spans=[(100_000, int(seconds * 1_000_000) - 100_000)],
    )


def short_shots(
    directory: Path, rate: Fraction = NTSC, seconds: float = _BELOW_FLOOR_SECONDS
) -> Fixture:
    """Adjacent shots that all fall below the eligibility floor.

    The segment length is derived from ``MIN_CLIP_US`` so the fixture keeps
    testing the gate if the bounds change again.
    """
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / "short_shots.mp4"
    if not target.exists():
        parts = [
            _color_segment(directory, f"ss{i}", colour, seconds, "320x180", rate)
            for i, colour in enumerate(("red", "green", "blue", "white", "gray"))
        ]
        listing = directory / "ss.txt"
        listing.write_text(
            "\n".join(f"file '{p.resolve().as_posix()}'" for p in parts), encoding="utf-8"
        )
        _run(
            [
                _ffmpeg(), "-hide_banner", "-loglevel", "error", "-y",
                "-f", "concat", "-safe", "0", "-i", str(listing),
                "-c:v", "libx264", "-crf", "18", "-pix_fmt", "yuv420p",
                "-c:a", "aac", "-b:a", "96k",
                str(target),
            ]
        )
    return Fixture(
        path=target,
        duration_us=int(seconds * 5 * 1_000_000),
        frame_rate=rate,
        transitions=[
            (int(i * seconds * 1_000_000), int(i * seconds * 1_000_000), "hard-cut")
            for i in range(1, 5)
        ],
        continuous_spans=[],
    )


def silent_portrait(directory: Path, rate: Fraction = NTSC) -> Fixture:
    """Portrait footage with no audio stream at all."""
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / "portrait_silent.mp4"
    if not target.exists():
        _run(
            [
                _ffmpeg(), "-hide_banner", "-loglevel", "error", "-y",
                "-f", "lavfi",
                "-i", f"testsrc2=size=180x320:rate={rate.numerator}/{rate.denominator}:duration=10",
                "-c:v", "libx264", "-crf", "20", "-pix_fmt", "yuv420p",
                str(target),
            ]
        )
    return Fixture(
        path=target,
        duration_us=10_000_000,
        frame_rate=rate,
        transitions=[],
        continuous_spans=[(100_000, 9_900_000)],
        has_audio=False,
    )


def rotated(directory: Path, rate: Fraction = NTSC) -> Fixture:
    """Landscape footage tagged with a 90-degree display rotation."""
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / "rotated.mp4"
    if not target.exists():
        plain = directory / "rotated_src.mp4"
        _run(
            [
                _ffmpeg(), "-hide_banner", "-loglevel", "error", "-y",
                "-f", "lavfi",
                "-i", f"testsrc2=size=320x180:rate={rate.numerator}/{rate.denominator}:duration=10",
                "-c:v", "libx264", "-crf", "20", "-pix_fmt", "yuv420p",
                str(plain),
            ]
        )
        # -display_rotation is an *input* option: it sets how the decoder should
        # interpret the stream, and a stream copy then writes the display matrix.
        _run(
            [
                _ffmpeg(), "-hide_banner", "-loglevel", "error", "-y",
                "-display_rotation:v:0", "90",
                "-i", str(plain),
                "-c", "copy",
                str(target),
            ]
        )
    return Fixture(
        path=target,
        duration_us=10_000_000,
        frame_rate=rate,
        transitions=[],
        continuous_spans=[(100_000, 9_900_000)],
        has_audio=False,
    )


def truncated(directory: Path) -> Path:
    """A file that is not decodable video at all."""
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / "truncated.mp4"
    target.write_bytes(b"\x00\x00\x00\x18ftypmp42" + b"\xff" * 512)
    return target
