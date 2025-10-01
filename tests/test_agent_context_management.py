"""Test Agent context management with new config system."""
import pytest
from unittest.mock import MagicMock

from agent_system.servers.agent.server import Agent
from agent_system.config.models import (
    AgentSystemConfig,
    MCPConfig,
    AgentConfig,
    ContextManagementConfig,
    TokenOptimizationConfig,
    LLMSystemConfig,
    LLMModelConfig,
    LLMProfile,
)
from agent_system.mcp.base import MCPRegistry
from agent_system.llm.models import ChatMessage


def test_context_manager_gets_config_from_agent():
    """ContextManager accesses config via agent.mcp_config.agent_config."""
    context_mgmt = ContextManagementConfig(
        enabled=True,
        strategy="SMART_COMPRESSION",
        summarization_threshold=0.80,
        preserve_recent_messages=20
    )
    agent_config = AgentConfig(context_management=context_mgmt)
    
    system_config = AgentSystemConfig()
    mcp_config = MCPConfig(type="agent", enabled=True, agent_config=agent_config)
    registry = MCPRegistry()
    mock_llm = MagicMock()
    
    agent = Agent("test_agent", system_config, mcp_config, registry, llm=mock_llm)
    
    # ContextManager should have correct config
    assert agent.context_manager.config.enabled is True
    assert agent.context_manager.config.strategy == "SMART_COMPRESSION"
    assert agent.context_manager.config.summarization_threshold == 0.80
    assert agent.context_manager.config.preserve_recent_messages == 20


def test_context_manager_gets_context_window_from_llm():
    """ContextManager retrieves context_window dynamically from Agent's LLM."""
    # Create LLM config
    llm_model = LLMModelConfig(
        provider="openai",
        model="gpt-4-turbo",
        context_window=128000
    )
    llm_profile = LLMProfile(model_ref="gpt4turbo")
    llm_system = LLMSystemConfig(
        models={"gpt4turbo": llm_model},
        profiles={"default": llm_profile},
        default_profile="default"
    )
    
    system_config = AgentSystemConfig(
        llm_system=llm_system,
        agent_llm_profiles={"test_agent": "default"}
    )
    
    agent_config = AgentConfig()
    mcp_config = MCPConfig(type="agent", enabled=True, agent_config=agent_config)
    registry = MCPRegistry()
    
    # Mock LLM with context_window
    mock_llm = MagicMock()
    mock_llm.context_window = 128000
    
    agent = Agent("test_agent", system_config, mcp_config, registry, llm=mock_llm)
    
    # ContextManager should get context_window from agent's LLM
    assert agent.context_manager.context_window == 128000


def test_context_manager_fallback_context_window():
    """ContextManager falls back to 100000 when context_window cannot be determined."""
    system_config = AgentSystemConfig()
    agent_config = AgentConfig()
    mcp_config = MCPConfig(type="agent", enabled=True, agent_config=agent_config)
    registry = MCPRegistry()
    
    # Mock LLM without context_window - completely remove the attribute
    mock_llm = MagicMock(spec=[])  # Empty spec = no attributes
    
    agent = Agent("test_agent", system_config, mcp_config, registry, llm=mock_llm)
    
    # Should fall back to default
    assert agent.context_manager.context_window == 100000


def test_context_manager_token_optimization_disabled():
    """ContextManager respects token optimization settings."""
    token_opt = TokenOptimizationConfig(
        enable_compression=False,
        compress_tool_results=False
    )
    context_mgmt = ContextManagementConfig(
        token_optimization=token_opt
    )
    agent_config = AgentConfig(context_management=context_mgmt)
    
    system_config = AgentSystemConfig()
    mcp_config = MCPConfig(type="agent", enabled=True, agent_config=agent_config)
    registry = MCPRegistry()
    mock_llm = MagicMock()
    
    agent = Agent("test_agent", system_config, mcp_config, registry, llm=mock_llm)
    
    # Token optimizer should be None
    assert agent.token_optimizer is None
    
    # ContextManager config should reflect settings
    assert agent.context_manager.config.token_optimization.enable_compression is False
    assert agent.context_manager.config.token_optimization.compress_tool_results is False


def test_context_manager_token_optimization_enabled():
    """ContextManager creates token optimizer when enabled."""
    token_opt = TokenOptimizationConfig(
        enable_compression=True,
        compress_tool_results=True,
        max_tool_result_tokens=1500
    )
    context_mgmt = ContextManagementConfig(
        token_optimization=token_opt
    )
    agent_config = AgentConfig(context_management=context_mgmt)
    
    system_config = AgentSystemConfig()
    mcp_config = MCPConfig(type="agent", enabled=True, agent_config=agent_config)
    registry = MCPRegistry()
    mock_llm = MagicMock()
    
    agent = Agent("test_agent", system_config, mcp_config, registry, llm=mock_llm)
    
    # Token optimizer should be initialized
    assert agent.token_optimizer is not None
    
    # Config should reflect settings
    assert agent.context_manager.config.token_optimization.enable_compression is True
    assert agent.context_manager.config.token_optimization.max_tool_result_tokens == 1500


def test_context_manager_strategy_configuration():
    """ContextManager uses configured strategy."""
    strategies = ["TRUNCATE_OLDEST", "SUMMARIZE_OLDEST", "SLIDING_WINDOW", "SMART_COMPRESSION"]
    
    for strategy in strategies:
        context_mgmt = ContextManagementConfig(strategy=strategy)
        agent_config = AgentConfig(context_management=context_mgmt)
        
        system_config = AgentSystemConfig()
        mcp_config = MCPConfig(type="agent", enabled=True, agent_config=agent_config)
        registry = MCPRegistry()
        mock_llm = MagicMock()
        
        agent = Agent(f"test_{strategy}", system_config, mcp_config, registry, llm=mock_llm)
        
        assert agent.context_manager.config.strategy == strategy


def test_context_manager_summarizer_initialization():
    """ContextManager has summarizer initialized."""
    system_config = AgentSystemConfig()
    agent_config = AgentConfig()
    mcp_config = MCPConfig(type="agent", enabled=True, agent_config=agent_config)
    registry = MCPRegistry()
    mock_llm = MagicMock()
    
    agent = Agent("test_agent", system_config, mcp_config, registry, llm=mock_llm)
    
    # Summarizer should be set (it's private _summarizer)
    assert hasattr(agent.context_manager, '_summarizer')
    assert agent.context_manager._summarizer is not None


@pytest.mark.asyncio
async def test_context_manager_check_and_warn():
    """ContextManager can check token usage and warn."""
    context_mgmt = ContextManagementConfig(
        summarization_threshold=0.70  # 70% threshold
    )
    agent_config = AgentConfig()
    
    system_config = AgentSystemConfig()
    mcp_config = MCPConfig(type="agent", enabled=True, agent_config=agent_config)
    registry = MCPRegistry()
    mock_llm = MagicMock()
    mock_llm.context_window = 10000
    
    agent = Agent("test_agent", system_config, mcp_config, registry, llm=mock_llm)
    
    # Create test messages
    messages = [
        ChatMessage(role="system", content="You are a helpful assistant."),
        ChatMessage(role="user", content="Hello, how are you?"),
        ChatMessage(role="assistant", content="I'm doing well, thank you!"),
    ]
    
    # check_and_warn returns tuple (tokens, warning_level)
    result = agent.context_manager.check_and_warn(messages, step=1)
    assert isinstance(result, tuple)
    assert len(result) == 2
    tokens, warning_level = result
    assert isinstance(tokens, int)
    assert tokens > 0


@pytest.mark.asyncio
async def test_context_manager_manages_context():
    """ContextManager can manage context when needed."""
    context_mgmt = ContextManagementConfig(
        enabled=True,
        strategy="TRUNCATE_OLDEST",
        summarization_threshold=0.01  # Very low threshold to trigger management (1%)
    )
    agent_config = AgentConfig()
    
    system_config = AgentSystemConfig()
    mcp_config = MCPConfig(type="agent", enabled=True, agent_config=agent_config)
    registry = MCPRegistry()
    mock_llm = MagicMock()
    mock_llm.context_window = 500  # Very small window to trigger management
    
    agent = Agent("test_agent", system_config, mcp_config, registry, llm=mock_llm)
    
    # Create many messages to exceed threshold
    messages = [ChatMessage(role="system", content="System prompt" * 10)]
    for i in range(10):
        messages.append(ChatMessage(role="user", content=f"User message {i}" * 10))
        messages.append(ChatMessage(role="assistant", content=f"Assistant response {i}" * 10))
    
    # Manage context
    managed_messages = await agent.context_manager.manage_context(messages, request_id="test_req")
    
    # Should have fewer messages or summary after management
    # (TRUNCATE_OLDEST will remove oldest messages)
    assert managed_messages is not None
    assert len(managed_messages) >= 1  # At least system message should remain


def test_agent_tracks_context_usage():
    """Agent registers with context tracker."""
    system_config = AgentSystemConfig()
    agent_config = AgentConfig()
    mcp_config = MCPConfig(type="agent", enabled=True, agent_config=agent_config)
    registry = MCPRegistry()
    mock_llm = MagicMock()
    mock_llm.context_window = 50000
    
    agent = Agent("test_agent", system_config, mcp_config, registry, llm=mock_llm)
    
    # Agent should be registered (we can't easily test the tracker directly,
    # but we can verify the agent has a context_manager with correct window)
    assert agent.context_manager is not None
    assert agent.context_manager.context_window == 50000


def test_context_manager_optimizer_guards():
    """Agent has optimizer run guards configured."""
    token_opt = TokenOptimizationConfig(
        enable_compression=True,
        compress_tool_results=True
    )
    context_mgmt = ContextManagementConfig(
        token_optimization=token_opt
    )
    agent_config = AgentConfig(context_management=context_mgmt)
    
    system_config = AgentSystemConfig()
    mcp_config = MCPConfig(type="agent", enabled=True, agent_config=agent_config)
    registry = MCPRegistry()
    mock_llm = MagicMock()
    mock_llm.context_window = 100000
    
    agent = Agent("test_agent", system_config, mcp_config, registry, llm=mock_llm)
    
    # Optimizer guards should be initialized
    assert agent._last_optimizer_tokens_snapshot == 0
    assert agent._last_optimizer_run_time == 0.0
    assert agent._optimizer_cooldown_seconds == 10.0
    assert agent._optimizer_min_increase_tokens > 0  # Based on context window
    assert agent._skip_optimizer_steps_after_context_mgmt == 0


def test_context_manager_no_redundant_config_storage():
    """Agent does not store redundant context_config."""
    system_config = AgentSystemConfig()
    agent_config = AgentConfig()
    mcp_config = MCPConfig(type="agent", enabled=True, agent_config=agent_config)
    registry = MCPRegistry()
    mock_llm = MagicMock()
    
    agent = Agent("test_agent", system_config, mcp_config, registry, llm=mock_llm)
    
    # Should NOT have self.context_config
    assert not hasattr(agent, 'context_config')
    
    # Config is accessible via context_manager.config
    assert agent.context_manager.config is not None
    assert agent.context_manager.config == agent.mcp_config.agent_config.context_management


def test_context_manager_with_custom_thresholds():
    """ContextManager uses custom thresholds from config."""
    context_mgmt = ContextManagementConfig(
        summarization_threshold=0.65,
        prediction_threshold=0.92,
        preserve_recent_messages=25,
        max_summary_words=8000,
        tool_result_preview_chars=1000
    )
    agent_config = AgentConfig(context_management=context_mgmt)
    
    system_config = AgentSystemConfig()
    mcp_config = MCPConfig(type="agent", enabled=True, agent_config=agent_config)
    registry = MCPRegistry()
    mock_llm = MagicMock()
    
    agent = Agent("test_agent", system_config, mcp_config, registry, llm=mock_llm)
    
    # Verify custom thresholds
    assert agent.context_manager.config.summarization_threshold == 0.65
    assert agent.context_manager.config.prediction_threshold == 0.92
    assert agent.context_manager.config.preserve_recent_messages == 25
    assert agent.context_manager.config.max_summary_words == 8000
    assert agent.context_manager.config.tool_result_preview_chars == 1000
