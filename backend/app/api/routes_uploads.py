"""Resumable upload routes (spec 8.3)."""

from __future__ import annotations

from fastapi import APIRouter, Header, Request, Response, status

from app.api.deps import AuthDep, CsrfDep, DbDep, SettingsDep
from app.api.errors import AppError, validation_error
from app.api.serializers import upload_response
from app.db import session_scope
from app.schemas import CreateUploadRequest, CreateUrlUploadRequest, UploadResponse
from app.services import idempotency, uploads
from app.workers.queue import enqueue_upload_verification, enqueue_url_download

router = APIRouter(tags=["uploads"])

CHUNK_READ_BYTES = 512 * 1024


@router.post(
    "/uploads",
    response_model=UploadResponse,
    response_model_by_alias=True,
    status_code=status.HTTP_201_CREATED,
)
def create_upload(
    payload: CreateUploadRequest,
    db: DbDep,
    settings: SettingsDep,
    auth: CsrfDep,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> UploadResponse:
    body = payload.model_dump(by_alias=True)
    replay = idempotency.lookup(
        db,
        key=idempotency_key,
        principal=auth.username,
        method="POST",
        route="/uploads",
        body=body,
    )
    if replay is not None and replay.resource_id:
        return upload_response(uploads.get_upload(db, replay.resource_id))

    upload = uploads.create_upload(
        db,
        settings,
        file_name=payload.file_name,
        size_bytes=payload.size_bytes,
        mime_type=payload.mime_type,
        client_sha256=payload.sha256,
    )
    response = upload_response(upload)
    idempotency.remember(
        db,
        key=idempotency_key,
        principal=auth.username,
        method="POST",
        route="/uploads",
        body=body,
        status_code=status.HTTP_201_CREATED,
        response_body=response.model_dump(by_alias=True),
        resource_id=upload.id,
    )
    return response


@router.post(
    "/uploads/from-url",
    response_model=UploadResponse,
    response_model_by_alias=True,
    status_code=status.HTTP_202_ACCEPTED,
)
def create_url_upload(
    payload: CreateUrlUploadRequest,
    db: DbDep,
    settings: SettingsDep,
    auth: CsrfDep,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> UploadResponse:
    """Queue one public video URL for highest-quality download and verification."""
    route = "/uploads/from-url"
    body = payload.model_dump(by_alias=True)
    replay = idempotency.lookup(
        db,
        key=idempotency_key,
        principal=auth.username,
        method="POST",
        route=route,
        body=body,
    )
    if replay is not None and replay.resource_id:
        upload = uploads.get_upload(db, replay.resource_id)
        if upload.state == "downloading":
            enqueue_url_download(upload.id)
        return upload_response(upload)

    upload = uploads.create_url_upload(db, settings, url=payload.url)
    response = upload_response(upload)
    idempotency.remember(
        db,
        key=idempotency_key,
        principal=auth.username,
        method="POST",
        route=route,
        body=body,
        status_code=status.HTTP_202_ACCEPTED,
        response_body=response.model_dump(by_alias=True),
        resource_id=upload.id,
    )
    enqueue_url_download(upload.id)
    return response


@router.get(
    "/uploads/{upload_id}", response_model=UploadResponse, response_model_by_alias=True
)
def read_upload(upload_id: str, db: DbDep, auth: AuthDep) -> UploadResponse:
    return upload_response(uploads.get_upload(db, upload_id))


@router.head("/uploads/{upload_id}")
def head_upload(upload_id: str, db: DbDep, auth: AuthDep) -> Response:
    upload = uploads.get_upload(db, upload_id)
    return Response(
        status_code=status.HTTP_200_OK,
        headers={
            "Upload-Offset": str(upload.verified_offset_bytes),
            "Upload-Length": str(upload.declared_size_bytes),
            "Upload-Status": upload.state,
            "Upload-Chunk-Size": str(upload.chunk_size_bytes),
        },
    )


@router.put("/uploads/{upload_id}/chunks", status_code=status.HTTP_204_NO_CONTENT)
async def put_chunk(
    upload_id: str,
    request: Request,
    settings: SettingsDep,
    auth: CsrfDep,
    upload_offset: str = Header(alias="Upload-Offset"),
    content_length: str = Header(alias="Content-Length"),
    upload_checksum: str = Header(alias="Upload-Checksum"),
) -> Response:
    """Accept one sequential chunk.

    The route owns the streaming read; :mod:`app.services.uploads` owns every
    decision. Database transactions are deliberately short and never span the
    body transfer.
    """
    try:
        offset = int(upload_offset)
        declared_length = int(content_length)
    except ValueError:
        await _drain(request)
        raise validation_error(
            "invalid_headers", "Upload-Offset and Content-Length must be integers."
        ) from None

    digest = uploads.decode_checksum_header(upload_checksum)

    # --- Decide what to do, holding no lock during the transfer ------------
    with session_scope() as db:
        upload = uploads.get_upload(db, upload_id)
        uploads.validate_chunk_request(
            upload, settings, offset=offset, declared_length=declared_length
        )
        disposition = uploads.classify_offset(upload, offset)
        current_offset = upload.verified_offset_bytes
        if disposition == "ahead":
            error = uploads.offset_mismatch_error(upload)
        else:
            error = None

    if error is not None:
        await _drain(request)
        raise error

    if disposition == "replay":
        # Already-verified bytes: compare what is on disk, do not rewrite it.
        await _drain(request)
        with session_scope() as db:
            upload = uploads.get_upload(db, upload_id)
            outcome = uploads.verify_replayed_range(
                upload, settings, offset=offset, length=declared_length, digest=digest
            )
        return _chunk_ok(outcome.offset)

    # --- Forward chunk: stream straight into the source file ---------------
    with session_scope() as db:
        upload = uploads.get_upload(db, upload_id)
        writer_upload = upload

    accepted = 0
    try:
        with uploads.ChunkWriter(
            writer_upload, settings, offset=offset, declared_length=declared_length
        ) as writer:
            async for block in request.stream():
                writer.write(block)
            accepted = writer.finish(digest)
    except AppError:
        with session_scope() as db:
            upload = uploads.get_upload(db, upload_id)
            uploads.truncate_to_verified(upload, settings)
        raise
    except OSError as exc:
        with session_scope() as db:
            upload = uploads.get_upload(db, upload_id)
            uploads.truncate_to_verified(upload, settings)
        if getattr(exc, "errno", None) == 28:  # ENOSPC
            from app.api.errors import insufficient_storage

            raise insufficient_storage("The server ran out of disk space.") from None
        raise

    with session_scope() as db:
        upload = uploads.get_upload(db, upload_id)
        # Guard against a concurrent duplicate request having advanced first.
        if upload.verified_offset_bytes != current_offset:
            return _chunk_ok(upload.verified_offset_bytes)
        outcome = uploads.commit_chunk(db, upload, accepted_bytes=accepted)
    return _chunk_ok(outcome.offset)


@router.post(
    "/uploads/{upload_id}/complete",
    response_model=UploadResponse,
    response_model_by_alias=True,
    status_code=status.HTTP_202_ACCEPTED,
)
def complete_upload(
    upload_id: str,
    db: DbDep,
    settings: SettingsDep,
    auth: CsrfDep,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> UploadResponse:
    """Freeze the transfer and hand off to asynchronous media verification."""
    route = "/uploads/{uploadId}/complete"
    body = {"uploadId": upload_id}
    replay = idempotency.lookup(
        db, key=idempotency_key, principal=auth.username, method="POST", route=route, body=body
    )

    upload = uploads.get_upload(db, upload_id)
    if replay is None:
        uploads.begin_completion(db, upload, settings)
        idempotency.remember(
            db,
            key=idempotency_key,
            principal=auth.username,
            method="POST",
            route=route,
            body=body,
            status_code=status.HTTP_202_ACCEPTED,
            response_body=None,
            resource_id=upload.id,
        )

    if upload.state == "verifying":
        # Verification is restart-safe: a worker that never picks this up is
        # recovered by the startup requeue sweep.
        enqueue_upload_verification(upload.id)
    return upload_response(upload)


@router.delete("/uploads/{upload_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_upload(upload_id: str, db: DbDep, settings: SettingsDep, auth: CsrfDep) -> Response:
    upload = uploads.get_upload(db, upload_id)
    uploads.delete_upload(db, upload, settings)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# --- helpers ---------------------------------------------------------------


def _chunk_ok(offset: int) -> Response:
    return Response(
        status_code=status.HTTP_204_NO_CONTENT, headers={"Upload-Offset": str(offset)}
    )


async def _drain(request: Request) -> None:
    """Consume a rejected body so the connection can be reused cleanly."""
    try:
        async for _ in request.stream():
            pass
    except Exception:  # noqa: BLE001 - the client may have already hung up
        return
