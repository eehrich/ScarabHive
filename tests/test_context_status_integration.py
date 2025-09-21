"""Tests for StatusEvent integration in context management."""

import pytest
from unittest.mock import AsyncMock, patch, Mock
from agent_system.context.manager import ContextManager
from agent_system.context.config import ContextConfig, ContextStrategy
from agent_system.llm.clients import ChatMessage
from agent_system.mcp.improved_status import StatusPhase


class TestContextStatusIntegration:
    """Test status event integration across context management components."""
    
    def setup_method(self):
        """Set up test fixtures."""
        self.config = ContextConfig(
            context_window=1000,
            strategy=ContextStrategy.TRUNCATE_OLDEST,
            summarization_threshold=800
        )
        self.manager = ContextManager(self.config)
    
    @pytest.mark.asyncio
    async def test_context_management_full_status_flow(self):
        """Test complete status event flow during context management."""
        messages = [
            ChatMessage(role="user", content="Message " + "x" * 100)
            for _ in range(10)  # Create messages that exceed threshold
        ]
        
        with patch.object(self.manager, 'estimate_token_count', return_value=900):
            with patch.object(self.manager, '_truncate_oldest', return_value=messages[:5]):
                with patch('agent_system.context.manager.publish_status_improved', new_callable=AsyncMock) as mock_publish:
                    await self.manager.manage_context(messages)
        
        # Verify status events were published
        assert mock_publish.call_count >= 2
        
        # Check status event sequence
        calls = mock_publish.call_args_list
        
        # First call should be START
        start_call = calls[0]
        assert start_call[1]['phase'] == StatusPhase.START
        assert start_call[1]['server'] == 'context-manager'
        assert 'Starting context management' in start_call[1]['message']
        
        # Last call should be END (success)
        end_call = calls[-1]
        assert end_call[1]['phase'] == StatusPhase.END
        assert end_call[1]['server'] == 'context-manager'
        assert 'complete' in end_call[1]['message'].lower()
        
        # Verify metadata
        assert 'tokens' in start_call[1]['meta']
        assert 'strategy' in start_call[1]['meta']
        assert 'original_messages' in end_call[1]['meta']
        assert 'final_messages' in end_call[1]['meta']
    
    @pytest.mark.asyncio 
    async def test_context_management_error_status(self):
        """Test error status events during context management failures."""
        messages = [ChatMessage(role="user", content="Test message")]
        
        with patch.object(self.manager, 'estimate_token_count', return_value=900):
            with patch.object(self.manager, '_truncate_oldest', side_effect=Exception("Truncation failed")):
                with patch('agent_system.context.manager.publish_status_improved', new_callable=AsyncMock) as mock_publish:
                    result = await self.manager.manage_context(messages)
        
        # Should return original messages on error
        assert result == messages
        
        # Check for error status event
        error_calls = [call for call in mock_publish.call_args_list 
                      if call[1]['phase'] == StatusPhase.ERROR]
        assert len(error_calls) == 1
        
        error_call = error_calls[0]
        assert error_call[1]['server'] == 'context-manager'
        assert error_call[1]['level'] == 'error'
        assert 'failed' in error_call[1]['message'].lower()
        assert 'error' in error_call[1]['meta']
    
    @pytest.mark.asyncio
    async def test_summarizer_status_integration(self):
        """Test status events from summarizer integration."""
        
        # Create manager with summarizer strategy and low threshold for testing
        config = ContextConfig(
            context_window=1000,
            strategy=ContextStrategy.SUMMARIZE_OLDEST,
            summarization_threshold=800
        )
        manager = ContextManager(config)
        
        # Mock summarizer
        mock_summarizer = Mock()
        mock_summarizer.summarize_conversation = AsyncMock(return_value=[
            ChatMessage(role="system", content="Summary"),
            ChatMessage(role="user", content="Recent message")
        ])
        manager._summarizer = mock_summarizer
        
        messages = [
            ChatMessage(role="user", content="Old message"),
            ChatMessage(role="assistant", content="Old response"),
            ChatMessage(role="user", content="Recent message")
        ]
        
        with patch.object(manager, 'estimate_token_count', return_value=900):
            with patch('agent_system.context.manager.publish_status_improved', new_callable=AsyncMock) as mock_publish:
                await manager.manage_context(messages)
        
        # Should call summarizer
        mock_summarizer.summarize_conversation.assert_called_once()
        
        # Should publish status events
        assert mock_publish.call_count >= 2
        start_calls = [call for call in mock_publish.call_args_list if call[1]['phase'] == StatusPhase.START]
        end_calls = [call for call in mock_publish.call_args_list if call[1]['phase'] == StatusPhase.END]
        assert len(start_calls) >= 1
        assert len(end_calls) >= 1
    
    @pytest.mark.asyncio
    async def test_optimizer_status_integration(self):
        """Test status events from token optimizer integration."""
        from agent_system.context.optimizer import TokenOptimizer
        
        optimizer = TokenOptimizer()
        messages = [
            ChatMessage(role="user", content="Message with lots of   extra    spaces   "),
            ChatMessage(role="assistant", content="  Response  with  formatting  issues  ")
        ]
        
        with patch('agent_system.context.optimizer.publish_status_improved', new_callable=AsyncMock) as mock_publish:
            await optimizer.optimize_messages(messages)
        
        # Should publish start and end events
        assert mock_publish.call_count >= 2
        
        start_calls = [call for call in mock_publish.call_args_list if call[1]['phase'] == StatusPhase.START]
        end_calls = [call for call in mock_publish.call_args_list if call[1]['phase'] == StatusPhase.END]
        
        assert len(start_calls) == 1
        assert len(end_calls) == 1
        
        # Check start event
        start_call = start_calls[0]
        assert start_call[1]['server'] == 'token-optimizer'
        assert 'Starting token optimization' in start_call[1]['message']
        assert start_call[1]['meta']['message_count'] == 2
        
        # Check end event
        end_call = end_calls[0]
        assert end_call[1]['server'] == 'token-optimizer'
        assert 'complete' in end_call[1]['message'].lower()
        assert 'original_tokens' in end_call[1]['meta']
        assert 'optimized_tokens' in end_call[1]['meta']


class TestStatusEventMetadata:
    """Test status event metadata content and structure."""
    
    @pytest.mark.asyncio
    async def test_context_manager_metadata_structure(self):
        """Test context manager status event metadata structure."""
        config = ContextConfig(context_window=1000)
        manager = ContextManager(config)
        messages = [ChatMessage(role="user", content="Test")]
        
        with patch.object(manager, 'estimate_token_count', return_value=900):
            with patch.object(manager, '_truncate_oldest', return_value=messages):
                with patch('agent_system.context.manager.publish_status_improved', new_callable=AsyncMock) as mock_publish:
                    await manager.manage_context(messages)
        
        # Check start event metadata
        start_call = mock_publish.call_args_list[0]
        start_meta = start_call[1]['meta']
        assert 'tokens' in start_meta
        assert 'strategy' in start_meta
        assert isinstance(start_meta['tokens'], int)
        assert isinstance(start_meta['strategy'], str)
        
        # Check end event metadata
        end_call = mock_publish.call_args_list[-1]
        end_meta = end_call[1]['meta']
        required_fields = [
            'original_messages', 'final_messages', 'original_tokens', 
            'final_tokens', 'tokens_saved', 'processing_time_ms'
        ]
        for field in required_fields:
            assert field in end_meta
            assert isinstance(end_meta[field], (int, float))
    
    @pytest.mark.asyncio
    async def test_optimizer_metadata_structure(self):
        """Test token optimizer status event metadata structure."""
        from agent_system.context.optimizer import TokenOptimizer
        
        optimizer = TokenOptimizer()
        messages = [ChatMessage(role="user", content="Test message")]
        
        with patch('agent_system.context.optimizer.publish_status_improved', new_callable=AsyncMock) as mock_publish:
            await optimizer.optimize_messages(messages)
        
        # Check end event metadata
        end_calls = [call for call in mock_publish.call_args_list if call[1]['phase'] == StatusPhase.END]
        assert len(end_calls) == 1
        
        end_meta = end_calls[0][1]['meta']
        required_fields = [
            'original_tokens', 'optimized_tokens', 'tokens_saved', 
            'compression_ratio', 'messages_processed'
        ]
        for field in required_fields:
            assert field in end_meta
            assert isinstance(end_meta[field], (int, float))


class TestStatusEventErrorHandling:
    """Test error handling in status event publishing."""
    
    @pytest.mark.asyncio
    async def test_status_publish_failure_resilience(self):
        """Test that status publishing failures don't break context management."""
        config = ContextConfig(context_window=1000)
        manager = ContextManager(config)
        messages = [ChatMessage(role="user", content="Test")]
        
        with patch.object(manager, 'estimate_token_count', return_value=900):
            with patch.object(manager, '_truncate_oldest', return_value=messages):
                with patch('agent_system.context.manager.publish_status_improved', 
                          new_callable=AsyncMock, side_effect=Exception("Status publish failed")):
                    # Should not raise exception despite status publishing failure
                    await manager.manage_context(messages)
        
        # Context management should still work
        # Note: No assertion here as error handling returns original messages
    
    @pytest.mark.asyncio
    async def test_error_status_event_content(self):
        """Test error status event contains proper error information."""
        config = ContextConfig(context_window=1000)
        manager = ContextManager(config)
        messages = [ChatMessage(role="user", content="Test")]
        
        test_error = Exception("Custom test error")
        
        with patch.object(manager, 'estimate_token_count', return_value=900):
            with patch.object(manager, '_truncate_oldest', side_effect=test_error):
                with patch('agent_system.context.manager.publish_status_improved', new_callable=AsyncMock) as mock_publish:
                    await manager.manage_context(messages)
        
        # Find error event
        error_calls = [call for call in mock_publish.call_args_list if call[1]['phase'] == StatusPhase.ERROR]
        assert len(error_calls) == 1
        
        error_call = error_calls[0]
        assert error_call[1]['level'] == 'error'
        assert 'Custom test error' in error_call[1]['message']
        assert error_call[1]['meta']['error'] == 'Custom test error'
        assert error_call[1]['meta']['strategy'] == config.strategy.value


class TestStatusEventMessageContent:
    """Test status event message content is descriptive and useful."""
    
    @pytest.mark.asyncio
    async def test_status_messages_are_descriptive(self):
        """Test that status messages contain useful information."""
        config = ContextConfig(context_window=1000)
        manager = ContextManager(config)
        messages = [ChatMessage(role="user", content="Test message")]
        
        with patch.object(manager, 'estimate_token_count', return_value=900):
            with patch.object(manager, '_truncate_oldest', return_value=messages):
                with patch('agent_system.context.manager.publish_status_improved', new_callable=AsyncMock) as mock_publish:
                    await manager.manage_context(messages)
        
        for call in mock_publish.call_args_list:
            message = call[1]['message']
            assert isinstance(message, str)
            assert len(message) > 0
            # Should contain emoji or descriptive text
            assert any(char in message for char in ['🔄', '✅', '❌']) or \
                   any(word in message.lower() for word in ['starting', 'complete', 'failed'])
    
    @pytest.mark.asyncio
    async def test_progress_messages_show_progress(self):
        """Test that progress messages show meaningful progress information."""
        from agent_system.context.optimizer import TokenOptimizer
        
        optimizer = TokenOptimizer()
        # Create enough messages to trigger progress events
        messages = [ChatMessage(role="user", content=f"Message {i}") for i in range(15)]
        
        with patch('agent_system.context.optimizer.publish_status_improved', new_callable=AsyncMock) as mock_publish:
            await optimizer.optimize_messages(messages)
        
        progress_calls = [call for call in mock_publish.call_args_list if call[1]['phase'] == StatusPhase.PROGRESS]
        
        for call in progress_calls:
            message = call[1]['message']
            meta = call[1]['meta']
            
            # Progress message should contain numbers
            assert any(char.isdigit() for char in message)
            # Meta should contain progress information
            assert 'processed' in meta
            assert 'total' in meta
            assert meta['processed'] <= meta['total']