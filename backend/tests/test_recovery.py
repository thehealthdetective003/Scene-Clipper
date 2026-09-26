"""Restart, retry, cancellation, and lease recovery (spec 5.5, 11, 12.5)."""

from __future__ import annotations

import pytest
from sqlalchemy import select

from app.analysis import detector as detector_module
from app.db import session_scope
from app.models import CandidateShot, DetectedShot, Export, Job
from app.services import jobs as job_service
from app.workers import leases
from tests import fixtures_media
from tests.conftest import requires_media
from tests.helpers import JOBS, analyse, configure_key, run_job, upload_file
from tests.mock_provider import MockProvider

pytestmark = requires_media


@pytest.fixture
def source(tmp_path):
    return fixtures_media.hard_cuts(tmp_path / "media").path


class TestCheckpointResume:
    def test_rerunning_a_finished_job_is_a_no_op(self, auth_client, source):
        from app.workers.tasks import run_analysis

        job_id = analyse(auth_client, source, MockProvider())
        before = auth_client.get(f"{JOBS}/{job_id}/candidates").json()

        run_analysis(job_id)

        after = auth_client.get(f"{JOBS}/{job_id}/candidates").json()
        assert len(after["candidates"]) == len(before["candidates"])
        assert after["reviewRevision"] == before["reviewRevision"]

    def test_resume_after_a_crash_produces_no_duplicate_candidates(self, auth_client, source):
        """A worker that dies mid-detection must not double the candidate set."""
        from app.workers.tasks import run_analysis

        configure_key(auth_client, MockProvider())
        upload_id = upload_file(auth_client, source)
        job_id = auth_client.post(
            JOBS, json={"uploadId": upload_id, "useGemini": True}
        ).json()["id"]

        # First attempt: blow up after detection has written its rows.
        real_detect = detector_module.get_detector().detect
        calls = {"count": 0}

        class ExplodingDetector:
            def detect(self, *args, **kwargs):
                calls["count"] += 1
                result = real_detect(*args, **kwargs)
                if calls["count"] == 1:
                    raise RuntimeError("simulated worker crash")
                return result

        detector_module.set_detector(ExplodingDetector())
        try:
            run_analysis(job_id)
            assert auth_client.get(f"{JOBS}/{job_id}").json()["state"] == "failed"

            # Retry resumes and completes.
            auth_client.post(f"{JOBS}/{job_id}/retry")
            run_analysis(job_id)
        finally:
            detector_module.set_detector(None)

        job = auth_client.get(f"{JOBS}/{job_id}").json()
        assert job["state"] == "review-ready"

        with session_scope() as db:
            shots = db.execute(
                select(DetectedShot).where(DetectedShot.job_id == job_id)
            ).scalars().all()
            candidates = db.execute(
                select(CandidateShot).where(CandidateShot.job_id == job_id)
            ).scalars().all()

        assert len(candidates) == 4
        assert len({c.detected_shot_id for c in candidates}) == len(candidates)
        assert len({s.shot_number for s in shots}) == len(shots)

    def test_retry_resumes_from_the_committed_checkpoint(self, auth_client, source):
        configure_key(auth_client, MockProvider())
        upload_id = upload_file(auth_client, source)
        job_id = auth_client.post(JOBS, json={"uploadId": upload_id}).json()["id"]

        with session_scope() as db:
            job = db.get(Job, job_id)
            job_service.commit_checkpoint(db, job, job_service.CHECKPOINT_DETECTED)
            job_service.fail(
                db, job, phase="ranking", code="analysis_failed", message="boom", retryable=True
            )

        response = auth_client.post(f"{JOBS}/{job_id}/retry")
        assert response.status_code == 202
        # Detection is already committed, so the job resumes at ranking.
        assert response.json()["state"] == "ranking"

    def test_a_non_retryable_failure_is_refused(self, auth_client, source):
        configure_key(auth_client, MockProvider())
        upload_id = upload_file(auth_client, source)
        job_id = auth_client.post(JOBS, json={"uploadId": upload_id}).json()["id"]

        with session_scope() as db:
            job = db.get(Job, job_id)
            job_service.fail(
                db, job, phase="probing", code="decode_failed",
                message="corrupt", retryable=False,
            )

        response = auth_client.post(f"{JOBS}/{job_id}/retry")
        assert response.status_code == 409
        assert response.json()["error"]["code"] == "invalid_job_state"


class TestCancellation:
    def test_cancelling_mid_analysis_publishes_nothing_partial(self, auth_client, source):
        from app.workers.tasks import run_analysis

        configure_key(auth_client, MockProvider())
        upload_id = upload_file(auth_client, source)
        job_id = auth_client.post(JOBS, json={"uploadId": upload_id}).json()["id"]

        # Request cancellation before the worker starts.
        auth_client.post(f"{JOBS}/{job_id}/cancel")
        run_analysis(job_id)

        job = auth_client.get(f"{JOBS}/{job_id}").json()
        assert job["state"] == "cancelled"
        assert job["selectedCount"] == 0

    def test_cancellation_leaves_no_attempt_directory(self, auth_client, source, settings):
        from app.workers.tasks import run_analysis

        configure_key(auth_client, MockProvider())
        upload_id = upload_file(auth_client, source)
        job_id = auth_client.post(JOBS, json={"uploadId": upload_id}).json()["id"]

        auth_client.post(f"{JOBS}/{job_id}/cancel")
        run_analysis(job_id)

        assert not (settings.jobs_dir / job_id / "attempts").exists()

    def test_cancellation_retains_the_verified_source(self, auth_client, source, settings):
        from app.workers.tasks import run_analysis

        configure_key(auth_client, MockProvider())
        upload_id = upload_file(auth_client, source)
        job_id = auth_client.post(JOBS, json={"uploadId": upload_id}).json()["id"]

        auth_client.post(f"{JOBS}/{job_id}/cancel")
        run_analysis(job_id)

        assert (settings.uploads_dir / upload_id).exists()
        assert auth_client.get(f"/api/v1/uploads/{upload_id}").json()["state"] == "ready"

    def test_cancelling_a_finished_job_is_idempotent(self, auth_client, source):
        job_id = analyse(auth_client, source, MockProvider())
        first = auth_client.post(f"{JOBS}/{job_id}/cancel")
        second = auth_client.post(f"{JOBS}/{job_id}/cancel")
        assert first.status_code == second.status_code == 202
        assert auth_client.get(f"{JOBS}/{job_id}").json()["state"] == "review-ready"

    def test_job_cancellation_during_export_is_refused(self, auth_client, source):
        job_id = analyse(auth_client, source, MockProvider())
        job = auth_client.get(f"{JOBS}/{job_id}").json()
        auth_client.post(
            f"{JOBS}/{job_id}/exports",
            json={"reviewRevision": job["reviewRevision"], "resolutions": ["original"]},
        )

        response = auth_client.post(f"{JOBS}/{job_id}/cancel")
        assert response.status_code == 409
        assert response.json()["error"]["code"] == "export_active"

    def test_cancelled_export_restores_the_prior_state(self, auth_client, source):
        from app.workers.tasks import run_export

        job_id = analyse(auth_client, source, MockProvider())
        job = auth_client.get(f"{JOBS}/{job_id}").json()
        export_id = auth_client.post(
            f"{JOBS}/{job_id}/exports",
            json={"reviewRevision": job["reviewRevision"], "resolutions": ["original"]},
        ).json()["id"]

        auth_client.post(f"{JOBS}/{job_id}/exports/{export_id}/cancel")
        run_export(export_id)

        record = auth_client.get(f"{JOBS}/{job_id}/exports/{export_id}").json()
        assert record["state"] == "cancelled"
        assert record["downloadAvailable"] is False
        # The job returns to review-ready, its state before the first export.
        assert auth_client.get(f"{JOBS}/{job_id}").json()["state"] == "review-ready"


class TestLeases:
    def test_a_lease_blocks_a_second_worker(self):
        with session_scope() as db:
            assert leases.acquire(db, "analysis:job-1", "worker-a")
            assert not leases.acquire(db, "analysis:job-1", "worker-b")
            assert leases.is_held_by_other(db, "analysis:job-1", "worker-b")

    def test_an_expired_lease_is_reclaimable(self):
        import datetime as dt

        from app.models import WorkerLease, utcnow

        with session_scope() as db:
            assert leases.acquire(db, "analysis:job-2", "worker-a", ttl_seconds=1)
            lease = db.get(WorkerLease, "analysis:job-2")
            lease.expires_at = utcnow() - dt.timedelta(seconds=5)

        with session_scope() as db:
            assert leases.acquire(db, "analysis:job-2", "worker-b")

    def test_heartbeat_fails_once_taken_over(self):
        with session_scope() as db:
            leases.acquire(db, "analysis:job-3", "worker-a")
        with session_scope() as db:
            assert leases.heartbeat(db, "analysis:job-3", "worker-a")
            assert not leases.heartbeat(db, "analysis:job-3", "worker-b")

    def test_release_only_affects_the_owner(self):
        with session_scope() as db:
            leases.acquire(db, "analysis:job-4", "worker-a")
            leases.release(db, "analysis:job-4", "worker-b")
            assert leases.is_held_by_other(db, "analysis:job-4", "worker-b")
            leases.release(db, "analysis:job-4", "worker-a")
            assert not leases.is_held_by_other(db, "analysis:job-4", "worker-b")

    def test_a_leased_task_is_skipped_rather_than_duplicated(self, auth_client, source):
        from app.workers.tasks import run_analysis

        configure_key(auth_client, MockProvider())
        upload_id = upload_file(auth_client, source)
        job_id = auth_client.post(JOBS, json={"uploadId": upload_id}).json()["id"]

        with session_scope() as db:
            leases.acquire(db, leases.lease_key("analysis", job_id), "other-worker")

        run_analysis(job_id)

        # The task returned without touching the job.
        assert auth_client.get(f"{JOBS}/{job_id}").json()["state"] == "uploaded"


class TestServiceRestart:
    def test_a_completed_job_survives_a_restart(self, auth_client, source, settings):
        """Rebinding the engine is the in-process equivalent of a restart."""
        from app import db as db_module

        job_id = analyse(auth_client, source, MockProvider())
        before = auth_client.get(f"{JOBS}/{job_id}/candidates").json()

        engine = db_module.build_engine(settings)
        db_module.reset_engine(engine)

        after = auth_client.get(f"{JOBS}/{job_id}/candidates").json()
        assert len(after["candidates"]) == len(before["candidates"])
        assert auth_client.get(f"{JOBS}/{job_id}").json()["state"] == "review-ready"

    def test_recovery_sweep_requeues_abandoned_work(self, auth_client, source, monkeypatch):
        from app.workers import recovery

        configure_key(auth_client, MockProvider())
        upload_id = upload_file(auth_client, source)
        job_id = auth_client.post(JOBS, json={"uploadId": upload_id}).json()["id"]

        # Leave the job in an active state with no live lease, as a crash would.
        with session_scope() as db:
            job = db.get(Job, job_id)
            job_service.transition(db, job, "detecting")

        requeued: list[str] = []
        monkeypatch.setattr(
            recovery, "enqueue_analysis", lambda jid: requeued.append(jid) or True
        )
        monkeypatch.setattr(recovery, "enqueue_export", lambda _id: True)
        monkeypatch.setattr(recovery, "enqueue_upload_verification", lambda _id: True)
        monkeypatch.setattr(recovery, "enqueue_job_cleanup", lambda _id: True)
        monkeypatch.setattr(recovery, "enqueue_provider_file_cleanup", lambda: True)

        counts = recovery.requeue_abandoned_work()

        assert job_id in requeued
        assert counts["jobs"] >= 1

    def test_recovery_skips_work_another_worker_holds(self, auth_client, source, monkeypatch):
        from app.workers import recovery

        configure_key(auth_client, MockProvider())
        upload_id = upload_file(auth_client, source)
        job_id = auth_client.post(JOBS, json={"uploadId": upload_id}).json()["id"]

        with session_scope() as db:
            job = db.get(Job, job_id)
            job_service.transition(db, job, "ranking")
            leases.acquire(db, leases.lease_key("analysis", job_id), "live-worker")

        requeued: list[str] = []
        monkeypatch.setattr(
            recovery, "enqueue_analysis", lambda jid: requeued.append(jid) or True
        )
        monkeypatch.setattr(recovery, "enqueue_export", lambda _id: True)
        monkeypatch.setattr(recovery, "enqueue_upload_verification", lambda _id: True)
        monkeypatch.setattr(recovery, "enqueue_job_cleanup", lambda _id: True)
        monkeypatch.setattr(recovery, "enqueue_provider_file_cleanup", lambda: True)

        recovery.requeue_abandoned_work()
        assert job_id not in requeued


class TestStorageFailures:
    def test_insufficient_storage_fails_the_export_retryably(
        self, auth_client, source, monkeypatch
    ):
        from app.api.errors import insufficient_storage
        from app.services import storage as storage_module
        from app.workers.tasks import run_export

        job_id = analyse(auth_client, source, MockProvider())
        job = auth_client.get(f"{JOBS}/{job_id}").json()
        export_id = auth_client.post(
            f"{JOBS}/{job_id}/exports",
            json={"reviewRevision": job["reviewRevision"], "resolutions": ["original"]},
        ).json()["id"]

        def no_space(*_args, **_kwargs):
            raise insufficient_storage()

        monkeypatch.setattr(storage_module, "require_free_space", no_space)
        run_export(export_id)

        record = auth_client.get(f"{JOBS}/{job_id}/exports/{export_id}").json()
        assert record["state"] == "failed"
        assert record["error"]["code"] == "insufficient_storage"
        assert record["error"]["retryable"] is True
        # The job is restored, not left stuck in exporting.
        assert auth_client.get(f"{JOBS}/{job_id}").json()["state"] == "review-ready"

    def test_a_failed_export_can_be_retried_and_succeeds(self, auth_client, source, monkeypatch):
        from app.workers.tasks import run_export

        job_id = analyse(auth_client, source, MockProvider())
        job = auth_client.get(f"{JOBS}/{job_id}").json()
        export_id = auth_client.post(
            f"{JOBS}/{job_id}/exports",
            json={"reviewRevision": job["reviewRevision"], "resolutions": ["original"]},
        ).json()["id"]

        with session_scope() as db:
            export = db.get(Export, export_id)
            job_row = db.get(Job, job_id)
            from app.services import exports as export_service

            export_service.fail(
                db, export, job_row, code="media_processing_failed",
                message="transient", retryable=True,
            )

        assert auth_client.post(f"{JOBS}/{job_id}/exports/{export_id}/retry").status_code == 202
        run_export(export_id)

        record = auth_client.get(f"{JOBS}/{job_id}/exports/{export_id}").json()
        assert record["state"] == "complete"
        assert record["downloadAvailable"] is True
