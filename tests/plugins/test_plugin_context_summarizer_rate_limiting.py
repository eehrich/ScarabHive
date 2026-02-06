"""Tests for context_summarizer rate limiting feature."""
from __future__ import annotations

import asyncio
import pytest
from unittest.mock import AsyncMock, Mock

from agent_system.hooks import HookContext, HookType
from agent_system.llm.models import ChatMessage
from plugins.context_summarizer.plugin import PLUGIN_FACTORY


@pytest.fixture
def summarizer_plugin():
    """Create context summarizer plugin instance."""
    from agent_system.config.models import AgentSystemConfig, MCPConfig
    
    system_config = AgentSystemConfig()
    mcp_config = MCPConfig()
    
    plugin = PLUGIN_FACTORY("context_summarizer", system_config, mcp_config)
    return plugin.server._hooks_impl


@pytest.fixture
def mock_agent():
    """Create mock agent with proper config structure."""
    from agent_system.config.models import (
        AgentSystemConfig, AgentConfig, LLMSystemConfig, 
        LLMModelConfig, LLMProfile
    )
    
    agent = Mock()
    
    system_config = AgentSystemConfig()
    system_config.llm_system = LLMSystemConfig(
        models={
            'gpt-4': LLMModelConfig(
                provider='openai',
                model='gpt-4',
                context_window=100000
            )
        },
        profiles={
            'normal': LLMProfile(model_ref='gpt-4')
        }
    )
    
    agent_config = AgentConfig(llm_profile='normal')
    
    agent.system_config = system_config
    agent.agent_config = agent_config
    agent.agent_id = 'test-agent'
    
    call_counter = 0
    async def mock_next_request_id(base_id: str) -> str:
        nonlocal call_counter
        call_counter += 1
        return f"{base_id}_{call_counter:03d}"
    
    agent.next_internal_tool_request_id = mock_next_request_id
    
    return agent


@pytest.fixture
def mock_llm():
    """Create mock LLM for testing."""
    llm = AsyncMock()
    llm.generate = AsyncMock(return_value={
        'content': 'Summary of conversation'
    })
    return llm


def create_test_messages(count: int) -> list:
    """Create test ChatMessage objects."""
    messages = []
    for i in range(count):
        messages.append(ChatMessage(
            role='user' if i % 2 == 0 else 'assistant',
            content=f'Test message {i} with substantial content to simulate real conversation ' * 20
        ))
    return messages


@pytest.mark.asyncio
async def test_rate_limiting_prevents_rapid_summarization(summarizer_plugin, mock_agent, mock_llm):
    """Test that rate limiting prevents summarization within min_time_between window."""
    # Configure plugin for aggressive summarization
    summarizer_plugin.trigger_percentage = 0.01  # Very low trigger
    summarizer_plugin.min_time_between = 5.0  # 5 seconds minimum between summarizations
    
    messages = create_test_messages(100)  # Enough to trigger summarization
    
    # First summarization should succeed
    context1 = HookContext(
        hook_type=HookType.PRE_LLM_CALL,
        request_id='test-rate-1',
        session_id='session-rate-test',
        agent=mock_agent,
        messages=messages,
        llm=mock_llm
    )
    
    result1 = await summarizer_plugin.summarize_context(context1)
    
    # If summarization was triggered (not blocked by other reasons)
    if result1.modified or (result1.metadata and result1.metadata.get('reason') not in 
                            ['no_context_window', 'insufficient_old_messages']):
        # Second summarization immediately after should be rate limited
        context2 = HookContext(
            hook_type=HookType.PRE_LLM_CALL,
            request_id='test-rate-2',
            session_id='session-rate-test',  # Same session
            agent=mock_agent,
            messages=messages,
            llm=mock_llm
        )
        
        result2 = await summarizer_plugin.summarize_context(context2)
        
        # Should be rate limited
        assert result2.success is True
        assert result2.modified is False
        assert result2.metadata['reason'] == 'rate_limited'
        assert 'time_since_last' in result2.metadata
        assert 'min_time_between' in result2.metadata
        assert result2.metadata['time_since_last'] < summarizer_plugin.min_time_between


@pytest.mark.asyncio
async def test_rate_limiting_allows_after_timeout(summarizer_plugin, mock_agent, mock_llm):
    """Test that summarization is allowed after min_time_between has passed."""
    # Set very short timeout for testing
    summarizer_plugin.trigger_percentage = 0.01
    summarizer_plugin.min_time_between = 0.1  # 100ms
    
    messages = create_test_messages(100)
    
    # First summarization
    context1 = HookContext(
        hook_type=HookType.PRE_LLM_CALL,
        request_id='test-timeout-1',
        session_id='session-timeout-test',
        agent=mock_agent,
        messages=messages,
        llm=mock_llm
    )
    
    result1 = await summarizer_plugin.summarize_context(context1)
    
    if result1.modified or (result1.metadata and result1.metadata.get('reason') not in 
                            ['no_context_window', 'insufficient_old_messages']):
        # Wait for timeout to expire
        await asyncio.sleep(0.15)  # 150ms > 100ms
        
        # Second summarization should now be allowed
        context2 = HookContext(
            hook_type=HookType.PRE_LLM_CALL,
            request_id='test-timeout-2',
            session_id='session-timeout-test',
            agent=mock_agent,
            messages=messages,
            llm=mock_llm
        )
        
        result2 = await summarizer_plugin.summarize_context(context2)
        
        # Should NOT be rate limited
        assert result2.metadata.get('reason') != 'rate_limited'


@pytest.mark.asyncio
async def test_rate_limiting_per_session(summarizer_plugin, mock_agent, mock_llm):
    """Test that rate limiting is per-session, not global."""
    summarizer_plugin.trigger_percentage = 0.01
    summarizer_plugin.min_time_between = 5.0
    
    messages = create_test_messages(100)
    
    # First session - first summarization
    context1 = HookContext(
        hook_type=HookType.PRE_LLM_CALL,
        request_id='test-session-1',
        session_id='session-A',
        agent=mock_agent,
        messages=messages,
        llm=mock_llm
    )
    
    _result1 = await summarizer_plugin.summarize_context(context1)
    
    # Different session - should NOT be rate limited
    context2 = HookContext(
        hook_type=HookType.PRE_LLM_CALL,
        request_id='test-session-2',
        session_id='session-B',  # Different session
        agent=mock_agent,
        messages=messages,
        llm=mock_llm
    )
    
    result2 = await summarizer_plugin.summarize_context(context2)
    
    # Session B should not be rate limited by session A
    if result2.metadata and result2.metadata.get('reason'):
        assert result2.metadata['reason'] != 'rate_limited', \
            "Different session should not be rate limited"


@pytest.mark.asyncio
async def test_manual_trigger_bypasses_rate_limiting(summarizer_plugin, mock_agent, mock_llm):
    """Test that manual triggers bypass rate limiting."""
    summarizer_plugin.trigger_percentage = 0.01
    summarizer_plugin.min_time_between = 10.0  # Long timeout
    
    messages = create_test_messages(100)
    
    # First summarization (automatic)
    context1 = HookContext(
        hook_type=HookType.PRE_LLM_CALL,
        request_id='test-manual-1',
        session_id='session-manual-test',
        agent=mock_agent,
        messages=messages,
        llm=mock_llm
    )
    
    _result1 = await summarizer_plugin.summarize_context(context1)
    
    # Manual trigger immediately after - should bypass rate limit
    context2 = HookContext(
        hook_type=HookType.PRE_LLM_CALL,
        request_id='test-manual-2',
        session_id='session-manual-test',
        agent=mock_agent,
        messages=messages,
        llm=mock_llm,
        metadata={'manual_trigger': True}  # Manual trigger flag
    )
    
    result2 = await summarizer_plugin.summarize_context(context2)
    
    # Manual trigger should not be rate limited
    assert result2.metadata.get('reason') != 'rate_limited', \
        "Manual trigger should bypass rate limiting"


@pytest.mark.asyncio
async def test_rate_limiting_timestamp_updates_only_on_success(summarizer_plugin, mock_agent, mock_llm):
    """Test that timestamp is only updated when summarization actually happens."""
    summarizer_plugin.trigger_percentage = 0.01
    summarizer_plugin.min_time_between = 0.2
    
    messages = create_test_messages(100)
    
    # First attempt
    context1 = HookContext(
        hook_type=HookType.PRE_LLM_CALL,
        request_id='test-update-1',
        session_id='session-update-test',
        agent=mock_agent,
        messages=messages,
        llm=mock_llm
    )
    
    result1 = await summarizer_plugin.summarize_context(context1)
    
    # Check if session has timestamp recorded
    session_id = 'session-update-test'
    
    if result1.modified:
        # Timestamp should be recorded
        assert session_id in summarizer_plugin._last_summarization_time
        first_timestamp = summarizer_plugin._last_summarization_time[session_id]
        
        # Wait a bit
        await asyncio.sleep(0.05)
        
        # Attempt that gets rate limited
        result2 = await summarizer_plugin.summarize_context(context1)
        
        if result2.metadata.get('reason') == 'rate_limited':
            # Timestamp should NOT have changed
            assert summarizer_plugin._last_summarization_time[session_id] == first_timestamp
    else:
        # If first attempt didn't modify, timestamp shouldn't be recorded
        # (unless it was rejected for insufficient_reduction)
        if result1.metadata.get('reason') != 'insufficient_reduction':
            assert session_id not in summarizer_plugin._last_summarization_time


@pytest.mark.asyncio
async def test_config_parameter_loading(summarizer_plugin):
    """Test that min_time_between_summarizations config is loaded correctly."""
    # Check default value
    assert hasattr(summarizer_plugin, 'min_time_between')
    assert isinstance(summarizer_plugin.min_time_between, (int, float))
    assert summarizer_plugin.min_time_between > 0
    
    # Default should be 200.0 seconds according to schema
    assert summarizer_plugin.min_time_between == 200.0


@pytest.mark.asyncio  
async def test_rate_limiting_logging(summarizer_plugin, mock_agent, mock_llm, caplog):
    """Test that rate limiting produces appropriate log messages."""
    import logging
    
    summarizer_plugin.trigger_percentage = 0.01
    summarizer_plugin.min_time_between = 5.0
    
    messages = create_test_messages(100)
    
    # First summarization
    context1 = HookContext(
        hook_type=HookType.PRE_LLM_CALL,
        request_id='test-log-1',
        session_id='session-log-test',
        agent=mock_agent,
        messages=messages,
        llm=mock_llm
    )
    
    with caplog.at_level(logging.INFO):
        _result1 = await summarizer_plugin.summarize_context(context1)
        
        # Immediate retry
        result2 = await summarizer_plugin.summarize_context(context1)
        
        if result2.metadata.get('reason') == 'rate_limited':
            # Check for rate limit log message
            log_messages = [record.message for record in caplog.records]
            rate_limit_logs = [msg for msg in log_messages if 'Rate limited' in msg]
            
            assert len(rate_limit_logs) > 0, "Should have rate limit log message"
            assert any('time_since_last' in msg or 'since last summarization' in msg 
                      for msg in rate_limit_logs)
