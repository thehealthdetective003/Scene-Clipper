"""Clip rendering: previews, provider proxies, and frame-accurate exports.

Export rules (spec 5.7):

* H.264 video and AAC audio in MP4; never a keyframe-limited stream copy;
* ``libx264``, CRF 20, ``medium`` preset, ``yuv420p``, MP4 fast-start;
* AAC at 192 kbps from the default audio stream, else the first one;
* timestamp-based trims with both output timelines reset to zero;
* the source display aspect ratio and decoded-frame cadence are preserved,
  rotation is baked into the pixels, rotation metadata is cleared, and the
  sample aspect ratio is normalized to 1:1;
* every output is validated with ``ffprobe`` and a decode pass before it is
  published.
"""

from __future__ import annotations

import hashlib
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path
from typing import Any

from PIL import ImageFont

from app.config import Settings, get_settings
from app.media.probe import MediaInfo, probe_media
from app.media.runner import check_tool, ffmpeg_binary, run_tool
from app.media.timebase import US_PER_SECOND, frame_duration_us_ceil, parse_rational
from app.source_labels import font_path, normalize_source_name, normalize_style

#: Resolution presets (spec 5.7).
RESOLUTION_BOUNDS = {
    "original": None,
    "max1080p": (1920, 1080),
    "max720p": (1280, 720),
}

EXPORT_CRF = 20
EXPORT_PRESET = "medium"
EXPORT_PIX_FMT = "yuv420p"
AUDIO_BITRATE = "192k"

PREVIEW_WIDTH = 640
PREVIEW_CRF = 28

#: Provider proxies are capped at 640x360 and always silent (spec 6.9).
PROXY_MAX_WIDTH = 640
PROXY_MAX_HEIGHT = 360
PROXY_CRF = 30


class ClipRenderError(RuntimeError):
    """Rendering or post-render validation failed; nothing is published."""


@dataclass(frozen=True, slots=True)
class RenderedClip:
    path: Path
    width: int
    height: int
    size_bytes: int
    sha256: str
    duration_us: int


def _even(value: int) -> int:
    """Encoders require even dimensions for yuv420p."""
    return max(2, value - (value % 2))


def target_dimensions(info: MediaInfo, resolution: str) -> tuple[int, int]:
    """Resolve a preset to concrete, even output dimensions.

    No preset may exceed either source dimension, so a 720p request against a
    480p source simply produces 480p -- the source is never upscaled.
    """
    source_width, source_height = info.display_width, info.display_height
    bounds = RESOLUTION_BOUNDS.get(resolution)
    if bounds is None:
        return _even(source_width), _even(source_height)

    max_width, max_height = bounds
    scale = min(max_width / source_width, max_height / source_height, 1.0)
    return _even(int(round(source_width * scale))), _even(int(round(source_height * scale)))


def _timestamp(position_us: int) -> str:
    return f"{position_us / US_PER_SECOND:.6f}"


def _video_filters(
    width: int,
    height: int,
    *,
    reset_timeline: bool = True,
    source_label_filter: str | None = None,
) -> str:
    # setsar=1 normalizes the sample aspect ratio; because the scale target is
    # the *display* size, the display aspect ratio is preserved exactly.
    parts = [f"scale={width}:{height}:flags=lanczos"]
    if source_label_filter:
        # Draw after scaling so size and margins are relative to the output.
        parts.append(source_label_filter)
    parts.append("setsar=1")
    if reset_timeline:
        # Rebase the output timeline to zero. Container-level flags alone leave
        # the first frame at its source offset when the cut point is not on a
        # keyframe, so the reset is done in the filtergraph where it is exact.
        parts.append("setpts=PTS-STARTPTS")
    return ",".join(parts)


def _filter_path(path: Path) -> str:
    """Escape an absolute path for a quoted FFmpeg filter option."""
    value = str(path.resolve()).replace("\\", "/")
    for char in ("\\", ":", "'", ",", ";", "[", "]"):
        value = value.replace(char, f"\\{char}")
    return value


def _font_size_that_fits(
    text: str, font_file: Path, desired: int, available_width: int
) -> tuple[int, int]:
    """Return the largest font size no greater than ``desired`` that fits."""

    def measurement(size: int) -> tuple[int, int]:
        outline = max(1, round(size * 0.06))
        font = ImageFont.truetype(str(font_file), size=size)
        left, _top, right, _bottom = font.getbbox(text, stroke_width=outline)
        return max(0, right - left), outline

    low, high = 1, max(1, desired)
    winner, winner_outline = 1, 1
    while low <= high:
        middle = (low + high) // 2
        measured, outline = measurement(middle)
        if measured <= available_width:
            winner, winner_outline = middle, outline
            low = middle + 1
        else:
            high = middle - 1
    return winner, winner_outline


@contextmanager
def _source_label_filter(
    source_label: dict[str, Any] | None,
    *,
    width: int,
    height: int,
    workspace: Path,
) -> Iterator[str | None]:
    """Create a safe drawtext filter and short-lived UTF-8 text file."""
    if not source_label:
        yield None
        return

    try:
        text = normalize_source_name(str(source_label.get("text", "")))
        style = normalize_style(dict(source_label.get("style") or {}))
    except (TypeError, ValueError) as exc:
        raise ClipRenderError("The snapshotted source label is invalid.") from exc
    if not text:
        yield None
        return

    selected_font = font_path(str(style["fontPreset"]))
    if not selected_font.is_file():
        raise ClipRenderError("A bundled source-label font is missing.")

    x = max(1, round(width * 0.0075))
    y = max(1, round(height * 0.01))
    desired = max(1, round(height * float(style["sizePercent"]) / 100.0))
    try:
        font_size, outline = _font_size_that_fits(
            text, selected_font, desired, max(1, width - (2 * x))
        )
    except OSError as exc:
        raise ClipRenderError("A bundled source-label font could not be read.") from exc

    workspace.mkdir(parents=True, exist_ok=True)
    handle = tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        newline="",
        prefix="source-label-",
        suffix=".txt",
        dir=workspace,
        delete=False,
    )
    text_path = Path(handle.name)
    try:
        with handle:
            handle.write(text)
        fill = str(style["fillColor"]).removeprefix("#")
        border = str(style["outlineColor"]).removeprefix("#")
        yield (
            "drawtext="
            f"fontfile='{_filter_path(selected_font)}':"
            f"textfile='{_filter_path(text_path)}':"
            "expansion=none:"
            f"fontsize={font_size}:fontcolor=0x{fill}:"
            f"borderw={outline}:bordercolor=0x{border}:"
            f"x={x}:y={y}:fix_bounds=1"
        )
    finally:
        text_path.unlink(missing_ok=True)


#: Audio counterpart: align to zero, then rebase, so both streams start together.
_AUDIO_FILTERS = "aresample=async=1:first_pts=0,asetpts=PTS-STARTPTS"


def render_export_clip(
    source: Path,
    destination: Path,
    *,
    info: MediaInfo,
    start_us: int,
    end_us: int,
    resolution: str,
    include_audio: bool,
    source_label: dict[str, Any] | None = None,
    settings: Settings | None = None,
    should_cancel=None,  # noqa: ANN001
) -> RenderedClip:
    """Encode one frame-accurate clip and validate it before returning."""
    settings = settings or get_settings()
    duration_us = end_us - start_us
    if duration_us <= 0:
        raise ClipRenderError("Refusing to render an empty interval.")

    width, height = target_dimensions(info, resolution)
    destination.parent.mkdir(parents=True, exist_ok=True)

    wants_audio = include_audio and info.has_audio and info.audio is not None
    with _source_label_filter(
        source_label, width=width, height=height, workspace=destination.parent
    ) as label_filter:
        argv: list[str] = [
            ffmpeg_binary(settings),
            "-nostdin",
            "-hide_banner",
            "-loglevel", "error",
            "-protocol_whitelist", "file",
            # Input seek: FFmpeg decodes and discards up to the target, so the cut
            # lands on the exact frame rather than the preceding keyframe.
            "-ss", _timestamp(start_us),
            "-t", _timestamp(duration_us),
            "-i", str(source),
            "-map", "0:v:0",
        ]

        if wants_audio:
            # Default audio stream if one is flagged, otherwise the first.
            argv += ["-map", f"0:{info.audio.stream_index}"]

        argv += [
            "-vf", _video_filters(width, height, source_label_filter=label_filter),
            "-c:v", "libx264",
            "-crf", str(EXPORT_CRF),
            "-preset", EXPORT_PRESET,
            "-pix_fmt", EXPORT_PIX_FMT,
            "-profile:v", "high",
            # Preserve the decoded cadence rather than forcing CFR on a VFR source.
            "-fps_mode", "vfr",
        ]

        if wants_audio:
            argv += ["-c:a", "aac", "-b:a", AUDIO_BITRATE, "-af", _AUDIO_FILTERS]
        else:
            argv += ["-an"]

        # Rotation is baked into the pixels; metadata and chapters stay out of
        # every generated clip.  -avoid_negative_ts remains deliberately absent.
        argv += [
            "-metadata:s:v:0", "rotate=0",
            "-map_metadata", "-1",
            "-map_chapters", "-1",
            "-movflags", "+faststart",
            "-y",
            str(destination),
        ]

        check_tool(
            run_tool(
                argv,
                timeout_seconds=settings.media_timeout_seconds,
                settings=settings,
                should_cancel=should_cancel,
                capture_stdout=False,
            ),
            "clip export",
        )
    return validate_output(
        destination,
        expected_width=width,
        expected_height=height,
        expect_audio=wants_audio,
        expected_duration_us=duration_us,
        source_rate=info.frame_rate,
        settings=settings,
        should_cancel=should_cancel,
    )


def render_preview(
    source: Path,
    destination: Path,
    *,
    info: MediaInfo,
    start_us: int,
    end_us: int,
    source_label: dict[str, Any] | None = None,
    settings: Settings | None = None,
    should_cancel=None,  # noqa: ANN001
) -> Path:
    """A small review preview. Audio is kept so trims can be judged."""
    settings = settings or get_settings()
    duration_us = end_us - start_us
    if duration_us <= 0:
        raise ClipRenderError("Refusing to render an empty preview.")

    width = min(PREVIEW_WIDTH, _even(info.display_width))
    height = _even(int(round(width * info.display_height / max(1, info.display_width))))
    destination.parent.mkdir(parents=True, exist_ok=True)

    with _source_label_filter(
        source_label, width=width, height=height, workspace=destination.parent
    ) as label_filter:
        argv = [
            ffmpeg_binary(settings),
            "-nostdin", "-hide_banner", "-loglevel", "error",
            "-protocol_whitelist", "file",
            "-ss", _timestamp(start_us),
            "-t", _timestamp(duration_us),
            "-i", str(source),
            "-map", "0:v:0",
        ]
        if info.has_audio and info.audio is not None:
            argv += [
                "-map", f"0:{info.audio.stream_index}",
                "-c:a", "aac", "-b:a", "96k",
                "-af", _AUDIO_FILTERS,
            ]
        else:
            argv += ["-an"]

        argv += [
            "-vf", _video_filters(width, height, source_label_filter=label_filter),
            "-c:v", "libx264",
            "-crf", str(PREVIEW_CRF),
            "-preset", "veryfast",
            "-pix_fmt", EXPORT_PIX_FMT,
            "-fps_mode", "vfr",
            "-metadata:s:v:0", "rotate=0",
            "-map_metadata", "-1",
            "-movflags", "+faststart",
            "-y",
            str(destination),
        ]
        check_tool(
            run_tool(
                argv,
                timeout_seconds=600,
                settings=settings,
                should_cancel=should_cancel,
                capture_stdout=False,
            ),
            "preview render",
        )
    return destination


def render_proxy(
    source: Path,
    destination: Path,
    *,
    info: MediaInfo,
    start_us: int,
    end_us: int,
    settings: Settings | None = None,
    should_cancel=None,  # noqa: ANN001
) -> Path:
    """A short, silent, low-resolution excerpt for the rare proxy fallback.

    Audio is always stripped: audio and transcription analysis are outside the
    MVP, and no audio should ever leave the deployment (spec 6.9).
    """
    settings = settings or get_settings()
    duration_us = end_us - start_us
    if duration_us <= 0:
        raise ClipRenderError("Refusing to render an empty proxy.")

    scale = min(
        PROXY_MAX_WIDTH / max(1, info.display_width),
        PROXY_MAX_HEIGHT / max(1, info.display_height),
        1.0,
    )
    width = _even(int(round(info.display_width * scale)))
    height = _even(int(round(info.display_height * scale)))
    destination.parent.mkdir(parents=True, exist_ok=True)

    argv = [
        ffmpeg_binary(settings),
        "-nostdin", "-hide_banner", "-loglevel", "error",
        "-protocol_whitelist", "file",
        "-ss", _timestamp(start_us),
        "-t", _timestamp(duration_us),
        "-i", str(source),
        "-map", "0:v:0",
        "-an",
        "-vf", _video_filters(width, height),
        "-c:v", "libx264",
        "-crf", str(PROXY_CRF),
        "-preset", "veryfast",
        "-pix_fmt", EXPORT_PIX_FMT,
        "-fps_mode", "vfr",
        "-metadata:s:v:0", "rotate=0",
        "-map_metadata", "-1",
        "-movflags", "+faststart",
        "-y",
        str(destination),
    ]
    check_tool(
        run_tool(
            argv,
            timeout_seconds=600,
            settings=settings,
            should_cancel=should_cancel,
            capture_stdout=False,
        ),
        "proxy render",
    )
    return destination


def validate_output(
    path: Path,
    *,
    expected_width: int,
    expected_height: int,
    expect_audio: bool,
    expected_duration_us: int,
    source_rate: Fraction,
    settings: Settings | None = None,
    should_cancel=None,  # noqa: ANN001
) -> RenderedClip:
    """ffprobe checks plus a full decode pass (spec 5.7, 13.7)."""
    settings = settings or get_settings()
    if not path.is_file() or path.stat().st_size == 0:
        raise ClipRenderError("The encoder produced no output.")

    info = probe_media(path, settings)

    if info.video_codec != "h264":
        raise ClipRenderError("The exported clip is not H.264.")
    if (info.display_width, info.display_height) != (expected_width, expected_height):
        raise ClipRenderError("The exported clip has unexpected dimensions.")
    if expect_audio and not info.has_audio:
        raise ClipRenderError("The exported clip is missing its audio stream.")
    if not expect_audio and info.has_audio:
        raise ClipRenderError("A muted export must contain no audio stream.")
    if info.rotation_degrees not in (0, 360):
        raise ClipRenderError("The exported clip still carries rotation metadata.")

    # Duration may move by at most one source frame through quantization.
    tolerance_us = max(frame_duration_us_ceil(source_rate), 40_000)
    if abs(info.duration_us - expected_duration_us) > tolerance_us:
        raise ClipRenderError("The exported clip duration is outside tolerance.")

    _decode_fully(path, settings, should_cancel)
    if expect_audio:
        _assert_stream_sync(path, source_rate, settings)

    return RenderedClip(
        path=path,
        width=info.display_width,
        height=info.display_height,
        size_bytes=path.stat().st_size,
        sha256=sha256_file(path),
        duration_us=info.duration_us,
    )


def _decode_fully(path: Path, settings: Settings, should_cancel=None) -> None:  # noqa: ANN001
    """Decode every frame to prove the file is not truncated or corrupt."""
    argv = [
        ffmpeg_binary(settings),
        "-nostdin", "-hide_banner", "-loglevel", "error",
        "-protocol_whitelist", "file",
        "-xerror",
        "-i", str(path),
        "-f", "null",
        "-",
    ]
    result = run_tool(
        argv,
        timeout_seconds=600,
        settings=settings,
        should_cancel=should_cancel,
        capture_stdout=False,
    )
    if result.exit_code != 0 or result.stderr.strip():
        raise ClipRenderError("The exported clip failed its decode validation.")


def _assert_stream_sync(path: Path, source_rate: Fraction, settings: Settings) -> None:
    """Both streams start at zero, and their ends agree within tolerance.

    Spec 5.7: end times may differ by no more than the greater of one video
    frame duration or one encoded audio packet duration.
    """
    from app.media.probe import run_ffprobe

    payload = run_ffprobe(path, settings)
    streams = payload.get("streams") or []
    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)
    if video is None or audio is None:
        return

    def _seconds(stream: dict, key: str, default: float = 0.0) -> float:
        try:
            return float(stream.get(key))
        except (TypeError, ValueError):
            return default

    video_start = _seconds(video, "start_time")
    audio_start = _seconds(audio, "start_time")

    video_frame_seconds = float(frame_duration_us_ceil(source_rate)) / US_PER_SECOND
    try:
        audio_rate = parse_rational(audio.get("sample_rate") or 48000)
        # AAC-LC encodes 1024 samples per packet.
        audio_packet_seconds = 1024.0 / float(audio_rate)
    except Exception:  # noqa: BLE001
        audio_packet_seconds = 1024.0 / 48000.0

    start_tolerance = max(video_frame_seconds, audio_packet_seconds)
    if abs(video_start) > start_tolerance or abs(audio_start) > start_tolerance:
        raise ClipRenderError("The exported streams do not both start at zero.")

    video_end = video_start + _seconds(video, "duration")
    audio_end = audio_start + _seconds(audio, "duration")
    if abs(video_end - audio_end) > max(video_frame_seconds, audio_packet_seconds):
        raise ClipRenderError("The exported audio and video end times are out of sync.")


def sha256_file(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            hasher.update(block)
    return hasher.hexdigest()
