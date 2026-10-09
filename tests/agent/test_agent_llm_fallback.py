"""The agent's LLM fallback chain, and the blocks it honours: a rate limit, an
exhausted quota or a refused key blocks the LLM for every agent
(llm/model_health.py), not the agent that ran into it."""
import httpx
import pytest
from unittest.mock import MagicMock, patch

from agent_system.servers.agent.server import Agent
from agent_system.config.models import (
    AgentSystemConfig,
    ToolServerConfig,
    AgentConfig,
    LLMSystemConfig,
    LLMModelConfig,
    LLMProfile,
)
from agent_system.tools.base import ToolServerRegistry
from agent_system.llm.model_health import FIRST_RATE_LIMIT_PAUSE, model_health
from agent_system.llm.models import LLMRateLimitError, LLMQuotaExhaustedError, LLMConnectionError, LLMServerError


@pytest.fixture
def system_config_with_profiles():
    """Create system config with multiple LLM profiles for fallback testing."""
    llm_models = {
        "gemini": LLMModelConfig(
            provider="gemini_sdk",
            model="gemini-2.5-flash",
            context_window=200000
        ),
        "openai": LLMModelConfig(
            provider="openai",
            model="gpt-4o-mini",
            context_window=128000
        ),
        "openai_secondary": LLMModelConfig(
            provider="openai_httpx",
            model="gpt-4o-mini",
            context_window=128000
        ),
    }
    llm_profiles = {
        "gemini": LLMProfile(model_ref="gemini"),
        "openai": LLMProfile(model_ref="openai"),
        "openai_secondary": LLMProfile(model_ref="openai_secondary"),
    }
    llm_system = LLMSystemConfig(
        models=llm_models,
        profiles=llm_profiles,
        default_profile="gemini"
    )
    return AgentSystemConfig(llm_system=llm_system)


@pytest.fixture
def agent_config_with_fallbacks():
    """Create agent config with fallback profiles (Ketten-Semantik)."""
    return AgentConfig(
        llm_profile=["gemini", "openai", "openai_secondary"],
        max_steps=5
    )


def test_agent_config_fallback_profiles():
    """Chain semantics: llm_profile = [primary, fallback1, ...]."""
    # No fallbacks
    config = AgentConfig(llm_profile="gemini")
    assert config.fallback_profiles == []
    assert config.advanced_llm_profile is None

    # With fallbacks (chain)
    config = AgentConfig(llm_profile=["gemini", "openai", "openai_secondary"])
    assert config.default_llm_profile == "gemini"
    assert config.fallback_profiles == ["openai", "openai_secondary"]


def test_agent_config_advanced_chain():
    """Advanced chain + safety-net fallback order."""
    config = AgentConfig(
        llm_profile=["gemini", "openai"],
        llm_profile_advanced=["openai_secondary", "openai"],
    )
    assert config.advanced_llm_profile == "openai_secondary"
    # normal: own remaining chain + the complete advanced chain as a net (deduplicated)
    assert config.fallback_chain(False) == ["openai", "openai_secondary"]
    # advanced: own remaining chain + the complete normal chain as a net (deduplicated)
    assert config.fallback_chain(True) == ["openai", "gemini"]
    # without an advanced chain use_advanced is a no-op -> normal fallbacks
    config2 = AgentConfig(llm_profile=["gemini", "openai"], llm_profile_advanced=[])
    assert config2.advanced_llm_profile is None
    assert config2.fallback_chain(True) == ["openai"]
    # exclude: the model actually in use (escalation/override) drops out of
    # the chain -- otherwise it would be retried as its own fallback
    assert config.fallback_chain(False, exclude="openai_secondary") == ["openai"]
    assert config.fallback_chain(True, exclude="gemini") == ["openai"]


def test_agent_config_legacy_fallbacks_rejected():
    """The old llm_profile_fallbacks must fail loudly (migration hint)."""
    with pytest.raises(Exception, match="migrate_llm_profiles"):
        AgentConfig(llm_profile="gemini", llm_profile_fallbacks=["openai"])


def test_llm_rate_limit_error_attributes():
    """Test LLMRateLimitError exception attributes."""
    error = LLMRateLimitError(
        "Rate limit exceeded",
        provider="gemini_sdk",
        model="gemini-2.5-flash",
        retry_after=60.0
    )
    assert error.provider == "gemini_sdk"
    assert error.model == "gemini-2.5-flash"
    assert error.retry_after == 60.0
    assert "Rate limit exceeded" in str(error)


def test_llm_quota_exhausted_error_inherits():
    """Test that LLMQuotaExhaustedError inherits from LLMRateLimitError."""
    error = LLMQuotaExhaustedError(
        "Daily quota exhausted",
        provider="gemini_sdk",
        model="gemini-2.5-flash"
    )
    assert isinstance(error, LLMRateLimitError)
    assert error.provider == "gemini_sdk"


def test_agent_create_fallback_llm(system_config_with_profiles, agent_config_with_fallbacks):
    """Test Agent._create_fallback_llm method."""
    server_config = ToolServerConfig(
        type="agent",
        enabled=True,
        agent_config=agent_config_with_fallbacks
    )
    registry = ToolServerRegistry()
    mock_llm = MagicMock()
    
    agent = Agent(
        "test_agent",
        system_config_with_profiles,
        server_config,
        registry,
        llm=mock_llm
    )
    
    # Mock the factory function at its source module
    with patch('agent_system.llm.factory.create_llm_from_profile') as mock_factory:
        mock_fallback_llm = MagicMock()
        mock_factory.return_value = mock_fallback_llm

        # Spy on hook wiring — fallback LLM must have hooks wired so that
        # debugger + cost-tracking capture pre_llm_request / post_llm_response.
        # Without this, every fallback round-trip is silently unrecorded.
        with patch.object(agent._hook_manager, 'wire_llm_hooks') as mock_wire:
            result = agent._create_fallback_llm("openai")

            assert result == mock_fallback_llm
            mock_factory.assert_called_once()
            # Verify the profile was passed
            call_kwargs = mock_factory.call_args
            assert call_kwargs.kwargs["llm_profile"] == "openai"
            # Regression guard: hooks must be wired to the fallback LLM
            mock_wire.assert_called_once_with(mock_fallback_llm)


def test_agent_create_fallback_llm_failure(system_config_with_profiles, agent_config_with_fallbacks):
    """Test Agent._create_fallback_llm returns None on failure."""
    server_config = ToolServerConfig(
        type="agent",
        enabled=True,
        agent_config=agent_config_with_fallbacks
    )
    registry = ToolServerRegistry()
    mock_llm = MagicMock()
    
    agent = Agent(
        "test_agent",
        system_config_with_profiles,
        server_config,
        registry,
        llm=mock_llm
    )
    
    # Mock the factory to raise an error
    with patch('agent_system.llm.factory.create_llm_from_profile') as mock_factory:
        mock_factory.side_effect = ValueError("Profile not found")
        
        result = agent._create_fallback_llm("nonexistent")
        
        assert result is None


def test_agent_config_fallback_recovery_seconds_default():
    """Test default fallback_recovery_seconds value."""
    config = AgentConfig(llm_profile="gemini")
    assert config.fallback_recovery_seconds == 3600  # 1 hour default


def test_agent_config_fallback_recovery_seconds_custom():
    """Test custom fallback_recovery_seconds value."""
    config = AgentConfig(llm_profile="gemini", fallback_recovery_seconds=1800)
    assert config.fallback_recovery_seconds == 1800  # 30 minutes


class _ScriptedLLM:
    """Fake client: call 1 -> a tool call (uses the step), call 2 -> the final answer."""

    def __init__(self, name: str, model: str | None = None):
        self.name = name
        if model:
            self.model = model   # the block is keyed by it
        self.call_count = 0

    def supports_streaming(self):
        return False

    async def chat_tools(self, messages, tools, cancellation_token=None, status_scope=None):
        self.call_count += 1
        if self.call_count == 1:
            return {"assistant": {"role": "assistant", "content": "",
                                  "tool_calls": [{"id": "c1", "function": {
                                      "name": "some_tool", "arguments": "{}"}}]}}
        return {"assistant": {"role": "assistant",
                              "content": f"FINAL-{self.name}", "tool_calls": None}}


class _FailingLLM:
    """Raises the given error on every call."""

    def __init__(self, exc: Exception, model: str = "m-primary"):
        self.exc = exc
        self.model = model
        self.call_count = 0

    def supports_streaming(self):
        return False

    async def chat_tools(self, messages, tools, cancellation_token=None, status_scope=None):
        self.call_count += 1
        raise self.exc


def _chain_agent(system_config, llm, fallback=None, **agent_cfg):
    """An agent with the chain gemini -> openai whose fallback client is *fallback*."""
    agent_cfg.setdefault("llm_profile", ["gemini", "openai"])
    agent_cfg.setdefault("max_steps", 3)
    agent_config = AgentConfig(**agent_cfg)
    agent_config.tools.allowed = ["*"]
    server_config = ToolServerConfig(type="agent", enabled=True, agent_config=agent_config)
    agent = Agent("test_agent", system_config, server_config, ToolServerRegistry(), llm=llm)
    if fallback is not None:
        # The net boundary: the switch itself runs as in production, only the
        # fallback client is not built (that would be a real API client).
        agent._create_fallback_llm = lambda profile: fallback
    return agent


async def _run(agent, **kwargs):
    events = []
    async for event in agent.run_events("test task", **kwargs):
        events.append(event)
        if event.get("type") == "end":
            break
    return events


def _final(events):
    finals = [e for e in events if e.get("type") == "final"]
    assert finals, f"no final event; events={[e.get('type') for e in events]}"
    return str(finals[-1].get("summary"))


def _block(llm, seconds=3600):
    model_health.block(llm, max_pause=seconds, rate_limit=False, reason="test")


# ------------------------------------------------------------ the block is the LLM's


@pytest.mark.asyncio
async def test_a_429_blocks_the_llm_for_every_agent(system_config_with_profiles):
    """The bug of 2026-09-14: one 429 pinned the agent that saw it for an hour.
    The block belongs to the LLM: another agent with its own client for the same
    model walks around it, without paying a 429 of its own first."""
    limited = _FailingLLM(LLMRateLimitError("429 upstream", model="m-shared"), model="m-shared")
    limited.base_url = "https://openrouter.ai/api/v1"
    first = _chain_agent(system_config_with_profiles, limited, _ScriptedLLM("fallback-a"))
    await _run(first)
    assert limited.call_count == 1, "fixture: the first agent did not run into the 429"

    own_client = _ScriptedLLM("b-primary", model="m-shared")
    own_client.base_url = "https://openrouter.ai/api/v1"   # another client object, the same LLM
    fallback_b = _ScriptedLLM("fallback-b")
    second = _chain_agent(system_config_with_profiles, own_client, fallback_b)
    events = await _run(second)

    assert own_client.call_count == 0, "the second agent called an LLM another agent found rate-limited"
    assert "FINAL-fallback-b" in _final(events)
    assert 0 < model_health.remaining(own_client) <= FIRST_RATE_LIMIT_PAUSE, (
        "a rate limit blocks short first, not for the agent's whole recovery time")


@pytest.mark.asyncio
async def test_another_llm_runs_although_one_is_blocked(system_config_with_profiles):
    """The user picks another LLM for the run: a block on the agent's configured
    one must not hold that choice back."""
    configured = _ScriptedLLM("configured", model="m-limited")
    chosen = _ScriptedLLM("chosen", model="m-free")
    fallback = _ScriptedLLM("fallback")
    agent = _chain_agent(system_config_with_profiles, configured, fallback)
    _block(configured)

    events = await _run(agent, llm_override=chosen, llm_profile_info_override="openai:openai/m-free")

    assert chosen.call_count == 2 and fallback.call_count == 0
    assert "FINAL-chosen" in _final(events)


@pytest.mark.asyncio
async def test_a_blocked_llm_chosen_explicitly_stays_blocked(system_config_with_profiles):
    """No exception for an explicit choice: the block holds for its time."""
    chosen = _ScriptedLLM("chosen", model="m-limited")
    fallback = _ScriptedLLM("fallback")
    agent = _chain_agent(system_config_with_profiles, _ScriptedLLM("configured"), fallback)
    _block(chosen)

    events = await _run(agent, llm_override=chosen, llm_profile_info_override="openai_secondary:openai/m-limited")

    assert chosen.call_count == 0, "the explicit choice walked past the block"
    assert "FINAL-fallback" in _final(events)


@pytest.mark.asyncio
async def test_without_a_free_fallback_the_blocked_llm_answers_and_is_freed(system_config_with_profiles, monkeypatch):
    """A blocked LLM beats no LLM — and its answer frees it for every agent."""
    # A clock that moves on every read: time.monotonic ticks in ~16 ms on
    # Windows, and an answer in the block's own tick rightly keeps the block.
    ticks = iter(range(1_000, 10**9))
    monkeypatch.setattr(model_health, "_clock", lambda: float(next(ticks)))
    only = _ScriptedLLM("only", model="m-limited")
    agent = _chain_agent(system_config_with_profiles, only, llm_profile="gemini")
    _block(only)

    events = await _run(agent)

    assert only.call_count == 2 and "FINAL-only" in _final(events)
    assert model_health.remaining(only) == 0, "the answer did not lift the block"
    assert model_health.available(_ScriptedLLM("elsewhere", model="m-limited"), "another-request")


@pytest.mark.asyncio
async def test_with_the_whole_chain_blocked_the_wanted_llm_runs(system_config_with_profiles):
    primary = _ScriptedLLM("primary", model="m-primary")
    fallback = _ScriptedLLM("fallback", model="m-fallback")
    agent = _chain_agent(system_config_with_profiles, primary, fallback)
    _block(primary)
    _block(fallback)

    events = await _run(agent)

    assert fallback.call_count == 0 and "FINAL-primary" in _final(events), (
        "a blocked fallback was preferred to the blocked LLM the run wants")


@pytest.mark.asyncio
async def test_a_failed_call_takes_a_blocked_fallback_rather_than_none(system_config_with_profiles):
    primary = _FailingLLM(LLMRateLimitError("429", model="m-primary"))
    fallback = _ScriptedLLM("fallback", model="m-fallback")
    agent = _chain_agent(system_config_with_profiles, primary, fallback)
    _block(fallback)

    events = await _run(agent)

    assert "FINAL-fallback" in _final(events), "the run failed although a (blocked) fallback was left"


@pytest.mark.asyncio
async def test_a_blocked_chain_member_passed_over_is_still_there_when_the_free_one_fails(
        system_config_with_profiles):
    """Chain gemini -> openai (blocked) -> openai_secondary: the base fails, the
    step takes the free openai_secondary, which fails too. The blocked openai
    must still be tried — a blocked LLM beats none."""
    base = _FailingLLM(LLMServerError("504", model="m-base", status_code=504), model="m-base")
    blocked = _ScriptedLLM("blocked", model="m-blocked")
    free_but_failing = _FailingLLM(LLMServerError("504", model="m-free", status_code=504), model="m-free")
    agent = _chain_agent(system_config_with_profiles, base,
                         llm_profile=["gemini", "openai", "openai_secondary"])
    agent._create_fallback_llm = {"openai": blocked, "openai_secondary": free_but_failing}.get
    _block(blocked)

    events = await _run(agent)

    assert free_but_failing.call_count >= 1, "fixture: the free member was not tried first"
    assert "FINAL-blocked" in _final(events), "the run failed although the blocked member was left"


@pytest.mark.asyncio
async def test_a_walk_keeps_the_blocked_members_it_passed(system_config_with_profiles):
    """Base and openai blocked: the step walks past openai onto openai_secondary,
    which fails. openai, passed on the walk, is still the chain's next one."""
    base = _ScriptedLLM("base", model="m-base")
    passed = _ScriptedLLM("passed", model="m-passed")
    failing = _FailingLLM(LLMServerError("504", model="m-free", status_code=504), model="m-free")
    agent = _chain_agent(system_config_with_profiles, base,
                         llm_profile=["gemini", "openai", "openai_secondary"])
    agent._create_fallback_llm = {"openai": passed, "openai_secondary": failing}.get
    _block(base)
    _block(passed)

    events = await _run(agent)

    assert failing.call_count >= 1, "fixture: the walk did not reach the free member"
    assert passed.call_count >= 1 and "FINAL-passed" in _final(events), (
        "the walk dropped the blocked member it passed")


@pytest.mark.asyncio
async def test_a_step_that_walked_around_the_blocked_base_returns_to_it_when_the_walk_fails(
        system_config_with_profiles, monkeypatch):
    ticks = iter(range(1_000, 10**9))
    monkeypatch.setattr(model_health, "_clock", lambda: float(next(ticks)))
    base = _ScriptedLLM("base", model="m-base")
    failing = _FailingLLM(LLMServerError("504", model="m-fallback", status_code=504), model="m-fallback")
    agent = _chain_agent(system_config_with_profiles, base, failing)
    _block(base)

    events = await _run(agent)

    assert failing.call_count >= 1, "fixture: the step did not walk around the blocked base"
    assert "FINAL-base" in _final(events), "the run failed although the blocked base was left"


@pytest.mark.asyncio
async def test_the_client_that_just_failed_is_not_its_own_fallback(system_config_with_profiles):
    """No chain: the base is the last member of every step's chain, and the
    base is what failed — calling it again is no fallback."""
    base = _FailingLLM(LLMServerError("504", model="m-base", status_code=504), model="m-base")
    agent = _chain_agent(system_config_with_profiles, base, llm_profile="gemini")

    events = await _run(agent)

    assert base.call_count == 1, f"the failed client was retried as its own fallback ({base.call_count} calls)"
    assert any(e.get("type") == "error" for e in events)


class _BlockedInFlightThenLimitedLLM:
    """While its call is in flight another request's 429 blocks the LLM; then
    this call gets its 429 too — the same burst."""

    model = "m-burst"

    def __init__(self):
        self.call_count = 0

    def supports_streaming(self):
        return False

    async def chat_tools(self, messages, tools, cancellation_token=None, status_scope=None):
        self.call_count += 1
        model_health.block(self, max_pause=3600, rate_limit=True, reason="another request")
        raise LLMRateLimitError("429", model=self.model)


@pytest.mark.asyncio
async def test_a_429_of_a_call_already_in_flight_does_not_double_the_block(system_config_with_profiles):
    limited = _BlockedInFlightThenLimitedLLM()
    agent = _chain_agent(system_config_with_profiles, limited, _ScriptedLLM("fallback"))

    await _run(agent)

    assert limited.call_count == 1
    assert model_health.remaining(limited) <= FIRST_RATE_LIMIT_PAUSE, (
        "the call that went out before the block doubled it")


@pytest.mark.asyncio
async def test_a_blocked_fallback_is_walked_past(system_config_with_profiles):
    """Chain gemini -> openai -> openai_secondary: the primary fails, the first
    fallback is blocked, the call goes to the second."""
    primary = _FailingLLM(LLMRateLimitError("429", model="m-primary"))
    blocked = _ScriptedLLM("blocked", model="m-blocked")
    free = _ScriptedLLM("free")
    agent = _chain_agent(system_config_with_profiles, primary,
                         llm_profile=["gemini", "openai", "openai_secondary"])
    agent._create_fallback_llm = {"openai": blocked, "openai_secondary": free}.get
    _block(blocked)

    events = await _run(agent)

    assert blocked.call_count == 0 and "FINAL-free" in _final(events)


@pytest.mark.asyncio
@pytest.mark.parametrize("exc,expected", [
    (LLMRateLimitError("429", model="m-primary", retry_after=300.0), 300.0),
    (LLMQuotaExhaustedError("daily quota", model="m-primary"), 1800.0),
], ids=["retry_after_is_the_lower_bound", "quota_blocks_the_full_pause"])
async def test_the_pause_follows_the_error(system_config_with_profiles, exc, expected):
    primary = _FailingLLM(exc)
    agent = _chain_agent(system_config_with_profiles, primary, _ScriptedLLM("fallback"),
                         fallback_recovery_seconds=1800)

    await _run(agent)

    assert expected - 5 < model_health.remaining(primary) <= expected


@pytest.mark.asyncio
async def test_step_and_final_call_walk_around_a_blocked_llm(system_config_with_profiles):
    """R1 regression: the final-answer call after max_steps must avoid the
    blocked original too, or the request fails in its last step although the
    fallback works. max_steps=1: step 1 asks for a tool, then the final call."""
    original = _ScriptedLLM("original", model="m-original")
    fallback = _ScriptedLLM("fallback")
    agent = _chain_agent(system_config_with_profiles, original, fallback, max_steps=1)
    _block(original)

    events = await _run(agent)

    assert "FINAL-fallback" in _final(events)
    assert fallback.call_count == 2
    assert original.call_count == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("exc", [
    LLMConnectionError("HTTP request failed after 2 attempts (ConnectTimeout)",
                       provider="openai_httpx", model="deepseek-v4-flash"),
    httpx.ConnectTimeout("timed out"),
    # 400 = request-shaped, the one 4xx class that blocks nothing.
    # 401/402/404 block and are covered by test_4xx_blocks_follow_the_status.
    httpx.HTTPStatusError(
        "Client error '400 Bad Request' for url "
        "'https://openrouter.ai/api/v1/chat/completions'",
        request=httpx.Request("POST", "https://openrouter.ai/api/v1/chat/completions"),
        response=httpx.Response(400, text="max_tokens above provider cap")),
], ids=["typed_llm_connection_error", "raw_httpx_transport_error",
        "http_400_request_shaped"])
async def test_transport_error_switches_to_fallback_profile(system_config_with_profiles, exc):
    """Job 532 regression: a ConnectTimeout after all client retries killed the
    sub-agent although its llm_profile chain had a cross-provider fallback.
    LLMConnectionError and raw httpx transport errors (clients that re-raise
    untyped) switch to the next profile of the chain — for this request, with
    no block on the LLM.

    Since 2026-08-20 httpx.HTTPStatusError (4xx) too: the primary of 186 chains
    is an OpenRouter profile with a direct fallback behind it. An unknown slug,
    an output cap below the requested max_tokens or a context overflow came as
    4xx and killed the run without ever trying the fallback the chain is for."""
    primary = _FailingLLM(exc)
    fallback = _ScriptedLLM("fallback")
    agent = _chain_agent(system_config_with_profiles, primary, fallback)

    events = await _run(agent)

    assert "FINAL-fallback" in _final(events)
    assert primary.call_count >= 1, "fixture never called the primary"
    assert fallback.call_count >= 1, "fallback chain was never consulted"
    # No block (like LLMServerError): the next request tries the original again.
    assert model_health.available(primary, "next-request")


@pytest.mark.asyncio
async def test_transport_error_final_call_after_max_steps_uses_fallback(
    system_config_with_profiles,
):
    """Review F1: the request-scoped transport switch must move the run's base,
    or the final-answer call after max_steps goes to the dead endpoint again —
    its error text contains "timeout", which the final-call catch-all misreports
    as cancelled. Side effect of the swap: later steps do not re-probe the dead
    endpoint every time (review F2, ~connect timeout x retries per step)."""
    primary = _FailingLLM(LLMConnectionError("connect timeout", model="deepseek-v4-flash"))
    fallback = _ScriptedLLM("fallback")
    agent = _chain_agent(system_config_with_profiles, primary, fallback, max_steps=1)

    events = await _run(agent)

    assert "FINAL-fallback" in _final(events)
    # step 1 AND the final call ran on the fallback; the dead primary was not
    # touched again after the switch
    assert primary.call_count == 1
    assert fallback.call_count == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("status,expect_block", [
    (400, False),   # request-shaped: a different request may pass
    (401, True),    # expired key: holds for every request on this endpoint
    (402, True),    # credit exhausted: same
    (404, True),    # model withdrawn: same
], ids=["400_request_shaped", "401_auth", "402_credit", "404_model_gone"])
async def test_4xx_blocks_follow_the_status(
        system_config_with_profiles, status, expect_block):
    """Review finding: lumping all 4xx together re-probed an expired key on EVERY
    step (max_steps up to 500) and logged it as a network error. Key-, credit-
    and model-level refusals hold for every request on the endpoint, so they
    block the LLM for the full pause; request-shaped refusals block nothing."""
    exc = httpx.HTTPStatusError(
        f"HTTP {status}",
        request=httpx.Request("POST", "https://openrouter.ai/api/v1/chat/completions"),
        response=httpx.Response(status, text="refused"))
    primary = _FailingLLM(exc)
    fallback = _ScriptedLLM("fallback")
    agent = _chain_agent(system_config_with_profiles, primary, fallback)

    await _run(agent)

    assert fallback.call_count >= 1, "fallback chain was never consulted"
    if expect_block:
        assert model_health.remaining(primary) > 3500, (
            f"HTTP {status} must block, or the dead endpoint is re-probed on every step")
    else:
        assert model_health.available(primary, "next-request"), (
            f"HTTP {status} is request-shaped and must not block")


class _UpstreamErrorLLM:
    """Answers HTTP 200 with an error body — how a gateway proxies its own
    5xx. The first *failures* calls fail, later calls succeed."""

    def __init__(self, failures: int = 1, model: str = "m-gateway", name: str = "gateway"):
        self.failures = failures
        self.model = model
        self.name = name
        self.call_count = 0

    def supports_streaming(self):
        return False

    async def chat_tools(self, messages, tools, cancellation_token=None, status_scope=None):
        self.call_count += 1
        if self.call_count <= self.failures:
            return {"assistant": {"role": "assistant", "content": "", "error": {
                "message": "stream closed with reason: error",
                "type": "upstream_error_server_error"}}}
        return {"assistant": {"role": "assistant", "content": f"FINAL-{self.name}",
                              "tool_calls": None}}


@pytest.mark.asyncio
async def test_an_upstream_error_does_not_block_the_llm(system_config_with_profiles):
    """A gateway hiccup says nothing about the model's availability. Blocking it
    routed whole hours onto the next chain member — for the writer chains that
    is a 4x-priced model."""
    primary = _UpstreamErrorLLM(failures=2)
    fallback = _ScriptedLLM("fallback")
    agent = _chain_agent(system_config_with_profiles, primary, fallback)

    await _run(agent)

    assert fallback.call_count >= 1, "this request was not rescued"
    assert model_health.available(primary, "next-request"), (
        "the upstream error blocked the LLM — every agent would walk around it")


@pytest.mark.asyncio
async def test_a_single_upstream_error_is_retried_on_the_same_model(system_config_with_profiles):
    """One error in the body is asked again on the SAME model before the chain
    moves on. Two gateway hiccups 3 s apart (22.09.2026) otherwise put whole
    runs on the fallback."""
    primary = _UpstreamErrorLLM(failures=1)
    fallback = _ScriptedLLM("fallback")
    agent = _chain_agent(system_config_with_profiles, primary, fallback)

    events = await _run(agent)

    assert _final(events) == "FINAL-gateway"
    assert primary.call_count == 2
    assert fallback.call_count == 0, "one hiccup must not switch the model"


@pytest.mark.asyncio
async def test_a_repeated_upstream_error_goes_to_the_fallback(system_config_with_profiles):
    """A deterministic error (a content filter) fails the retry too: one call
    more, then the chain."""
    primary = _UpstreamErrorLLM(failures=99)
    fallback = _UpstreamErrorLLM(failures=0, model="m-fallback", name="fallback")
    agent = _chain_agent(system_config_with_profiles, primary, fallback)

    events = await _run(agent)

    assert _final(events) == "FINAL-fallback"
    assert primary.call_count == 2, "exactly one retry on the failing model"


class _FinalBodyErrorLLM(_UpstreamErrorLLM):
    """Fails every call with the given error body."""

    def __init__(self, error: dict):
        super().__init__(failures=99)
        self.error = error

    async def chat_tools(self, messages, tools, cancellation_token=None, status_scope=None):
        self.call_count += 1
        return {"assistant": {"role": "assistant", "content": "", "error": dict(self.error)}}


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [
    {"message": "blocked by the content filter", "type": "content_filter"},
    # httpx names it after the provider's own reason
    {"message": "Provider content filter blocked response", "type": "content_filter_safety"},
    # the client already went through its retry cycle with backoff
    {"message": "Stream failed after 4 attempts: reset", "retried": True},
], ids=["content_filter", "content_filter_native", "client_retried"])
async def test_what_a_retry_cannot_fix_switches_at_once(system_config_with_profiles, error):
    """A content filter blocks the same text again; a client that already
    retried would run its whole cycle a second time. Neither is asked again."""
    primary = _FinalBodyErrorLLM(error)
    fallback = _ScriptedLLM("fallback")
    agent = _chain_agent(system_config_with_profiles, primary, fallback)

    await _run(agent)

    assert primary.call_count == 1
    assert fallback.call_count >= 1


@pytest.mark.asyncio
async def test_the_fallback_gets_its_own_retry(system_config_with_profiles):
    """The retry belongs to the model, not to the step: a fallback that
    stumbles once is asked again too, instead of ending the run."""
    primary = _UpstreamErrorLLM(failures=99)
    fallback = _UpstreamErrorLLM(failures=1, model="m-fallback", name="fallback")
    agent = _chain_agent(system_config_with_profiles, primary, fallback)

    events = await _run(agent)

    assert _final(events) == "FINAL-fallback"
    assert fallback.call_count == 2


class _ArtifactLLM:
    """Answers with a reasoning_details block and records, per call, whether the
    history it was handed still carried one."""

    def __init__(self, name, before_answer=None, model=None):
        self.name = name
        if model:
            self.model = model
        self.call_count = 0
        self.saw_artifacts = []
        self._before_answer = before_answer

    def supports_streaming(self):
        return False

    async def chat_tools(self, messages, tools, cancellation_token=None, status_scope=None):
        self.call_count += 1
        self.saw_artifacts.append(
            any(getattr(m, "reasoning_details", None) for m in messages))
        if self._before_answer:
            self._before_answer()
        if self.call_count == 1:
            return {"assistant": {
                "role": "assistant", "content": "",
                "tool_calls": [{"id": "c1", "function": {
                    "name": "some_tool", "arguments": "{}"}}],
                "reasoning_details": [{"format": "openai-responses-items-v1",
                                       "type": "reasoning.responses_items",
                                       "items": [{"type": "reasoning", "id": "rs_1"}]}]}}
        return {"assistant": {"role": "assistant",
                              "content": f"FINAL-{self.name}", "tool_calls": None}}


@pytest.mark.asyncio
async def test_coming_back_to_a_freed_llm_strips_the_fallbacks_reasoning(
        system_config_with_profiles):
    """The other half of the model switch. Without it the first call after the
    block replays the fallback's reasoning items to the original model, which
    rejects them — straight back onto the fallback.

    Step 1 runs on the fallback of the blocked original and produces a
    reasoning block; the original then answers another request. Step 2 must
    reach the original with a clean history."""
    original = _ArtifactLLM("original", model="m-original")
    fallback = _ArtifactLLM("fallback", before_answer=lambda: model_health.release(original))
    agent = _chain_agent(system_config_with_profiles, original, fallback)
    _block(original)

    await _run(agent)

    assert fallback.call_count == 1, "step 1 did not run on the fallback"
    assert original.call_count >= 1, "the freed original never got the run back"
    assert original.saw_artifacts[0] is False, (
        "the fallback's reasoning reached the original model — the gateway "
        "rejects that and the agent falls straight back")


@pytest.mark.asyncio
async def test_descriptor_exhaustion_does_not_burn_the_fallback_chain(
    system_config_with_profiles,
):
    """A local EMFILE must NOT be treated as an unreachable endpoint.

    httpx reports "out of file descriptors" as a ConnectError, the same class
    an unreachable provider produces — so the transport clause switched
    profiles. No profile can help: the next client cannot open a socket
    either. On 2026-08-30 a descriptor leak on the writer host did this 52
    times, walking runs onto pricier fallback models that never had a chance
    of succeeding, while the log blamed the provider.
    """
    import errno as _errno

    # The exact chain httpx produces, measured against a real socket under a
    # lowered RLIMIT_NOFILE on the writer host:
    #   httpx.ConnectError -> httpcore.ConnectError -> OSError(EMFILE)
    # Three levels deep on purpose — a one-level fixture stays green even if
    # the cause-chain walk stops after the first hop, which is exactly the
    # mistake this fixture has to be able to catch.
    import httpcore

    try:
        try:
            raise OSError(_errno.EMFILE, "Too many open files")
        except OSError as os_exc:
            raise httpcore.ConnectError("[Errno 24] Too many open files") from os_exc
    except httpcore.ConnectError as core_exc:
        exhausted = httpx.ConnectError("[Errno 24] Too many open files")
        exhausted.__cause__ = core_exc

    primary = _FailingLLM(exhausted)
    fallback = _ScriptedLLM("fallback")
    agent = _chain_agent(system_config_with_profiles, primary, fallback)

    events = await _run(agent)

    assert primary.call_count >= 1, "fixture never called the primary"
    assert fallback.call_count == 0, (
        "a local descriptor limit switched the run onto a fallback profile — "
        "it cannot succeed there and the swap costs a pricier model"
    )
    # And it has to fail LOUDLY. Refusing the fallback is only half the
    # contract: swallowing the error instead would let a caller treat an
    # unfinished run as a finished one, and this assertion is what stops a
    # later refactor from doing that quietly.
    assert any(e.get("type") == "error" for e in events), (
        f"the run ended without an error event; types={[e.get('type') for e in events]}"
    )
