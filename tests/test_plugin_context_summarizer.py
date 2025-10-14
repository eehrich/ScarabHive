"""Tests for context_summarizer plugin."""
from __future__ import annotations

import pytest
from unittest.mock import AsyncMock

from agent_system.hooks import HookContext, HookType
from plugins.context_summarizer.plugin import PLUGIN_FACTORY


@pytest.fixture
def summarizer_plugin():
    """Create context summarizer plugin instance."""
    return PLUGIN_FACTORY()


@pytest.fixture
def mock_llm():
    """Create mock LLM for testing."""
    llm = AsyncMock()
    llm.generate = AsyncMock(return_value={
        'content': 'This is a concise summary of the conversation discussing project plans and technical details.'
    })
    return llm


def create_test_messages(count: int) -> list[dict]:
    """Create test messages for summarization."""
    messages = []
    for i in range(count):
        messages.append({
            'role': 'user' if i % 2 == 0 else 'assistant',
            'content': f'Test message {i} with some content to make it longer ' * 10,
            'timestamp': f'2025-10-14T10:{i:02d}:00'
        })
    return messages


@pytest.mark.asyncio
async def test_plugin_initialization(summarizer_plugin):
    """Test that plugin initializes correctly."""
    assert summarizer_plugin.name == 'context_summarizer'
    assert summarizer_plugin.trigger_tokens == 50000
    assert summarizer_plugin.chunk_size == 10
    assert summarizer_plugin.preserve_recent == 10
    assert summarizer_plugin.llm_profile == 'fast'


@pytest.mark.asyncio
async def test_below_threshold_no_summarization(summarizer_plugin, mock_llm):
    """Test that messages below threshold are not summarized."""
    messages = create_test_messages(5)
    
    context = HookContext(
        hook_type=HookType.PRE_LLM_CALL,
        request_id='test-123',
        session_id='session-1',
        messages=messages,
        llm=mock_llm
    )
    
    result = await summarizer_plugin.summarize_context(context)
    
    assert result.success is True
    assert result.modified is False
    assert result.metadata['reason'] == 'below_threshold'
    # LLM should not be called
    mock_llm.generate.assert_not_called()


@pytest.mark.asyncio
async def test_summarization_triggered(summarizer_plugin, mock_llm):
    """Test that summarization is triggered when threshold exceeded."""
    # Create many messages to exceed threshold
    messages = []
    # Add system message
    messages.append({'role': 'system', 'content': 'You are a helpful assistant.'})
    # Add many user/assistant messages (trigger summarization)
    messages.extend(create_test_messages(50))
    
    # Set lower threshold for testing
    summarizer_plugin.trigger_tokens = 1000
    summarizer_plugin.preserve_recent = 5
    
    context = HookContext(
        hook_type=HookType.PRE_LLM_CALL,
        request_id='test-123',
        session_id='session-1',
        messages=messages,
        llm=mock_llm
    )
    
    result = await summarizer_plugin.summarize_context(context)
    
    assert result.success is True
    assert result.modified is True
    assert 'summarization' in result.metadata
    
    summary_stats = result.metadata['summarization']
    assert summary_stats['original_message_count'] == len(messages)
    assert summary_stats['summarized_message_count'] < len(messages)
    assert summary_stats['reduction_ratio'] > 0
    
    # LLM should be called for summarization
    assert mock_llm.generate.called


@pytest.mark.asyncio
async def test_preserve_system_and_recent(summarizer_plugin, mock_llm):
    """Test that system and recent messages are preserved."""
    messages = []
    # System message
    messages.append({'role': 'system', 'content': 'System prompt'})
    # Old messages (should be summarized)
    for i in range(30):
        messages.append({
            'role': 'user' if i % 2 == 0 else 'assistant',
            'content': f'Old message {i} ' * 50
        })
    # Recent messages (should be preserved)
    for i in range(5):
        messages.append({
            'role': 'user',
            'content': f'Recent message {i}'
        })
    
    summarizer_plugin.trigger_tokens = 1000
    summarizer_plugin.preserve_recent = 5
    
    context = HookContext(
        hook_type=HookType.PRE_LLM_CALL,
        request_id='test-123',
        session_id='session-1',
        messages=messages,
        llm=mock_llm
    )
    
    result = await summarizer_plugin.summarize_context(context)
    
    assert result.success is True
    assert result.modified is True
    
    # Check that result has system + summarized + recent
    new_messages = result.context.messages
    
    # First message should still be system
    assert new_messages[0]['role'] == 'system'
    assert new_messages[0]['content'] == 'System prompt'
    
    # Last 5 messages should be the recent ones
    assert new_messages[-5:] == messages[-5:]


@pytest.mark.asyncio
async def test_insufficient_reduction_keeps_original(summarizer_plugin, mock_llm):
    """Test that if summary doesn't reduce enough, original is kept."""
    # Mock LLM to return very long summary (no real reduction)
    mock_llm.generate = AsyncMock(return_value={
        'content': 'Very long summary ' * 1000
    })
    
    messages = create_test_messages(20)
    summarizer_plugin.trigger_tokens = 1000
    summarizer_plugin.preserve_recent = 5
    summarizer_plugin.min_reduction = 0.3  # Require 30% reduction
    
    context = HookContext(
        hook_type=HookType.PRE_LLM_CALL,
        request_id='test-123',
        session_id='session-1',
        messages=messages,
        llm=mock_llm
    )
    
    result = await summarizer_plugin.summarize_context(context)
    
    assert result.success is True
    assert result.modified is False
    assert result.metadata['reason'] == 'insufficient_reduction'


@pytest.mark.asyncio
async def test_empty_messages(summarizer_plugin, mock_llm):
    """Test handling of empty message list."""
    context = HookContext(
        hook_type=HookType.PRE_LLM_CALL,
        request_id='test-123',
        session_id='session-1',
        messages=[],
        llm=mock_llm
    )
    
    result = await summarizer_plugin.summarize_context(context)
    
    assert result.success is True
    assert result.modified is False
    assert result.metadata['reason'] == 'no_messages'


@pytest.mark.asyncio
async def test_chunked_summarization(summarizer_plugin, mock_llm):
    """Test that large message sets are summarized in chunks."""
    messages = create_test_messages(50)
    summarizer_plugin.trigger_tokens = 1000
    summarizer_plugin.chunk_size = 10
    summarizer_plugin.preserve_recent = 5
    
    context = HookContext(
        hook_type=HookType.PRE_LLM_CALL,
        request_id='test-123',
        session_id='session-1',
        messages=messages,
        llm=mock_llm
    )
    
    result = await summarizer_plugin.summarize_context(context)
    
    if result.modified:
        summary_stats = result.metadata['summarization']
        # Should have processed multiple chunks
        assert summary_stats['total_chunks'] > 1
        assert summary_stats['summary_count'] > 0
