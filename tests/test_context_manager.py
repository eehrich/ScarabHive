"""Tests for ContextManager functionality."""

import pytest
from unittest.mock import Mock, AsyncMock, patch
from agent_system.context.manager import ContextManager
from agent_system.context.config import ContextConfig, ContextStrategy, WarningLevel
from agent_system.llm.models import ChatMessage


class TestContextManager:
    """Test the ContextManager class."""
    
    def setup_method(self):
        """Set up test fixtures."""
        self.config = ContextConfig(
            context_window=1000,
            preserve_recent_messages=5,
            summarization_threshold=800
        )
        self.manager = ContextManager(self.config)
    
    def test_init(self):
        """Test manager initialization."""
        assert self.manager.config == self.config
        assert self.manager._summarizer is None
        assert hasattr(self.manager, '_last_warning_level')
    
    def test_estimate_token_count(self):
        """Test token count estimation."""
        messages = [
            ChatMessage(role="user", content="Hello world"),
            ChatMessage(role="assistant", content="Hi there! How can I help?")
        ]
        
        estimated = self.manager.estimate_token_count(messages)
        assert isinstance(estimated, int)
        assert estimated > 0  # Should be positive
    
    def test_estimate_token_count_empty(self):
        """Test token estimation with empty messages."""
        assert self.manager.estimate_token_count([]) == 0
    
    def test_estimate_token_count_none_content(self):
        """Test token estimation with None content."""
        messages = [ChatMessage(role="user", content=None)]
        estimated = self.manager.estimate_token_count(messages)
        assert estimated >= 0  # Should handle None content gracefully
    
    def test_check_and_warn_no_warnings(self):
        """Test check_and_warn with low token count."""
        messages = [ChatMessage(role="user", content="Short message")]
        
        with patch.object(self.manager, 'estimate_token_count', return_value=500):
            tokens, level = self.manager.check_and_warn(messages, 1)
            
        assert tokens == 500
        assert level is None
    
    def test_check_and_warn_yellow_level(self):
        """Test check_and_warn with yellow warning level."""
        messages = [ChatMessage(role="user", content="Medium message")]
        
        # Yellow threshold is 70% of 1000 = 700
        with patch.object(self.manager, 'estimate_token_count', return_value=750):
            tokens, level = self.manager.check_and_warn(messages, 1)
            
        assert tokens == 750
        assert level == WarningLevel.YELLOW
    
    def test_check_and_warn_orange_level(self):
        """Test check_and_warn with orange warning level."""
        messages = [ChatMessage(role="user", content="Longer message")]
        
        # Orange threshold is 85% of 1000 = 850
        with patch.object(self.manager, 'estimate_token_count', return_value=860):
            tokens, level = self.manager.check_and_warn(messages, 1)
            
        assert tokens == 860
        assert level == WarningLevel.ORANGE
    
    def test_check_and_warn_red_level(self):
        """Test check_and_warn with red warning level."""
        messages = [ChatMessage(role="user", content="Very long message")]
        
        # Red threshold is 95% of 1000 = 950
        with patch.object(self.manager, 'estimate_token_count', return_value=970):
            tokens, level = self.manager.check_and_warn(messages, 1)
            
        assert tokens == 970
        assert level == WarningLevel.RED
    
    def test_should_manage_context_below_threshold(self):
        """Test should_manage_context with tokens below threshold."""
        result = self.manager.should_manage_context(500, None)
        assert not result
    
    def test_should_manage_context_orange_level(self):
        """Test should_manage_context with orange warning level."""
        result = self.manager.should_manage_context(860, WarningLevel.ORANGE)
        assert result
    
    def test_should_manage_context_red_level(self):
        """Test should_manage_context with red warning level."""
        result = self.manager.should_manage_context(970, WarningLevel.RED)
        assert result
    
    def test_should_manage_context_early_summarization(self):
        """Test should_manage_context with early summarization threshold."""
        # Early threshold is 800, which should trigger management
        result = self.manager.should_manage_context(850, WarningLevel.YELLOW)
        assert result


class TestContextManagerTruncation:
    """Test truncation functionality."""
    
    def setup_method(self):
        """Set up test fixtures."""
        self.config = ContextConfig(context_window=1000, preserve_recent_messages=3)
        self.manager = ContextManager(self.config)
    
    def test_truncate_oldest_basic(self):
        """Test basic truncation of oldest messages."""
        messages = [
            ChatMessage(role="system", content="System message"),
            ChatMessage(role="user", content="Message 1"),
            ChatMessage(role="assistant", content="Response 1"),
            ChatMessage(role="user", content="Message 2"),
            ChatMessage(role="assistant", content="Response 2"),
            ChatMessage(role="user", content="Message 3")
        ]
        
        # Mock estimate_token_count to return specific values
        def mock_estimate(msgs):
            if len(msgs) == 6:
                return 1200  # Over limit
            elif len(msgs) == 5:
                return 1000  # Still over limit  
            elif len(msgs) == 4:
                return 800   # Under limit
            else:
                return len(msgs) * 100
        
        with patch.object(self.manager, 'estimate_token_count', side_effect=mock_estimate):
            result = self.manager._truncate_oldest(messages)
        
        # Should keep system message plus 3 most recent messages  
        assert len(result) == 4
        assert result[0].content == "System message"  # System message preserved
        assert result[1].content == "Message 2"
        assert result[2].content == "Response 2"
        assert result[3].content == "Message 3"
    
    def test_truncate_oldest_no_system_message(self):
        """Test truncation without system message."""
        messages = [
            ChatMessage(role="user", content="Message 1"),
            ChatMessage(role="assistant", content="Response 1"),
            ChatMessage(role="user", content="Message 2"),
            ChatMessage(role="assistant", content="Response 2"),
            ChatMessage(role="user", content="Message 3")
        ]
        
        def mock_estimate(msgs):
            return len(msgs) * 200  # Each message = 200 tokens
        
        with patch.object(self.manager, 'estimate_token_count', side_effect=mock_estimate):
            result = self.manager._truncate_oldest(messages)
        
        # Should keep only the 3 most recent messages (3 * 200 = 600 < 1000)
        assert len(result) == 3
        assert result[0].content == "Message 2"
        assert result[1].content == "Response 2"
        assert result[2].content == "Message 3"
    
    def test_truncate_oldest_preserve_minimum(self):
        """Test that truncation preserves minimum number of messages."""
        messages = [
            ChatMessage(role="user", content="Very long message that exceeds token limit")
        ]
        
        with patch.object(self.manager, 'estimate_token_count', return_value=2000):
            result = self.manager._truncate_oldest(messages)
        
        # Should preserve at least 1 message even if it exceeds the limit
        assert len(result) == 1
        assert result[0].content == "Very long message that exceeds token limit"


class TestContextManagerSlidingWindow:
    """Test sliding window functionality."""
    
    def setup_method(self):
        """Set up test fixtures."""
        self.config = ContextConfig(context_window=1000)
        self.manager = ContextManager(self.config)
    
    def test_sliding_window_basic(self):
        """Test basic sliding window functionality."""
        messages = [
            ChatMessage(role="system", content="System message"),
            ChatMessage(role="user", content="Message 1"),
            ChatMessage(role="assistant", content="Response 1"),
            ChatMessage(role="user", content="Message 2"),
            ChatMessage(role="assistant", content="Response 2"),
            ChatMessage(role="user", content="Current message")
        ]
        
        # Mock to simulate 60% target (600 tokens)
        def mock_estimate(msgs):
            if len(msgs) == 1:
                return 100 if msgs[0].role != "system" else 50
            return sum(100 if m.role != "system" else 50 for m in msgs)
        
        with patch.object(self.manager, 'estimate_token_count', side_effect=mock_estimate):
            result = self.manager._apply_sliding_window(messages)
        
        # Should keep system message + recent messages that fit in 60% of context window
        assert len(result) >= 1  # At least system message
        assert result[0].content == "System message"  # System message preserved
        assert result[-1].content == "Current message"  # Most recent preserved


@pytest.mark.asyncio
class TestContextManagerAsync:
    """Test async functionality of ContextManager."""
    
    def setup_method(self):
        """Set up test fixtures."""
        self.config = ContextConfig(
            context_window=1000,
            strategy=ContextStrategy.TRUNCATE_OLDEST,
            summarization_threshold=800
        )
        self.manager = ContextManager(self.config)
    
    async def test_manage_context_below_threshold(self):
        """Test manage_context when below summarization threshold."""
        messages = [ChatMessage(role="user", content="Short message")]
        
        with patch.object(self.manager, 'estimate_token_count', return_value=500):
            with patch('agent_system.mcp.status.status_bus.publish', new_callable=AsyncMock):
                result = await self.manager.manage_context(messages)
        
        # Should return original messages unchanged
        assert result == messages
    
    async def test_manage_context_truncation_strategy(self):
        """Test manage_context with truncation strategy."""
        messages = [
            ChatMessage(role="user", content="Message 1"),
            ChatMessage(role="user", content="Message 2"),
            ChatMessage(role="user", content="Message 3")
        ]
        
        mock_truncate = Mock(return_value=messages[:2])
        
        with patch.object(self.manager, 'estimate_token_count', return_value=900):
            with patch.object(self.manager, '_truncate_oldest', mock_truncate):
                with patch('agent_system.mcp.status.status_bus.publish', new_callable=AsyncMock) as mock_publish:
                    result = await self.manager.manage_context(messages)
        
        # Should call truncation
        mock_truncate.assert_called_once_with(messages)
        assert result == messages[:2]
        
        # Should publish status events via StatusScope
        assert mock_publish.call_count >= 2  # START and END events
    
    async def test_manage_context_error_handling(self):
        """Test manage_context error handling."""
        messages = [ChatMessage(role="user", content="Test message")]
        
        with patch.object(self.manager, 'estimate_token_count', return_value=900):
            with patch.object(self.manager, '_truncate_oldest', side_effect=Exception("Test error")):
                with patch('agent_system.mcp.status.status_bus.publish', new_callable=AsyncMock) as mock_publish:
                    result = await self.manager.manage_context(messages)
        
        # Should return original messages on error
        assert result == messages
        
        # Should publish error status event via StatusScope
        # StatusScope automatically publishes ERROR event on exception
        assert mock_publish.call_count >= 1


class TestContextManagerIntegration:
    """Integration tests for ContextManager."""
    
    def setup_method(self):
        """Set up test fixtures."""
        self.config = ContextConfig(context_window=2000, preserve_recent_messages=5)
        self.manager = ContextManager(self.config)
    
    def test_real_token_estimation(self):
        """Test token estimation with real messages."""
        messages = [
            ChatMessage(role="system", content="You are a helpful assistant."),
            ChatMessage(role="user", content="What is the weather like today?"),
            ChatMessage(role="assistant", content="I don't have access to real-time weather data. To get current weather information, you would need to check a weather service like weather.com, your local weather app, or ask me to help you find weather resources for your specific location."),
            ChatMessage(role="user", content="Can you help me write a Python function?"),
            ChatMessage(role="assistant", content="Of course! I'd be happy to help you write a Python function. Could you tell me what you'd like the function to do? For example, do you want it to calculate something, process data, or perform a specific task?")
        ]
        
        tokens = self.manager.estimate_token_count(messages)
        assert tokens > 0
        assert isinstance(tokens, int)
        
        # Rough validation - should be reasonable for the content length
        total_chars = sum(len(str(msg.content or "")) for msg in messages)
        # Very rough estimate: 1 token per 3-5 characters
        assert tokens >= total_chars // 6  # Conservative lower bound
        assert tokens <= total_chars // 2  # Conservative upper bound