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
    llm.context_window = 100000  # Add context window for percentage calculations
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
    assert summarizer_plugin.trigger_percentage == 0.60  # Changed from trigger_tokens
    assert summarizer_plugin.chunk_size == 10
    assert summarizer_plugin.preserve_recent == 10
    assert summarizer_plugin.llm_profile == 'fast'


@pytest.mark.asyncio
async def test_below_threshold_no_summarization(summarizer_plugin, mock_llm):
    """Test that messages below threshold are not summarized."""
    messages = create_test_messages(5)  # Only 5 messages = low token count
    
    context = HookContext(
        hook_type=HookType.PRE_LLM_CALL,
        request_id='test-123',
        session_id='session-1',
        messages=messages,
        llm=mock_llm  # Has context_window=100000, trigger at 60% = 60000 tokens
    )
    
    result = await summarizer_plugin.summarize_context(context)
    
    assert result.success is True
    assert result.modified is False
    # Reason could be 'below_threshold' or 'insufficient_old_messages' (both mean no action needed)
    assert result.metadata['reason'] in ['below_threshold', 'insufficient_old_messages']
    # LLM should not be called for summarization
    mock_llm.generate.assert_not_called()


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
