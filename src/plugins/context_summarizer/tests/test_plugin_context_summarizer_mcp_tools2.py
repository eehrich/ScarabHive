"""Tests for context_summarizer MCP tools (manual summarization)."""
from __future__ import annotations

import pytest
from unittest.mock import AsyncMock, Mock

from agent_system.config.models import (
    AgentSystemConfig, AgentConfig, LLMSystemConfig,
    LLMModelConfig, LLMProfile, MCPConfig
)
from plugins.context_summarizer.plugin import PLUGIN_FACTORY


@pytest.fixture
def plugin():
    """Create context summarizer plugin with MCP server."""
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
            'turbo': LLMProfile(model_ref='gpt-4'),
            'normal': LLMProfile(model_ref='gpt-4')
        }
    )

    mcp_config = MCPConfig()

    plugin = PLUGIN_FACTORY("context_summarizer", system_config, mcp_config)

    # Pin the summariser's OWN llm. Until the config chain was fixed, the
    # configured profile was the invalid 'fast', client creation failed, and
    # the plugin silently fell back to the agent's mock — which is why these
    # tests passed without ever pinning anything. With a valid profile it
    # builds a real client, so the seam has to be closed explicitly.
    summary_llm = AsyncMock()
    summary_llm.chat = AsyncMock(return_value="Kurze Zusammenfassung des Abschnitts.")
    summary_llm.model_name = "stub"
    plugin.server._hooks_impl._summarizer_llm = summary_llm

    return plugin


@pytest.fixture
def mock_agent_with_session():
    """Create mock agent with session tracker."""
    from agent_system.llm.models import ChatMessage

    agent = Mock()
    agent.name = "test_agent"
    agent.agent_id = "test-agent-123"

    # Mock next_internal_tool_request_id as AsyncMock
    agent.next_internal_tool_request_id = AsyncMock(return_value="summary-req-123")

    # Mock get_context_window to return integer, not AsyncMock
    agent.get_context_window = Mock(return_value=100000)

    # Mock LLM with context_window (as in real LLM clients)
    llm_mock = Mock()  # Use Mock for attributes, AsyncMock for async methods
    llm_mock.chat = AsyncMock(return_value={
        'content': '[Summary] Planning discussion covered database choices, API design, and deployment strategy.'
    })
    llm_mock.context_window = 100000  # Set directly on LLM, not on model_config

    agent.llm = llm_mock

    # Mock session tracker
    session_tracker = Mock()

    # Create test messages (50 messages = ~50k tokens)
    test_messages = []
    for i in range(50):
        msg = ChatMessage(
            role='user' if i % 2 == 0 else 'assistant',
            content=f'Message {i}: ' + 'This is test content ' * 100  # ~1000 tokens each
        )
        test_messages.append(msg)

    # Mock session tracker methods (sync methods)
    session_tracker.get_session_messages = Mock(return_value=test_messages)
    session_tracker.set_session_messages = Mock()
    session_tracker.set_compacted_messages = Mock()  # Used by summarize tool

    agent._session_tracker = session_tracker

    # Mock agent config
    agent_config = AgentConfig(llm_profile='normal')
    agent.agent_config = agent_config

    # Mock system config
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
            'turbo': LLMProfile(model_ref='gpt-4'),
            'normal': LLMProfile(model_ref='gpt-4')
        }
    )
    agent.system_config = system_config

    return agent


@pytest.mark.asyncio
async def test_mcp_tools_registration(plugin):
    """Test that MCP tools are properly registered."""
    tools = await plugin.list_tools()  # Use list_tools() instead of get_tools()

    assert len(tools) >= 2, "Should have at least 2 MCP tools"

    # Tools are MCPTool objects with .name attribute
    tool_names = [t.name for t in tools]
    assert 'context_summarizer_summarize' in tool_names
    assert 'context_summarizer_check_stats' in tool_names


@pytest.mark.asyncio
async def test_check_stats_tool(plugin, mock_agent_with_session):
    """Test check_stats tool returns correct statistics."""
    params = {
        '_session_id': 'test-session-123',
        '_agent': mock_agent_with_session
    }

    result = await plugin.call('context_summarizer_check_stats', params)

    assert result['status'] == 'success'
    assert 'message_count' in result
    assert 'total_tokens' in result
    assert 'context_window' in result
    assert 'utilization_percentage' in result
    assert 'recommendation' in result

    # Should have 50 messages
    assert result['message_count'] == 50

    # Context window should be 100k
    assert result['context_window'] == 100000

    # Should recommend summarization (high utilization)
    assert result['recommendation'] in ['summarize', 'ok']


@pytest.mark.asyncio
async def test_check_stats_empty_conversation(plugin, mock_agent_with_session):
    """Test check_stats with empty conversation."""
    # Mock empty messages - sync method
    mock_agent_with_session._session_tracker.get_session_messages = Mock(return_value=[])

    params = {
        '_session_id': 'test-session-123',
        '_agent': mock_agent_with_session
    }

    result = await plugin.call('context_summarizer_check_stats', params)

    assert result['status'] == 'success'
    assert result['message_count'] == 0
    assert result['total_tokens'] == 0
    assert result['utilization_percentage'] == 0.0
    assert result['recommendation'] == 'ok'


@pytest.mark.asyncio
async def test_summarize_tool_basic(plugin, mock_agent_with_session):
    """Test basic summarization via MCP tool."""
    params = {
        '_session_id': 'test-session-123',
        '_agent': mock_agent_with_session,
        'reason': 'test_manual_trigger'
    }

    result = await plugin.call('context_summarizer_summarize', params)

    assert result['status'] == 'success'
    assert 'original_count' in result
    assert 'summarized_count' in result
    assert 'tokens_saved' in result

    # Should have reduced message count
    assert result['original_count'] == 50
    assert result['summarized_count'] < result['original_count']

    # Should have saved tokens
    assert result['tokens_saved'] > 0

    # Session tracker should have been updated with compacted messages
    mock_agent_with_session._session_tracker.set_compacted_messages.assert_called_once()


@pytest.mark.asyncio
async def test_summarize_with_custom_params(plugin, mock_agent_with_session):
    """Test summarization with custom chunk_size and preserve_recent."""
    params = {
        '_session_id': 'test-session-123',
        '_agent': mock_agent_with_session,
        'reason': 'custom_params_test',
        'chunk_size': 5,
        'preserve_recent': 15
    }

    result = await plugin.call('context_summarizer_summarize', params)

    assert result['status'] == 'success'
    assert result['reason'] == 'custom_params_test'

    # Should have more messages preserved (15 instead of default 10)
    # 50 total - 15 preserved = 35 candidates for summarization
    assert result['summarized_count'] >= 15


@pytest.mark.asyncio
async def test_summarize_empty_conversation(plugin, mock_agent_with_session):
    """Test summarization on empty conversation."""
    # Mock empty messages
    mock_agent_with_session._session_tracker.get_session_messages = Mock(return_value=[])

    params = {
        '_session_id': 'test-session-123',
        '_agent': mock_agent_with_session
    }

    result = await plugin.call('context_summarizer_summarize', params)

    assert result['status'] == 'success'
    assert result['original_count'] == 0
    assert result['summarized_count'] == 0
    assert result['tokens_saved'] == 0


@pytest.mark.asyncio
async def test_summarize_missing_session(plugin):
    """Test error handling when session context is missing."""
    params = {
        # Missing _session_id and _agent
    }

    result = await plugin.call('context_summarizer_summarize', params)

    assert result['status'] == 'error'
    assert 'session context not available' in result['error'].lower()


@pytest.mark.asyncio
async def test_check_stats_missing_session(plugin):
    """Test error handling for check_stats without session."""
    params = {}

    result = await plugin.call('context_summarizer_check_stats', params)

    assert result['status'] == 'error'
    assert 'session context not available' in result['error'].lower()


@pytest.mark.asyncio
async def test_summarize_preserves_system_messages(plugin, mock_agent_with_session):
    """Test that summarization preserves system messages."""
    from agent_system.llm.models import ChatMessage

    # Add system message at start
    messages = [
        ChatMessage(role='system', content='You are a helpful assistant')
    ]
    # Add regular messages
    for i in range(49):
        messages.append(ChatMessage(
            role='user' if i % 2 == 0 else 'assistant',
            content=f'Message {i}: ' + 'test content ' * 100
        ))

    mock_agent_with_session._session_tracker.get_session_messages = Mock(return_value=messages)

    params = {
        '_session_id': 'test-session-123',
        '_agent': mock_agent_with_session
    }

    result = await plugin.call('context_summarizer_summarize', params)

    assert result['status'] == 'success'

    # Check that set_compacted_messages was called
    assert mock_agent_with_session._session_tracker.set_compacted_messages.called

    # Get the updated messages
    updated_messages = mock_agent_with_session._session_tracker.set_compacted_messages.call_args[0][1]

    # First message should still be system message
    first_msg = updated_messages[0]
    first_msg_dict = first_msg.model_dump() if hasattr(first_msg, 'model_dump') else first_msg
    assert first_msg_dict['role'] == 'system'


@pytest.mark.asyncio
async def test_tool_with_status_callback(plugin, mock_agent_with_session):
    """Test that tools properly use status callback."""
    status_mock = AsyncMock()

    params = {
        '_session_id': 'test-session-123',
        '_agent': mock_agent_with_session,
        '_status': status_mock
    }

    result = await plugin.call('context_summarizer_check_stats', params)

    assert result['status'] == 'success'

    # Status should have been called (at least end)
    assert status_mock.end.called or status_mock.progress.called


@pytest.mark.asyncio
async def test_summarize_with_status_progress(plugin, mock_agent_with_session):
    """Test that summarize reports progress via status."""
    status_mock = AsyncMock()

    params = {
        '_session_id': 'test-session-123',
        '_agent': mock_agent_with_session,
        '_status': status_mock
    }

    result = await plugin.call('context_summarizer_summarize', params)

    assert result['status'] == 'success'

    # Should have reported progress and end
    assert status_mock.progress.called or status_mock.end.called


@pytest.mark.asyncio
async def test_integration_check_then_summarize(plugin, mock_agent_with_session):
    """Test realistic workflow: check stats, then summarize if needed."""
    params = {
        '_session_id': 'test-session-123',
        '_agent': mock_agent_with_session
    }

    # Step 1: Check stats
    stats = await plugin.call('context_summarizer_check_stats', params)

    assert stats['status'] == 'success'
    assert stats['message_count'] == 50

    # Step 2: If recommendation is summarize, do it
    if stats['recommendation'] == 'summarize':
        params['reason'] = 'triggered_by_check_stats'
        summary_result = await plugin.call('context_summarizer_summarize', params)

        assert summary_result['status'] == 'success'
        assert summary_result['original_count'] == 50
        assert summary_result['summarized_count'] < 50
        assert summary_result['reason'] == 'triggered_by_check_stats'


@pytest.mark.asyncio
async def test_tool_schema_validation(plugin):
    """Test that tool schemas are valid MCP format."""
    tools = await plugin.list_tools()  # Fix: use list_tools() instead of get_tools()

    for tool in tools:
        # Tools are MCPTool objects with attributes
        assert hasattr(tool, 'name')
        assert hasattr(tool, 'description')
        assert hasattr(tool, 'input_schema')

        schema = tool.input_schema
        assert 'type' in schema
        assert schema['type'] == 'object'
        assert 'properties' in schema


@pytest.mark.asyncio
async def test_summarize_tokens_saved_calculation(plugin, mock_agent_with_session):
    """Test that tokens_saved is calculated correctly."""
    params = {
        '_session_id': 'test-session-123',
        '_agent': mock_agent_with_session
    }

    result = await plugin.call('context_summarizer_summarize', params)

    assert result['status'] == 'success'

    # tokens_saved should be positive if summarization happened
    if result['modified']:
        assert result['tokens_saved'] > 0
    else:
        # If not modified, tokens saved should be 0
        assert result['tokens_saved'] == 0


@pytest.mark.asyncio
async def test_multiple_summarizations_in_sequence(plugin, mock_agent_with_session):
    """Test running multiple summarizations in sequence."""
    params = {
        '_session_id': 'test-session-123',
        '_agent': mock_agent_with_session
    }

    # First summarization
    result1 = await plugin.call('context_summarizer_summarize', params)
    assert result1['status'] == 'success'

    # Update mock to return fewer messages (simulating the summarization)
    from agent_system.llm.models import ChatMessage
    reduced_messages = [
        ChatMessage(role='user', content='Summary of previous messages'),
    ] + [
        ChatMessage(role='user' if i % 2 == 0 else 'assistant', content=f'Recent {i}')
        for i in range(15)
    ]
    mock_agent_with_session._session_tracker.get_session_messages = Mock(return_value=reduced_messages)

    # Second summarization (should work on reduced set)
    result2 = await plugin.call('context_summarizer_summarize', params)
    assert result2['status'] == 'success'

    # Second summarization should have fewer messages to work with
    assert result2['original_count'] < result1['original_count']
