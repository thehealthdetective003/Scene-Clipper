"""Export execution: encode, validate, package, publish (spec 5.7, 8.5).

Everything is written into an attempt-specific directory and only the validated
ZIP is atomically renamed into its published location, so a failed or cancelled
export leaves no partial archive behind and prior successful exports stay
intact.
"""

from __future__ import annotations

import datetime as dt
import zipfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sqlalchemy import delete, select

from app.config import Settings, get_settings
from app.db import session_scope
from app.exporting import manifest as manifest_module
from app.logging_setup import get_logger
from app.media.clips import ClipRenderError, render_export_clip, sha256_file, target_dimensions
from app.media.probe import MediaInfo, probe_media
from app.models import CandidateShot, Export, ExportFile, Job, JobSource, SelectedClip, Upload
from app.services import exports as export_service
from app.services import storage
from app.util.ids import new_id

logger = get_logger("app.exporting.runner")


class ExportCancelled(Exception):
    """Cooperative cancellation was requested mid-export."""


@dataclass(slots=True)
class _ClipPlan:
    serial: int
    candidate_id: str
    source_id: str
    source_file_name: str
    source_name: str | None
    start_us: int
    end_us: int
    score: float
    confidence: float
    reason: str


@dataclass(slots=True)
class _SourcePlan:
    id: str
    path: Path
    file_name: str
    sha256: str
    source_label: dict[str, Any] | None
    info: MediaInfo | None = None


def run_export(
    export_id: str,
    *,
    should_cancel: Callable[[], bool] | None = None,
    heartbeat: Callable[[], None] | None = None,
) -> None:  # noqa: PLR0911, PLR0912, PLR0915
    settings = get_settings()

    def cancelled() -> bool:
        if should_cancel is not None and should_cancel():
            return True
        with session_scope() as db:
            export = db.get(Export, export_id)
            if export is None or export.cancel_requested:
                return True
            job = db.get(Job, export.job_id)
            return job is None or job.deleted_at is not None

    def beat() -> None:
        if heartbeat is not None:
            heartbeat()

    with session_scope() as db:
        export = db.get(Export, export_id)
        if export is None:
            return
        if export.state in ("complete", "failed", "cancelled"):
            return
        job = db.get(Job, export.job_id)
        if job is None or job.deleted_at is not None:
            return
        source_rows = list(
            db.execute(
                select(JobSource).where(JobSource.job_id == job.id).order_by(JobSource.order_index)
            ).scalars()
        )
        source_plans: dict[str, _SourcePlan] = {}
        for source_row in source_rows:
            upload = db.get(Upload, source_row.upload_id)
            if upload is None:
                continue
            source_plans[source_row.id] = _SourcePlan(
                id=source_row.id,
                path=storage.resolve(upload.relative_source_path, settings),
                file_name=upload.file_name,
                sha256=source_row.source_sha256,
                source_label=(
                    {"text": source_row.source_name, "style": source_row.source_label_style}
                    if source_row.source_name and source_row.source_label_style
                    else None
                ),
            )
        if not source_plans:
            export_service.fail(
                db, export, job, code="source_missing",
                message="A source video is no longer available.", retryable=False,
            )
            return

        export_service.mark_running(db, export)
        job_id = job.id
        resolutions = list(export.resolutions)
        include_audio = export.include_audio
        review_revision = export.review_revision

        if job.review_revision != review_revision:
            export_service.fail(
                db, export, job, code="stale_review_revision",
                message="The review changed after this export was queued.", retryable=False,
            )
            return

        plans = _build_plans(db, job_id)
        # Any ledger rows from a prior failed attempt are discarded; files are
        # re-encoded so the archive always matches this attempt's validation.
        db.execute(delete(ExportFile).where(ExportFile.export_id == export_id))

    if not plans:
        with session_scope() as db:
            export, job = db.get(Export, export_id), None
            if export is not None:
                job = db.get(Job, export.job_id)
            if export is not None and job is not None:
                export_service.fail(
                    db, export, job, code="no_selected_clips",
                    message="Select at least one clip before exporting.", retryable=False,
                )
        return

    attempt_id = new_id()
    try:
        for source_plan in source_plans.values():
            source_plan.info = probe_media(source_plan.path, settings)
        _require_space(plans, resolutions, settings)

        with storage.attempt_directory(
            storage.export_dir(job_id, export_id), attempt_id, settings
        ) as workspace:
            clip_entries = _encode_clips(
                export_id=export_id,
                sources=source_plans,
                plans=plans,
                resolutions=resolutions,
                include_audio=include_audio,
                workspace=workspace,
                settings=settings,
                cancelled=cancelled,
                beat=beat,
            )

            source_manifest = _source_manifest(job_id, source_plans)
            manifest = manifest_module.build_manifest(
                job_id=job_id,
                export_id=export_id,
                created_at=dt.datetime.now(dt.UTC),
                source=source_manifest,
                resolutions=resolutions,
                include_audio=include_audio,
                source_label=(
                    next(iter(source_plans.values())).source_label
                    if len(source_plans) == 1
                    else None
                ),
                clips=clip_entries,
            )
            manifest_module.write_json(manifest, workspace / "manifest.json")
            manifest_module.write_csv(manifest, workspace / "manifest.csv")

            if cancelled():
                raise ExportCancelled()

            archive = workspace / "archive.zip"
            _build_archive(workspace, archive, manifest)
            _validate_archive(archive, manifest)

            published_relative = (
                f"{storage.export_dir(job_id, export_id)}/"
                f"scene-clips-{job_id}-{export_id}.zip"
            )
            storage.atomic_publish(archive, published_relative, settings)
            size_bytes = storage.file_size(published_relative, settings)
            digest = sha256_file(storage.resolve(published_relative, settings))

            # Publish each validated clip next to the archive so a single MP4
            # can be downloaded without unpacking anything. Only files that
            # already passed validation and made it into the archive are moved,
            # so nothing partial is ever published.
            published_clips = _publish_clips(
                workspace, clip_entries, job_id, export_id, settings
            )

        with session_scope() as db:
            export = db.get(Export, export_id)
            job = db.get(Job, job_id)
            if export is None or job is None:
                return
            for entry in clip_entries:
                for file_entry in entry["files"]:
                    export_service.record_file(
                        db,
                        export,
                        candidate_id=entry["candidateId"],
                        serial=entry["serial"],
                        resolution=file_entry["resolution"],
                        # The clip's own published path, not the archive's.
                        relative_path=published_clips[file_entry["path"]],
                        width=file_entry["width"],
                        height=file_entry["height"],
                        size_bytes=file_entry["sizeBytes"],
                        sha256=file_entry["sha256"],
                        start_us=entry["sourceStartUs"],
                        end_us=entry["sourceEndUs"],
                    )
            export_service.complete(
                db,
                export,
                job,
                zip_relative_path=published_relative,
                zip_size_bytes=size_bytes,
                zip_sha256=digest,
            )

    except ExportCancelled:
        _finish(export_id, outcome="cancelled")
    except ClipRenderError:
        logger.error("export validation failed", extra={"export_id": export_id})
        _finish(
            export_id,
            outcome="failed",
            code="export_validation_failed",
            message="An exported clip failed validation, so nothing was published.",
            retryable=True,
        )
    except Exception as exc:  # noqa: BLE001
        code, message, retryable = _classify(exc)
        logger.exception("export failed", extra={"export_id": export_id})
        _finish(export_id, outcome="failed", code=code, message=message, retryable=retryable)


# --- helpers ---------------------------------------------------------------


def _source_manifest(job_id: str, sources: dict[str, _SourcePlan]) -> dict[str, Any]:
    entries = []
    for source in sources.values():
        assert source.info is not None
        entries.append(
            {
                "id": source.id,
                "fileName": source.file_name,
                "sha256": source.sha256,
                "durationUs": source.info.duration_us,
                "width": source.info.display_width,
                "height": source.info.display_height,
                "averageFrameRate": source.info.average_frame_rate,
                "hasAudio": source.info.has_audio,
                "sourceLabel": source.source_label,
            }
        )
    if len(entries) == 1:
        return entries[0]
    return {
        "fileName": f"{len(entries)} source videos",
        "sha256": "",
        "durationUs": sum(int(entry["durationUs"]) for entry in entries),
        "width": max(int(entry["width"]) for entry in entries),
        "height": max(int(entry["height"]) for entry in entries),
        "averageFrameRate": "multiple",
        "hasAudio": any(bool(entry["hasAudio"]) for entry in entries),
        "jobId": job_id,
        "sources": entries,
    }


def _build_plans(db, job_id: str) -> list[_ClipPlan]:  # noqa: ANN001
    """Serials are assigned in review order, gapless from 1 (spec 5.7)."""
    clips = list(
        db.execute(
            select(SelectedClip)
            .where(SelectedClip.job_id == job_id)
            .order_by(SelectedClip.order_index.asc())
        ).scalars()
    )
    plans: list[_ClipPlan] = []
    for serial, clip in enumerate(clips, start=1):
        candidate = db.get(CandidateShot, clip.candidate_id)
        if candidate is None:
            continue
        plans.append(
            _ClipPlan(
                serial=serial,
                candidate_id=clip.candidate_id,
                source_id=candidate.source_id,
                source_file_name=candidate.source_file_name,
                source_name=candidate.source_name,
                start_us=clip.start_us,
                end_us=clip.end_us,
                score=round(candidate.score, 4),
                confidence=round(candidate.confidence, 4),
                reason=candidate.reason or "",
            )
        )
    return plans


def _require_space(plans: list[_ClipPlan], resolutions: list[str], settings: Settings) -> None:
    """Rough headroom check before encoding anything (spec 7.3)."""
    seconds = sum((plan.end_us - plan.start_us) for plan in plans) / 1_000_000
    # ~4 Mb/s at CRF 20 for 1080p, doubled for the ZIP copy.
    estimate = int(seconds * len(resolutions) * 1_000_000) * 2
    storage.require_free_space(max(estimate, 128 * 1024 * 1024), settings)


def _encode_clips(
    *,
    export_id: str,
    sources: dict[str, _SourcePlan],
    plans: list[_ClipPlan],
    resolutions: list[str],
    include_audio: bool,
    workspace: Path,
    settings: Settings,
    cancelled: Callable[[], bool],
    beat: Callable[[], None],
) -> list[dict[str, Any]]:
    total = max(1, len(plans) * len(resolutions))
    done = 0
    entries: list[dict[str, Any]] = []
    # If two presets resolve to the same dimensions, encode once and reuse the
    # validated output for both folders (spec 5.7).
    encoded_by_dimensions: dict[tuple[int, int], Path] = {}

    for plan in plans:
        if cancelled():
            raise ExportCancelled()
        files: list[dict[str, Any]] = []
        source_plan = sources.get(plan.source_id)
        if source_plan is None or source_plan.info is None:
            raise FileNotFoundError(plan.source_id)

        for resolution in resolutions:
            if cancelled():
                raise ExportCancelled()
            beat()

            folder = export_service.FOLDER_NAMES[resolution]
            name = storage.serial_name(plan.serial)
            destination = workspace / folder / name
            destination.parent.mkdir(parents=True, exist_ok=True)

            dimensions = target_dimensions(source_plan.info, resolution)
            reuse = encoded_by_dimensions.get(dimensions)
            if reuse is not None and reuse.exists():
                destination.write_bytes(reuse.read_bytes())
                rendered_width, rendered_height = dimensions
                size_bytes = destination.stat().st_size
                digest = sha256_file(destination)
            else:
                rendered = render_export_clip(
                    source_plan.path,
                    destination,
                    info=source_plan.info,
                    start_us=plan.start_us,
                    end_us=plan.end_us,
                    resolution=resolution,
                    include_audio=include_audio,
                    source_label=source_plan.source_label,
                    settings=settings,
                    should_cancel=cancelled,
                )
                encoded_by_dimensions[dimensions] = destination
                rendered_width, rendered_height = rendered.width, rendered.height
                size_bytes, digest = rendered.size_bytes, rendered.sha256

            files.append(
                {
                    "resolution": resolution,
                    # ZIP-relative, built from a fixed folder name and a
                    # validated serial (spec 10.2).
                    "path": f"{folder}/{name}",
                    "width": rendered_width,
                    "height": rendered_height,
                    "sizeBytes": size_bytes,
                    "sha256": digest,
                }
            )

            done += 1
            with session_scope() as db:
                export = db.get(Export, export_id)
                if export is not None:
                    export_service.set_progress(
                        db,
                        export,
                        percent=round(done / total * 95.0, 2),
                        message=f"Encoded {done} of {total} file(s).",
                    )

        entries.append(
            {
                "serial": plan.serial,
                "candidateId": plan.candidate_id,
                "sourceId": plan.source_id,
                "sourceFileName": plan.source_file_name,
                "sourceName": plan.source_name,
                "sourceStartUs": plan.start_us,
                "sourceEndUs": plan.end_us,
                "durationUs": plan.end_us - plan.start_us,
                "score": plan.score,
                "confidence": plan.confidence,
                "reason": plan.reason,
                "files": files,
            }
        )
        encoded_by_dimensions.clear()

    return entries


def _publish_clips(
    workspace: Path,
    clip_entries: list[dict[str, Any]],
    job_id: str,
    export_id: str,
    settings: Settings,
) -> dict[str, str]:
    """Move each validated clip into its published location.

    Returns a map of ZIP-relative path (``original/0001.mp4``) to the
    DATA_DIR-relative published path, so each ``export_files`` row can point at
    its own file rather than at the archive.
    """
    base = storage.export_dir(job_id, export_id)
    published: dict[str, str] = {}

    for entry in clip_entries:
        for file_entry in entry["files"]:
            member = file_entry["path"]
            _assert_safe_member(member)
            relative = f"{base}/{member}"
            storage.atomic_publish(workspace / member, relative, settings)
            published[member] = relative

    return published


def _build_archive(workspace: Path, archive: Path, manifest: dict[str, Any]) -> None:
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        for clip in manifest["clips"]:
            for file_entry in clip["files"]:
                member = file_entry["path"]
                _assert_safe_member(member)
                zf.write(workspace / member, arcname=member)
        zf.write(workspace / "manifest.json", arcname="manifest.json")
        zf.write(workspace / "manifest.csv", arcname="manifest.csv")


def _assert_safe_member(member: str) -> None:
    """Archive paths come only from fixed folders and validated serials."""
    if member.startswith("/") or ".." in member.split("/") or "\\" in member:
        raise ClipRenderError("Refusing to write an unsafe archive path.")
    folder, _, name = member.partition("/")
    if folder not in set(export_service.FOLDER_NAMES.values()) or not name:
        raise ClipRenderError("Refusing to write an unexpected archive folder.")


def _validate_archive(archive: Path, manifest: dict[str, Any]) -> None:
    """The archive must be readable and contain exactly what the manifest lists."""
    expected = {"manifest.json", "manifest.csv"}
    for clip in manifest["clips"]:
        for file_entry in clip["files"]:
            expected.add(file_entry["path"])

    with zipfile.ZipFile(archive, "r") as zf:
        broken = zf.testzip()
        if broken is not None:
            raise ClipRenderError("The generated archive is corrupt.")
        present = set(zf.namelist())
        if present != expected:
            raise ClipRenderError("The archive contents do not match the manifest.")


def _classify(exc: Exception) -> tuple[str, str, bool]:
    from app.api.errors import AppError
    from app.media.runner import MediaToolCancelled, MediaToolError

    if isinstance(exc, AppError) and exc.code == "insufficient_storage":
        return (
            "insufficient_storage",
            "There is not enough disk space to complete the export.",
            True,
        )
    if isinstance(exc, MediaToolCancelled):
        return "export_cancelled", "The export was cancelled.", False
    if isinstance(exc, MediaToolError):
        return "media_processing_failed", "A clip could not be encoded.", True
    if isinstance(exc, OSError) and getattr(exc, "errno", None) == 28:
        return (
            "insufficient_storage",
            "There is not enough disk space to complete the export.",
            True,
        )
    return "export_failed", "The export failed unexpectedly. You can retry it.", True


def _finish(
    export_id: str,
    *,
    outcome: str,
    code: str = "",
    message: str = "",
    retryable: bool = False,
) -> None:
    with session_scope() as db:
        export = db.get(Export, export_id)
        if export is None:
            return
        job = db.get(Job, export.job_id)
        if job is None:
            return
        if outcome == "cancelled":
            export_service.cancel(db, export, job)
        else:
            export_service.fail(
                db, export, job, code=code, message=message, retryable=retryable
            )
