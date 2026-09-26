"""Frame extraction helpers built on FFmpeg.

Samples are produced on demand and reused across stages -- the full source is
never transcoded as an analysis prerequisite (spec 6.1). Grayscale analysis
frames come back as raw planar bytes so no image codec sits between the decoder
and the measurements.
"""

from __future__ import annotations

from fractions import Fraction
from pathlib import Path

import numpy as np

from app.config import Settings, get_settings
from app.media.runner import check_tool, ffmpeg_binary, run_tool
from app.media.timebase import US_PER_SECOND

#: Analysis sampling rate and cap (spec 6.5).
ANALYSIS_FPS = 2.0
MAX_ANALYSIS_FRAMES = 240
ANALYSIS_WIDTH = 320


def _us_to_timestamp(position_us: int) -> str:
    """FFmpeg timestamp with microsecond precision."""
    return f"{position_us / US_PER_SECOND:.6f}"


def _even(value: int) -> int:
    return value if value % 2 == 0 else value - 1


def analysis_dimensions(width: int, height: int, target_width: int = ANALYSIS_WIDTH) -> tuple[int, int]:
    """Scale down preserving aspect ratio, with even dimensions for the decoder."""
    if width <= 0 or height <= 0:
        return target_width, target_width
    if width <= target_width:
        return max(2, _even(width)), max(2, _even(height))
    scale = target_width / width
    return max(2, _even(target_width)), max(2, _even(int(round(height * scale))))


def extract_gray_frames(
    source: Path,
    *,
    start_us: int,
    duration_us: int,
    source_width: int,
    source_height: int,
    fps: float = ANALYSIS_FPS,
    max_frames: int = MAX_ANALYSIS_FRAMES,
    target_width: int = ANALYSIS_WIDTH,
    settings: Settings | None = None,
    should_cancel=None,  # noqa: ANN001
) -> list[np.ndarray]:
    """Decode uniformly spaced grayscale samples from one interval.

    Luma is delivered in the full 8-bit 0-255 range (``format=gray`` with an
    explicit full-range scaler), matching the normalization spec 6.5 requires
    before any measurement is taken.
    """
    settings = settings or get_settings()
    if duration_us <= 0:
        return []

    width, height = analysis_dimensions(source_width, source_height, target_width)

    # Raise the sample rate if the interval is too short to yield two samples,
    # so a brief candidate still gets a motion measurement.
    effective_fps = fps
    expected = int((duration_us / US_PER_SECOND) * fps)
    if expected < 2:
        effective_fps = max(fps, 2.0 * US_PER_SECOND / max(duration_us, 1))

    argv = [
        ffmpeg_binary(settings),
        "-nostdin",
        "-hide_banner",
        "-loglevel", "error",
        "-protocol_whitelist", "file",
        "-ss", _us_to_timestamp(start_us),
        "-t", _us_to_timestamp(duration_us),
        "-i", str(source),
        "-map", "0:v:0",
        # -vsync was removed in FFmpeg 9; -fps_mode is the supported spelling
        # and exists from 5.1 onward, so it works on older images too.
        "-fps_mode", "passthrough",
        "-vf", f"fps={effective_fps},scale={width}:{height}:flags=bilinear,format=gray",
        "-frames:v", str(max_frames),
        "-f", "rawvideo",
        "-pix_fmt", "gray",
        "-",
    ]
    result = check_tool(
        run_tool(argv, timeout_seconds=600, settings=settings, should_cancel=should_cancel),
        "frame extraction",
    )

    frame_bytes = width * height
    if frame_bytes <= 0:
        return []
    buffer = result.stdout
    count = len(buffer) // frame_bytes
    frames = [
        np.frombuffer(buffer, dtype=np.uint8, count=frame_bytes, offset=index * frame_bytes)
        .reshape(height, width)
        for index in range(min(count, max_frames))
    ]
    return frames


def extract_jpeg(
    source: Path,
    destination: Path,
    *,
    position_us: int,
    width: int,
    quality: int = 82,
    settings: Settings | None = None,
    should_cancel=None,  # noqa: ANN001
) -> Path:
    """Write one still frame, with rotation baked into the pixels."""
    settings = settings or get_settings()
    destination.parent.mkdir(parents=True, exist_ok=True)

    argv = [
        ffmpeg_binary(settings),
        "-nostdin",
        "-hide_banner",
        "-loglevel", "error",
        "-protocol_whitelist", "file",
        "-ss", _us_to_timestamp(position_us),
        "-i", str(source),
        "-map", "0:v:0",
        "-frames:v", "1",
        # scale accepts -2 to derive an even height from the aspect ratio.
        "-vf", f"scale={width}:-2:flags=bicubic",
        "-qscale:v", str(_jpeg_qscale(quality)),
        # Strip every source tag: filenames and camera metadata must not
        # travel with an image sent to a provider (spec 6.5).
        "-map_metadata", "-1",
        "-fflags", "+bitexact",
        "-y",
        str(destination),
    ]
    check_tool(
        run_tool(
            argv,
            timeout_seconds=120,
            settings=settings,
            should_cancel=should_cancel,
            capture_stdout=False,
        ),
        "thumbnail extraction",
    )
    return destination


def _jpeg_qscale(quality: int) -> int:
    """Map a 0-100 quality onto FFmpeg's inverted 2-31 qscale."""
    quality = max(1, min(100, quality))
    return max(2, min(31, int(round(31 - (quality / 100) * 29))))


def uniform_positions(start_us: int, end_us: int, count: int) -> list[int]:
    """``count`` uniformly spaced positions strictly inside ``[start, end)``."""
    if count <= 0 or end_us <= start_us:
        return []
    span = end_us - start_us
    if count == 1:
        return [start_us + span // 2]
    step = span / (count + 1)
    return [int(start_us + step * (index + 1)) for index in range(count)]


def representative_frame_count(duration_us: int) -> int:
    """Storyboard sample count by shot length (spec 6.5)."""
    seconds = duration_us / US_PER_SECOND
    if seconds <= 12:
        return 3
    if seconds <= 60:
        return 5
    return 9


def frames_to_seconds(frame_count: int, rate: Fraction) -> float:
    return float(frame_count / rate)
