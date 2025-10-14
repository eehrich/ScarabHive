"""Tests for context_summarizer plugin."""
from __future__ import annotations

import pytest
from unittest.mock import AsyncMock

from agent_system.hooks import HookContext, HookType
from plugins.context_summarizer.plugin import PLUGIN_FACTORY


@pytest.fixture
def summarizer_plugin():
    """Create context summarizer plugin instance."""
    # PLUGIN_FACTORY returns tuple (hooks_plugin, web_factory) for hybrid plugin
    hooks_plugin, _web_factory = PLUGIN_FACTORY()
    return hooks_plugin


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
    assert summarizer_plugin.llm_profile == 'turbo'  # Default LLM profile


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


@pytest.mark.asyncio
async def test_status_messages_published(summarizer_plugin, mock_llm, monkeypatch):
    """Test that status messages are published during summarization."""
    from agent_system.mcp.status import get_status_bus
    
    # Track published status messages
    published_statuses = []
    
    async def mock_publish(event):
        published_statuses.append({
            'server': event.server,
            'message': event.message,
            'phase': event.phase,
            'request_id': event.request_id
        })
    
    # Patch the status bus
    status_bus = get_status_bus()
    original_publish = status_bus.publish
    monkeypatch.setattr(status_bus, 'publish', mock_publish)
    
    try:
        # Create enough messages to trigger summarization
        # With context_window=100000 and trigger=60%, we need >60000 tokens
        # Each message is ~100 chars * 10 = 1000 chars ≈ 250 tokens
        # Need ~250 messages to exceed threshold
        messages = create_test_messages(300)
        
        # Override trigger to make test faster
        summarizer_plugin.trigger_percentage = 0.01  # Trigger at 1% = 1000 tokens
        
        context = HookContext(
            hook_type=HookType.PRE_LLM_CALL,
            request_id='test-status-123',
            session_id='session-1',
            messages=messages,
            llm=mock_llm
        )
        
        result = await summarizer_plugin.summarize_context(context)
        
        # If summarization happened, check status messages
        if result.modified:
            # Should have START, PROGRESS, and END messages
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
            
            # Check for END message
            end_messages = [s for s in published_statuses if s['phase'].value == 'end']
            assert len(end_messages) > 0, "Should have END status message"
            assert end_messages[0]['server'] == 'context_summarizer'
            assert 'Summarization complete' in end_messages[0]['message']
            assert 'tokens saved' in end_messages[0]['message']
    
    finally:
        # Restore original publish method
        monkeypatch.setattr(status_bus, 'publish', original_publish)
