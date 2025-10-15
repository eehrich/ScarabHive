"""Test Agent initialization with new config system."""
import pytest
from unittest.mock import MagicMock

from agent_system.servers.agent.server import Agent
from agent_system.config.models import (
    AgentSystemConfig,
    MCPConfig,
    AgentConfig,
    ToolConfig,
    LLMSystemConfig,
    LLMModelConfig,
    LLMProfile,
)
from agent_system.mcp.base import MCPRegistry


def test_agent_requires_agent_config():
    """Agent must have agent_config in MCPConfig (no fallbacks)."""
    system_config = AgentSystemConfig()
    mcp_config = MCPConfig(type="agent", enabled=True)  # No agent_config
    registry = MCPRegistry()
    
    with pytest.raises(ValueError, match="requires agent_config"):
        Agent("test_agent", system_config, mcp_config, registry)


def test_agent_requires_registry():
    """Agent must have MCPRegistry instance (no fallbacks)."""
    system_config = AgentSystemConfig()
    agent_config = AgentConfig()
    mcp_config = MCPConfig(type="agent", enabled=True, agent_config=agent_config)
    
    with pytest.raises(ValueError, match="requires MCPRegistry"):
        Agent("test_agent", system_config, mcp_config, None)


def test_agent_initialization_with_minimal_config():
    """Agent initializes with minimal valid config."""
    system_config = AgentSystemConfig()
    agent_config = AgentConfig()
    mcp_config = MCPConfig(type="agent", enabled=True, agent_config=agent_config)
    registry = MCPRegistry()
    
    # Mock LLM to avoid API key requirements
    mock_llm = MagicMock()
    
    agent = Agent("test_agent", system_config, mcp_config, registry, llm=mock_llm)
    
    assert agent.name == "test_agent"
    assert agent.agent_config == agent_config
    assert agent.registry == registry
    assert agent.llm == mock_llm
    assert agent._mcp_public is False


def test_agent_initialization_with_full_config():
    """Agent initializes with comprehensive config."""
    # Create LLM system config
    llm_model = LLMModelConfig(
        provider="openai",
        model="gpt-4",
        context_window=128000
    )
    llm_profile = LLMProfile(
        model_ref="gpt4"
    )
    llm_system = LLMSystemConfig(
        models={"gpt4": llm_model},
        profiles={"default": llm_profile},
        default_profile="default"
    )
    
    # Create agent config with tools allowed
    tool_config = ToolConfig(allowed=["*"])
    agent_config = AgentConfig(
        max_steps=10,
        tools=tool_config
    )
    
    # Create system config
    system_config = AgentSystemConfig(
        llm_system=llm_system
    )
    
    mcp_config = MCPConfig(type="agent", enabled=True, agent_config=agent_config)
    registry = MCPRegistry()
    
    # Mock LLM
    mock_llm = MagicMock()
    
    agent = Agent("test_agent", system_config, mcp_config, registry, llm=mock_llm)
    
    assert agent.name == "test_agent"
    assert agent.description == "Agent: test_agent"  # No description field in AgentConfig
    assert agent.agent_config.max_steps == 10


# Test removed: context_manager is now handled by hook plugins (context_optimizer, context_summarizer)


# Test removed: context_manager is now handled by hook plugins (context_optimizer, context_summarizer)


# Test removed: token_optimizer is now handled by context_optimizer hook plugin


# Test removed: token_optimizer is now handled by context_optimizer hook plugin


def test_agent_description_property():
    """Agent description property returns agent name."""
    agent_config = AgentConfig()
    system_config = AgentSystemConfig()
    mcp_config = MCPConfig(type="agent", enabled=True, agent_config=agent_config)
    registry = MCPRegistry()
    mock_llm = MagicMock()
    
    agent = Agent("test_agent", system_config, mcp_config, registry, llm=mock_llm)
    
    assert agent.description == "Agent: test_agent"


def test_agent_description_fallback():
    """Agent description always uses agent name format."""
    agent_config = AgentConfig()
    system_config = AgentSystemConfig()
    mcp_config = MCPConfig(type="agent", enabled=True, agent_config=agent_config)
    registry = MCPRegistry()
    mock_llm = MagicMock()
    
    agent = Agent("my_custom_agent", system_config, mcp_config, registry, llm=mock_llm)
    
    assert agent.description == "Agent: my_custom_agent"


def test_agent_internal_tool_counter():
    """Agent has centralized internal tool counter."""
    system_config = AgentSystemConfig()
    agent_config = AgentConfig()
    mcp_config = MCPConfig(type="agent", enabled=True, agent_config=agent_config)
    registry = MCPRegistry()
    mock_llm = MagicMock()
    
    agent = Agent("test_agent", system_config, mcp_config, registry, llm=mock_llm)
    
    assert agent._internal_tool_counter == 0
    assert agent._internal_tool_counter_lock is not None


@pytest.mark.asyncio
async def test_agent_next_internal_tool_request_id():
    """Agent generates unique internal tool request IDs."""
    system_config = AgentSystemConfig()
    agent_config = AgentConfig()
    mcp_config = MCPConfig(type="agent", enabled=True, agent_config=agent_config)
    registry = MCPRegistry()
    mock_llm = MagicMock()
    
    agent = Agent("test_agent", system_config, mcp_config, registry, llm=mock_llm)
    
    # Generate IDs
    id1 = await agent.next_internal_tool_request_id("base_id")
    id2 = await agent.next_internal_tool_request_id("base_id")
    id3 = await agent.next_internal_tool_request_id("base_id")
    
    assert id1 == "base_id_001"
    assert id2 == "base_id_002"
    assert id3 == "base_id_003"


def test_agent_no_legacy_context_config_storage():
    """Agent does not store self.context_config (removed redundancy)."""
    system_config = AgentSystemConfig()
    agent_config = AgentConfig()
    mcp_config = MCPConfig(type="agent", enabled=True, agent_config=agent_config)
    registry = MCPRegistry()
    mock_llm = MagicMock()
    
    agent = Agent("test_agent", system_config, mcp_config, registry, llm=mock_llm)
    
    # Should not have context_config attribute (now in hook plugins)
    assert not hasattr(agent, 'context_config')
    # Should also not have context_manager attribute (removed)
    assert not hasattr(agent, 'context_manager')
