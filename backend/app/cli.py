"""Operator command line.

    python -m app.cli generate-key      # APP_ENCRYPTION_KEY
    python -m app.cli check-config      # validate the environment
    python -m app.cli check-media       # verify ffmpeg/ffprobe are usable
    python -m app.cli requeue           # recover abandoned work after a restart

"""

from __future__ import annotations

import argparse
import base64
import os
import sys


def _generate_key() -> int:
    print(base64.b64encode(os.urandom(32)).decode("ascii"))
    return 0


def _check_config() -> int:
    from app.config import ConfigurationError, get_settings

    try:
        settings = get_settings()
    except ConfigurationError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    settings.ensure_directories()
    print("Configuration is valid.")
    print(f"  base URL         : {settings.app_base_url}")
    print(f"  allowed origin   : {settings.allowed_origin}")
    print(f"  data directory   : {settings.data_dir}")
    print(f"  cookies secure   : {settings.cookie_secure}")
    print(f"  max upload bytes : {settings.max_upload_bytes}")
    print(f"  gemini model     : {settings.gemini_model}")
    print(f"  gemini cap       : {settings.gemini_request_cap}")
    return 0


def _check_media() -> int:
    from app.config import get_settings
    from app.media.runner import MediaToolError, ffmpeg_binary, ffprobe_binary, run_tool
    from app.source_labels import FONT_PRESETS, font_path

    settings = get_settings()
    status = 0
    for name, resolver in (("ffmpeg", ffmpeg_binary), ("ffprobe", ffprobe_binary)):
        try:
            binary = resolver(settings)
            result = run_tool([binary, "-version"], timeout_seconds=30, settings=settings)
            first_line = result.stdout.decode("utf-8", "replace").splitlines()[0]
            print(f"{name}: {first_line}")
        except (MediaToolError, IndexError) as exc:
            print(f"{name}: UNAVAILABLE ({exc})", file=sys.stderr)
            status = 1

    try:
        binary = ffmpeg_binary(settings)
        filters = run_tool(
            [binary, "-hide_banner", "-filters"], timeout_seconds=30, settings=settings
        )
        listing = filters.stdout.decode("utf-8", "replace") + filters.stderr
        if " drawtext " not in listing:
            print("ffmpeg drawtext filter: UNAVAILABLE", file=sys.stderr)
            status = 1
        else:
            print("ffmpeg drawtext filter: available")
    except MediaToolError as exc:
        print(f"ffmpeg drawtext filter: UNAVAILABLE ({exc})", file=sys.stderr)
        status = 1

    for preset in FONT_PRESETS:
        path = font_path(preset)
        if not path.is_file() or path.stat().st_size == 0:
            print(f"source-label font {preset}: MISSING", file=sys.stderr)
            status = 1
    return status


def _db_ready() -> int:
    """Exit 0 once the schema is migrated and stamped.

    Checks the stamped revision directly. ``alembic current`` is unsuitable as a
    readiness probe because it reports the revision through the logging handler
    on stderr, leaving stdout empty whether or not the database is ready.
    """
    from sqlalchemy import inspect, text

    from app.config import get_settings
    from app.db import build_engine

    try:
        engine = build_engine(get_settings())
        with engine.connect() as connection:
            if not inspect(connection).has_table("alembic_version"):
                print("database has no alembic_version table yet", file=sys.stderr)
                return 1
            revision = connection.execute(
                text("SELECT version_num FROM alembic_version")
            ).scalar_one_or_none()
    except Exception as exc:  # noqa: BLE001 - any failure means "not ready"
        print(f"database is not ready: {type(exc).__name__}", file=sys.stderr)
        return 1

    if not revision:
        print("database is created but not stamped with a revision", file=sys.stderr)
        return 1
    print(revision)
    return 0


def _requeue() -> int:
    from app.workers.recovery import requeue_abandoned_work

    counts = requeue_abandoned_work()
    print(
        f"Requeued {counts['jobs']} job(s), {counts['exports']} export(s), "
        f"{counts['uploads']} upload verification(s), {counts['downloads']} download(s)."
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="app.cli", description="Scene Clipper operator tools")
    subparsers = parser.add_subparsers(dest="command", required=True)
    for name, help_text in (
        ("generate-key", "Print a new base64 32-byte APP_ENCRYPTION_KEY."),
        ("check-config", "Validate the deployment configuration."),
        ("check-media", "Verify that ffmpeg and ffprobe are usable."),
        ("db-ready", "Exit 0 once the database schema is migrated and stamped."),
        ("requeue", "Requeue abandoned jobs, exports, and upload verifications."),
    ):
        subparsers.add_parser(name, help=help_text)

    args = parser.parse_args(argv)
    return {
        "generate-key": _generate_key,
        "check-config": _check_config,
        "check-media": _check_media,
        "db-ready": _db_ready,
        "requeue": _requeue,
    }[args.command]()


if __name__ == "__main__":
    raise SystemExit(main())
