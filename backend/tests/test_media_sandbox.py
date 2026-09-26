"""Media subprocess sandboxing (spec 10.2).

The rlimit path is POSIX-only, so on Windows it is skipped entirely -- which is
exactly how a too-tight ``RLIMIT_NPROC`` once passed every test on a developer
machine and then broke every encode in the Linux container. These tests run the
real tool under the real limits wherever the platform supports them.
"""

from __future__ import annotations

import sys

import pytest

from app.config import get_settings
from app.media.runner import (
    PROTOCOL_ALLOWLIST,
    MediaToolError,
    MediaToolTimeout,
    _posix_limits,
    ffmpeg_binary,
    run_tool,
)
from tests.conftest import requires_media

posix_only = pytest.mark.skipif(sys.platform == "win32", reason="rlimits are POSIX-only")


class TestLimitConstruction:
    def test_windows_has_no_preexec_hook(self, monkeypatch):
        monkeypatch.setattr(sys, "platform", "win32")
        assert _posix_limits(1 << 30, 60) is None

    @posix_only
    def test_posix_returns_a_callable(self):
        assert callable(_posix_limits(1 << 30, 60))

    @posix_only
    def test_nproc_is_not_limited(self):
        """RLIMIT_NPROC is per-UID and starves FFmpeg's filter threads."""
        import resource

        before = resource.getrlimit(resource.RLIMIT_NPROC)
        applied: dict[int, tuple[int, int]] = {}
        original = resource.setrlimit

        def capture(which, limits):
            applied[which] = limits

        resource.setrlimit = capture
        try:
            hook = _posix_limits(1 << 30, 60)
            assert hook is not None
            # os.setsid() would detach the test runner; only the limits matter.
            try:
                hook()
            except Exception:
                pass
        finally:
            resource.setrlimit = original

        assert resource.RLIMIT_NPROC not in applied
        assert resource.RLIMIT_AS in applied
        assert resource.RLIMIT_CPU in applied
        assert resource.RLIMIT_CORE in applied
        assert resource.getrlimit(resource.RLIMIT_NPROC) == before


@requires_media
class TestRealInvocation:
    """A real encode under the real limits, on whatever platform is running."""

    def test_generate_and_scale_under_limits(self, tmp_path):
        settings = get_settings()
        source = tmp_path / "gen.mp4"

        generate = [
            ffmpeg_binary(settings), "-hide_banner", "-loglevel", "error", "-y",
            "-f", "lavfi", "-i", "testsrc2=size=480x270:rate=30:duration=2",
            "-c:v", "libx264", "-crf", "23", "-pix_fmt", "yuv420p", str(source),
        ]
        assert run_tool(generate, timeout_seconds=120, capture_stdout=False).exit_code == 0
        assert source.stat().st_size > 0

        # The exact chain Stage E runs, which is where the rlimit bug surfaced.
        extract = [
            ffmpeg_binary(settings), "-nostdin", "-hide_banner", "-loglevel", "error",
            "-protocol_whitelist", PROTOCOL_ALLOWLIST,
            "-ss", "0.5", "-t", "1.0", "-i", str(source), "-map", "0:v:0",
            "-fps_mode", "passthrough",
            "-vf", "fps=2.0,scale=320:180:flags=bilinear,format=gray",
            "-f", "rawvideo", "-pix_fmt", "gray", "-",
        ]
        result = run_tool(extract, timeout_seconds=120)
        assert result.exit_code == 0, result.stderr
        # 320x180 grayscale frames, so an exact multiple of the frame size.
        assert result.stdout and len(result.stdout) % (320 * 180) == 0


class TestSandboxRules:
    def test_protocols_are_local_only(self):
        assert PROTOCOL_ALLOWLIST == "file"

    def test_empty_command_is_refused(self):
        with pytest.raises(MediaToolError):
            run_tool([])

    def test_non_string_arguments_are_refused(self):
        with pytest.raises(MediaToolError):
            run_tool(["ffmpeg", 123])  # type: ignore[list-item]

    @requires_media
    def test_timeout_terminates_the_process(self):
        settings = get_settings()
        # An unbounded lavfi source runs forever unless the timeout fires.
        argv = [
            ffmpeg_binary(settings), "-hide_banner", "-loglevel", "error",
            "-f", "lavfi", "-i", "testsrc2=size=320x180:rate=30",
            "-f", "null", "-",
        ]
        with pytest.raises(MediaToolTimeout):
            run_tool(argv, timeout_seconds=2, capture_stdout=False)

    @requires_media
    def test_cancellation_terminates_the_process(self):
        settings = get_settings()
        argv = [
            ffmpeg_binary(settings), "-hide_banner", "-loglevel", "error",
            "-f", "lavfi", "-i", "testsrc2=size=320x180:rate=30",
            "-f", "null", "-",
        ]
        from app.media.runner import MediaToolCancelled

        with pytest.raises(MediaToolCancelled):
            run_tool(
                argv,
                timeout_seconds=60,
                should_cancel=lambda: True,
                cancel_poll_seconds=0.2,
                capture_stdout=False,
            )
