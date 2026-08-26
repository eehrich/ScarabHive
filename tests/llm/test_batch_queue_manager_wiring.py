"""What the queue manager does with per-provider config and listings.

Two defects lived here, both from naming providers in core code:

* the per-provider settings were copied by NAME — `gemini` and `openai` only,
  so `anthropic` (added later) never arrived, and any future provider would
  have been forgotten the same way;
* job recovery switched on the provider name to parse `list_batches()`
  output, with an `else` branch that assumed OpenAI's keys.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).parents[2]
sys.path.insert(0, str(REPO_ROOT / "src"))

from agent_system.config.models import BatchProviderConfig, BatchSystemConfig
from agent_system.llm.batch.models import BatchStatus
from agent_system.llm.batch.queue_manager import BatchQueueManager


def _manager(tmp_path, **providers) -> BatchQueueManager:
    return BatchQueueManager(
        batch_system_config=BatchSystemConfig(
            storage_path=str(tmp_path), providers=providers),
        storage_path=tmp_path)


class TestEveryConfiguredProviderIsKept:
    def test_all_providers_arrive_whatever_they_are_called(self, tmp_path):
        manager = _manager(
            tmp_path,
            gemini=BatchProviderConfig(poll_interval_seconds=10),
            openai_httpx=BatchProviderConfig(poll_interval_seconds=20),
            anthropic=BatchProviderConfig(poll_interval_seconds=30),
            some_future_provider=BatchProviderConfig(),
        )
        assert set(manager._provider_configs) == {
            "gemini", "openai_httpx", "anthropic", "some_future_provider"}

    def test_anthropic_is_not_dropped(self, tmp_path):
        """The concrete regression: it was configured, enabled, and silently
        absent from the manager.

        Asserted through the EFFECT, not just the dict: the settings are
        read to seed the polling defaults, so a provider that never arrives
        leaves the manager on its hardcoded 10s instead of the configured
        30s. Checking `_provider_configs` alone would stay green if the
        name filter simply moved one function further down.
        """
        manager = _manager(tmp_path, anthropic=BatchProviderConfig(
            poll_interval_seconds=30, max_requests_per_batch=7))
        assert "anthropic" in manager._provider_configs
        assert manager._poll_interval == 30
        assert manager._max_requests == 7

    def test_no_batch_config_is_not_a_crash(self, tmp_path):
        assert BatchQueueManager(storage_path=tmp_path)._provider_configs == {}


class TestTheTrackerKeyComesFromRegistration:
    """The job tracker is keyed by the CONFIGURED batch provider name.

    Every batch client used to hardcode its own name for the lookup
    (`get_tracked_jobs("openai")`). That silently stopped matching the
    moment a provider was renamed — `openai` → `openai_httpx` made startup
    cancellation find nothing, so paid jobs kept running and the tracker
    entries were never cleaned up.
    """

    def test_registration_stamps_the_name_on_the_client(self, tmp_path):
        from agent_system.llm.batch.base import BatchProviderClient

        class _Client(BatchProviderClient):
            async def submit_batch(self, *a, **k): ...
            async def get_batch_status(self, *a, **k): ...
            async def get_batch_results(self, *a, **k): ...
            async def cancel_batch(self, *a, **k): ...

        client = _Client()
        assert client._tracker_key() is None, (
            "an unregistered client must not claim a tracker key")

        _manager(tmp_path).register_batch_client("openai_httpx", client)
        assert client.provider_name == "openai_httpx"
        assert client._tracker_key() == "openai_httpx"

    @pytest.mark.parametrize("module,cls", [
        ("plugins_llm.llm_openai_compat.openai_batch", "OpenAIBatchClient"),
        ("plugins_llm.llm_gemini.gemini_batch", "GeminiBatchClient"),
        ("plugins_llm.llm_anthropic.anthropic_batch", "AnthropicBatchClient"),
    ])
    def test_no_batch_client_hardcodes_a_tracker_key(self, module, cls):
        """Read the source: a literal provider name in the cancellation path
        is the defect itself, and it is invisible to a mocked tracker."""
        import ast
        import importlib
        import inspect
        import textwrap

        target = getattr(importlib.import_module(module), cls)
        source = inspect.getsource(target.cancel_all_pending_batches)
        tree = ast.parse(textwrap.dedent(source))
        literals = [
            node.args[0].value
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in ("get_tracked_jobs", "remove_job")
            and node.args and isinstance(node.args[0], ast.Constant)
        ]
        assert not literals, (
            f"{cls} looks its tracked jobs up under hardcoded "
            f"{literals} instead of the name it was registered with")


class _Client:
    """A batch client that lists one open job and describes it itself."""

    def __init__(self, listing, described):
        self.listing = listing
        self.described = described
        self.describe_calls = []

    async def list_batches(self, limit=50):
        return self.listing

    def describe_listed_batch(self, batch_info):
        self.describe_calls.append(batch_info)
        return self.described


class TestRecoveryAsksTheClient:
    @pytest.mark.asyncio
    async def test_the_provider_describes_its_own_listing(self, tmp_path):
        raw = {"whatever": "shape", "the": "provider uses"}
        client = _Client([raw], {"job_id": "job-1",
                                 "status": BatchStatus.IN_PROGRESS,
                                 "model": "some-model"})
        manager = _manager(tmp_path, weird=BatchProviderConfig())
        manager.register_batch_client("weird", client)

        recovered = await manager.recover_jobs()

        assert client.describe_calls == [raw], (
            "the queue manager parsed the listing itself instead of asking")
        assert recovered == 1
        job = next(iter(manager._active_jobs.values()))
        assert job.provider_job_id == "job-1"
        assert job.model == "some-model"
        assert job.status == BatchStatus.IN_PROGRESS

    @pytest.mark.asyncio
    async def test_a_none_description_skips_the_entry(self, tmp_path):
        """Finished (or unrecognized) jobs must not be tracked — the client
        says so by returning None."""
        client = _Client([{"id": "done"}], None)
        manager = _manager(tmp_path, weird=BatchProviderConfig())
        manager.register_batch_client("weird", client)

        assert await manager.recover_jobs() == 0
        assert manager._active_jobs == {}

    @pytest.mark.asyncio
    async def test_a_client_that_never_implemented_it_is_named_and_skipped(
            self, tmp_path, caplog):
        """The base implementation returns None for everything, so inheriting
        it silently recovers nothing. Say which provider, once — and do not
        let it stop the providers behind it in the loop."""
        from agent_system.llm.batch.base import BatchProviderClient

        class _Bare(BatchProviderClient):
            async def submit_batch(self, *a, **k): ...
            async def get_batch_status(self, *a, **k): ...
            async def get_batch_results(self, *a, **k): ...
            async def cancel_batch(self, *a, **k): ...

            async def list_batches(self, limit=50):
                return [{"id": "open-1"}, {"id": "open-2"}]

        manager = _manager(tmp_path, bare=BatchProviderConfig(),
                           weird=BatchProviderConfig())
        manager.register_batch_client("bare", _Bare())
        manager.register_batch_client("weird", _Client(
            [{"id": "x"}], {"job_id": "job-1", "status": BatchStatus.IN_PROGRESS,
                            "model": "m"}))

        with caplog.at_level("WARNING"):
            recovered = await manager.recover_jobs()

        assert recovered == 1, "the second provider must still recover"
        warnings = [r.getMessage() for r in caplog.records
                    if r.levelname == "WARNING"
                    and "describe_listed_batch" in r.getMessage()]
        assert len(warnings) == 1 and "bare" in warnings[0], warnings
