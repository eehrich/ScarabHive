"""Test Agent LLM profile fallback behavior."""
import pytest
from unittest.mock import MagicMock, AsyncMock, patch

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
    """Create agent config with fallback profiles."""
    return AgentConfig(
        llm_profile="gemini",
        llm_profile_fallbacks=["openai", "openai_secondary"],
        max_steps=5
    )


def test_agent_config_fallback_profiles():
    """Test that AgentConfig correctly exposes fallback_profiles property."""
    # No fallbacks
    config = AgentConfig(llm_profile="gemini")
    assert config.fallback_profiles == []
    
    # With fallbacks
    config = AgentConfig(
        llm_profile="gemini",
        llm_profile_fallbacks=["openai", "openai_secondary"]
    )
    assert config.fallback_profiles == ["openai", "openai_secondary"]


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
        
        result = agent._create_fallback_llm("openai")
        
        assert result == mock_fallback_llm
        mock_factory.assert_called_once()
        # Verify the profile was passed
        call_kwargs = mock_factory.call_args
        assert call_kwargs.kwargs["llm_profile"] == "openai"


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
