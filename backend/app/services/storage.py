"""Filesystem layout, path safety, and atomic publication (spec 7.3, 10.2).

Rules enforced here:

* Every path component is server-generated (UUIDs, fixed folder names, zero
  padded serials). User-supplied names never reach the filesystem.
* Database paths are relative to ``DATA_DIR``; absolute paths are resolved only
  at the point of use and are re-checked to be inside ``DATA_DIR``.
* Intermediate output is written into an attempt-specific directory and only
  atomically renamed into its published location after validation.
* ``/data`` is never served statically; callers stream through authenticated
  routes.
"""

from __future__ import annotations

import os
import re
import shutil
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from app.api.errors import insufficient_storage
from app.config import Settings, get_settings
from app.logging_setup import get_logger

logger = get_logger("app.storage")

#: Only these characters may appear in a generated path component.
_SAFE_COMPONENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")

#: Windows reserved device names, rejected even on POSIX so archives created
#: here stay safe to extract anywhere (spec 10.2).
_RESERVED_NAMES = {
    "con", "prn", "aux", "nul",
    *{f"com{i}" for i in range(1, 10)},
    *{f"lpt{i}" for i in range(1, 10)},
}


class UnsafePathError(ValueError):
    """Raised when a path component fails validation."""


def validate_component(component: str) -> str:
    """Validate one generated path segment."""
    if not component or not _SAFE_COMPONENT.match(component):
        raise UnsafePathError("Path component contains unsupported characters.")
    if component in {".", ".."}:
        raise UnsafePathError("Relative path components are not permitted.")
    if component.split(".")[0].lower() in _RESERVED_NAMES:
        raise UnsafePathError("Reserved file name.")
    if any(ord(ch) < 32 for ch in component):
        raise UnsafePathError("Control characters are not permitted in paths.")
    return component


def sanitize_extension(file_name: str, *, default: str = "bin") -> str:
    """Derive a safe storage extension from a display-only filename.

    The extension is a hint used solely so the file is recognizable on disk;
    format support is decided by ffprobe, not by this value (spec 5.3).
    """
    suffix = Path(file_name or "").suffix.lstrip(".").lower()
    if not suffix or not re.fullmatch(r"[a-z0-9]{1,8}", suffix):
        return default
    return suffix


def data_root(settings: Settings | None = None) -> Path:
    settings = settings or get_settings()
    return settings.data_dir.resolve()


def resolve(relative_path: str, settings: Settings | None = None) -> Path:
    """Resolve a DATA_DIR-relative path, refusing anything that escapes it."""
    root = data_root(settings)
    if os.path.isabs(relative_path) or "\\" in relative_path:
        raise UnsafePathError("Only forward-slash relative paths are accepted.")
    parts = [part for part in relative_path.split("/") if part not in ("", ".")]
    if not parts:
        raise UnsafePathError("Empty relative path.")
    for part in parts:
        validate_component(part)
    candidate = root.joinpath(*parts)
    resolved = candidate.resolve()
    if resolved != root and root not in resolved.parents:
        raise UnsafePathError("Resolved path escapes the data directory.")
    return resolved


def ensure_dir(relative_path: str, settings: Settings | None = None) -> Path:
    path = resolve(relative_path, settings)
    path.mkdir(parents=True, exist_ok=True)
    return path


def relative_of(path: Path, settings: Settings | None = None) -> str:
    return path.resolve().relative_to(data_root(settings)).as_posix()


# --- Well-known locations --------------------------------------------------


def upload_dir(upload_id: str) -> str:
    return f"uploads/{validate_component(upload_id)}"


def job_dir(job_id: str) -> str:
    return f"jobs/{validate_component(job_id)}"


def job_subdir(job_id: str, name: str) -> str:
    return f"{job_dir(job_id)}/{validate_component(name)}"


def export_dir(job_id: str, export_id: str) -> str:
    return f"exports/{validate_component(job_id)}/{validate_component(export_id)}"


def cache_dir(cache_key: str) -> str:
    return f"cache/{validate_component(cache_key)}"


def serial_name(serial: int, extension: str = "mp4") -> str:
    """Gapless four-digit export name such as ``0001.mp4`` (spec 5.7)."""
    if serial < 1 or serial > 9999:
        raise UnsafePathError("Export serial is out of range.")
    return f"{serial:04d}.{validate_component(extension)}"


# --- Free space ------------------------------------------------------------


def free_space_bytes(settings: Settings | None = None) -> int:
    return shutil.disk_usage(data_root(settings)).free


def require_free_space(required_bytes: int, settings: Settings | None = None) -> None:
    """Check headroom before proxy generation and before export (spec 7.3)."""
    # Keep a margin so the database and WAL always have room to commit.
    margin = 256 * 1024 * 1024
    available = free_space_bytes(settings)
    if available < required_bytes + margin:
        raise insufficient_storage(
            "There is not enough free disk space to complete this operation."
        )


# --- Attempt directories and atomic publication ----------------------------


@contextmanager
def attempt_directory(
    owner_relative_dir: str, attempt_id: str, settings: Settings | None = None
) -> Iterator[Path]:
    """A scratch directory for one attempt, removed unless files were published.

    Cancellation and failure must leave no attempt-only files behind (spec 5.5,
    11), so the directory is always torn down on exit.
    """
    relative = f"{owner_relative_dir}/attempts/{validate_component(attempt_id)}"
    path = ensure_dir(relative, settings)
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


def atomic_publish(source: Path, destination_relative: str, settings: Settings | None = None) -> Path:
    """Rename a validated file into its published location.

    ``os.replace`` is atomic within a filesystem; the attempt directory lives
    under the same ``DATA_DIR`` volume precisely so this holds.
    """
    destination = resolve(destination_relative, settings)
    destination.parent.mkdir(parents=True, exist_ok=True)
    os.replace(source, destination)
    return destination


@contextmanager
def temporary_file(
    prefix: str = "tmp", suffix: str = "", settings: Settings | None = None
) -> Iterator[Path]:
    """A same-volume temporary file, so a later publish stays atomic."""
    settings = settings or get_settings()
    settings.tmp_dir.mkdir(parents=True, exist_ok=True)
    handle, name = tempfile.mkstemp(prefix=prefix, suffix=suffix, dir=settings.tmp_dir)
    os.close(handle)
    path = Path(name)
    try:
        yield path
    finally:
        path.unlink(missing_ok=True)


def remove_tree(relative_path: str, settings: Settings | None = None) -> None:
    try:
        target = resolve(relative_path, settings)
    except UnsafePathError:
        logger.warning("refused to remove unsafe path")
        return
    shutil.rmtree(target, ignore_errors=True)


def remove_file(relative_path: str, settings: Settings | None = None) -> None:
    try:
        resolve(relative_path, settings).unlink(missing_ok=True)
    except UnsafePathError:
        logger.warning("refused to remove unsafe path")


def file_size(relative_path: str, settings: Settings | None = None) -> int:
    path = resolve(relative_path, settings)
    return path.stat().st_size if path.exists() else 0
