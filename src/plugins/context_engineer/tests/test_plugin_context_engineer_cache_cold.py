"""A call after a pause the provider's prompt cache did not outlive compacts before it goes out.

Such a call pays the whole context at the input price anyway, so a rewrite costs no cache: Layer 1 runs below
its threshold, and neither min_reduction nor the hysteresis -- both there to spare a warm cache -- holds it.
Whoever opens the call does not matter. The pause is read from the usage tracker's database, the cache's
lifetime from the model the call goes to (prompt_cache_ttl_minutes), else cache_cold_after_minutes.
"""

from __future__ import annotations

import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_system.config.models import LLMModelConfig
from agent_system.hooks import HookContext, HookType
from agent_system.llm import registry
from agent_system.llm.models import ChatMessage
from plugins.context_engineer import hooks as hooks_mod
from plugins.context_engineer.hooks import ContextEngineerPlugin
from plugins.context_usage_tracker.tracker import UsageTracker

PLUGIN_DIR = Path(hooks_mod.__file__).parent
BIG = "a large tool result line with enough words to count " * 60   # ~945 tokens

#: Layer 1 far above what the messages hold (~8k): a warm call never reaches it.
CONFIG = {"layer1_threshold": 100_000, "layer2_threshold": 10**9, "layer3_threshold": 10**9,
          "target_tokens": 500, "min_tokens_between_compactions": 20_000,
          "always_compact_media_keep_last": 0, "tool_result_keep_last": 2}


@pytest.fixture
def plugin(tmp_path):
    impl = ContextEngineerPlugin(PLUGIN_DIR, stats_history=[])
    impl._storage_base = tmp_path
    yield impl
    for sid in list(impl._session_components):
        impl.cleanup_session(sid)


@pytest.fixture
def tracker(tmp_path):
    t = UsageTracker(storage_path=tmp_path / "usage")
    yield t
    t.db.close()


def _messages(results: int = 8) -> list[dict]:
    messages = [{"role": "user", "content": "go"}]
    for i in range(results):
        messages += [
            {"role": "assistant", "content": None, "tool_calls": [
                {"id": f"c{i}", "type": "function", "function": {"name": "t", "arguments": "{}"}}]},
            {"role": "tool", "tool_call_id": f"c{i}", "name": "t", "content": f"{i} {BIG}"},
        ]
    return messages


def _last_call(tracker, session: str) -> None:
    """The session's last LLM call as the usage tracker's hook writes it."""
    tracker.record_usage(agent_id="a", agent_name="a", session_id=session, total_tokens=8_000,
                         prompt_tokens=8_000, context_window=200_000).result(timeout=10)


async def _call(plugin, monkeypatch, *, session="s", tracker=None, pause_min=0.0, ttl="unset",
                reading=None, agent_config=None, **config):
    """One pre-LLM call ``pause_min`` minutes after the tracker's last call of ``session``."""
    if reading is not None:
        monkeypatch.setattr(plugin, "_read_tokens", lambda *a: (reading, True))
    now = time.time() + pause_min * 60
    monkeypatch.setattr(hooks_mod, "time", SimpleNamespace(time=lambda: now, monotonic=time.monotonic))
    llm = SimpleNamespace(context_window=200_000)
    if ttl != "unset":
        llm.prompt_cache_ttl_minutes = ttl   # build_client sets it, None included
    registry_ = SimpleNamespace(get_server=lambda name: SimpleNamespace(tracker=tracker)
                                if name == "context_usage_tracker" and tracker is not None else None)
    agent = SimpleNamespace(system_config=SimpleNamespace(tool_registry=registry_), agent_config=agent_config)
    return await plugin.engineer_context(HookContext(
        hook_type=HookType.PRE_LLM_CALL, request_id="r", session_id=session, llm=llm, agent=agent,
        messages=[ChatMessage(**m) for m in _messages()], hook_config={**CONFIG, **config}))


def _stored(answer) -> int:
    return (answer.metadata or {}).get("tool_results_stored", 0)


@pytest.mark.asyncio
@pytest.mark.parametrize("ttl,pause,cold", [
    (5, 4, False), (5, 6, True),          # the model's own lifetime
    (30, 20, False), (30, 40, True),
    (None, 20, False), (None, 40, True),  # none named: cache_cold_after_minutes (30)
])
async def test_a_call_after_the_cache_expired_runs_layer_one_below_its_threshold(
        plugin, tracker, monkeypatch, ttl, pause, cold):
    _last_call(tracker, "s")
    answer = await _call(plugin, monkeypatch, tracker=tracker, pause_min=pause, ttl=ttl)
    assert (_stored(answer) > 0) is cold, answer.metadata
    if not cold:
        assert answer.metadata["reason"] == "below_threshold"
        assert all(BIG in m.content for m in answer.context.messages if m.role == "tool")


@pytest.mark.asyncio
async def test_a_cold_cache_keeps_a_run_min_reduction_would_throw_away(plugin, tracker, monkeypatch):
    """150k read, ~5k to take: warm, not worth the break; cold, there is no break to pay for."""
    _last_call(tracker, "warm")
    _last_call(tracker, "cold")
    warm = await _call(plugin, monkeypatch, session="warm", tracker=tracker, pause_min=1, ttl=5, reading=150_000)
    cold = await _call(plugin, monkeypatch, session="cold", tracker=tracker, pause_min=6, ttl=5, reading=150_000)
    assert warm.metadata.get("reason") == "not_worth_it", "fixture: the warm run was kept"
    assert _stored(cold) > 0, cold.metadata


@pytest.mark.asyncio
async def test_a_cold_cache_is_not_held_by_the_hysteresis(plugin, tracker, monkeypatch):
    _last_call(tracker, "s")
    first = await _call(plugin, monkeypatch, tracker=tracker, ttl=5, reading=110_000, min_reduction=0)
    assert _stored(first) > 0, "fixture: the first run did not run"
    held = await _call(plugin, monkeypatch, tracker=tracker, pause_min=1, ttl=5, reading=111_000,
                       min_reduction=0, tool_result_keep_last=1)
    assert held.metadata.get("reason") == "hysteresis", "fixture: the warm call was not held"
    cold = await _call(plugin, monkeypatch, tracker=tracker, pause_min=6, ttl=5, reading=111_000,
                       min_reduction=0, tool_result_keep_last=1)
    assert _stored(cold) > 0, cold.metadata


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["switched_off", "no_tracker", "first_call"])
async def test_nothing_happens_without_a_known_pause_or_when_switched_off(plugin, tracker, monkeypatch, case):
    if case != "first_call":
        _last_call(tracker, "s")
    answer = await _call(plugin, monkeypatch, tracker=None if case == "no_tracker" else tracker,
                         pause_min=60, ttl=5, compact_when_cache_cold=case != "switched_off")
    assert answer.metadata["reason"] == "below_threshold", answer.metadata


def test_every_client_carries_its_models_cache_lifetime():
    """The hook reads it off the client of THIS call -- an llm_profile override included, a batch one too."""
    from agent_system.llm.batch.batch_client import BatchLLMClient

    for provider in ("openai_responses", "openai_httpx", "anthropic", "gemini"):
        cfg = LLMModelConfig(provider=provider, model="some/model", api_key="k",
                             base_url="https://openrouter.ai/api/v1", prompt_cache_ttl_minutes=5)
        # the real seam: the root conftest swaps build_client for a fake
        client = registry._orig_build_client(cfg)
        assert client.prompt_cache_ttl_minutes == 5, provider
        batch = BatchLLMClient(client, queue_manager=None, batch_provider_config=None, model_name="m",
                               batch_provider="openai")
        assert batch.prompt_cache_ttl_minutes == 5, provider


@pytest.mark.asyncio
async def test_a_client_without_a_lifetime_reads_the_agents_model(plugin, tracker, monkeypatch):
    """A client not built through build_client: the model the agent is configured with, 5 min -- not the 30 for
    a model that names none."""
    from agent_system.llm import factory

    monkeypatch.setattr(factory, "resolve_llm_config_for_agent",
                        lambda system, agent: SimpleNamespace(spec=SimpleNamespace(prompt_cache_ttl_minutes=5)))
    _last_call(tracker, "s")
    answer = await _call(plugin, monkeypatch, tracker=tracker, pause_min=10,   # no ttl on the client
                         agent_config=object())
    assert _stored(answer) > 0, answer.metadata
