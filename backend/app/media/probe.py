"""Stage A: probe and normalize source metadata (spec 6.1).

``ffprobe`` is the only authority on whether a file is usable. Extension and
browser-supplied MIME type are hints; a codec counts as supported when the
pinned FFmpeg build can fully probe it *and* decode a bounded smoke-test
interval within the configured resource limits (spec 5.3).
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from fractions import Fraction
from pathlib import Path
from typing import Any

from app.media.runner import (
    MediaToolError,
    check_tool,
    ffmpeg_binary,
    ffprobe_binary,
    run_tool,
)
from app.media.timebase import US_PER_SECOND, TimebaseError, format_rational, parse_rational
from app.config import Settings, get_settings

#: Containers accepted at the API boundary. ffprobe still has the final say.
SUPPORTED_CONTAINER_HINTS = {"mp4", "mov", "mkv", "webm", "m4v"}

#: Length of the decode smoke test (spec 5.3).
SMOKE_TEST_SECONDS = 2.0


class UnsupportedMediaError(Exception):
    """The file is not a video this deployment can process."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(slots=True)
class AudioInfo:
    codec: str
    channels: int
    sample_rate: int
    stream_index: int
    is_default: bool


@dataclass(slots=True)
class MediaInfo:
    duration_us: int
    time_base: str
    coded_width: int
    coded_height: int
    display_width: int
    display_height: int
    rotation_degrees: int
    average_frame_rate: str
    real_frame_rate: str
    is_variable_frame_rate: bool
    video_codec: str
    pixel_format: str
    sample_aspect_ratio: str
    display_aspect_ratio: str
    color_primaries: str | None
    color_transfer: str | None
    color_space: str | None
    is_hdr: bool
    has_audio: bool
    audio: AudioInfo | None
    container_format: str
    size_bytes: int
    warnings: list[str] = field(default_factory=list)

    @property
    def frame_rate(self) -> Fraction:
        return parse_rational(self.average_frame_rate)

    def to_json(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["audio"] = asdict(self.audio) if self.audio else None
        return payload

    def to_public_video(self) -> dict[str, Any]:
        """The ``Job.video`` projection: display-oriented dimensions (spec 9)."""
        return {
            "durationUs": self.duration_us,
            "width": self.display_width,
            "height": self.display_height,
            "averageFrameRate": self.average_frame_rate,
            "hasAudio": self.has_audio,
        }


def _rotation_from_stream(stream: dict[str, Any]) -> int:
    for side_data in stream.get("side_data_list", []) or []:
        if "rotation" in side_data:
            try:
                return int(round(float(side_data["rotation"]))) % 360
            except (TypeError, ValueError):
                continue
    tags = stream.get("tags", {}) or {}
    if "rotate" in tags:
        try:
            return int(round(float(tags["rotate"]))) % 360
        except (TypeError, ValueError):
            return 0
    return 0


def _first_video_stream(streams: list[dict[str, Any]]) -> dict[str, Any] | None:
    for stream in streams:
        if stream.get("codec_type") == "video" and stream.get("disposition", {}).get(
            "attached_pic", 0
        ) != 1:
            return stream
    return None


def _select_audio_stream(streams: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Default audio stream, else the first one (spec 5.7)."""
    audio_streams = [s for s in streams if s.get("codec_type") == "audio"]
    if not audio_streams:
        return None
    for stream in audio_streams:
        if stream.get("disposition", {}).get("default", 0) == 1:
            return stream
    return audio_streams[0]


def run_ffprobe(path: Path, settings: Settings | None = None) -> dict[str, Any]:
    settings = settings or get_settings()
    argv = [
        ffprobe_binary(settings),
        "-v", "error",
        "-hide_banner",
        "-print_format", "json",
        "-show_format",
        "-show_streams",
        "-show_entries", "stream_side_data",
        str(path),
    ]
    result = run_tool(argv, timeout_seconds=120, settings=settings)
    if result.exit_code != 0:
        raise UnsupportedMediaError(
            "media_probe_failed", "The file could not be read as a video."
        )
    try:
        return json.loads(result.stdout.decode("utf-8", errors="replace") or "{}")
    except json.JSONDecodeError as exc:
        raise UnsupportedMediaError(
            "media_probe_failed", "The file could not be read as a video."
        ) from exc


def probe_media(path: Path, settings: Settings | None = None) -> MediaInfo:
    """Collect normalized metadata, rejecting anything unusable."""
    settings = settings or get_settings()
    if not path.is_file():
        raise UnsupportedMediaError("media_missing", "The uploaded file is no longer available.")

    payload = run_ffprobe(path, settings)
    streams: list[dict[str, Any]] = payload.get("streams") or []
    fmt: dict[str, Any] = payload.get("format") or {}

    video = _first_video_stream(streams)
    if video is None:
        raise UnsupportedMediaError(
            "no_video_stream", "The file does not contain a playable video stream."
        )

    duration_text = video.get("duration") or fmt.get("duration")
    try:
        duration_seconds = float(duration_text)
    except (TypeError, ValueError):
        raise UnsupportedMediaError(
            "missing_duration", "The video has no readable duration and cannot be analyzed."
        ) from None
    if duration_seconds <= 0:
        raise UnsupportedMediaError(
            "missing_duration", "The video has no readable duration and cannot be analyzed."
        )
    duration_us = int(round(duration_seconds * US_PER_SECOND))

    warnings: list[str] = []
    try:
        average_rate = parse_rational(video.get("avg_frame_rate"))
    except TimebaseError:
        try:
            average_rate = parse_rational(video.get("r_frame_rate"))
            warnings.append("average_frame_rate_estimated")
        except TimebaseError:
            raise UnsupportedMediaError(
                "missing_frame_rate", "The video has no usable frame rate."
            ) from None
    try:
        real_rate = parse_rational(video.get("r_frame_rate"))
    except TimebaseError:
        real_rate = average_rate

    coded_width = int(video.get("coded_width") or video.get("width") or 0)
    coded_height = int(video.get("coded_height") or video.get("height") or 0)
    width = int(video.get("width") or coded_width)
    height = int(video.get("height") or coded_height)
    if width <= 0 or height <= 0:
        raise UnsupportedMediaError("invalid_dimensions", "The video has unusable dimensions.")

    rotation = _rotation_from_stream(video)
    display_width, display_height = (
        (height, width) if rotation in (90, 270) else (width, height)
    )

    # A large avg/real frame-rate divergence indicates variable frame timing.
    is_vfr = average_rate != real_rate and abs(float(average_rate) - float(real_rate)) > 0.01
    if is_vfr:
        warnings.append("variable_frame_rate")

    audio_stream = _select_audio_stream(streams)
    audio = None
    if audio_stream is not None:
        audio = AudioInfo(
            codec=str(audio_stream.get("codec_name") or "unknown"),
            channels=int(audio_stream.get("channels") or 0),
            sample_rate=int(audio_stream.get("sample_rate") or 0),
            stream_index=int(audio_stream.get("index") or 0),
            is_default=bool(audio_stream.get("disposition", {}).get("default", 0) == 1),
        )

    transfer = video.get("color_transfer")
    primaries = video.get("color_primaries")
    is_hdr = transfer in {"smpte2084", "arib-std-b67"} or primaries == "bt2020"
    if is_hdr:
        warnings.append("hdr_source_tonemapped_on_export")

    info = MediaInfo(
        duration_us=duration_us,
        time_base=str(video.get("time_base") or "1/1000"),
        coded_width=coded_width or width,
        coded_height=coded_height or height,
        display_width=display_width,
        display_height=display_height,
        rotation_degrees=rotation,
        average_frame_rate=format_rational(average_rate),
        real_frame_rate=format_rational(real_rate),
        is_variable_frame_rate=is_vfr,
        video_codec=str(video.get("codec_name") or "unknown"),
        pixel_format=str(video.get("pix_fmt") or "unknown"),
        sample_aspect_ratio=str(video.get("sample_aspect_ratio") or "1:1"),
        display_aspect_ratio=str(video.get("display_aspect_ratio") or ""),
        color_primaries=primaries,
        color_transfer=transfer,
        color_space=video.get("color_space"),
        is_hdr=is_hdr,
        has_audio=audio is not None,
        audio=audio,
        container_format=str(fmt.get("format_name") or "unknown"),
        size_bytes=int(fmt.get("size") or path.stat().st_size),
        warnings=warnings,
    )
    return info


def decode_smoke_test(path: Path, settings: Settings | None = None) -> None:
    """Decode a bounded interval to prove the codec is actually usable."""
    settings = settings or get_settings()
    argv = [
        ffmpeg_binary(settings),
        "-nostdin",
        "-hide_banner",
        "-loglevel", "error",
        "-protocol_whitelist", "file",
        "-t", str(SMOKE_TEST_SECONDS),
        "-i", str(path),
        "-map", "0:v:0",
        "-f", "null",
        "-",
    ]
    try:
        result = run_tool(argv, timeout_seconds=120, settings=settings, capture_stdout=False)
        check_tool(result, "decode smoke test")
    except MediaToolError as exc:
        raise UnsupportedMediaError(
            "decode_failed",
            "The video could not be decoded. It may be corrupt or use an unsupported codec.",
        ) from exc


def verify_source(path: Path, settings: Settings | None = None) -> MediaInfo:
    """Full verification: probe, then prove decodability."""
    info = probe_media(path, settings)
    decode_smoke_test(path, settings)
    return info
