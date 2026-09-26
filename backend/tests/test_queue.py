"""Enqueueing against a real RQ queue (spec 7.2).

These exercise RQ's own validation rather than a stub. A previous bug shipped a
job id of ``"{queue}:{id}"``; RQ rejects colons, and because ``_enqueue``
deliberately swallows queue failures, nothing was ever scheduled while the API
still returned 202.
"""

from __future__ import annotations

import pytest
from rq import Queue

from app.util.ids import new_id
from app.workers import queue as queue_module


@pytest.fixture
def rq_queues():
    """Real RQ queues backed by the fakeredis instance from conftest."""
    return {name: queue_module.get_queue(name) for name in queue_module.ALL_QUEUES}


class TestEnqueue:
    def test_upload_verification_is_scheduled(self, rq_queues):
        upload_id = new_id()
        assert queue_module.enqueue_upload_verification(upload_id) is True

        jobs = rq_queues[queue_module.UPLOAD_QUEUE].jobs
        assert len(jobs) == 1
        assert jobs[0].func_name == "app.workers.tasks.verify_upload"
        assert jobs[0].args == (upload_id,)

    def test_analysis_is_scheduled(self, rq_queues):
        job_id = new_id()
        assert queue_module.enqueue_analysis(job_id) is True
        jobs = rq_queues[queue_module.ANALYSIS_QUEUE].jobs
        assert [job.args for job in jobs] == [(job_id,)]

    def test_export_is_scheduled(self, rq_queues):
        export_id = new_id()
        assert queue_module.enqueue_export(export_id) is True
        assert rq_queues[queue_module.EXPORT_QUEUE].jobs[0].args == (export_id,)

    def test_cleanup_and_provider_file_sweeps_are_scheduled(self, rq_queues):
        assert queue_module.enqueue_job_cleanup(new_id()) is True
        assert queue_module.enqueue_provider_file_cleanup() is True
        assert len(rq_queues[queue_module.MAINTENANCE_QUEUE].jobs) == 2

    @pytest.mark.parametrize(
        "enqueue",
        [
            queue_module.enqueue_upload_verification,
            queue_module.enqueue_analysis,
            queue_module.enqueue_export,
            queue_module.enqueue_job_cleanup,
        ],
    )
    def test_job_ids_satisfy_rq_constraints(self, enqueue, rq_queues):
        """RQ allows only letters, numbers, underscores and dashes."""
        import re

        assert enqueue(new_id()) is True
        for queue in rq_queues.values():
            for job in queue.jobs:
                assert re.fullmatch(r"[A-Za-z0-9_-]+", job.id), job.id

    def test_payloads_carry_identifiers_only(self, rq_queues):
        """No secret, file content, or request body reaches a queue payload."""
        queue_module.enqueue_analysis(new_id())
        job = rq_queues[queue_module.ANALYSIS_QUEUE].jobs[0]
        assert all(isinstance(arg, str) for arg in job.args)
        assert job.kwargs == {}

    def test_a_queue_outage_is_reported_rather_than_raised(self, monkeypatch):
        """Durable state must survive Redis being unreachable."""

        def broken(_name: str) -> Queue:
            raise ConnectionError("redis is down")

        monkeypatch.setattr(queue_module, "get_queue", broken)
        # False, not an exception: the caller keeps its committed state and the
        # startup recovery sweep requeues the work later.
        assert queue_module.enqueue_analysis(new_id()) is False
