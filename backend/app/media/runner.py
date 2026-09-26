"""Sandboxed invocation of FFmpeg and ffprobe (spec 10.2).

Every call is an argument array -- never a shell string -- and runs with a wall
clock timeout, a bounded output buffer, and (on POSIX) CPU/memory/process
rlimits. Media tools are additionally restricted to local files by an explicit
protocol allow-list, so a crafted container cannot make the decoder open a
network URL.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import threading
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from app.config import Settings, get_settings
from app.logging_setup import get_logger

logger = get_logger("app.media")

#: Cap captured stderr so a chatty failure cannot exhaust memory.
_MAX_CAPTURED_BYTES = 64 * 1024

#: Local files only. `crypto` and `data` stay out; `pipe` is not needed.
PROTOCOL_ALLOWLIST = "file"


class MediaToolError(RuntimeError):
    """A media subprocess failed, timed out, or was cancelled."""

    def __init__(self, message: str, *, exit_code: int | None = None, stderr_tail: str = "") -> None:
        super().__init__(message)
        self.exit_code = exit_code
        # Retained for protected diagnostics only; never returned to a client.
        self.stderr_tail = stderr_tail


class MediaToolCancelled(MediaToolError):
    """The caller requested cancellation and the process was terminated."""


class MediaToolTimeout(MediaToolError):
    """The process exceeded its wall-clock budget."""


@dataclass(frozen=True, slots=True)
class ToolResult:
    exit_code: int
    stdout: bytes
    stderr: str


def _resolve_binary(configured: str, name: str) -> str:
    if configured:
        path = Path(configured)
        if not path.is_file():
            raise MediaToolError(f"Configured {name} path does not exist.")
        return str(path)
    found = shutil.which(name)
    if not found:
        raise MediaToolError(
            f"{name} was not found. Install it or set {name.upper()}_PATH."
        )
    return found


def ffmpeg_binary(settings: Settings | None = None) -> str:
    settings = settings or get_settings()
    return _resolve_binary(settings.ffmpeg_path, "ffmpeg")


def ffprobe_binary(settings: Settings | None = None) -> str:
    settings = settings or get_settings()
    return _resolve_binary(settings.ffprobe_path, "ffprobe")


def _posix_limits(memory_bytes: int, cpu_seconds: int) -> Callable[[], None] | None:
    """Per-process resource limits for a media subprocess (spec 10.2).

    Deliberately *not* set here:

    ``RLIMIT_NPROC``
        It bounds threads per real UID across the whole system, not per
        process, so it counts the worker's own threads too and FFmpeg's filter
        threading trips it on any multi-core host -- swscale then fails to
        allocate and the encode dies with a misleading "Failed to configure
        output pad". Process/thread count is bounded at the container instead,
        via Compose's ``pids_limit``, which cgroups enforce per container.
    """
    if sys.platform == "win32":  # rlimits are POSIX-only.
        return None
    import resource  # noqa: PLC0415 - platform-conditional import

    def apply() -> None:
        resource.setrlimit(resource.RLIMIT_AS, (memory_bytes, memory_bytes))
        resource.setrlimit(resource.RLIMIT_CPU, (cpu_seconds, cpu_seconds + 10))
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
        os.setsid()  # Own process group, so termination reaches children.

    return apply


def run_tool(
    argv: list[str],
    *,
    timeout_seconds: int | None = None,
    settings: Settings | None = None,
    should_cancel: Callable[[], bool] | None = None,
    cancel_poll_seconds: float = 1.0,
    capture_stdout: bool = True,
) -> ToolResult:
    """Run a media tool to completion, honouring cancellation and timeouts.

    ``should_cancel`` is polled while the process runs; when it returns true the
    process (and its group, on POSIX) is terminated and
    :class:`MediaToolCancelled` is raised so the caller can clean up attempt
    files without publishing anything.
    """
    settings = settings or get_settings()
    timeout_seconds = timeout_seconds or settings.media_timeout_seconds

    if not argv or not argv[0]:
        raise MediaToolError("Refusing to run an empty command.")
    for argument in argv:
        if not isinstance(argument, str):
            raise MediaToolError("Media tool arguments must all be strings.")

    creationflags = 0
    if sys.platform == "win32":
        creationflags = subprocess.CREATE_NEW_PROCESS_GROUP

    process = subprocess.Popen(  # noqa: S603 - argv array, shell=False, fixed binary
        argv,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE if capture_stdout else subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        shell=False,
        preexec_fn=_posix_limits(settings.media_memory_bytes, timeout_seconds),  # noqa: PLW1509
        creationflags=creationflags,
        close_fds=True,
    )

    stdout_chunks: list[bytes] = []
    stderr_chunks: list[bytes] = []
    stderr_bytes = 0

    def drain(stream, sink: list[bytes], cap: int | None) -> None:  # noqa: ANN001
        nonlocal stderr_bytes
        if stream is None:
            return
        for line in iter(stream.readline, b""):
            if cap is None:
                sink.append(line)
            elif stderr_bytes < cap:
                sink.append(line)
                stderr_bytes += len(line)
        stream.close()

    threads = [
        threading.Thread(target=drain, args=(process.stdout, stdout_chunks, None), daemon=True),
        threading.Thread(
            target=drain, args=(process.stderr, stderr_chunks, _MAX_CAPTURED_BYTES), daemon=True
        ),
    ]
    for thread in threads:
        thread.start()

    cancelled = False
    timed_out = False
    waited = 0.0
    while True:
        try:
            process.wait(timeout=cancel_poll_seconds)
            break
        except subprocess.TimeoutExpired:
            waited += cancel_poll_seconds
            if should_cancel is not None and should_cancel():
                cancelled = True
                _terminate(process)
                break
            if waited >= timeout_seconds:
                timed_out = True
                _terminate(process)
                break

    for thread in threads:
        thread.join(timeout=5)

    stderr_text = b"".join(stderr_chunks).decode("utf-8", errors="replace")

    if cancelled:
        raise MediaToolCancelled("The media operation was cancelled.", stderr_tail=stderr_text)
    if timed_out:
        raise MediaToolTimeout(
            "The media operation exceeded its time limit.", stderr_tail=stderr_text
        )

    return ToolResult(
        exit_code=process.returncode or 0,
        stdout=b"".join(stdout_chunks),
        stderr=stderr_text,
    )


def _terminate(process: subprocess.Popen) -> None:
    """Terminate a media subprocess and its children, escalating if needed."""
    try:
        if sys.platform != "win32":
            os.killpg(os.getpgid(process.pid), 15)
        else:
            process.terminate()
    except (ProcessLookupError, PermissionError, OSError):
        pass

    try:
        process.wait(timeout=5)
        return
    except subprocess.TimeoutExpired:
        pass

    try:
        if sys.platform != "win32":
            os.killpg(os.getpgid(process.pid), 9)
        else:
            process.kill()
        process.wait(timeout=5)
    except (ProcessLookupError, PermissionError, OSError, subprocess.TimeoutExpired):
        logger.warning("media subprocess did not exit after kill")


def check_tool(result: ToolResult, operation: str) -> ToolResult:
    """Raise a sanitized error when a tool exits non-zero."""
    if result.exit_code != 0:
        raise MediaToolError(
            f"{operation} failed.", exit_code=result.exit_code, stderr_tail=result.stderr
        )
    return result
