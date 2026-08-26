"""Shutdown cancellation must only cancel what is still running.

`cancel_all_pending_batches` read `batch_info["state"]`, but
`get_batch_status` returns the MAPPED status under `"status"` — so the
lookup always produced `""`, which is in no terminal set, and every tracked
job got a cancel call: finished ones failed it and showed up as warnings.
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parents[3]))

from agent_system.llm.batch.models import BatchStatus
from plugins_llm.llm_gemini.gemini_batch import GeminiBatchClient


@pytest.fixture
def client():
    with patch("google.genai.Client"):
        c = GeminiBatchClient(api_key="test-key")
    # What BatchQueueManager.register_batch_client does: the tracker is keyed
    # by the CONFIGURED batch provider name, and the client reads it from
    # here instead of hardcoding one.
    c.provider_name = "gemini"
    return c


def _tracker(job_ids):
    tracker = MagicMock()
    tracker.get_tracked_jobs = AsyncMock(return_value=list(job_ids))
    tracker.remove_job = AsyncMock()
    return tracker


@pytest.mark.asyncio
async def test_finished_jobs_are_not_cancelled(client):
    statuses = {
        "done": {"status": BatchStatus.COMPLETED.value},
        "failed": {"status": BatchStatus.FAILED.value},
        "gone": {"status": BatchStatus.CANCELLED.value},
        "stale": {"status": BatchStatus.EXPIRED.value},
    }
    client.get_batch_status = AsyncMock(side_effect=lambda job: statuses[job])
    client.cancel_batch = AsyncMock()

    with patch("plugins_llm.llm_gemini.gemini_batch.get_job_tracker",
               return_value=_tracker(statuses)):
        cancelled = await client.cancel_all_pending_batches()

    assert cancelled == 0
    client.cancel_batch.assert_not_awaited()


@pytest.mark.asyncio
async def test_running_jobs_are_still_cancelled(client):
    """Counter-check: the guard must not turn cancellation off entirely —
    that is the expensive direction (old batch jobs keep billing)."""
    statuses = {
        "running": {"status": BatchStatus.IN_PROGRESS.value},
        "queued": {"status": BatchStatus.SUBMITTED.value},
        "done": {"status": BatchStatus.COMPLETED.value},
    }
    client.get_batch_status = AsyncMock(side_effect=lambda job: statuses[job])
    client.cancel_batch = AsyncMock()

    with patch("plugins_llm.llm_gemini.gemini_batch.get_job_tracker",
               return_value=_tracker(statuses)):
        cancelled = await client.cancel_all_pending_batches()

    assert cancelled == 2
    assert {c.args[0] for c in client.cancel_batch.await_args_list} == {
        "running", "queued"}


class TestTheContractBetweenTheTwoSides:
    """The bug was a MISMATCH: the producer writes `status`, the consumer
    read `state`. Mocking `get_batch_status` tests only the consumer half —
    these pin the producer, so moving the key back breaks something.
    """

    def _job(self, state_name):
        job = MagicMock()
        job.state.name = state_name
        job.error = None
        return job

    @pytest.mark.asyncio
    async def test_get_batch_status_answers_under_status(self, client):
        client._sdk_client.batches.get = MagicMock(
            return_value=self._job("JOB_STATE_RUNNING"))
        info = await client.get_batch_status("batches/x")
        assert "state" not in info, (
            "the key the cancel path used to read is back — one of the two "
            "sides will be wrong again")
        assert info["status"] == BatchStatus.IN_PROGRESS.value

    @pytest.mark.asyncio
    async def test_producer_and_consumer_agree_on_a_running_job(self, client):
        """End to end through both halves: a real status answer must lead to
        a cancel, without the test inventing the payload in between."""
        client._sdk_client.batches.get = MagicMock(
            return_value=self._job("JOB_STATE_RUNNING"))
        client.cancel_batch = AsyncMock()

        with patch("plugins_llm.llm_gemini.gemini_batch.get_job_tracker",
                   return_value=_tracker(["batches/x"])):
            cancelled = await client.cancel_all_pending_batches()

        assert cancelled == 1
        client.cancel_batch.assert_awaited_once_with("batches/x")

    @pytest.mark.asyncio
    async def test_producer_and_consumer_agree_on_a_finished_job(self, client):
        client._sdk_client.batches.get = MagicMock(
            return_value=self._job("JOB_STATE_SUCCEEDED"))
        client.cancel_batch = AsyncMock()

        with patch("plugins_llm.llm_gemini.gemini_batch.get_job_tracker",
                   return_value=_tracker(["batches/x"])):
            cancelled = await client.cancel_all_pending_batches()

        assert cancelled == 0
        client.cancel_batch.assert_not_awaited()


@pytest.mark.asyncio
async def test_every_job_leaves_the_tracker(client):
    """Cancelled or not: a job that stays tracked is retried on every
    shutdown forever."""
    client.get_batch_status = AsyncMock(
        return_value={"status": BatchStatus.COMPLETED.value})
    client.cancel_batch = AsyncMock()
    tracker = _tracker(["a", "b"])

    with patch("plugins_llm.llm_gemini.gemini_batch.get_job_tracker",
               return_value=tracker):
        await client.cancel_all_pending_batches()

    assert {c.args[1] for c in tracker.remove_job.await_args_list} == {"a", "b"}
