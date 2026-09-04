"""Batch backends are built on first use, not at every process start.

Measured 2026-09-04: ``init_batch_system`` ran at every ``agent-cli run`` and
API start, built all three configured backends (google.genai import 1.1 s,
88 MB; three httpx clients with their own TLS contexts) and asked each to
cancel its tracked jobs -- for a run that never touched batch. The startup
cancellation is kept exactly where it matters: a TRACKED job still gets
cancelled, and only then is the client built.

Driven through the real ``init_batch_system`` with a real tracker directory;
only the provider plugin's factory is replaced by a counting fake.
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from agent_system.config.models import (
    AgentSystemConfig, BatchProviderConfig, BatchSystemConfig, LLMModelConfig,
    LLMSystemConfig,
)
from agent_system.llm import factory as llm_factory
from agent_system.llm import registry
from agent_system.llm.batch import initialization
from agent_system.llm.batch.job_tracker import BatchJobTracker, set_job_tracker


def _config(tmp_path: Path, *, cancel_on_startup: bool) -> AgentSystemConfig:
    return AgentSystemConfig(llm_system=LLMSystemConfig(
        models={
            "plain": LLMModelConfig(provider="ollama", model="q"),
            "batched": LLMModelConfig(provider="batch", model="m", batch_provider="gemini"),
        },
        batch=BatchSystemConfig(
            storage_path=str(tmp_path),
            providers={"gemini": BatchProviderConfig(enabled=True, cancel_on_startup=cancel_on_startup)},
        ),
    ))


class _FakeBackend:
    def __init__(self):
        self.cancel_all_pending_batches = AsyncMock(return_value=1)


@pytest.fixture
def counting_backend(monkeypatch):
    """registry.get_batch_backend -> a factory that counts how often the
    backend is BUILT (the expensive step this test is about)."""
    built = []

    def backend_factory(model_config):
        built.append(model_config)
        return _FakeBackend()

    monkeypatch.setattr(registry, "get_batch_backend", lambda provider: backend_factory)
    yield built
    llm_factory.set_batch_queue_manager(None)
    set_job_tracker(None)


@pytest.mark.asyncio
async def test_init_builds_no_backend_when_nothing_is_tracked(tmp_path, counting_backend):
    manager = await initialization.init_batch_system(_config(tmp_path, cancel_on_startup=True))

    assert manager is not None, "fixture: batch system did not come up"
    assert "gemini" in manager.batch_providers(), "provider must still be known"
    assert counting_backend == [], "backend was built at startup although nothing was tracked"


@pytest.mark.asyncio
async def test_a_tracked_job_still_gets_cancelled_at_startup(tmp_path, counting_backend):
    """The semantics cancel_on_startup exists for: a job this system tracked
    before a restart is cancelled -- so here, and only here, the client is
    built at startup."""
    seed = BatchJobTracker(tmp_path)
    await seed.add_job("gemini", "batches/left-over")

    manager = await initialization.init_batch_system(_config(tmp_path, cancel_on_startup=True))

    assert manager is not None
    assert len(counting_backend) == 1, f"expected one build for the tracked job, got {len(counting_backend)}"
    manager._client_for("gemini").cancel_all_pending_batches.assert_awaited_once()


@pytest.mark.asyncio
async def test_first_use_builds_the_backend_once(tmp_path, counting_backend):
    """cancel_on_startup=True so init itself builds nothing (with the flag
    off, recovery has to list the provider's batches and therefore builds the
    client at startup -- that is inherent, not lazy)."""
    manager = await initialization.init_batch_system(_config(tmp_path, cancel_on_startup=True))
    assert manager is not None
    assert counting_backend == [], "fixture: init already built the backend, first use cannot be measured"

    first = manager._client_for("gemini")
    second = manager._client_for("gemini")

    assert first is second
    assert len(counting_backend) == 1
    assert first.provider_name == "gemini", "the tracker key must be stamped on a lazily built client too"


@pytest.mark.asyncio
async def test_a_backend_that_fails_to_build_is_refused_at_submit(tmp_path, monkeypatch):
    """The failure the old startup wrapper caught (SDK import, client
    construction) now happens on first use -- and must surface to the caller
    right away as 'no client', not strand a queued job until its timeout."""
    def exploding_factory(model_config):
        raise RuntimeError("SDK exploded")

    monkeypatch.setattr(registry, "get_batch_backend", lambda provider: exploding_factory)
    try:
        manager = await initialization.init_batch_system(_config(tmp_path, cancel_on_startup=True))
        assert manager is not None

        with pytest.raises(ValueError, match="No batch client registered"):
            await manager.submit_request(model="m", provider="gemini", messages=[])

        assert manager._active_jobs == {}, "a request must not be queued for a provider that cannot come up"
        assert "gemini" not in manager.batch_providers(), "a failed build must not be retried on every submit"
    finally:
        llm_factory.set_batch_queue_manager(None)
        set_job_tracker(None)
