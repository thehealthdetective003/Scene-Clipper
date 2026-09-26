"""Export routes (spec 8.5)."""

from __future__ import annotations

import json
import zipfile
from typing import Annotated

from fastapi import APIRouter, Header, Query, Response, status
from starlette.responses import StreamingResponse

from app.api.deps import AuthDep, CsrfDep, DbDep, RequestIdDep, SettingsDep
from app.api.errors import conflict, not_found, validation_error
from app.api.serializers import export_file_model, export_response
from app.api.streaming import stream_file
from app.exporting import bundle
from app.exporting import manifest as manifest_module
from app.schemas import CreateExportRequest, ExportFilesResponse, ExportResponse
from app.services import audit, exports, idempotency, jobs, storage
from app.workers.queue import enqueue_export

router = APIRouter(tags=["exports"])


@router.post(
    "/jobs/{job_id}/exports",
    response_model=ExportResponse,
    response_model_by_alias=True,
    status_code=status.HTTP_202_ACCEPTED,
)
def create_export(
    job_id: str,
    payload: CreateExportRequest,
    db: DbDep,
    auth: CsrfDep,
    request_id: RequestIdDep,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> ExportResponse:
    job = jobs.get_job(db, job_id)
    route = "/jobs/{jobId}/exports"
    body = payload.model_dump(by_alias=True) | {"jobId": job_id}

    replay = idempotency.lookup(
        db, key=idempotency_key, principal=auth.username, method="POST", route=route, body=body
    )
    if replay is not None and replay.resource_id:
        return export_response(exports.get_export(db, job_id, replay.resource_id))

    export = exports.create_export(
        db,
        job,
        review_revision=payload.review_revision,
        resolutions=list(payload.resolutions),
        include_audio=payload.include_audio,
    )
    audit.record(
        db,
        audit.EXPORT_CREATED,
        subject=auth.username,
        request_id=request_id,
        detail={
            "jobId": job.id,
            "exportId": export.id,
            "resolutions": list(export.resolutions),
            "includeAudio": export.include_audio,
        },
    )
    response = export_response(export)
    idempotency.remember(
        db,
        key=idempotency_key,
        principal=auth.username,
        method="POST",
        route=route,
        body=body,
        status_code=status.HTTP_202_ACCEPTED,
        response_body=response.model_dump(by_alias=True),
        resource_id=export.id,
    )
    enqueue_export(export.id)
    return response


@router.get(
    "/jobs/{job_id}/exports/{export_id}",
    response_model=ExportResponse,
    response_model_by_alias=True,
)
def read_export(job_id: str, export_id: str, db: DbDep, auth: AuthDep) -> ExportResponse:
    jobs.get_job(db, job_id)
    return export_response(exports.get_export(db, job_id, export_id))


@router.post(
    "/jobs/{job_id}/exports/{export_id}/cancel",
    response_model=ExportResponse,
    response_model_by_alias=True,
    status_code=status.HTTP_202_ACCEPTED,
)
def cancel_export(job_id: str, export_id: str, db: DbDep, auth: CsrfDep) -> ExportResponse:
    jobs.get_job(db, job_id)
    export = exports.get_export(db, job_id, export_id)
    exports.request_cancel(db, export)
    return export_response(export)


@router.post(
    "/jobs/{job_id}/exports/{export_id}/retry",
    response_model=ExportResponse,
    response_model_by_alias=True,
    status_code=status.HTTP_202_ACCEPTED,
)
def retry_export(
    job_id: str,
    export_id: str,
    db: DbDep,
    auth: CsrfDep,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> ExportResponse:
    job = jobs.get_job(db, job_id)
    export = exports.get_export(db, job_id, export_id)
    route = "/jobs/{jobId}/exports/{exportId}/retry"
    body = {"jobId": job_id, "exportId": export_id}

    replay = idempotency.lookup(
        db, key=idempotency_key, principal=auth.username, method="POST", route=route, body=body
    )
    if replay is None:
        exports.reset_for_retry(db, export, job)
        idempotency.remember(
            db,
            key=idempotency_key,
            principal=auth.username,
            method="POST",
            route=route,
            body=body,
            status_code=status.HTTP_202_ACCEPTED,
            response_body=None,
            resource_id=export.id,
        )
    enqueue_export(export.id)
    return export_response(export)


@router.get(
    "/jobs/{job_id}/exports/{export_id}/files",
    response_model=ExportFilesResponse,
    response_model_by_alias=True,
)
def list_export_files(
    job_id: str, export_id: str, db: DbDep, settings: SettingsDep, auth: AuthDep
) -> ExportFilesResponse:
    """Every clip in the export, each downloadable on its own as an MP4."""
    jobs.get_job(db, job_id)
    export = exports.get_export(db, job_id, export_id)

    files = []
    for row in exports.list_files(db, export.id):
        # Exports produced before individual clips were published still have
        # their rows, but only the archive exists on disk.
        try:
            available = storage.resolve(row.relative_path, settings).is_file()
        except storage.UnsafePathError:
            available = False
        files.append(export_file_model(row, available=available))

    return ExportFilesResponse(
        files=files,
        zip_download_url=(
            f"/api/v1/jobs/{job_id}/exports/{export_id}/download"
            if export.state == "complete" and export.zip_relative_path
            else None
        ),
    )


@router.get("/jobs/{job_id}/exports/{export_id}/files/{file_id}/download")
def download_export_file(
    job_id: str,
    export_id: str,
    file_id: str,
    db: DbDep,
    settings: SettingsDep,
    auth: AuthDep,
    range_header: str | None = Header(default=None, alias="Range"),
) -> Response:
    """Stream one clip directly as MP4, with range support."""
    jobs.get_job(db, job_id)
    export = exports.get_export(db, job_id, export_id)

    row = exports.get_file(db, export.id, file_id)
    if row is None:
        raise not_found("export file")

    path = storage.resolve(row.relative_path, settings)
    if not path.is_file():
        # Pre-existing export whose clips were never published individually.
        raise not_found("export file")

    return stream_file(
        path,
        media_type="video/mp4",
        range_header=range_header,
        download_name=f"clip-{row.serial:04d}-{row.resolution}.mp4",
    )


@router.get("/jobs/{job_id}/exports/{export_id}/bundle")
def download_export_bundle(
    job_id: str,
    export_id: str,
    db: DbDep,
    settings: SettingsDep,
    auth: AuthDep,
    group: Annotated[list[str], Query()] = [],  # noqa: B006 - FastAPI query list
) -> Response:
    """Stream a ZIP of exactly the selected clips.

    ``group`` repeats once per resolution, each carrying a compact serial spec:
    ``?group=original:1-5,8&group=max720p:2``. Nothing is written to disk -- the
    archive is assembled as it is sent (see :mod:`app.exporting.bundle`).
    """
    jobs.get_job(db, job_id)
    export = exports.get_export(db, job_id, export_id)

    try:
        selection = bundle.parse_selection(list(group))
    except bundle.SelectionError as exc:
        raise validation_error("invalid_selection", str(exc)) from None

    by_slot = exports.existing_files(db, export.id)
    entries: list[bundle.BundleEntry] = []
    missing: list[str] = []
    for resolution, serials in selection.items():
        for serial in sorted(serials):
            row = by_slot.get((resolution, serial))
            if row is None:
                missing.append(f"{resolution}:{serial}")
                continue
            try:
                path = storage.resolve(row.relative_path, settings)
            except storage.UnsafePathError:
                missing.append(row.archive_path)
                continue
            if not path.is_file():
                # An export from before clips were published individually, or
                # one whose files were cleaned up.
                missing.append(row.archive_path)
                continue
            entries.append(bundle.BundleEntry(arcname=row.archive_path, path=path))

    if not entries:
        raise not_found("export files")
    if missing:
        # Partial delivery would look like success while quietly dropping
        # clips the editor asked for, so it is refused instead.
        raise conflict(
            "selection_unavailable",
            "Some selected clips are no longer available. Re-export the job.",
            {"missing": sorted(missing)[:20], "missingCount": len(missing)},
        )

    entries.sort(key=lambda entry: entry.arcname)
    extras = _bundle_manifests(export, entries, settings)

    headers = {
        "Cache-Control": "private, no-store",
        "X-Content-Type-Options": "nosniff",
        # Built from server-side identifiers only (spec 10.2).
        "Content-Disposition": (
            f'attachment; filename="scene-clips-{job_id}-{export_id}-{len(entries)}.zip"'
        ),
        # The archive is produced as it streams, so its length is not known
        # up front and range requests cannot be served from it.
        "Accept-Ranges": "none",
    }
    return StreamingResponse(
        bundle.iter_zip(entries, extras), media_type="application/zip", headers=headers
    )


def _bundle_manifests(
    export, entries: list[bundle.BundleEntry], settings  # noqa: ANN001
) -> list[tuple[str, bytes]]:
    """The export's own manifests, filtered to the bundled clips.

    They are read back out of the canonical archive rather than rebuilt, so a
    bundle can never disagree with the full export about what a clip contains.
    A bundle is still useful without them, so any failure here is silent.
    """
    if not export.zip_relative_path:
        return []
    wanted = {entry.arcname for entry in entries}
    try:
        archive_path = storage.resolve(export.zip_relative_path, settings)
        if not archive_path.is_file():
            return []
        with zipfile.ZipFile(archive_path) as zf:
            manifest = json.loads(zf.read("manifest.json"))
    except (OSError, KeyError, ValueError, zipfile.BadZipFile, storage.UnsafePathError):
        return []

    clips = []
    for clip in manifest.get("clips", []):
        files = [entry for entry in clip.get("files", []) if entry.get("path") in wanted]
        if files:
            clips.append(clip | {"files": files})
    if not clips:
        return []

    filtered = manifest | {"clips": clips, "partialSelection": True}
    return [
        ("manifest.json", json.dumps(filtered, indent=2, ensure_ascii=False).encode("utf-8")),
        ("manifest.csv", manifest_module.render_csv(filtered).encode("utf-8-sig")),
    ]


@router.get("/jobs/{job_id}/exports/{export_id}/download")
def download_export(
    job_id: str,
    export_id: str,
    db: DbDep,
    settings: SettingsDep,
    auth: AuthDep,
    range_header: str | None = Header(default=None, alias="Range"),
) -> Response:
    """Stream the completed ZIP with range support (spec 8.5)."""
    jobs.get_job(db, job_id)
    export = exports.get_export(db, job_id, export_id)
    if export.state != "complete" or not export.zip_relative_path:
        raise not_found("export archive")

    return stream_file(
        storage.resolve(export.zip_relative_path, settings),
        media_type="application/zip",
        range_header=range_header,
        # Built from server-side identifiers only (spec 10.2).
        download_name=f"scene-clips-{job_id}-{export_id}.zip",
    )
