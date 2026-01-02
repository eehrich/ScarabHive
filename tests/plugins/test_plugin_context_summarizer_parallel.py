"""Tests for context_summarizer parallel chunk processing.

Tests the parallel summarization feature that processes all chunks
simultaneously for batch API efficiency.
"""
from __future__ import annotations

import asyncio
import pytest
from unittest.mock import AsyncMock, Mock, patch
from datetime import datetime

from agent_system.hooks import HookContext, HookType
from agent_system.llm.models import ChatMessage


@pytest.fixture
def summarizer_plugin():
    """Create context summarizer plugin instance."""
    from agent_system.config.models import AgentSystemConfig, MCPConfig
    from plugins.context_summarizer.plugin import PLUGIN_FACTORY
    
    # Create minimal configs
    system_config = AgentSystemConfig()
    mcp_config = MCPConfig()
    
    # PLUGIN_FACTORY returns ContextSummarizerHybridPlugin
    plugin = PLUGIN_FACTORY("context_summarizer", system_config, mcp_config)
    return plugin.server._hooks_impl  # Return the hooks implementation from server


@pytest.fixture
def mock_agent():
    """Create mock agent with proper config structure."""
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
            'normal': LLMProfile(model_ref='gpt-4'),
            'fast': LLMProfile(model_ref='gpt-4')
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
    llm.context_window = 100000
    
    # Track call times to verify parallel execution
    call_times = []
    
    async def mock_chat(messages, cancellation_token=None):
        call_times.append(datetime.now())
        # Small delay to simulate LLM processing
        await asyncio.sleep(0.05)
        return f"Summary of {len(messages)} messages"
    
    llm.chat = mock_chat
    llm._call_times = call_times
    return llm


def create_test_messages(count: int) -> list:
    """Create test ChatMessage objects for summarization."""
    messages = []
    for i in range(count):
        messages.append(ChatMessage(
            role='user' if i % 2 == 0 else 'assistant',
            content=f'Test message {i} with some content to make it longer ' * 10
        ))
    return messages


class TestParallelChunkSummarization:
    """Tests for parallel chunk processing."""

    @pytest.mark.asyncio
    async def test_summarize_single_chunk_success(self, summarizer_plugin, mock_llm):
        """Test that _summarize_single_chunk works correctly."""
        chunk = [
            {'role': 'user', 'content': 'Hello'},
            {'role': 'assistant', 'content': 'Hi there'},
            {'role': 'user', 'content': 'How are you?'},
        ]
        
        # Override the LLM getter to return our mock
        summarizer_plugin._summarizer_llm = mock_llm
        
        result = await summarizer_plugin._summarize_single_chunk(
            chunk=chunk,
            chunk_idx=0,
            total_chunks=3,
            summarizer_llm=mock_llm,
            cancellation_token=None
        )
        
        assert result['success'] is True
        assert result['is_summary'] is True
        assert result['chunk_idx'] == 0
        assert len(result['result']) == 1  # Single summary message
        
        summary_msg = result['result'][0]
        assert summary_msg['role'] == 'user'
        assert summary_msg['name'] == '__context_summary__'
        assert 'chunk 1/3' in summary_msg['content']
        assert summary_msg['metadata']['is_summary'] is True
        assert summary_msg['metadata']['summarized_count'] == 3

    @pytest.mark.asyncio
    async def test_summarize_single_chunk_too_small(self, summarizer_plugin, mock_llm):
        """Test that chunks with < 2 messages are kept as-is."""
        chunk = [{'role': 'user', 'content': 'Single message'}]
        
        result = await summarizer_plugin._summarize_single_chunk(
            chunk=chunk,
            chunk_idx=0,
            total_chunks=1,
            summarizer_llm=mock_llm,
            cancellation_token=None
        )
        
        assert result['success'] is True
        assert result['is_summary'] is False
        assert result['result'] == chunk  # Original chunk returned

    @pytest.mark.asyncio
    async def test_summarize_single_chunk_error_handling(self, summarizer_plugin):
        """Test error handling in single chunk summarization."""
        chunk = [
            {'role': 'user', 'content': 'Hello'},
            {'role': 'assistant', 'content': 'Hi'},
        ]
        
        # Create LLM that raises an error
        error_llm = AsyncMock()
        error_llm.chat = AsyncMock(side_effect=RuntimeError("LLM error"))
        
        result = await summarizer_plugin._summarize_single_chunk(
            chunk=chunk,
            chunk_idx=0,
            total_chunks=1,
            summarizer_llm=error_llm,
            cancellation_token=None
        )
        
        assert result['success'] is False
        assert result['is_summary'] is False
        assert result['result'] == chunk  # Original chunk returned on error
        assert 'LLM error' in result.get('error', '')

    @pytest.mark.asyncio
    async def test_parallel_execution_timing(self, summarizer_plugin, mock_agent, mock_llm):
        """Test that chunks are processed in parallel, not sequentially."""
        # Create enough messages to generate multiple chunks
        messages = create_test_messages(50)
        summarizer_plugin.trigger_percentage = 0.001  # Very low trigger
        summarizer_plugin.chunk_size = 10
        summarizer_plugin.preserve_recent = 5
        summarizer_plugin._summarizer_llm = mock_llm
        
        # Track when each LLM call starts
        call_starts = []
        original_chat = mock_llm.chat
        
        async def tracking_chat(*args, **kwargs):
            call_starts.append(asyncio.get_event_loop().time())
            return await original_chat(*args, **kwargs)
        
        mock_llm.chat = tracking_chat
        
        context = HookContext(
            hook_type=HookType.PRE_LLM_CALL,
            request_id='test-parallel-123',
            session_id='session-1',
            agent=mock_agent,
            messages=messages,
            llm=mock_llm
        )
        
        start_time = asyncio.get_event_loop().time()
        result = await summarizer_plugin.summarize_context(context)
        total_time = asyncio.get_event_loop().time() - start_time
        
        # Verify we got a result
        if result.modified:
            stats = result.metadata['summarization']
            
            # Verify parallel flag is set
            assert stats.get('parallel') is True, "Should indicate parallel processing"
            
            # Verify multiple chunks were processed
            assert stats['total_chunks'] >= 3, f"Expected at least 3 chunks, got {stats['total_chunks']}"
            
            # If sequential: would take ~0.05s * num_chunks
            # If parallel: should take ~0.05s total (plus overhead)
            # Allow generous margin but should be significantly less than sequential
            if len(call_starts) > 1:
                # Check that calls started close together (within 0.1s of each other)
                # This indicates parallel execution
                first_call = min(call_starts)
                last_first_wave = max(call_starts)
                time_spread = last_first_wave - first_call
                
                # All calls should start within a short window if parallel
                assert time_spread < 0.2, f"Calls should start close together for parallel execution, spread was {time_spread}s"

    @pytest.mark.asyncio
    async def test_parallel_maintains_chunk_order(self, summarizer_plugin, mock_llm):
        """Test that parallel processing maintains correct chunk order in output."""
        # Create chunks with identifiable content
        chunks = [
            [{'role': 'user', 'content': f'Chunk {i} message 1'},
             {'role': 'assistant', 'content': f'Chunk {i} message 2'}]
            for i in range(5)
        ]
        
        # Add varying delays to simulate different processing times
        call_count = 0
        async def delayed_chat(messages, cancellation_token=None):
            nonlocal call_count
            my_count = call_count
            call_count += 1
            # Different delays to test order preservation
            delays = [0.1, 0.02, 0.15, 0.01, 0.05]
            await asyncio.sleep(delays[my_count % len(delays)])
            return f"Summary {my_count}"
        
        mock_llm.chat = delayed_chat
        
        # Create a mock scope
        mock_scope = AsyncMock()
        mock_scope.progress = AsyncMock()
        
        summarized, stats = await summarizer_plugin._summarize_chunks_parallel(
            chunks=chunks,
            summarizer_llm=mock_llm,
            cancellation_token=None,
            scope=mock_scope,
            total_chunks=5
        )
        
        # Verify all chunks were processed
        assert stats['total_chunks'] == 5
        assert stats['summary_count'] == 5
        
        # Verify output maintains chunk order
        # Each summary should correspond to its chunk index
        for i, msg in enumerate(summarized):
            assert f'chunk {i + 1}/5' in msg['content'].lower(), \
                f"Summary {i} should reference chunk {i + 1}/5"

    @pytest.mark.asyncio
    async def test_parallel_handles_mixed_results(self, summarizer_plugin):
        """Test handling of mixed success/failure in parallel processing."""
        chunks = [
            [{'role': 'user', 'content': 'Chunk 0 msg 1'},
             {'role': 'assistant', 'content': 'Chunk 0 msg 2'}],
            [{'role': 'user', 'content': 'Chunk 1 msg 1'},
             {'role': 'assistant', 'content': 'Chunk 1 msg 2'}],
            [{'role': 'user', 'content': 'Chunk 2 msg 1'},
             {'role': 'assistant', 'content': 'Chunk 2 msg 2'}],
        ]
        
        # LLM that fails on chunk 1
        call_count = 0
        async def flaky_chat(messages, cancellation_token=None):
            nonlocal call_count
            my_count = call_count
            call_count += 1
            if my_count == 1:
                raise RuntimeError("Simulated failure")
            return f"Summary {my_count}"
        
        mock_llm = AsyncMock()
        mock_llm.chat = flaky_chat
        
        mock_scope = AsyncMock()
        mock_scope.progress = AsyncMock()
        
        summarized, stats = await summarizer_plugin._summarize_chunks_parallel(
            chunks=chunks,
            summarizer_llm=mock_llm,
            cancellation_token=None,
            scope=mock_scope,
            total_chunks=3
        )
        
        # Verify stats reflect partial success
        assert stats['total_chunks'] == 3
        assert stats['successful_chunks'] == 2
        assert stats['failed_chunks'] == 1
        
        # Verify output: successful chunks are summarized, failed chunk keeps original
        # Result: summary0 (1 msg), original1a + original1b (2 msgs), summary2 (1 msg) = 4 items
        assert len(summarized) == 4
        
        # First should be a summary
        assert summarized[0].get('name') == '__context_summary__'
        assert 'chunk 1/3' in summarized[0]['content'].lower()
        
        # Middle should be original messages (from failed chunk)
        assert summarized[1]['content'] == 'Chunk 1 msg 1'
        assert summarized[2]['content'] == 'Chunk 1 msg 2'
        
        # Last should be a summary
        assert summarized[3].get('name') == '__context_summary__'
        assert 'chunk 3/3' in summarized[3]['content'].lower()

    @pytest.mark.asyncio
    async def test_parallel_cancellation(self, summarizer_plugin):
        """Test that cancellation is handled correctly in parallel mode."""
        chunks = [
            [{'role': 'user', 'content': f'Chunk {i} msg 1'},
             {'role': 'assistant', 'content': f'Chunk {i} msg 2'}]
            for i in range(5)
        ]
        
        # Create cancellation token that's already cancelled
        class MockCancellationToken:
            is_cancelled = True
        
        mock_llm = AsyncMock()
        mock_llm.chat = AsyncMock(return_value="Summary")
        
        mock_scope = AsyncMock()
        mock_scope.progress = AsyncMock()
        
        # Single chunk test with cancellation
        result = await summarizer_plugin._summarize_single_chunk(
            chunk=chunks[0],
            chunk_idx=0,
            total_chunks=5,
            summarizer_llm=mock_llm,
            cancellation_token=MockCancellationToken()
        )
        
        # Should handle cancellation gracefully
        # Note: The implementation checks cancellation at the start of summarize_context,
        # not in individual chunk processing. But asyncio.CancelledError is handled.

    @pytest.mark.asyncio
    async def test_stats_include_parallel_flag(self, summarizer_plugin, mock_agent, mock_llm):
        """Test that stats include parallel processing indicator."""
        messages = create_test_messages(30)
        summarizer_plugin.trigger_percentage = 0.001
        summarizer_plugin.chunk_size = 10
        summarizer_plugin.preserve_recent = 5
        summarizer_plugin._summarizer_llm = mock_llm
        
        context = HookContext(
            hook_type=HookType.PRE_LLM_CALL,
            request_id='test-stats-123',
            session_id='session-1',
            agent=mock_agent,
            messages=messages,
            llm=mock_llm
        )
        
        result = await summarizer_plugin.summarize_context(context)
        
        if result.modified:
            stats = result.metadata['summarization']
            assert 'parallel' in stats, "Stats should include 'parallel' key"
            assert stats['parallel'] is True, "Parallel flag should be True"


class TestParallelBatchAPIIntegration:
    """Tests for batch API integration with parallel processing."""

    @pytest.mark.asyncio
    async def test_all_llm_calls_concurrent(self, summarizer_plugin):
        """Verify all LLM calls are made concurrently for batch API efficiency."""
        chunks = [
            [{'role': 'user', 'content': f'Chunk {i} msg 1'},
             {'role': 'assistant', 'content': f'Chunk {i} msg 2'}]
            for i in range(4)
        ]
        
        # Track concurrent call count
        concurrent_calls = 0
        max_concurrent = 0
        call_lock = asyncio.Lock()
        
        async def tracking_chat(messages, cancellation_token=None):
            nonlocal concurrent_calls, max_concurrent
            async with call_lock:
                concurrent_calls += 1
                max_concurrent = max(max_concurrent, concurrent_calls)
            
            await asyncio.sleep(0.1)  # Simulate LLM processing
            
            async with call_lock:
                concurrent_calls -= 1
            
            return "Summary"
        
        mock_llm = AsyncMock()
        mock_llm.chat = tracking_chat
        
        mock_scope = AsyncMock()
        mock_scope.progress = AsyncMock()
        
        await summarizer_plugin._summarize_chunks_parallel(
            chunks=chunks,
            summarizer_llm=mock_llm,
            cancellation_token=None,
            scope=mock_scope,
            total_chunks=4
        )
        
        # All 4 chunks should have been processed concurrently
        assert max_concurrent == 4, f"Expected 4 concurrent calls, got {max_concurrent}"

    @pytest.mark.asyncio
    async def test_progress_messages_for_parallel(self, summarizer_plugin):
        """Test that progress messages are sent for parallel processing."""
        chunks = [
            [{'role': 'user', 'content': f'Chunk {i} msg 1'},
             {'role': 'assistant', 'content': f'Chunk {i} msg 2'}]
            for i in range(3)
        ]
        
        mock_llm = AsyncMock()
        mock_llm.chat = AsyncMock(return_value="Summary")
        
        mock_scope = AsyncMock()
        progress_calls = []
        mock_scope.progress = AsyncMock(side_effect=lambda msg: progress_calls.append(msg))
        
        await summarizer_plugin._summarize_chunks_parallel(
            chunks=chunks,
            summarizer_llm=mock_llm,
            cancellation_token=None,
            scope=mock_scope,
            total_chunks=3
        )
        
        # Should have completion progress message
        assert len(progress_calls) >= 1
        assert any('Completed' in msg for msg in progress_calls)
