"""Job listing, events, and idempotency (spec 8.1, 8.4)."""

from __future__ import annotations

import json

import pytest

from app.db import session_scope
from app.models import Job, Upload, utcnow
from app.services import events, jobs as job_service
from app.util.ids import new_id

JOBS = "/api/v1/jobs"


def make_job(file_name: str = "clip.mp4") -> str:
    """Create a ready upload and a job directly, without running media work."""
    with session_scope() as db:
        upload = Upload(
            file_name=file_name,
            declared_size_bytes=1024,
            verified_offset_bytes=1024,
            chunk_size_bytes=1024,
            state="ready",
            sha256="a" * 64,
            storage_ext="mp4",
        )
        db.add(upload)
        db.flush()

        job = Job(
            upload_id=upload.id,
            state="review-ready",
            source_sha256=upload.sha256,
            target_clip_count=20,
            detector_config_version="1",
            pipeline_version="1",
            feature_version="1",
            progress_phase="review-ready",
            progress_percent=100.0,
            progress_message="Ready for review.",
            warnings=[],
        )
        db.add(job)
        db.flush()
        return job.id


class TestListing:
    def test_empty_listing(self, auth_client):
        body = auth_client.get(JOBS).json()
        assert body["items"] == []
        assert body["nextCursor"] is None

    def test_newest_first(self, auth_client):
        first = make_job("first.mp4")
        second = make_job("second.mp4")
        items = auth_client.get(JOBS).json()["items"]
        assert [item["id"] for item in items] == [second, first]

    def test_cursor_pagination_covers_every_job_once(self, auth_client):
        created = [make_job(f"clip-{index}.mp4") for index in range(7)]

        seen: list[str] = []
        cursor = None
        for _ in range(10):
            params = {"limit": 3}
            if cursor:
                params["cursor"] = cursor
            page = auth_client.get(JOBS, params=params).json()
            seen.extend(item["id"] for item in page["items"])
            cursor = page["nextCursor"]
            if not cursor:
                break

        assert sorted(seen) == sorted(created)
        assert len(seen) == len(set(seen))

    def test_limit_is_capped(self, auth_client):
        assert auth_client.get(JOBS, params={"limit": 500}).status_code == 422

    def test_invalid_cursor_is_rejected(self, auth_client):
        response = auth_client.get(JOBS, params={"cursor": "!!!not-base64!!!"})
        assert response.status_code == 422
        assert response.json()["error"]["code"] == "invalid_cursor"

    def test_deleted_jobs_are_hidden(self, auth_client):
        job_id = make_job()
        assert auth_client.delete(f"{JOBS}/{job_id}").status_code == 204
        assert all(item["id"] != job_id for item in auth_client.get(JOBS).json()["items"])


class TestIdempotency:
    def test_cancel_accepts_an_idempotency_key(self, auth_client):
        job_id = make_job()
        headers = {"Idempotency-Key": "cancel-key-1"}
        first = auth_client.post(f"{JOBS}/{job_id}/cancel", headers=headers)
        second = auth_client.post(f"{JOBS}/{job_id}/cancel", headers=headers)
        assert first.status_code == second.status_code == 202

    def test_delete_accepts_an_idempotency_key(self, auth_client):
        job_id = make_job()
        headers = {"Idempotency-Key": "delete-key-1"}
        assert auth_client.delete(f"{JOBS}/{job_id}", headers=headers).status_code == 204
        # A replay finds the job already tombstoned, hence 404.
        assert auth_client.delete(f"{JOBS}/{job_id}", headers=headers).status_code == 404

    def test_same_key_different_body_conflicts(self, auth_client):
        headers = {"Idempotency-Key": "shared-key"}
        first = make_job()
        second = make_job()
        assert auth_client.post(f"{JOBS}/{first}/cancel", headers=headers).status_code == 202
        clash = auth_client.post(f"{JOBS}/{second}/cancel", headers=headers)
        assert clash.status_code == 409
        assert clash.json()["error"]["code"] == "idempotency_conflict"


class TestJobEventsStream:
    """The SSE body never ends, so it is exercised as a bounded async take.

    The route's own guards (auth, unknown job) return before streaming starts
    and are checked over HTTP normally.
    """

    @staticmethod
    async def _take(job_id: str, last_event_id: int, count: int) -> list[dict]:
        frames: list[dict] = []

        def snapshot_factory(db):
            return job_service.snapshot(db, db.get(Job, job_id))

        generator = events.stream(job_id, last_event_id, snapshot_factory)
        try:
            async for chunk in generator:
                payload = next(
                    (
                        line[len("data: ") :]
                        for line in chunk.splitlines()
                        if line.startswith("data: ")
                    ),
                    None,
                )
                if payload:
                    frames.append(json.loads(payload))
                if len(frames) >= count:
                    break
        finally:
            await generator.aclose()
        return frames

    @pytest.mark.asyncio
    async def test_stream_sends_a_snapshot_first(self):
        job_id = make_job()
        frames = await self._take(job_id, 0, 1)

        assert frames
        first = frames[0]
        assert first["jobId"] == job_id
        assert first["type"] == "job.updated"
        assert "snapshot" in first["payload"]
        assert first["payload"]["snapshot"]["state"] == "review-ready"

    @pytest.mark.asyncio
    async def test_stream_replays_events_after_the_resume_point(self):
        job_id = make_job()
        with session_scope() as db:
            for index in range(3):
                events.append(db, job_id, events.JOB_UPDATED, {"n": index})

        # Resuming after sequence 1 delivers only 2 and 3, and no snapshot.
        frames = await self._take(job_id, 1, 2)
        assert [frame["sequence"] for frame in frames] == [2, 3]
        assert all("snapshot" not in frame["payload"] for frame in frames)

    @pytest.mark.asyncio
    async def test_an_unknown_resume_point_restarts_with_a_snapshot(self):
        job_id = make_job()
        with session_scope() as db:
            events.append(db, job_id, events.JOB_UPDATED, {"n": 0})

        frames = await self._take(job_id, 999, 1)
        assert "snapshot" in frames[0]["payload"]

    def test_stream_requires_authentication(self, client):
        job_id = make_job()
        assert client.get(f"{JOBS}/{job_id}/events").status_code == 401

    def test_unknown_job_is_not_found(self, auth_client):
        assert auth_client.get(f"{JOBS}/{new_id()}/events").status_code == 404


class TestEventLedger:
    def test_sequences_are_monotonic_per_job(self):
        job_id = make_job()
        with session_scope() as db:
            for index in range(5):
                event = events.append(db, job_id, events.JOB_UPDATED, {"n": index})
                assert event.sequence == index + 1

    def test_events_after_resumes_at_the_right_point(self):
        job_id = make_job()
        with session_scope() as db:
            for index in range(5):
                events.append(db, job_id, events.JOB_UPDATED, {"n": index})

        with session_scope() as db:
            pending = events.events_after(db, job_id, 3)
            assert [event.sequence for event in pending] == [4, 5]

    def test_unknown_sequence_is_reported_as_missing(self):
        job_id = make_job()
        with session_scope() as db:
            events.append(db, job_id, events.JOB_UPDATED, {})
            assert events.sequence_exists(db, job_id, 1) is True
            assert events.sequence_exists(db, job_id, 99) is False
            # Zero means "from the beginning", which always exists.
            assert events.sequence_exists(db, job_id, 0) is True

    def test_two_jobs_keep_independent_sequences(self):
        first, second = make_job(), make_job()
        with session_scope() as db:
            assert events.append(db, first, events.JOB_UPDATED, {}).sequence == 1
            assert events.append(db, second, events.JOB_UPDATED, {}).sequence == 1

    @pytest.mark.parametrize(
        ("header", "expected"), [(None, 0), ("", 0), ("7", 7), ("-3", 0), ("nope", 0)]
    )
    def test_last_event_id_parsing(self, header, expected):
        assert events.parse_last_event_id(header) == expected

    def test_sse_frame_format(self):
        frame = events.format_sse(42, "job.updated", {"sequence": 42})
        assert frame.startswith("id: 42\nevent: job.updated\ndata: ")
        assert frame.endswith("\n\n")


class TestJobCreationValidation:
    def test_upload_must_be_ready(self, auth_client):
        with session_scope() as db:
            upload = Upload(
                file_name="pending.mp4",
                declared_size_bytes=10,
                chunk_size_bytes=10,
                state="uploading",
                storage_ext="mp4",
            )
            db.add(upload)
            db.flush()
            upload_id = upload.id

        response = auth_client.post(JOBS, json={"uploadId": upload_id})
        assert response.status_code == 409
        assert response.json()["error"]["code"] == "upload_not_ready"

    def test_unknown_upload_is_not_found(self, auth_client):
        assert auth_client.post(JOBS, json={"uploadId": new_id()}).status_code == 404

    @pytest.mark.parametrize("target", [0, -1, 101, 1000])
    def test_target_clip_count_bounds(self, auth_client, target):
        response = auth_client.post(
            JOBS, json={"uploadId": new_id(), "targetClipCount": target}
        )
        assert response.status_code == 422

    def test_prompt_normalization(self):
        assert job_service.normalize_prompt("  sunset   over  water \n") == "sunset over water"
        assert job_service.normalize_prompt("   ") is None
        assert job_service.normalize_prompt(None) is None

    def test_prompt_normalization_is_unicode_stable(self):
        # NFKC folds the composed and decomposed forms together, so both
        # produce the same cache key (spec 6.10).
        composed = job_service.normalize_prompt("café")
        decomposed = job_service.normalize_prompt("café")
        assert composed == decomposed


class TestJobTimestamps:
    def test_timestamps_are_rfc3339_utc(self, auth_client):
        job_id = make_job()
        body = auth_client.get(f"{JOBS}/{job_id}").json()
        assert body["createdAt"].endswith("Z")
        assert body["updatedAt"].endswith("Z")
        # Sanity: parseable and close to now.
        import datetime as dt

        parsed = dt.datetime.fromisoformat(body["createdAt"].replace("Z", "+00:00"))
        assert abs((utcnow() - parsed).total_seconds()) < 120
