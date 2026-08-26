"""Every batch bootstrap entry point must survive the config shape.

`llm_system.batch.providers` became a Dict (the provider vocabulary belongs
to the plugins, not to a model with one field per provider). Two of the three
entry points in initialization.py were migrated with it, `init_batch_system`
was not — and nothing here covered it, so a full green suite hid an
AttributeError on every CLI and Writer bootstrap:

    providers_config.gemini.enabled  ->  'dict' object has no attribute 'gemini'

Six call sites reach that function, two of them (writer_audio's
audio_pipeline, writer_content's polish_pipeline) with no exception handler,
where it takes `bootstrap_servers` and the hook registration down with it.
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

REPO_ROOT = Path(__file__).parents[2]
sys.path.insert(0, str(REPO_ROOT / "src"))

from agent_system.config.models import (
    AgentSystemConfig, BatchProviderConfig, BatchSystemConfig, LLMModelConfig,
    LLMSystemConfig,
)
from agent_system.llm.batch import initialization


def _config(tmp_path, *, with_batch_model=True, **providers):
    models = {"plain": LLMModelConfig(provider="ollama", model="q")}
    if with_batch_model:
        models["batched"] = LLMModelConfig(
            provider="batch", model="m", batch_provider="gemini")
    return AgentSystemConfig(llm_system=LLMSystemConfig(
        models=models,
        batch=BatchSystemConfig(storage_path=str(tmp_path),
                                providers=providers),
    ))


class TestEveryEntryPointSurvivesTheConfigShape:
    """All three read `batch.providers` — they must agree on what it is."""

    def test_sync_setup_runs(self, tmp_path):
        with patch.object(initialization, "_register_batch_clients", AsyncMock()):
            manager = initialization.setup_batch_queue_manager_sync(
                _config(tmp_path, gemini=BatchProviderConfig(enabled=True)))
        assert manager is not None

    @pytest.mark.asyncio
    async def test_start_runs(self, tmp_path):
        """Starts an EXISTING manager (it creates none), so the manager is
        registered first — otherwise the function returns before ever
        touching batch.providers, and this would measure nothing."""
        from agent_system.llm import factory

        config = _config(tmp_path, gemini=BatchProviderConfig(
            enabled=True, cancel_on_startup=False))
        with patch.object(initialization, "_register_batch_clients", AsyncMock()):
            manager = initialization.setup_batch_queue_manager_sync(config)
        factory.set_batch_queue_manager(manager)
        try:
            with patch.object(initialization, "_register_batch_clients", AsyncMock()), \
                 patch("agent_system.llm.batch.queue_manager.BatchQueueManager"
                       ".start", AsyncMock()), \
                 patch("agent_system.llm.batch.queue_manager.BatchQueueManager"
                       ".recover_jobs", AsyncMock(return_value=0)) as recover:
                await initialization.start_batch_queue_manager(config)
            recover.assert_awaited()
        finally:
            factory.set_batch_queue_manager(None)

    @pytest.mark.asyncio
    async def test_init_batch_system_runs(self, tmp_path):
        """The one that was forgotten."""
        with patch.object(initialization, "_register_batch_clients", AsyncMock()), \
             patch("agent_system.llm.batch.queue_manager.BatchQueueManager.start",
                   AsyncMock()), \
             patch("agent_system.llm.batch.queue_manager.BatchQueueManager"
                   ".recover_jobs", AsyncMock(return_value=0)):
            manager = await initialization.init_batch_system(
                _config(tmp_path, gemini=BatchProviderConfig(
                    enabled=True, cancel_on_startup=False)))
        assert manager is not None

    @pytest.mark.asyncio
    async def test_init_batch_system_reads_cancel_on_startup(self, tmp_path):
        """Per-provider settings must be found under the provider's KEY.

        `getattr(providers_dict, name, None)` returns None for every name, so
        this stayed silently off even after the crash was fixed: no batch job
        would ever be cancelled at startup.
        """
        cancelled = AsyncMock(return_value=0)
        with patch.object(initialization, "_register_batch_clients", AsyncMock()), \
             patch.object(initialization, "_cancel_provider_batches", cancelled), \
             patch("agent_system.llm.batch.queue_manager.BatchQueueManager.start",
                   AsyncMock()), \
             patch("agent_system.llm.batch.queue_manager.BatchQueueManager"
                   ".recover_jobs", AsyncMock(return_value=0)):
            await initialization.init_batch_system(
                _config(tmp_path, gemini=BatchProviderConfig(
                    enabled=True, cancel_on_startup=True)))
        cancelled.assert_awaited_once()
        assert cancelled.await_args.args[1] == {"gemini"}

    @pytest.mark.asyncio
    async def test_no_enabled_provider_is_not_a_crash(self, tmp_path):
        assert await initialization.init_batch_system(
            _config(tmp_path, gemini=BatchProviderConfig(enabled=False))) is None

    @pytest.mark.asyncio
    async def test_no_providers_configured_is_not_a_crash(self, tmp_path):
        """A config that names no provider disables batch — the honest
        reading, and it must not raise on the way there."""
        assert await initialization.init_batch_system(_config(tmp_path)) is None
