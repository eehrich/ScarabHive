"""Startup cancellation must reach every job that is still running.

This provider checked `status in (IN_PROGRESS,)` while the others cancel
everything non-terminal — so a SUBMITTED batch survived shutdown and kept
billing. "Finished" now has one definition (batch.models.TERMINAL_STATUSES).
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parents[3]))

from agent_system.llm.batch.models import BatchStatus
from plugins_llm.llm_anthropic.anthropic_batch import AnthropicBatchClient


@pytest.fixture
def client():
    with patch("httpx.AsyncClient"):
        c = AnthropicBatchClient(api_key="k", default_model="claude-sonnet-4-6")
    # What BatchQueueManager.register_batch_client does.
    c.provider_name = "anthropic"
    return c


def _tracker(job_ids):
    tracker = MagicMock()
    tracker.get_tracked_jobs = AsyncMock(return_value=list(job_ids))
    tracker.remove_job = AsyncMock()
    return tracker


async def _cancel_with(client, statuses):
    client.get_batch_status = AsyncMock(side_effect=lambda job: statuses[job])
    client.cancel_batch = AsyncMock()
    with patch("plugins_llm.llm_anthropic.anthropic_batch.get_job_tracker",
               return_value=_tracker(statuses)):
        cancelled = await client.cancel_all_pending_batches()
    return cancelled, client.cancel_batch


@pytest.mark.asyncio
async def test_a_submitted_batch_is_cancelled(client):
    """The regression: only IN_PROGRESS was cancelled, so a batch that had
    been submitted but not started yet kept running after shutdown."""
    cancelled, cancel = await _cancel_with(client, {
        "queued": {"status": BatchStatus.SUBMITTED.value},
        "running": {"status": BatchStatus.IN_PROGRESS.value},
    })
    assert cancelled == 2
    assert {c.args[0] for c in cancel.await_args_list} == {"queued", "running"}


@pytest.mark.asyncio
async def test_finished_batches_are_left_alone(client):
    """Counter-check: cancelling a finished job is an API call that fails and
    logs a warning."""
    cancelled, cancel = await _cancel_with(client, {
        "done": {"status": BatchStatus.COMPLETED.value},
        "failed": {"status": BatchStatus.FAILED.value},
        "gone": {"status": BatchStatus.CANCELLED.value},
        "stale": {"status": BatchStatus.EXPIRED.value},
    })
    assert cancelled == 0
    cancel.assert_not_awaited()


@pytest.mark.asyncio
async def test_an_unregistered_client_does_not_guess_a_tracker_key(client):
    """Without a registration there is no key — looking one up by guessing is
    how the openai -> openai_httpx rename lost every tracked job."""
    client.provider_name = ""
    tracker = _tracker(["x"])
    with patch("plugins_llm.llm_anthropic.anthropic_batch.get_job_tracker",
               return_value=tracker):
        assert await client.cancel_all_pending_batches() == 0
    tracker.get_tracked_jobs.assert_not_awaited()


class TestUnknownProcessingStatus:
    """An unrecognized processing_status must not reach the queue manager raw.

    It compares against BatchStatus values, so a bare Anthropic string reads
    as "neither terminal nor any known state" and the job is polled forever.
    """

    async def _status_for(self, client, processing):
        response = MagicMock()
        response.status_code = 200
        response.json.return_value = {
            "id": "b", "processing_status": processing,
            "request_counts": {"total": 1, "processing": 1},
        }
        client._client.get = AsyncMock(return_value=response)
        return (await client.get_batch_status("b"))["status"]

    @pytest.mark.asyncio
    async def test_it_becomes_in_progress(self, client):
        assert await self._status_for(client, "some_new_state") == (
            BatchStatus.IN_PROGRESS.value)

    @pytest.mark.asyncio
    async def test_known_states_are_unaffected(self, client):
        assert await self._status_for(client, "canceling") == (
            BatchStatus.CANCELLING.value)
