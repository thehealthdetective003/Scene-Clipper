"""Resumable upload semantics (spec 5.3, 8.3, 12.3)."""

from __future__ import annotations

import base64
import hashlib

import pytest

UPLOADS = "/api/v1/uploads"


def checksum_header(payload: bytes) -> str:
    return "sha256 " + base64.b64encode(hashlib.sha256(payload).digest()).decode()


def create(client, *, size: int, name: str = "holiday.mp4", sha256: str | None = None):
    body = {"fileName": name, "sizeBytes": size, "mimeType": "video/mp4"}
    if sha256:
        body["sha256"] = sha256
    return client.post(UPLOADS, json=body)


def put_chunk(client, upload_id: str, offset: int, payload: bytes, *, checksum: str | None = None):
    return client.put(
        f"{UPLOADS}/{upload_id}/chunks",
        content=payload,
        headers={
            "Upload-Offset": str(offset),
            "Content-Length": str(len(payload)),
            "Upload-Checksum": checksum or checksum_header(payload),
            "Content-Type": "application/octet-stream",
        },
    )


class TestCreate:
    def test_returns_chunk_size(self, auth_client, settings):
        response = create(auth_client, size=1024)
        assert response.status_code == 201
        body = response.json()
        assert body["chunkSizeBytes"] == settings.upload_chunk_bytes
        assert body["state"] == "created"
        assert body["verifiedOffsetBytes"] == 0

    def test_oversized_declaration_is_refused(self, auth_client, settings):
        response = create(auth_client, size=settings.max_upload_bytes + 1)
        assert response.status_code == 413
        assert response.json()["error"]["code"] == "upload_too_large"

    def test_malicious_filename_is_kept_as_display_metadata_only(self, auth_client):
        response = create(auth_client, size=16, name="../../etc/passwd\x00.mp4")
        assert response.status_code == 201
        stored = response.json()["fileName"]
        assert "/" not in stored and "\x00" not in stored

    def test_storage_path_is_server_generated(self, auth_client, settings):
        upload_id = create(auth_client, size=16, name="weird name.MOV").json()["id"]
        # The directory is named by the upload UUID, never the client's name.
        assert (settings.uploads_dir / upload_id).is_dir()

    def test_idempotency_key_replays_the_same_upload(self, auth_client):
        headers = {"Idempotency-Key": "upload-key-1"}
        body = {"fileName": "a.mp4", "sizeBytes": 64, "mimeType": "video/mp4"}
        first = auth_client.post(UPLOADS, json=body, headers=headers)
        second = auth_client.post(UPLOADS, json=body, headers=headers)
        assert first.json()["id"] == second.json()["id"]

    def test_idempotency_key_with_a_different_body_conflicts(self, auth_client):
        headers = {"Idempotency-Key": "upload-key-2"}
        auth_client.post(
            UPLOADS, json={"fileName": "a.mp4", "sizeBytes": 64}, headers=headers
        )
        clash = auth_client.post(
            UPLOADS, json={"fileName": "b.mp4", "sizeBytes": 64}, headers=headers
        )
        assert clash.status_code == 409
        assert clash.json()["error"]["code"] == "idempotency_conflict"


class TestChunkTransfer:
    @pytest.fixture
    def upload(self, auth_client):
        return create(auth_client, size=30).json()

    @pytest.fixture
    def upload_without_csrf(self, client):
        """Create an upload with CSRF, then drop the header from the client."""
        client.post(
            "/api/v1/auth/login", json={"username": "admin", "password": _password()}
        )
        client.headers["X-CSRF-Token"] = client.get("/api/v1/session").json()["csrfToken"]
        upload_id = create(client, size=30).json()["id"]
        del client.headers["X-CSRF-Token"]
        return upload_id

    def test_sequential_chunks_advance_the_offset(self, auth_client, upload):
        first = put_chunk(auth_client, upload["id"], 0, b"A" * 10)
        assert first.status_code == 204
        assert first.headers["Upload-Offset"] == "10"

        second = put_chunk(auth_client, upload["id"], 10, b"B" * 10)
        assert second.headers["Upload-Offset"] == "20"

    def test_head_reports_progress(self, auth_client, upload):
        put_chunk(auth_client, upload["id"], 0, b"A" * 10)
        response = auth_client.head(f"{UPLOADS}/{upload['id']}")
        assert response.headers["Upload-Offset"] == "10"
        assert response.headers["Upload-Length"] == "30"
        assert response.headers["Upload-Status"] == "uploading"

    def test_identical_retransmission_is_accepted(self, auth_client, upload):
        put_chunk(auth_client, upload["id"], 0, b"A" * 10)
        put_chunk(auth_client, upload["id"], 10, b"B" * 10)

        replay = put_chunk(auth_client, upload["id"], 0, b"A" * 10)
        assert replay.status_code == 204
        # The offset is unchanged by a replay (spec 5.3).
        assert replay.headers["Upload-Offset"] == "20"

    def test_overlapping_content_with_different_bytes_is_rejected(self, auth_client, upload):
        put_chunk(auth_client, upload["id"], 0, b"A" * 10)
        clash = put_chunk(auth_client, upload["id"], 0, b"Z" * 10)
        assert clash.status_code == 409
        assert clash.json()["error"]["code"] == "upload_chunk_mismatch"

    def test_offset_ahead_of_the_server_is_refused_with_the_expected_offset(
        self, auth_client, upload
    ):
        put_chunk(auth_client, upload["id"], 0, b"A" * 10)
        ahead = put_chunk(auth_client, upload["id"], 20, b"C" * 10)
        assert ahead.status_code == 409
        body = ahead.json()["error"]
        assert body["code"] == "upload_offset_mismatch"
        assert body["details"]["expectedOffset"] == 10

    def test_checksum_mismatch_is_rejected_and_does_not_advance(self, auth_client, upload):
        bad = put_chunk(
            auth_client, upload["id"], 0, b"A" * 10, checksum=checksum_header(b"different")
        )
        assert bad.status_code == 409
        assert bad.json()["error"]["code"] == "upload_chunk_mismatch"
        assert auth_client.get(f"{UPLOADS}/{upload['id']}").json()["verifiedOffsetBytes"] == 0

    def test_interrupted_upload_resumes_without_retransmitting_verified_bytes(
        self, auth_client
    ):
        """Acceptance criterion 14: resume sends only what is still missing."""
        payload = bytes(range(256)) * 4  # 1024 bytes
        upload = create(auth_client, size=len(payload)).json()
        chunk = 256

        # Transfer the first two chunks, then "lose" the connection.
        put_chunk(auth_client, upload["id"], 0, payload[0:chunk])
        put_chunk(auth_client, upload["id"], chunk, payload[chunk : 2 * chunk])

        # A resuming client asks where to continue.
        head = auth_client.head(f"{UPLOADS}/{upload['id']}")
        resume_at = int(head.headers["Upload-Offset"])
        assert resume_at == 2 * chunk

        sent_bytes = 0
        offset = resume_at
        while offset < len(payload):
            block = payload[offset : offset + chunk]
            response = put_chunk(auth_client, upload["id"], offset, block)
            assert response.status_code == 204
            sent_bytes += len(block)
            offset = int(response.headers["Upload-Offset"])

        # Only the remaining half crossed the wire.
        assert sent_bytes == len(payload) - resume_at
        assert auth_client.post(f"{UPLOADS}/{upload['id']}/complete").status_code == 202

        # And the stored bytes are exactly the original file.
        from app.services import storage, uploads as upload_service
        from app.db import session_scope

        with session_scope() as db:
            row = upload_service.get_upload(db, upload["id"])
            stored = storage.resolve(row.relative_source_path).read_bytes()
        assert stored == payload

    def test_resume_after_a_rejected_chunk(self, auth_client, upload):
        put_chunk(auth_client, upload["id"], 0, b"A" * 10, checksum=checksum_header(b"x"))
        good = put_chunk(auth_client, upload["id"], 0, b"A" * 10)
        assert good.status_code == 204
        assert good.headers["Upload-Offset"] == "10"

    def test_chunk_past_the_declared_size_is_refused(self, auth_client, upload):
        response = put_chunk(auth_client, upload["id"], 0, b"A" * 40)
        assert response.status_code == 413

    @pytest.mark.parametrize("header", ["", "md5 abcdef", "sha256", "sha256 !!!notbase64"])
    def test_malformed_checksum_headers_are_refused(self, auth_client, upload, header):
        response = auth_client.put(
            f"{UPLOADS}/{upload['id']}/chunks",
            content=b"A" * 10,
            headers={
                "Upload-Offset": "0",
                "Content-Length": "10",
                "Upload-Checksum": header,
            },
        )
        assert response.status_code == 422

    def test_malformed_offset_header_is_refused(self, auth_client, upload):
        response = auth_client.put(
            f"{UPLOADS}/{upload['id']}/chunks",
            content=b"A" * 10,
            headers={
                "Upload-Offset": "not-a-number",
                "Content-Length": "10",
                "Upload-Checksum": checksum_header(b"A" * 10),
            },
        )
        assert response.status_code == 422

    def test_chunk_upload_requires_csrf(self, client, upload_without_csrf):
        # Authenticated but with no CSRF header: rejected before any bytes land.
        response = client.put(
            f"{UPLOADS}/{upload_without_csrf}/chunks",
            content=b"A" * 10,
            headers={
                "Upload-Offset": "0",
                "Content-Length": "10",
                "Upload-Checksum": checksum_header(b"A" * 10),
            },
        )
        assert response.status_code == 403
        assert response.json()["error"]["code"] == "invalid_csrf_token"


class TestCompletion:
    def test_completion_requires_all_bytes(self, auth_client):
        upload = create(auth_client, size=30).json()
        put_chunk(auth_client, upload["id"], 0, b"A" * 10)
        response = auth_client.post(f"{UPLOADS}/{upload['id']}/complete")
        assert response.status_code == 409
        assert response.json()["error"]["code"] == "upload_incomplete"

    def test_completion_moves_to_verifying_and_enqueues(self, auth_client, no_queue):
        payload = b"A" * 30
        upload = create(auth_client, size=len(payload)).json()
        put_chunk(auth_client, upload["id"], 0, payload)

        response = auth_client.post(f"{UPLOADS}/{upload['id']}/complete")
        assert response.status_code == 202
        assert response.json()["state"] == "verifying"
        assert upload["id"] in no_queue["upload"]

    def test_completion_is_idempotent(self, auth_client):
        payload = b"A" * 30
        upload = create(auth_client, size=len(payload)).json()
        put_chunk(auth_client, upload["id"], 0, payload)
        first = auth_client.post(f"{UPLOADS}/{upload['id']}/complete")
        second = auth_client.post(f"{UPLOADS}/{upload['id']}/complete")
        assert first.status_code == second.status_code == 202

    def test_chunks_are_refused_after_completion(self, auth_client):
        payload = b"A" * 30
        upload = create(auth_client, size=len(payload)).json()
        put_chunk(auth_client, upload["id"], 0, payload)
        auth_client.post(f"{UPLOADS}/{upload['id']}/complete")

        response = put_chunk(auth_client, upload["id"], 0, payload)
        assert response.status_code == 409
        assert response.json()["error"]["code"] == "upload_already_complete"

    def test_client_hash_mismatch_fails_verification(self, auth_client):
        from app.workers.tasks import verify_upload

        payload = b"A" * 30
        upload = create(
            auth_client, size=len(payload), sha256=hashlib.sha256(b"other").hexdigest()
        ).json()
        put_chunk(auth_client, upload["id"], 0, payload)
        auth_client.post(f"{UPLOADS}/{upload['id']}/complete")

        verify_upload(upload["id"])

        body = auth_client.get(f"{UPLOADS}/{upload['id']}").json()
        assert body["state"] == "failed"
        assert body["error"]["code"] == "upload_hash_mismatch"
        # The authoritative server digest is not disclosed on mismatch.
        assert body["sha256"] is None

    def test_corrupt_media_fails_verification(self, auth_client):
        from app.workers.tasks import verify_upload

        payload = b"\x00\x00\x00\x18ftypmp42" + b"\xff" * 100
        upload = create(auth_client, size=len(payload)).json()
        put_chunk(auth_client, upload["id"], 0, payload)
        auth_client.post(f"{UPLOADS}/{upload['id']}/complete")

        verify_upload(upload["id"])

        body = auth_client.get(f"{UPLOADS}/{upload['id']}").json()
        assert body["state"] == "failed"
        assert body["error"]["retryable"] is False


class TestDeletion:
    def test_unreferenced_upload_can_be_deleted(self, auth_client, settings):
        upload = create(auth_client, size=10).json()
        assert auth_client.delete(f"{UPLOADS}/{upload['id']}").status_code == 204
        assert not (settings.uploads_dir / upload["id"]).exists()
        assert auth_client.get(f"{UPLOADS}/{upload['id']}").status_code == 404

    def test_unknown_upload_is_not_found(self, auth_client):
        assert auth_client.get(f"{UPLOADS}/missing-id").status_code == 404


def _password() -> str:
    from tests.conftest import TEST_PASSWORD

    return TEST_PASSWORD
