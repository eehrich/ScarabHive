"""Test Agent LLM profile fallback behavior."""
import httpx
import pytest
import time
from unittest.mock import MagicMock, patch

from agent_system.servers.agent.server import Agent
from agent_system.config.models import (
    AgentSystemConfig,
    MCPConfig,
    AgentConfig,
    LLMSystemConfig,
    LLMModelConfig,
    LLMProfile,
)
from agent_system.mcp.base import MCPRegistry
from agent_system.llm.models import LLMRateLimitError, LLMQuotaExhaustedError, LLMConnectionError


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
    """Ketten-Semantik: llm_profile = [primär, fallback1, ...]."""
    # No fallbacks
    config = AgentConfig(llm_profile="gemini")
    assert config.fallback_profiles == []
    assert config.advanced_llm_profile is None

    # With fallbacks (Kette)
    config = AgentConfig(llm_profile=["gemini", "openai", "openai_secondary"])
    assert config.default_llm_profile == "gemini"
    assert config.fallback_profiles == ["openai", "openai_secondary"]


def test_agent_config_advanced_chain():
    """Advanced-Kette + Sicherheitsnetz-Fallback-Reihenfolge."""
    config = AgentConfig(
        llm_profile=["gemini", "openai"],
        llm_profile_advanced=["openai_secondary", "openai"],
    )
    assert config.advanced_llm_profile == "openai_secondary"
    # normal: eigene Rest-Kette + komplette Advanced-Kette als Netz (dedupliziert)
    assert config.fallback_chain(False) == ["openai", "openai_secondary"]
    # advanced: eigene Rest-Kette + komplette normale Kette als Netz (dedupliziert)
    assert config.fallback_chain(True) == ["openai", "gemini"]
    # ohne Advanced-Kette ist use_advanced ein No-Op → normale Fallbacks
    config2 = AgentConfig(llm_profile=["gemini", "openai"], llm_profile_advanced=[])
    assert config2.advanced_llm_profile is None
    assert config2.fallback_chain(True) == ["openai"]
    # exclude: tatsächlich aktives Modell (Eskalation/Override) fliegt aus
    # der Kette — sonst würde es als sein eigener Fallback erneut versucht
    assert config.fallback_chain(False, exclude="openai_secondary") == ["openai"]
    assert config.fallback_chain(True, exclude="gemini") == ["openai"]


def test_agent_config_legacy_fallbacks_rejected():
    """Altes llm_profile_fallbacks muss laut scheitern (Migrations-Hinweis)."""
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
    mcp_config = MCPConfig(
        type="agent",
        enabled=True,
        agent_config=agent_config_with_fallbacks
    )
    registry = MCPRegistry()
    mock_llm = MagicMock()
    
    agent = Agent(
        "test_agent",
        system_config_with_profiles,
        mcp_config,
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
    mcp_config = MCPConfig(
        type="agent",
        enabled=True,
        agent_config=agent_config_with_fallbacks
    )
    registry = MCPRegistry()
    mock_llm = MagicMock()
    
    agent = Agent(
        "test_agent",
        system_config_with_profiles,
        mcp_config,
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


def test_agent_reset_fallback(system_config_with_profiles, agent_config_with_fallbacks):
    """Test Agent.reset_fallback() method."""
    mcp_config = MCPConfig(
        type="agent",
        enabled=True,
        agent_config=agent_config_with_fallbacks
    )
    registry = MCPRegistry()
    mock_llm = MagicMock()
    
    agent = Agent(
        "test_agent",
        system_config_with_profiles,
        mcp_config,
        registry,
        llm=mock_llm
    )
    
    # Set fallback state
    mock_fallback_llm = MagicMock()
    agent._active_fallback_llm = mock_fallback_llm
    agent._active_fallback_profile = "openai"
    agent._fallback_activated_at = time.time()
    agent.llm_profile_info = "gemini:fallback"
    
    # Reset
    agent.reset_fallback()
    
    # Verify reset
    assert agent._active_fallback_llm is None
    assert agent._active_fallback_profile is None
    assert agent._fallback_activated_at is None
    assert agent.llm_profile_info == "gemini"  # :fallback suffix removed


def test_agent_check_fallback_recovery_no_fallback(system_config_with_profiles, agent_config_with_fallbacks):
    """Test _check_fallback_recovery returns False when no fallback active."""
    mcp_config = MCPConfig(
        type="agent",
        enabled=True,
        agent_config=agent_config_with_fallbacks
    )
    registry = MCPRegistry()
    mock_llm = MagicMock()
    
    agent = Agent(
        "test_agent",
        system_config_with_profiles,
        mcp_config,
        registry,
        llm=mock_llm
    )
    
    # No fallback active
    result = agent._check_fallback_recovery()
    assert result is False


def test_agent_check_fallback_recovery_not_elapsed(system_config_with_profiles):
    """Test _check_fallback_recovery returns False when recovery period not elapsed."""
    agent_config = AgentConfig(
        llm_profile=["gemini", "openai"],
        fallback_recovery_seconds=60  # 1 minute
    )
    mcp_config = MCPConfig(
        type="agent",
        enabled=True,
        agent_config=agent_config
    )
    registry = MCPRegistry()
    mock_llm = MagicMock()
    
    agent = Agent(
        "test_agent",
        system_config_with_profiles,
        mcp_config,
        registry,
        llm=mock_llm
    )
    
    # Set fallback state just activated
    mock_fallback_llm = MagicMock()
    agent._active_fallback_llm = mock_fallback_llm
    agent._active_fallback_profile = "openai"
    agent._fallback_activated_at = time.time()  # Just now
    
    # Check recovery - should be False (not enough time elapsed)
    result = agent._check_fallback_recovery()
    assert result is False
    assert agent._active_fallback_llm is not None  # Still in fallback


def test_agent_check_fallback_recovery_elapsed(system_config_with_profiles):
    """Test _check_fallback_recovery returns True and resets when recovery period elapsed."""
    agent_config = AgentConfig(
        llm_profile=["gemini", "openai"],
        fallback_recovery_seconds=1  # 1 second for testing
    )
    mcp_config = MCPConfig(
        type="agent",
        enabled=True,
        agent_config=agent_config
    )
    registry = MCPRegistry()
    mock_llm = MagicMock()
    
    agent = Agent(
        "test_agent",
        system_config_with_profiles,
        mcp_config,
        registry,
        llm=mock_llm
    )
    
    # Set fallback state activated 2 seconds ago
    mock_fallback_llm = MagicMock()
    agent._active_fallback_llm = mock_fallback_llm
    agent._active_fallback_profile = "openai"
    agent._fallback_activated_at = time.time() - 2  # 2 seconds ago
    agent.llm_profile_info = "gemini:fallback"
    
    # Check recovery - should be True (enough time elapsed)
    result = agent._check_fallback_recovery()
    assert result is True
    
    # Verify reset occurred
    assert agent._active_fallback_llm is None
    assert agent._active_fallback_profile is None
    assert agent._fallback_activated_at is None
    assert agent.llm_profile_info == "gemini"


def test_agent_fallback_state_initialization(system_config_with_profiles, agent_config_with_fallbacks):
    """Test that fallback state is properly initialized on agent creation."""
    mcp_config = MCPConfig(
        type="agent",
        enabled=True,
        agent_config=agent_config_with_fallbacks
    )
    registry = MCPRegistry()
    mock_llm = MagicMock()
    
    agent = Agent(
        "test_agent",
        system_config_with_profiles,
        mcp_config,
        registry,
        llm=mock_llm
    )
    
    # Verify initial state
    assert agent._active_fallback_llm is None
    assert agent._active_fallback_profile is None
    assert agent._fallback_activated_at is None
    assert agent._jittered_recovery_seconds is None


def test_agent_config_fallback_jitter_default():
    """Test that fallback_recovery_jitter_percent has correct default."""
    config = AgentConfig(llm_profile="gemini")
    assert config.fallback_recovery_jitter_percent == 20.0  # Default 20%


def test_agent_config_fallback_jitter_custom():
    """Test that fallback_recovery_jitter_percent can be customized."""
    config = AgentConfig(
        llm_profile="gemini",
        fallback_recovery_jitter_percent=30.0
    )
    assert config.fallback_recovery_jitter_percent == 30.0


def test_agent_fallback_recovery_uses_jitter(system_config_with_profiles, agent_config_with_fallbacks):
    """Test that fallback recovery applies jitter to prevent thundering herd."""
    mcp_config = MCPConfig(
        type="agent",
        enabled=True,
        agent_config=agent_config_with_fallbacks
    )
    registry = MCPRegistry()
    mock_llm = MagicMock()
    
    agent = Agent(
        "test_agent",
        system_config_with_profiles,
        mcp_config,
        registry,
        llm=mock_llm
    )
    
    # Activate fallback
    agent._active_fallback_llm = MagicMock()
    agent._active_fallback_profile = "openai"
    agent._fallback_activated_at = time.time()
    
    # First check should compute jitter
    agent._check_fallback_recovery()
    
    # Verify jitter was computed
    assert agent._jittered_recovery_seconds is not None
    
    # Jitter should be within ±20% of base (3600s)
    # With 20% jitter: 3600 ± 720 = [2880, 4320]
    assert 2880 <= agent._jittered_recovery_seconds <= 4320


def test_agent_fallback_jitter_varies_between_agents(system_config_with_profiles, agent_config_with_fallbacks):
    """Test that different agent instances get different jitter values."""
    mcp_config = MCPConfig(
        type="agent",
        enabled=True,
        agent_config=agent_config_with_fallbacks
    )
    registry = MCPRegistry()
    
    # Create multiple agents
    jitter_values = []
    for i in range(10):
        agent = Agent(
            f"test_agent_{i}",
            system_config_with_profiles,
            mcp_config,
            registry,
            llm=MagicMock()
        )
        agent._active_fallback_llm = MagicMock()
        agent._active_fallback_profile = "openai"
        agent._fallback_activated_at = time.time()
        agent._check_fallback_recovery()
        jitter_values.append(agent._jittered_recovery_seconds)
    
    # Should have some variance (not all identical)
    # With 10 random values, extremely unlikely all are equal
    unique_values = set(jitter_values)
    assert len(unique_values) > 1, "Jitter should vary between agents to prevent thundering herd"


def test_agent_reset_fallback_clears_jitter(system_config_with_profiles, agent_config_with_fallbacks):
    """Test that reset_fallback clears the jittered recovery time."""
    mcp_config = MCPConfig(
        type="agent",
        enabled=True,
        agent_config=agent_config_with_fallbacks
    )
    registry = MCPRegistry()
    mock_llm = MagicMock()
    
    agent = Agent(
        "test_agent",
        system_config_with_profiles,
        mcp_config,
        registry,
        llm=mock_llm
    )
    
    # Activate fallback and compute jitter
    agent._active_fallback_llm = MagicMock()
    agent._active_fallback_profile = "openai"
    agent._fallback_activated_at = time.time()
    agent._check_fallback_recovery()
    
    assert agent._jittered_recovery_seconds is not None
    
    # Reset fallback
    agent.reset_fallback()
    
    # Jitter should be cleared
    assert agent._jittered_recovery_seconds is None


class _ScriptedLLM:
    """Fake-Client: Call 1 → Tool-Call (verbraucht den Step), Call 2 → finale Antwort."""

    def __init__(self, name: str):
        self.name = name
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


@pytest.mark.asyncio
async def test_max_steps_final_call_uses_persistent_fallback(system_config_with_profiles):
    """R1-Regression: Der Final-Answer-Call NACH max_steps muss den persistenten
    Fallback nutzen, nicht das (rate-limitierte) Original — sonst scheitert der
    Request im letzten Schritt trotz funktionierendem Fallback.

    Aufbau: max_steps=1; Step 1 liefert einen Tool-Call (Loop erschöpft),
    danach macht der Agent den Final-Call. Mit aktivem persistentem Fallback
    müssen BEIDE Calls (Step + Final) auf dem Fallback laufen; das Original
    darf gar nicht angefasst werden."""
    agent_config = AgentConfig(llm_profile=["gemini", "openai"], max_steps=1)
    agent_config.tools.allowed = ["*"]
    mcp_config = MCPConfig(type="agent", enabled=True, agent_config=agent_config)

    original = _ScriptedLLM("original")
    fallback = _ScriptedLLM("fallback")

    agent = Agent("test_agent", system_config_with_profiles, mcp_config,
                  MCPRegistry(), llm=original)
    # Persistenten Fallback simulieren (Zustand wie nach LLMRateLimitError)
    agent._active_fallback_llm = fallback
    agent._active_fallback_profile = "openai"
    agent._fallback_activated_at = time.time()  # Recovery-Fenster frisch -> kein Reset

    events = []
    async for event in agent.run_events("test task"):
        events.append(event)
        if event.get("type") == "end":
            break

    final_events = [e for e in events if e.get("type") == "final"]
    assert final_events, f"kein final-Event; events={[e.get('type') for e in events]}"
    assert "FINAL-fallback" in str(final_events[-1].get("summary"))
    # Step 1 UND Final-Call liefen auf dem Fallback; Original blieb unberuehrt
    # (alter Bug: Final-Call ging an active_llm = Original -> call_count 1/1)
    assert fallback.call_count == 2
    assert original.call_count == 0


class _TransportErrorLLM:
    """Fake-Primary: wirft bei jedem Call einen Transportfehler (Endpoint tot)."""

    def __init__(self, exc: Exception):
        self.exc = exc
        self.call_count = 0

    def supports_streaming(self):
        return False

    async def chat_tools(self, messages, tools, cancellation_token=None, status_scope=None):
        self.call_count += 1
        raise self.exc


@pytest.mark.asyncio
@pytest.mark.parametrize("exc", [
    LLMConnectionError("HTTP request failed after 2 attempts (ConnectTimeout)",
                       provider="openai_httpx", model="deepseek-v4-flash"),
    httpx.ConnectTimeout("timed out"),
    # 400 = request-shaped, the one 4xx class that stays NON-persistent.
    # 401/402/404 stick and are covered by
    # test_4xx_persistence_follows_the_status below.
    httpx.HTTPStatusError(
        "Client error '400 Bad Request' for url "
        "'https://openrouter.ai/api/v1/chat/completions'",
        request=httpx.Request("POST", "https://openrouter.ai/api/v1/chat/completions"),
        response=httpx.Response(400, text="max_tokens above provider cap")),
], ids=["typed_llm_connection_error", "raw_httpx_transport_error",
        "http_400_request_shaped"])
async def test_transport_error_switches_to_fallback_profile(system_config_with_profiles, exc):
    """Job-532-Regression: Ein ConnectTimeout nach allen Client-Retries toetete
    den Sub-Agent, obwohl die llm_profile-Kette einen cross-provider-Fallback
    definierte — Transportfehler hatten keine Fehlerklasse, die der Retry-Loop
    im Agent-Server matcht (nur RateLimit/Quota/ServerError). Jetzt wechseln
    LLMConnectionError UND rohe httpx-Transportfehler (Clients, die ungetypt
    re-raisen) NON-persistent auf das naechste Profil der Kette.

    Seit 2026-08-20 auch httpx.HTTPStatusError (4xx): der Primaer von 186
    Ketten ist ein OpenRouter-Profil mit Direct-Fallback dahinter. Ein
    unbekannter Slug, ein Output-Cap unterm angefragten max_tokens (baidu:
    131k) oder ein Kontext-Ueberlauf kam als 4xx und toetete den Run, ohne
    den Fallback je zu versuchen, fuer den die Kette existiert."""
    agent_config = AgentConfig(llm_profile=["gemini", "openai"], max_steps=3)
    agent_config.tools.allowed = ["*"]
    mcp_config = MCPConfig(type="agent", enabled=True, agent_config=agent_config)

    primary = _TransportErrorLLM(exc)
    fallback = _ScriptedLLM("fallback")

    agent = Agent("test_agent", system_config_with_profiles, mcp_config,
                  MCPRegistry(), llm=primary)
    # Netzgrenze: der Profil-Wechsel selbst laeuft produktiv, nur der Bau des
    # Fallback-Clients wird ersetzt (sonst entstuende ein echter API-Client).
    agent._create_fallback_llm = lambda profile: fallback

    events = []
    async for event in agent.run_events("test task"):
        events.append(event)
        if event.get("type") == "end":
            break

    final_events = [e for e in events if e.get("type") == "final"]
    assert final_events, f"kein final-Event; events={[e.get('type') for e in events]}"
    assert "FINAL-fallback" in str(final_events[-1].get("summary"))
    assert primary.call_count >= 1, "fixture never called the primary"
    assert fallback.call_count >= 1, "fallback chain was never consulted"
    # NON-persistent (wie LLMServerError): der naechste Request soll wieder
    # das Original versuchen — kein Recovery-Fenster, kein gemerkter Fallback.
    assert agent._active_fallback_llm is None
    assert agent._active_fallback_profile is None


@pytest.mark.asyncio
async def test_transport_error_final_call_after_max_steps_uses_fallback(
    system_config_with_profiles,
):
    """Review-F1: Der Final-Answer-Call NACH max_steps waehlt
    ``_active_fallback_llm or active_llm`` — der non-persistente Transport-
    Switch muss ``active_llm`` mitziehen. Sonst geht der allerletzte Call an
    den toten Endpoint, dessen Fehlertext enthaelt "timeout", und der
    Final-Call-Catch-all meldet den Run per String-Match als cancelled statt
    mit der laengst vorliegenden Fallback-Antwort. Nebeneffekt des Swaps:
    Folge-Steps probieren den toten Endpoint nicht jedes Mal neu (Review-F2,
    ~Connect-Timeout x Retries pro Step)."""
    agent_config = AgentConfig(llm_profile=["gemini", "openai"], max_steps=1)
    agent_config.tools.allowed = ["*"]
    mcp_config = MCPConfig(type="agent", enabled=True, agent_config=agent_config)

    primary = _TransportErrorLLM(
        LLMConnectionError("connect timeout", model="deepseek-v4-flash"))
    fallback = _ScriptedLLM("fallback")

    agent = Agent("test_agent", system_config_with_profiles, mcp_config,
                  MCPRegistry(), llm=primary)
    agent._create_fallback_llm = lambda profile: fallback

    events = []
    async for event in agent.run_events("test task"):
        events.append(event)
        if event.get("type") == "end":
            break

    final_events = [e for e in events if e.get("type") == "final"]
    assert final_events, f"kein final-Event; events={[e.get('type') for e in events]}"
    assert "FINAL-fallback" in str(final_events[-1].get("summary"))
    # Step 1 UND Final-Call liefen auf dem Fallback; der tote Primary wurde
    # nach dem Switch nie wieder angefasst.
    assert primary.call_count == 1
    assert fallback.call_count == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("status,expect_persistent", [
    (400, False),   # request-shaped: a different request may pass
    (401, True),    # expired key: holds for every request on this endpoint
    (402, True),    # credit exhausted: same
    (404, True),    # model withdrawn: same
], ids=["400_request_shaped", "401_auth", "402_credit", "404_model_gone"])
async def test_4xx_persistence_follows_the_status(
        system_config_with_profiles, status, expect_persistent):
    """Review finding: lumping all 4xx as non-persistent re-probed an expired
    key on EVERY step (max_steps up to 500) and logged it as a network error.
    Key-, credit- and model-level refusals hold for every request on the
    endpoint, so the fallback must stick; request-shaped refusals must not."""
    exc = httpx.HTTPStatusError(
        f"HTTP {status}",
        request=httpx.Request("POST", "https://openrouter.ai/api/v1/chat/completions"),
        response=httpx.Response(status, text="refused"))
    agent_config = AgentConfig(llm_profile=["gemini", "openai"], max_steps=3)
    agent_config.tools.allowed = ["*"]
    mcp_config = MCPConfig(type="agent", enabled=True, agent_config=agent_config)

    primary = _TransportErrorLLM(exc)
    fallback = _ScriptedLLM("fallback")
    agent = Agent("test_agent", system_config_with_profiles, mcp_config,
                  MCPRegistry(), llm=primary)
    agent._create_fallback_llm = lambda profile: fallback

    async for event in agent.run_events("test task"):
        if event.get("type") == "end":
            break

    assert fallback.call_count >= 1, "fallback chain was never consulted"
    if expect_persistent:
        assert agent._active_fallback_llm is fallback,             f"HTTP {status} must stick — otherwise the dead endpoint is "             f"re-probed on every step"
    else:
        assert agent._active_fallback_llm is None,             f"HTTP {status} is request-shaped and must not stick"
