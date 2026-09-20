"""Startup cancellation for the httpx batch client.

The two defects this guards are the ones that made paid jobs survive a
shutdown: looking the tracked jobs up under a hardcoded provider name (the
`openai` -> `openai_httpx` rename made the lookup return nothing), and a
second, drifting definition of "finished" next to TERMINAL_STATUSES.

The sibling providers have had this test; this one had only an AST check,
which a `provider_key = "openai"` variable would have walked straight past.
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parents[3]))

from agent_system.llm.batch.models import BatchStatus
from plugins.llm_openai_compat.openai_batch import OpenAIBatchClient


@pytest.fixture
def client():
    with patch("httpx.AsyncClient"):
        c = OpenAIBatchClient(api_key="k")
    # What BatchQueueManager.register_batch_client does.
    c.provider_name = "openai_httpx"
    return c


def _tracker(job_ids):
    tracker = MagicMock()
    tracker.get_tracked_jobs = AsyncMock(return_value=list(job_ids))
    tracker.remove_job = AsyncMock()
    return tracker


async def _cancel_with(client, statuses):
    tracker = _tracker(statuses)
    client.get_batch_status = AsyncMock(side_effect=lambda job: statuses[job])
    client.cancel_batch = AsyncMock()
    with patch("plugins.llm_openai_compat.openai_batch.get_job_tracker",
               return_value=tracker):
        cancelled = await client.cancel_all_pending_batches()
    return cancelled, client.cancel_batch, tracker


@pytest.mark.asyncio
async def test_unfinished_batches_are_cancelled(client):
    cancelled, cancel, tracker = await _cancel_with(client, {
        "queued": {"status": BatchStatus.SUBMITTED.value},
        "running": {"status": BatchStatus.IN_PROGRESS.value},
    })
    assert cancelled == 2
    assert {c.args[0] for c in cancel.await_args_list} == {"queued", "running"}
    # The key must be the registered one, not the client's own idea of it.
    tracker.get_tracked_jobs.assert_awaited_once_with("openai_httpx")


@pytest.mark.asyncio
async def test_finished_batches_are_left_alone(client):
    """Counter-check: cancelling a finished job is an API call that fails."""
    cancelled, cancel, _ = await _cancel_with(client, {
        "done": {"status": BatchStatus.COMPLETED.value},
        "failed": {"status": BatchStatus.FAILED.value},
        "gone": {"status": BatchStatus.CANCELLED.value},
        "stale": {"status": BatchStatus.EXPIRED.value},
    })
    assert cancelled == 0
    cancel.assert_not_awaited()


@pytest.mark.asyncio
async def test_an_unregistered_client_does_not_guess_a_tracker_key(client):
    """Without a registration there is no key — guessing one is how the
    rename lost every tracked job."""
    client.provider_name = ""
    tracker = _tracker(["x"])
    with patch("plugins.llm_openai_compat.openai_batch.get_job_tracker",
               return_value=tracker):
        assert await client.cancel_all_pending_batches() == 0
    tracker.get_tracked_jobs.assert_not_awaited()
