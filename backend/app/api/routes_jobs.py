"""Job, candidate, preview, and review routes (spec 8.4)."""

from __future__ import annotations

from fastapi import APIRouter, Header, Query, Request, Response, status
from starlette.responses import StreamingResponse

from app.api.deps import AuthDep, CsrfDep, DbDep, RequestIdDep, SettingsDep
from app.api.errors import not_found
from app.api.serializers import (
    candidate_model,
    job_response,
    job_summary,
    selected_clip_model,
)
from app.api.streaming import stream_file
from app.db import session_scope
from app.models import CandidateShot
from app.schemas import (
    CandidatesResponse,
    CreateJobRequest,
    JobListResponse,
    JobResponse,
    ReviewRequest,
    ReviewResponse,
)
from app.services import audit, events, idempotency, jobs, review, storage
from app.workers.queue import enqueue_analysis, enqueue_job_cleanup

router = APIRouter(tags=["jobs"])


def _job_payload(db, job) -> JobResponse:  # noqa: ANN001
    return job_response(
        job, usage=jobs.usage_for(db, job.id), latest_export=jobs.latest_export(db, job.id)
    )


@router.get("/jobs", response_model=JobListResponse, response_model_by_alias=True)
def list_jobs(
    db: DbDep,
    auth: AuthDep,
    cursor: str | None = Query(default=None),
    limit: int = Query(default=25, ge=1, le=100),
) -> JobListResponse:
    page = jobs.list_jobs(db, cursor=cursor, limit=limit)
    return JobListResponse(
        items=[
            job_summary(job, source_file_name=file_name, latest_export=export)
            for job, file_name, export in page.items
        ],
        next_cursor=page.next_cursor,
    )


@router.post(
    "/jobs",
    response_model=JobResponse,
    response_model_by_alias=True,
    status_code=status.HTTP_202_ACCEPTED,
)
def create_job(
    payload: CreateJobRequest,
    db: DbDep,
    settings: SettingsDep,
    auth: CsrfDep,
    request_id: RequestIdDep,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> JobResponse:
    body = payload.model_dump(by_alias=True)
    replay = idempotency.lookup(
        db, key=idempotency_key, principal=auth.username, method="POST", route="/jobs", body=body
    )
    if replay is not None and replay.resource_id:
        return _job_payload(db, jobs.get_job(db, replay.resource_id))

    job = jobs.create_job(
        db,
        settings,
        upload_id=payload.upload_id,
        target_clip_count=payload.target_clip_count,
        content_prompt=payload.content_prompt,
        source_name=payload.source_name,
        use_gemini=payload.use_gemini,
    )
    audit.record(
        db,
        audit.JOB_CREATED,
        subject=auth.username,
        request_id=request_id,
        detail={
            "jobId": job.id,
            "useGemini": job.use_gemini,
            "target": job.target_clip_count,
            "sourceLabelEnabled": job.source_name is not None,
        },
    )
    response = _job_payload(db, job)
    idempotency.remember(
        db,
        key=idempotency_key,
        principal=auth.username,
        method="POST",
        route="/jobs",
        body=body,
        status_code=status.HTTP_202_ACCEPTED,
        response_body=response.model_dump(by_alias=True),
        resource_id=job.id,
    )
    enqueue_analysis(job.id)
    return response


@router.get("/jobs/{job_id}", response_model=JobResponse, response_model_by_alias=True)
def read_job(job_id: str, db: DbDep, auth: AuthDep) -> JobResponse:
    return _job_payload(db, jobs.get_job(db, job_id))


@router.get("/jobs/{job_id}/events")
async def job_events(
    job_id: str,
    request: Request,
    auth: AuthDep,
    last_event_id: str | None = Header(default=None, alias="Last-Event-ID"),
) -> StreamingResponse:
    """Authenticated SSE with ``Last-Event-ID`` recovery (spec 8.4)."""
    with session_scope() as db:
        jobs.get_job(db, job_id)

    # The query parameter form exists because EventSource cannot set headers on
    # a manual reconnect in some browsers.
    header_value = last_event_id or request.query_params.get("lastEventId")
    start_after = events.parse_last_event_id(header_value)

    def snapshot_factory(db):  # noqa: ANN001, ANN202
        return jobs.snapshot(db, jobs.get_job(db, job_id))

    return StreamingResponse(
        events.stream(job_id, start_after, snapshot_factory),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-store",
            "Connection": "keep-alive",
            # Defeats proxy buffering, which would otherwise batch events.
            "X-Accel-Buffering": "no",
        },
    )


@router.post(
    "/jobs/{job_id}/cancel",
    response_model=JobResponse,
    response_model_by_alias=True,
    status_code=status.HTTP_202_ACCEPTED,
)
def cancel_job(
    job_id: str,
    db: DbDep,
    auth: CsrfDep,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> JobResponse:
    job = jobs.get_job(db, job_id)
    route = "/jobs/{jobId}/cancel"
    body = {"jobId": job_id}

    # Cancellation is idempotent by construction; the key is honoured so a
    # client retrying a lost response sees the original outcome (spec 8.1).
    replay = idempotency.lookup(
        db, key=idempotency_key, principal=auth.username, method="POST", route=route, body=body
    )
    if replay is None:
        jobs.request_cancel(db, job)
        idempotency.remember(
            db,
            key=idempotency_key,
            principal=auth.username,
            method="POST",
            route=route,
            body=body,
            status_code=status.HTTP_202_ACCEPTED,
            response_body=None,
            resource_id=job.id,
        )
    return _job_payload(db, job)


@router.post(
    "/jobs/{job_id}/retry",
    response_model=JobResponse,
    response_model_by_alias=True,
    status_code=status.HTTP_202_ACCEPTED,
)
def retry_job(
    job_id: str,
    db: DbDep,
    auth: CsrfDep,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> JobResponse:
    job = jobs.get_job(db, job_id)
    route = "/jobs/{jobId}/retry"
    body = {"jobId": job_id, "revision": job.review_revision}
    replay = idempotency.lookup(
        db, key=idempotency_key, principal=auth.username, method="POST", route=route, body=body
    )
    if replay is None:
        jobs.prepare_retry(db, job)
        idempotency.remember(
            db,
            key=idempotency_key,
            principal=auth.username,
            method="POST",
            route=route,
            body=body,
            status_code=status.HTTP_202_ACCEPTED,
            response_body=None,
            resource_id=job.id,
        )
    enqueue_analysis(job.id)
    return _job_payload(db, job)


@router.get(
    "/jobs/{job_id}/candidates",
    response_model=CandidatesResponse,
    response_model_by_alias=True,
)
def read_candidates(job_id: str, db: DbDep, auth: AuthDep) -> CandidatesResponse:
    job = jobs.get_job(db, job_id)
    return CandidatesResponse(
        candidates=[candidate_model(c) for c in review.list_candidates(db, job.id)],
        selected_clips=[selected_clip_model(c) for c in review.list_selected(db, job.id)],
        review_revision=job.review_revision,
    )


def _load_candidate(db, job_id: str, candidate_id: str) -> CandidateShot:  # noqa: ANN001
    jobs.get_job(db, job_id)
    candidate = db.get(CandidateShot, candidate_id)
    # A candidate belonging to another job is reported as missing rather than
    # forbidden, so ids cannot be probed across jobs.
    if candidate is None or candidate.job_id != job_id:
        raise not_found("candidate")
    return candidate


@router.get("/jobs/{job_id}/candidates/{candidate_id}/thumbnail")
def candidate_thumbnail(
    job_id: str, candidate_id: str, db: DbDep, settings: SettingsDep, auth: AuthDep
) -> Response:
    candidate = _load_candidate(db, job_id, candidate_id)
    if not candidate.thumbnail_path:
        raise not_found("thumbnail")
    return stream_file(
        storage.resolve(candidate.thumbnail_path, settings),
        media_type="image/jpeg",
        allow_range=False,
    )


@router.get("/jobs/{job_id}/candidates/{candidate_id}/preview")
def candidate_preview(
    job_id: str,
    candidate_id: str,
    db: DbDep,
    settings: SettingsDep,
    auth: AuthDep,
    range_header: str | None = Header(default=None, alias="Range"),
) -> Response:
    """Authenticated, range-enabled preview stream (spec 8.4)."""
    candidate = _load_candidate(db, job_id, candidate_id)
    relative = f"{storage.job_subdir(job_id, 'previews')}/{candidate.id}.mp4"
    path = storage.resolve(relative, settings)

    if not path.is_file():
        # Previews are pre-rendered for the auto-selected clips; any other
        # candidate is rendered the first time it is actually previewed.
        from app.analysis.pipeline import ensure_preview
        from app.media.runner import MediaToolError

        try:
            path = ensure_preview(job_id, candidate.id, settings)
        except (FileNotFoundError, MediaToolError) as exc:
            raise not_found("preview") from exc

    return stream_file(path, media_type="video/mp4", range_header=range_header)


@router.put(
    "/jobs/{job_id}/review", response_model=ReviewResponse, response_model_by_alias=True
)
def replace_review(
    job_id: str,
    payload: ReviewRequest,
    db: DbDep,
    auth: CsrfDep,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> ReviewResponse:
    job = jobs.get_job(db, job_id)
    route = "/jobs/{jobId}/review"
    body = payload.model_dump(by_alias=True)
    replay = idempotency.lookup(
        db, key=idempotency_key, principal=auth.username, method="PUT", route=route, body=body
    )
    if replay is not None and replay.body is not None:
        return ReviewResponse.model_validate(replay.body)

    revision, rows = review.replace_review(
        db,
        job,
        revision=payload.revision,
        clips=[(c.candidate_id, c.order, c.start_us, c.end_us) for c in payload.clips],
        export_active=jobs.active_export(db, job.id) is not None,
    )
    response = ReviewResponse(
        review_revision=revision, clips=[selected_clip_model(row) for row in rows]
    )
    idempotency.remember(
        db,
        key=idempotency_key,
        principal=auth.username,
        method="PUT",
        route=route,
        body=body,
        status_code=status.HTTP_200_OK,
        response_body=response.model_dump(by_alias=True),
        resource_id=job.id,
    )
    return response


@router.delete("/jobs/{job_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_job(
    job_id: str,
    db: DbDep,
    auth: CsrfDep,
    request_id: RequestIdDep,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> Response:
    """Tombstone immediately; physical cleanup continues internally (spec 7.3)."""
    job = jobs.get_job(db, job_id)
    route = "/jobs/{jobId}"
    body = {"jobId": job_id}

    replay = idempotency.lookup(
        db, key=idempotency_key, principal=auth.username, method="DELETE", route=route, body=body
    )
    if replay is None:
        jobs.tombstone(db, job)
        audit.record(
            db,
            audit.JOB_DELETED,
            subject=auth.username,
            request_id=request_id,
            detail={"jobId": job.id},
        )
        idempotency.remember(
            db,
            key=idempotency_key,
            principal=auth.username,
            method="DELETE",
            route=route,
            body=body,
            status_code=status.HTTP_204_NO_CONTENT,
            response_body=None,
            resource_id=job.id,
        )
    enqueue_job_cleanup(job.id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
