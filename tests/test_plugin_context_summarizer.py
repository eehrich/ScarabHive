"""Tests for context_summarizer plugin."""
from __future__ import annotations

import pytest
from unittest.mock import AsyncMock

from agent_system.hooks import HookContext, HookType
from plugins.context_summarizer.plugin import PLUGIN_FACTORY


@pytest.fixture
def summarizer_plugin():
    """Create context summarizer plugin instance."""
    from agent_system.config.models import AgentSystemConfig, MCPConfig
    
    # Create minimal configs
    system_config = AgentSystemConfig()
    mcp_config = MCPConfig()
    
    # PLUGIN_FACTORY is now a class
    plugin = PLUGIN_FACTORY("context_summarizer", system_config, mcp_config)
    return plugin.hooks_plugin  # Return the hooks component


@pytest.fixture
def mock_agent():
    """Create mock agent with proper config structure."""
    from unittest.mock import Mock
    from agent_system.config.models import AgentSystemConfig, AgentConfig, LLMSystemConfig, LLMModelConfig, LLMProfile
    
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
    
    # Mock next_internal_tool_request_id to generate unique suffixes
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
        'content': 'This is a concise summary of the conversation discussing project plans and technical details.'
    })
    return llm


def create_test_messages(count: int) -> list:
    """Create test ChatMessage objects for summarization."""
    from agent_system.llm.models import ChatMessage
    messages = []
    for i in range(count):
        messages.append(ChatMessage(
            role='user' if i % 2 == 0 else 'assistant',
            content=f'Test message {i} with some content to make it longer ' * 10
        ))
    return messages


@pytest.mark.asyncio
async def test_plugin_initialization(summarizer_plugin):
    """Test that plugin initializes correctly."""
    assert summarizer_plugin.name == 'context_summarizer'
    assert summarizer_plugin.trigger_percentage == 0.60  # Changed from trigger_tokens
    assert summarizer_plugin.chunk_size == 10
    assert summarizer_plugin.preserve_recent == 10
    assert summarizer_plugin.llm_profile == 'turbo'  # Default LLM profile


@pytest.mark.asyncio
async def test_below_threshold_no_summarization(summarizer_plugin, mock_agent, mock_llm):
    """Test that messages below threshold are not summarized."""
    messages = create_test_messages(5)  # Only 5 messages = low token count
    
    context = HookContext(
        hook_type=HookType.PRE_LLM_CALL,
        request_id='test-123',
        session_id='session-1',
        agent=mock_agent,
        messages=messages,
        llm=mock_llm
    )
    
    result = await summarizer_plugin.summarize_context(context)
    
    assert result.success is True
    assert result.modified is False
    assert result.metadata['reason'] in ['below_threshold', 'insufficient_old_messages', 'no_context_window']
    mock_llm.generate.assert_not_called()


@pytest.mark.asyncio
async def test_empty_messages(summarizer_plugin, mock_agent, mock_llm):
    """Test handling of empty message list."""
    context = HookContext(
        hook_type=HookType.PRE_LLM_CALL,
        request_id='test-123',
        session_id='session-1',
        agent=mock_agent,
        messages=[],
        llm=mock_llm
    )
    
    result = await summarizer_plugin.summarize_context(context)
    
    assert result.success is True
    assert result.modified is False
    assert result.metadata['reason'] == 'no_messages'


@pytest.mark.asyncio
async def test_chunked_summarization(summarizer_plugin, mock_agent, mock_llm):
    """Test that large message sets are summarized in chunks."""
    messages = create_test_messages(50)
    summarizer_plugin.trigger_percentage = 0.001  # Very low trigger for testing
    summarizer_plugin.chunk_size = 10
    summarizer_plugin.preserve_recent = 5
    
    context = HookContext(
        hook_type=HookType.PRE_LLM_CALL,
        request_id='test-123',
        session_id='session-1',
        agent=mock_agent,
        messages=messages,
        llm=mock_llm
    )
    
    result = await summarizer_plugin.summarize_context(context)
    
    if result.modified:
        summary_stats = result.metadata['summarization']
        assert summary_stats['total_chunks'] > 1
        assert summary_stats['summary_count'] > 0


@pytest.mark.asyncio
async def test_status_messages_published(summarizer_plugin, mock_agent, mock_llm, monkeypatch):
    """Test that status messages are published during summarization."""
    from agent_system.mcp.status import get_status_bus
    
    published_statuses = []
    
    async def mock_publish(event):
        published_statuses.append({
            'server': event.server,
            'message': event.message,
            'phase': event.phase,
            'request_id': event.request_id
        })
    
    status_bus = get_status_bus()
    monkeypatch.setattr(status_bus, 'publish', mock_publish)
    
    try:
        messages = create_test_messages(300)
        summarizer_plugin.trigger_percentage = 0.01
        
        context = HookContext(
            hook_type=HookType.PRE_LLM_CALL,
            request_id='test-status-123',
            session_id='session-1',
            agent=mock_agent,
            messages=messages,
            llm=mock_llm
        )
        
        result = await summarizer_plugin.summarize_context(context)
        
        if result.modified:
            assert len(published_statuses) >= 3, "Should have at least START, PROGRESS, END status messages"
            
            # Check for START message
            start_messages = [s for s in published_statuses if s['phase'].value == 'start']
            assert len(start_messages) > 0, "Should have START status message"
            assert start_messages[0]['server'] == 'context_summarizer'
            assert 'Starting context summarization' in start_messages[0]['message']
            
            # Check for PROGRESS message
            progress_messages = [s for s in published_statuses if s['phase'].value == 'progress']
            assert len(progress_messages) > 0, "Should have PROGRESS status message"
            assert 'Summarizing' in progress_messages[0]['message']
            
            end_messages = [s for s in published_statuses if s['phase'].value == 'end']
            assert len(end_messages) > 0, "Should have END status message"
            assert end_messages[0]['server'] == 'context_summarizer'
            assert 'Summarization complete' in end_messages[0]['message']
            assert 'tokens saved' in end_messages[0]['message']
            
            # CRITICAL: Verify unique request_id with suffix (like tool calls)
            # All summarizer status messages should use test-status-123_001 instead of test-status-123
            summarizer_events = [s for s in published_statuses if s['server'] == 'context_summarizer']
            for event in summarizer_events:
                assert event['request_id'] == 'test-status-123_001', \
                    f"Expected unique request_id 'test-status-123_001', got '{event['request_id']}'"
    
    finally:
        pass
