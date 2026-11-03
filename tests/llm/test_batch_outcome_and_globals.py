"""Regressions in how a batch job ends and where its manager lives.

- an errored result may carry its error as a string (the Anthropic backend) or as
  ``{"message": ...}`` (Gemini): a string used to raise AttributeError in
  _process_results and leave every later caller of the job waiting;
- a completed job whose results name no entry for a request resolves that
  request's caller with an error instead of leaving it to its days-long timeout;
- cancelling one request of a job ends only that caller's wait while others
  still wait on the job, and cancels the job at the provider with the last one;
- the queue-full immediate submission is held by the manager until it is done;
- the batch queue manager is one global: the factory's lazy one is the one
  init_batch_system starts, not a second manager beside it;
- the job tracker writes its file atomically;
- is_rate_limit_error reads 429 as a status or a word, not as a substring.
"""
from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agent_system.config.models import (
    AgentSystemConfig, BatchProviderConfig, BatchSystemConfig, LLMModelConfig, LLMSystemConfig,
)
from agent_system.llm import factory
from agent_system.llm.batch import initialization
from agent_system.llm.batch.job_tracker import BatchJobTracker, set_job_tracker
from agent_system.llm.batch.models import BatchJob, BatchRequest, BatchStatus
from agent_system.llm.batch.queue_manager import BatchQueueManager
from agent_system.llm.retry_utils import is_rate_limit_error


@pytest.fixture
def manager(tmp_path):
    return BatchQueueManager(storage_path=tmp_path / "batch")


def _job(*request_ids: str) -> BatchJob:
    requests = [BatchRequest(request_id=rid, custom_id=f"c-{rid}", model="m",
                             messages=[{"role": "user", "content": rid}]) for rid in request_ids]
    job = BatchJob(job_id="job-1", provider="anthropic", model="m", requests=requests)
    job.provider_job_id = "prov-1"
    return job


def _register(manager: BatchQueueManager, job: BatchJob) -> dict:
    loop = asyncio.get_running_loop()
    manager._active_jobs[job.job_id] = job
    futures = {}
    for request in job.requests:
        manager._request_to_job[request.request_id] = job.job_id
        futures[request.request_id] = manager._request_futures[request.request_id] = loop.create_future()
    return futures


@pytest.mark.asyncio
async def test_a_string_error_reaches_its_caller_and_the_next_result_still_arrives(manager):
    job = _job("a", "b", "c")
    futures = _register(manager, job)

    await manager._process_results(job, [
        {"custom_id": "c-a", "response": None, "error": "overloaded"},           # Anthropic shape
        {"custom_id": "c-b", "response": None, "error": {"message": "blocked"}},  # Gemini shape
        {"custom_id": "c-c", "response": {"ok": 1}, "error": None},
    ])

    with pytest.raises(RuntimeError, match="overloaded"):
        futures["a"].result()
    with pytest.raises(RuntimeError, match="blocked"):
        futures["b"].result()
    assert futures["c"].result() == {"ok": 1}


@pytest.mark.asyncio
async def test_a_completed_job_without_a_result_for_a_request_resolves_its_caller(manager):
    job = _job("a", "b")
    futures = _register(manager, job)
    await manager._process_results(job, [{"custom_id": "c-a", "response": {"ok": 1}, "error": None}])
    job.status = BatchStatus.COMPLETED

    await manager._complete_job(job)

    assert futures["a"].result() == {"ok": 1}
    with pytest.raises(RuntimeError, match="without a result"):
        futures["b"].result()
    assert manager._request_futures == {}


@pytest.mark.asyncio
async def test_cancelling_one_request_leaves_the_job_to_the_others(manager):
    job = _job("a", "b")
    futures = _register(manager, job)
    client = MagicMock(cancel_batch=AsyncMock(return_value=True))
    manager.register_batch_client("anthropic", client)

    assert await manager.cancel_request("a") is True

    assert futures["a"].cancelled()
    assert not futures["b"].done(), "another caller's request must not be cancelled with it"
    client.cancel_batch.assert_not_awaited()
    assert job.job_id in manager._active_jobs

    assert await manager.cancel_request("b") is True
    client.cancel_batch.assert_awaited_once_with("prov-1")
    assert futures["b"].cancelled() and job.status == BatchStatus.CANCELLED


@pytest.mark.asyncio
async def test_the_immediate_submission_is_held_until_it_is_done(manager):
    manager._max_requests = 1
    manager._running = True
    release = asyncio.Event()

    async def submit(queue_key, provider, model):
        await release.wait()
        raise RuntimeError("submit failed")

    manager.register_batch_client("anthropic", MagicMock())
    with patch.object(manager, "_submit_batch", side_effect=submit), \
         patch.object(manager, "_collection_window_task", AsyncMock()):
        waiter = asyncio.create_task(manager.submit_request(model="m", provider="anthropic",
                                                            messages=[], timeout=5))
        for _ in range(20):
            await asyncio.sleep(0)
            if manager._immediate_submit_tasks:
                break
        assert len(manager._immediate_submit_tasks) == 1
        release.set()
        for _ in range(20):
            await asyncio.sleep(0)
            if not manager._immediate_submit_tasks:
                break
        assert manager._immediate_submit_tasks == set(), "a finished submission must be let go"
        waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiter


def _config(tmp_path) -> AgentSystemConfig:
    return AgentSystemConfig(llm_system=LLMSystemConfig(
        models={"batched": LLMModelConfig(provider="batch", model="m", batch_provider="gemini")},
        batch=BatchSystemConfig(storage_path=str(tmp_path),
                                providers={"gemini": BatchProviderConfig(enabled=True, cancel_on_startup=False)}),
    ))


@pytest.fixture
def clean_globals(monkeypatch):
    monkeypatch.setattr(initialization, "_batch_queue_manager", None)
    monkeypatch.setattr(initialization, "_batch_manager_config", None)
    yield
    set_job_tracker(None)


@pytest.mark.asyncio
async def test_init_starts_the_manager_the_factory_created_lazily(tmp_path, clean_globals):
    config = _config(tmp_path)
    factory.set_batch_config(config)
    lazy = factory.get_batch_queue_manager()
    assert lazy is not None, "fixture: the config enables batch"

    with patch.object(initialization, "_register_batch_clients", AsyncMock()), \
         patch.object(BatchQueueManager, "start", AsyncMock()) as start, \
         patch.object(BatchQueueManager, "recover_jobs", AsyncMock(return_value=0)):
        started = await initialization.init_batch_system(config)

    assert started is lazy, "a second manager leaves the clients wrapped before init on a dead one"
    start.assert_awaited_once()
    assert initialization.get_batch_queue_manager() is lazy

    factory.set_batch_queue_manager(None)
    assert initialization._batch_queue_manager is None


@pytest.mark.asyncio
async def test_the_tracker_file_is_replaced_whole(tmp_path):
    tracker = BatchJobTracker(tmp_path)
    with patch("agent_system.llm.batch.job_tracker.atomic_write_json") as write:
        await tracker.add_job("gemini", "batches/1")
    write.assert_called_once_with(tracker.tracker_file, {"gemini": ["batches/1"]})

    await tracker.add_job("gemini", "batches/2")
    assert sorted(json.loads(tracker.tracker_file.read_text())["gemini"]) == ["batches/1", "batches/2"]


class _StatusError(Exception):
    def __init__(self, message: str, status_code: int):
        super().__init__(message)
        self.status_code = status_code


@pytest.mark.parametrize("error, expected", [
    (Exception("HTTP 429 Too Many Requests"), True),
    (Exception("Error code: 429"), True),
    (Exception("request req_84291 failed: bad gateway"), False),
    (Exception("context length 14290 exceeded"), False),
    (_StatusError("slow down", 429), True),
    (_StatusError("invalid argument for id 429", 400), False),
    (Exception("RESOURCE_EXHAUSTED"), True),
    (Exception("Quota exceeded for metric"), True),
])
def test_429_is_a_status_or_a_word(error, expected):
    assert is_rate_limit_error(error) is expected
