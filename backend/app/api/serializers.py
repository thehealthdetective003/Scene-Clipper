"""ORM -> API projections.

Centralized so that no route can accidentally serialize a column that must
never leave the server (encrypted key material, absolute paths, raw provider
payloads, protected diagnostics).
"""

from __future__ import annotations

import datetime as dt
from typing import Any

from app import models
from app.schemas import (
    AnalysisUsageModel,
    CandidateShotModel,
    ExportFileModel,
    ExportProgressModel,
    ExportResponse,
    ExportSummaryModel,
    GeminiKeyModel,
    JobErrorModel,
    JobResponse,
    JobSummaryModel,
    ProgressModel,
    SelectedClipModel,
    SourceLabelModel,
    SourceLabelStyleModel,
    UploadResponse,
    VideoModel,
)
from app.services.uploads import progress_percent


def iso(value: dt.datetime | None) -> str | None:
    """RFC 3339 UTC (spec 8.1)."""
    if value is None:
        return None
    return value.astimezone(dt.timezone.utc).isoformat().replace("+00:00", "Z")


def iso_required(value: dt.datetime) -> str:
    result = iso(value)
    assert result is not None
    return result


def gemini_key_model(row: models.GeminiKey) -> GeminiKeyModel:
    """Health only. No part of the credential -- not even a masked prefix --
    ever leaves the server (spec 5.2)."""
    return GeminiKeyModel(
        id=row.id,
        label=row.label,
        position=row.position,
        status=row.status,  # type: ignore[arg-type]
        available=row.is_available(),
        last_error_code=row.last_error_code,
        last_error_at=iso(row.last_error_at),
        last_success_at=iso(row.last_success_at),
        cooldown_until=iso(row.cooldown_until),
        requests_succeeded=row.requests_succeeded,
        requests_failed=row.requests_failed,
        created_at=iso(row.created_at),
    )


def error_model(payload: dict[str, Any] | None) -> JobErrorModel | None:
    if not payload:
        return None
    return JobErrorModel(
        phase=str(payload.get("phase", "failed")),
        code=str(payload.get("code", "error")),
        message=str(payload.get("message", "")),
        retryable=bool(payload.get("retryable", False)),
        occurred_at=str(payload.get("occurredAt") or payload.get("occurred_at") or ""),
    )


def upload_response(upload: models.Upload) -> UploadResponse:
    return UploadResponse(
        id=upload.id,
        file_name=upload.file_name,
        declared_size_bytes=upload.declared_size_bytes,
        verified_offset_bytes=upload.verified_offset_bytes,
        chunk_size_bytes=upload.chunk_size_bytes,
        state=upload.state,  # type: ignore[arg-type]
        sha256=upload.sha256,
        progress_percent=progress_percent(upload),
        error=error_model(upload.error),
        created_at=iso_required(upload.created_at),
        updated_at=iso_required(upload.updated_at),
    )


def usage_model(usage: models.AnalysisUsage | None, job: models.Job) -> AnalysisUsageModel:
    if usage is None:
        return AnalysisUsageModel(
            model=job.gemini_model if job.use_gemini else None,
            request_cap=job.gemini_request_cap,
            requests_used=0,
            coarse_requests=0,
            fine_requests=0,
            proxy_video_requests=0,
            proxy_video_candidates=0,
            provider_file_operations=0,
            image_bytes_sent=0,
            proxy_video_bytes_sent=0,
            proxy_video_seconds_sent=0.0,
            input_tokens=None,
            output_tokens=None,
            total_tokens=None,
            cache_status="none",
            local_fallback_used=False,
            fallback_reason=None,
        )
    return AnalysisUsageModel(
        model=usage.model,
        request_cap=usage.request_cap,
        requests_used=usage.requests_used,
        coarse_requests=usage.coarse_requests,
        fine_requests=usage.fine_requests,
        proxy_video_requests=usage.proxy_video_requests,
        proxy_video_candidates=usage.proxy_video_candidates,
        provider_file_operations=usage.provider_file_operations,
        image_bytes_sent=usage.image_bytes_sent,
        proxy_video_bytes_sent=usage.proxy_video_bytes_sent,
        proxy_video_seconds_sent=usage.proxy_video_seconds_sent,
        input_tokens=usage.input_tokens,
        output_tokens=usage.output_tokens,
        total_tokens=usage.total_tokens,
        cache_status=usage.cache_status,  # type: ignore[arg-type]
        local_fallback_used=usage.local_fallback_used,
        fallback_reason=usage.fallback_reason,
    )


def export_summary(export: models.Export | None) -> ExportSummaryModel | None:
    if export is None:
        return None
    return ExportSummaryModel(
        id=export.id,
        state=export.state,  # type: ignore[arg-type]
        created_at=iso_required(export.created_at),
        completed_at=iso(export.completed_at),
    )


def export_response(export: models.Export) -> ExportResponse:
    return ExportResponse(
        id=export.id,
        job_id=export.job_id,
        state=export.state,  # type: ignore[arg-type]
        review_revision=export.review_revision,
        resolutions=list(export.resolutions),  # type: ignore[arg-type]
        include_audio=export.include_audio,
        progress=ExportProgressModel(
            percent=export.progress_percent, message=export.progress_message
        ),
        error=error_model(export.error),
        manifest_available=export.manifest_available,
        download_available=export.state == "complete" and export.zip_relative_path is not None,
        created_at=iso_required(export.created_at),
        updated_at=iso_required(export.updated_at),
        completed_at=iso(export.completed_at),
    )


def export_file_model(row: models.ExportFile, *, available: bool) -> ExportFileModel:
    base = f"/api/v1/jobs/{row.export.job_id}/exports/{row.export_id}"
    return ExportFileModel(
        id=row.id,
        serial=row.serial,
        resolution=row.resolution,  # type: ignore[arg-type]
        candidate_id=row.candidate_id,
        # Built from a validated serial and a fixed folder name (spec 10.2).
        file_name=f"clip-{row.serial:04d}-{row.resolution}.mp4",
        width=row.width,
        height=row.height,
        size_bytes=row.size_bytes,
        duration_us=row.duration_us,
        sha256=row.sha256,
        download_url=f"{base}/files/{row.id}/download",
        available=available,
    )


def video_model(job: models.Job) -> VideoModel | None:
    if not job.video:
        return None
    return VideoModel(
        sha256=job.source_sha256,
        duration_us=int(job.video["durationUs"]),
        width=int(job.video["width"]),
        height=int(job.video["height"]),
        average_frame_rate=str(job.video["averageFrameRate"]),
        has_audio=bool(job.video["hasAudio"]),
    )


def job_response(
    job: models.Job, *, usage: models.AnalysisUsage | None, latest_export: models.Export | None
) -> JobResponse:
    return JobResponse(
        id=job.id,
        upload_id=job.upload_id,
        state=job.state,  # type: ignore[arg-type]
        target_clip_count=job.target_clip_count,
        content_prompt=job.content_prompt,
        source_label=(
            SourceLabelModel(
                text=job.source_name,
                style=SourceLabelStyleModel.model_validate(job.source_label_style),
            )
            if job.source_name and job.source_label_style
            else None
        ),
        use_gemini=job.use_gemini,
        video=video_model(job),
        progress=ProgressModel(
            phase=job.progress_phase,  # type: ignore[arg-type]
            percent=round(job.progress_percent, 2),
            message=job.progress_message,
        ),
        eligible_count=job.eligible_count,
        selected_count=job.selected_count,
        review_revision=job.review_revision,
        latest_export=export_summary(latest_export),
        partial_result_reason=job.partial_result_reason,
        warnings=list(job.warnings or []),
        usage=usage_model(usage, job),
        error=error_model(job.error),
        created_at=iso_required(job.created_at),
        updated_at=iso_required(job.updated_at),
    )


def job_summary(
    job: models.Job, *, source_file_name: str, latest_export: models.Export | None
) -> JobSummaryModel:
    return JobSummaryModel(
        id=job.id,
        source_file_name=source_file_name,
        state=job.state,  # type: ignore[arg-type]
        progress_percent=round(job.progress_percent, 2),
        target_clip_count=job.target_clip_count,
        selected_count=job.selected_count,
        latest_export=export_summary(latest_export),
        created_at=iso_required(job.created_at),
        updated_at=iso_required(job.updated_at),
    )


def candidate_model(candidate: models.CandidateShot) -> CandidateShotModel:
    base = f"/api/v1/jobs/{candidate.job_id}/candidates/{candidate.id}"
    return CandidateShotModel(
        id=candidate.id,
        job_id=candidate.job_id,
        shot_number=candidate.shot_number,
        source_start_us=candidate.source_start_us,
        source_end_us=candidate.source_end_us,
        safe_start_us=candidate.safe_start_us,
        safe_end_us=candidate.safe_end_us,
        usable_duration_us=candidate.usable_duration_us,
        recommended_start_us=candidate.recommended_start_us,
        recommended_end_us=candidate.recommended_end_us,
        incoming_boundary=candidate.incoming_boundary,  # type: ignore[arg-type]
        outgoing_boundary=candidate.outgoing_boundary,  # type: ignore[arg-type]
        rank=candidate.rank,
        score=round(candidate.score, 4),
        confidence=round(candidate.confidence, 4),
        reason=candidate.reason,
        scoring_source=candidate.scoring_source,  # type: ignore[arg-type]
        cache_status=candidate.cache_status,  # type: ignore[arg-type]
        prompt_relevance_evaluated=candidate.prompt_relevance_evaluated,
        # Authenticated routes; /data is never served statically (spec 10.2).
        thumbnail_url=f"{base}/thumbnail",
        preview_url=f"{base}/preview",
        human_present=candidate.human_present,
        human_source=candidate.human_source,  # type: ignore[arg-type]
        product_visible=candidate.product_visible,
        product_prominence=candidate.product_prominence,
        excluded_reason=candidate.excluded_reason,  # type: ignore[arg-type]
    )


def selected_clip_model(clip: models.SelectedClip) -> SelectedClipModel:
    return SelectedClipModel(
        id=clip.id,
        candidate_id=clip.candidate_id,
        order=clip.order_index,
        start_us=clip.start_us,
        end_us=clip.end_us,
        duration_us=clip.duration_us,
    )
