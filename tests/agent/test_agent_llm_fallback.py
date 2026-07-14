"""Test Agent LLM profile fallback behavior."""
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
from agent_system.llm.models import LLMRateLimitError, LLMQuotaExhaustedError


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
