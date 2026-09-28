"""Download one public video URL into the existing upload pipeline with yt-dlp."""

from __future__ import annotations

import ipaddress
import socket
import time
import unicodedata
from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import yt_dlp
from yt_dlp.utils import DownloadError

from app.config import Settings, get_settings
from app.db import session_scope
from app.logging_setup import get_logger
from app.media.runner import ffmpeg_binary
from app.models import Upload
from app.services import storage, uploads
from app.source_labels import SOURCE_NAME_MAX_CHARS, normalize_source_name
from app.util.ids import new_id

logger = get_logger("app.downloading")

_PROGRESS_INTERVAL_SECONDS = 0.75
_MEDIA_EXTENSIONS = {
    ".3gp", ".avi", ".flv", ".m4v", ".mkv", ".mov", ".mp4", ".mpeg", ".mpg", ".ts", ".webm"
}


class UrlDownloadError(RuntimeError):
    def __init__(self, code: str, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable


class UrlDownloadCancelled(RuntimeError):
    pass


class _QuietLogger:
    """yt-dlp messages often contain signed URLs; never copy them into our log."""

    def debug(self, _message: str) -> None:
        return

    def warning(self, _message: str) -> None:
        return

    def error(self, _message: str) -> None:
        return


def run_url_download(
    upload_id: str,
    *,
    should_cancel: Callable[[], bool] | None = None,
    heartbeat: Callable[[], None] | None = None,
) -> None:
    """Download, atomically publish, then enqueue ordinary media verification."""
    settings = get_settings()
    with session_scope() as db:
        upload = db.get(Upload, upload_id)
        if upload is None or upload.deleted_at is not None or upload.state != "downloading":
            return
        if upload.source_kind != "url" or not upload.source_url:
            uploads.fail_upload(
                db,
                upload,
                "download_source_missing",
                "The saved video link is unavailable. Submit the link again.",
                phase="downloading",
            )
            return
        source_url = uploads.normalize_video_url(upload.source_url)

    try:
        _assert_public_origin(source_url)
        storage.require_free_space(min(settings.max_upload_bytes, 512 * 1024 * 1024), settings)
        with storage.attempt_directory(
            storage.upload_dir(upload_id), new_id(), settings
        ) as workspace:
            title, suggested_source_name, downloaded = _download(
                source_url,
                workspace,
                upload_id=upload_id,
                settings=settings,
                should_cancel=should_cancel,
                heartbeat=heartbeat,
            )
            if should_cancel is not None and should_cancel():
                raise UrlDownloadCancelled()

            size_bytes = downloaded.stat().st_size
            if size_bytes <= 0:
                raise UrlDownloadError(
                    "download_empty", "The video site returned an empty file."
                )
            if size_bytes > settings.max_upload_bytes:
                raise UrlDownloadError(
                    "download_too_large",
                    "The highest-quality video exceeds this server's upload-size limit.",
                )
            extension = storage.sanitize_extension(downloaded.name, default="bin")
            destination = f"{storage.upload_dir(upload_id)}/source.{extension}"
            storage.atomic_publish(downloaded, destination, settings)

            with session_scope() as db:
                upload = db.get(Upload, upload_id)
                if upload is None or upload.deleted_at is not None:
                    storage.remove_file(destination, settings)
                    return
                uploads.finish_url_download(
                    db,
                    upload,
                    title=title,
                    suggested_source_name=suggested_source_name,
                    extension=extension,
                    size_bytes=size_bytes,
                )

        # Kept local to avoid a queue -> task -> downloader -> queue cycle.
        from app.workers.queue import enqueue_upload_verification  # noqa: PLC0415

        enqueue_upload_verification(upload_id)
    except UrlDownloadCancelled:
        return
    except UrlDownloadError as exc:
        _fail(upload_id, exc.code, exc.message, retryable=exc.retryable)
    except Exception:  # noqa: BLE001 - third-party failures must be sanitized
        logger.exception("video URL download failed", extra={"context": {"upload_id": upload_id}})
        _fail(
            upload_id,
            "video_download_failed",
            "The video could not be downloaded. Check that the link is public and try again.",
            retryable=True,
        )


def _download(
    url: str,
    workspace: Path,
    *,
    upload_id: str,
    settings: Settings,
    should_cancel: Callable[[], bool] | None,
    heartbeat: Callable[[], None] | None,
) -> tuple[str | None, str | None, Path]:  # noqa: PLR0915 - one yt-dlp transaction

    last_persisted_at = 0.0
    hook_error: UrlDownloadError | UrlDownloadCancelled | None = None

    def progress(payload: dict[str, Any]) -> None:
        nonlocal hook_error, last_persisted_at
        if heartbeat is not None:
            heartbeat()
        if should_cancel is not None and should_cancel():
            hook_error = UrlDownloadCancelled()
            raise hook_error
        if payload.get("status") != "downloading":
            return
        downloaded_bytes = _positive_int(payload.get("downloaded_bytes"))
        total_bytes = _positive_int(
            payload.get("total_bytes") or payload.get("total_bytes_estimate")
        )
        if downloaded_bytes > settings.max_upload_bytes or total_bytes > settings.max_upload_bytes:
            hook_error = UrlDownloadError(
                "download_too_large",
                "The highest-quality video exceeds this server's upload-size limit.",
            )
            raise hook_error
        now = time.monotonic()
        if now - last_persisted_at < _PROGRESS_INTERVAL_SECONDS:
            return
        last_persisted_at = now
        _persist_progress(upload_id, downloaded_bytes, total_bytes or None)

    ffmpeg = Path(ffmpeg_binary(settings))
    options: dict[str, Any] = {
        # yt-dlp's documented highest-quality selection: best video plus best
        # audio, falling back to the best combined stream when necessary.
        "format": "bestvideo*+bestaudio/best",
        "merge_output_format": "mkv",
        "outtmpl": str(workspace / "download.%(ext)s"),
        "paths": {"temp": str(workspace)},
        "ffmpeg_location": str(ffmpeg.parent),
        "noplaylist": True,
        "cachedir": False,
        "quiet": True,
        "no_warnings": True,
        "logger": _QuietLogger(),
        "progress_hooks": [progress],
        "socket_timeout": 30,
        "retries": 3,
        "fragment_retries": 3,
        "file_access_retries": 3,
        "max_filesize": settings.max_upload_bytes,
        "overwrites": True,
        "continuedl": False,
    }

    try:
        with yt_dlp.YoutubeDL(options) as downloader:
            info = downloader.extract_info(url, download=False)
            if not isinstance(info, dict):
                raise UrlDownloadError(
                    "video_not_found", "No downloadable video was found at that link."
                )
            if info.get("_type") in {"playlist", "multi_video"} or info.get("entries"):
                raise UrlDownloadError(
                    "playlist_not_supported",
                    "Paste a link to one video, not a playlist or channel.",
                )
            if info.get("is_live") or info.get("live_status") in {"is_live", "is_upcoming"}:
                raise UrlDownloadError(
                    "live_video_not_supported",
                    "Live and upcoming streams cannot be imported. Use a finished video.",
                )
            estimated = _estimated_size(info)
            if estimated and estimated > settings.max_upload_bytes:
                raise UrlDownloadError(
                    "download_too_large",
                    "The highest-quality video exceeds this server's upload-size limit.",
                )
            if estimated:
                _persist_progress(upload_id, 0, estimated)
            if should_cancel is not None and should_cancel():
                raise UrlDownloadCancelled()
            downloader.process_ie_result(info, download=True)
            title = _safe_title(info.get("title"))
            suggested_source_name = _suggested_source_name(info)
    except (UrlDownloadError, UrlDownloadCancelled):
        raise
    except DownloadError as exc:
        if hook_error is not None:
            raise hook_error from exc
        # Do not expose the exception: it routinely contains the input or a
        # short-lived signed media URL.
        raise UrlDownloadError(
            "video_download_failed",
            "The video could not be downloaded. It may be private, unavailable, or unsupported.",
            retryable=True,
        ) from exc

    candidates = [
        path
        for path in workspace.iterdir()
        if path.is_file()
        and path.suffix.lower() in _MEDIA_EXTENSIONS
        and not path.name.endswith((".part", ".ytdl"))
    ]
    if not candidates:
        raise UrlDownloadError(
            "video_not_found", "The download finished without producing a usable video file."
        )
    return title, suggested_source_name, max(candidates, key=lambda path: path.stat().st_size)


def _assert_public_origin(url: str) -> None:
    parts = urlsplit(url)
    hostname = parts.hostname or ""
    port = parts.port or (443 if parts.scheme == "https" else 80)
    try:
        addresses = {
            item[4][0]
            for item in socket.getaddrinfo(hostname, port, type=socket.SOCK_STREAM)
        }
    except OSError as exc:
        raise UrlDownloadError(
            "video_host_unreachable",
            "The video site's address could not be resolved.",
            retryable=True,
        ) from exc
    if not addresses:
        raise UrlDownloadError(
            "video_host_unreachable",
            "The video site's address could not be resolved.",
            retryable=True,
        )
    if any(not ipaddress.ip_address(address).is_global for address in addresses):
        raise UrlDownloadError(
            "private_video_url", "Private-network and local video URLs are not accepted."
        )


def _estimated_size(info: dict[str, Any]) -> int:
    requested = info.get("requested_formats")
    if isinstance(requested, list):
        sizes = [
            _positive_int(item.get("filesize") or item.get("filesize_approx"))
            for item in requested
        ]
        if all(sizes):
            return sum(sizes)
    return _positive_int(info.get("filesize") or info.get("filesize_approx"))


def _positive_int(value: Any) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError, OverflowError):
        return 0


def _safe_title(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = "".join(char for char in value if char.isprintable() and char not in "\r\n\t").strip()
    return cleaned[:220] or None


def _suggested_source_name(info: dict[str, Any]) -> str | None:
    """Return the first channel-like yt-dlp field that is safe to render.

    ``channel`` is the YouTube channel display name.  The fallbacks also make
    the feature useful for other supported sites without exposing handles or
    extractor-internal IDs as a source label.
    """
    for field in ("channel", "uploader", "creator"):
        cleaned = _safe_source_name(info.get(field))
        if cleaned:
            return cleaned
    return None


def _safe_source_name(value: Any) -> str | None:
    if not isinstance(value, str):
        return None

    # Channel names frequently decorate otherwise renderable names with emoji.
    # Drop only unsupported glyphs instead of discarding the useful channel
    # text; final validation still uses the same contract as job creation.
    safe_characters: list[str] = []
    for char in unicodedata.normalize("NFKC", value):
        if char.isspace():
            safe_characters.append(" ")
            continue
        try:
            normalize_source_name(char)
        except ValueError:
            safe_characters.append(" ")
        else:
            safe_characters.append(char)

    cleaned = " ".join("".join(safe_characters).split())[:SOURCE_NAME_MAX_CHARS].strip()
    if not cleaned:
        return None
    try:
        normalize_source_name(cleaned)
    except ValueError:
        return None
    return cleaned


def _persist_progress(upload_id: str, downloaded_bytes: int, total_bytes: int | None) -> None:
    with session_scope() as db:
        upload = db.get(Upload, upload_id)
        if upload is not None and upload.deleted_at is None:
            uploads.update_download_progress(
                db,
                upload,
                downloaded_bytes=downloaded_bytes,
                total_bytes=total_bytes,
            )


def _fail(upload_id: str, code: str, message: str, *, retryable: bool) -> None:
    with session_scope() as db:
        upload = db.get(Upload, upload_id)
        if upload is not None and upload.deleted_at is None and upload.state == "downloading":
            uploads.fail_upload(
                db,
                upload,
                code,
                message,
                phase="downloading",
                retryable=retryable,
            )
