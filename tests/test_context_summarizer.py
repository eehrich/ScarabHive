"""Tests for ConversationSummarizer functionality."""

import pytest
from unittest.mock import Mock, AsyncMock, patch
from agent_system.context.summarizer import ConversationSummarizer
from agent_system.context.config import ContextConfig
from agent_system.llm.clients import ChatMessage


class TestConversationSummarizer:
    """Test the ConversationSummarizer class."""
    
    def setup_method(self):
        """Set up test fixtures."""
        self.mock_llm = Mock()
        self.summarizer = ConversationSummarizer(self.mock_llm)
        self.config = ContextConfig(preserve_recent_messages=3)
    
    @pytest.mark.asyncio
    async def test_summarize_conversation_few_messages(self):
        """Test summarization when there are few messages."""
        messages = [
            ChatMessage(role="user", content="Hello"),
            ChatMessage(role="assistant", content="Hi there!")
        ]
        
        with patch('agent_system.context.summarizer.publish_status', new_callable=AsyncMock):
            result = await self.summarizer.summarize_conversation(messages, self.config)
        
        # Should return messages unchanged when count <= preserve_recent_messages
        assert result == messages
    
    @pytest.mark.asyncio
    async def test_summarize_conversation_with_system_message(self):
        """Test summarization preserves system message."""
        messages = [
            ChatMessage(role="system", content="You are a helpful assistant"),
            ChatMessage(role="user", content="Message 1"),
            ChatMessage(role="assistant", content="Response 1"),
            ChatMessage(role="user", content="Message 2"),
            ChatMessage(role="assistant", content="Response 2"),
            ChatMessage(role="user", content="Message 3"),
            ChatMessage(role="assistant", content="Response 3")
        ]
        
        # Mock LLM response
        mock_response = Mock()
        mock_response.content = "This is a summary of the conversation about various topics."
        self.mock_llm.complete.return_value = mock_response
        
        with patch('agent_system.context.summarizer.publish_status', new_callable=AsyncMock):
            result = await self.summarizer.summarize_conversation(messages, self.config)
        
        # Should have system message + summary + recent messages
        assert len(result) >= 4  # system + summary + 3 recent
        assert result[0].role == "system"
        assert result[0].content == "You are a helpful assistant"
        assert "summary" in result[1].content.lower()
        
        # Last 3 messages should be preserved
        assert result[-3:] == messages[-3:]
    
    @pytest.mark.asyncio
    async def test_summarize_conversation_no_system_message(self):
        """Test summarization without system message."""
        messages = [
            ChatMessage(role="user", content="Message 1"),
            ChatMessage(role="assistant", content="Response 1"),
            ChatMessage(role="user", content="Message 2"),
            ChatMessage(role="assistant", content="Response 2"),
            ChatMessage(role="user", content="Message 3"),
            ChatMessage(role="assistant", content="Response 3")
        ]
        
        # Mock LLM response
        mock_response = Mock()
        mock_response.content = "Conversation summary."
        self.mock_llm.complete.return_value = mock_response
        
        with patch('agent_system.context.summarizer.publish_status', new_callable=AsyncMock):
            result = await self.summarizer.summarize_conversation(messages, self.config)
        
        # Should have summary + recent messages
        assert len(result) == 4  # summary + 3 recent
        assert "summary" in result[0].content.lower()
        assert result[-3:] == messages[-3:]
    
    @pytest.mark.asyncio
    async def test_create_summary_success(self):
        """Test successful LLM summary creation."""
        messages = [
            ChatMessage(role="user", content="What is Python?"),
            ChatMessage(role="assistant", content="Python is a programming language."),
            ChatMessage(role="user", content="How do I use loops?"),
            ChatMessage(role="assistant", content="You can use for and while loops.")
        ]
        
        # Mock successful LLM response
        mock_response = Mock()
        mock_response.content = "User asked about Python programming and loops."
        self.mock_llm.complete.return_value = mock_response
        
        with patch('agent_system.context.summarizer.publish_status', new_callable=AsyncMock):
            config = ContextConfig()  # Use default config
            summary = await self.summarizer._create_summary(messages, config)
        
        assert summary == "User asked about Python programming and loops."
        self.mock_llm.complete.assert_called_once()
    
    @pytest.mark.asyncio
    async def test_create_summary_llm_failure(self):
        """Test fallback when LLM fails."""
        messages = [
            ChatMessage(role="user", content="Test message"),
            ChatMessage(role="assistant", content="Test response")
        ]
        
        # Mock LLM failure
        self.mock_llm.complete.side_effect = Exception("LLM error")
        
        with patch('agent_system.context.summarizer.publish_status', new_callable=AsyncMock):
            with patch.object(self.summarizer, '_create_fallback_summary', 
                            new_callable=AsyncMock, return_value="Fallback summary") as mock_fallback:
                summary = await self.summarizer._create_summary(messages)
        
        assert summary == "Fallback summary"
        mock_fallback.assert_called_once_with(messages)
    
    @pytest.mark.asyncio
    async def test_create_summary_empty_response(self):
        """Test fallback when LLM returns empty response."""
        messages = [
            ChatMessage(role="user", content="Test message")
        ]
        
        # Mock empty LLM response
        mock_response = Mock()
        mock_response.content = None
        self.mock_llm.complete.return_value = mock_response
        
        with patch('agent_system.context.summarizer.publish_status', new_callable=AsyncMock):
            with patch.object(self.summarizer, '_create_fallback_summary',
                            new_callable=AsyncMock, return_value="Fallback summary") as mock_fallback:
                summary = await self.summarizer._create_summary(messages)
        
        assert summary == "Fallback summary"
        mock_fallback.assert_called_once_with(messages)
    
    @pytest.mark.asyncio
    async def test_create_fallback_summary(self):
        """Test fallback summary creation."""
        messages = [
            ChatMessage(role="user", content="Hello world"),
            ChatMessage(role="assistant", content="Hi there! How can I help you?"),
            ChatMessage(role="user", content="What's the weather?"),
            ChatMessage(role="assistant", content="I don't have weather data.")
        ]
        
        summary = await self.summarizer._create_fallback_summary(messages)
        
        assert isinstance(summary, str)
        assert len(summary) > 0
        # Should contain some indication it's a summary
        assert any(word in summary.lower() for word in ["summary", "conversation", "discussed"])
    
    def test_format_messages_for_summary(self):
        """Test message formatting for summarization."""
        messages = [
            ChatMessage(role="user", content="Question 1"),
            ChatMessage(role="assistant", content="Answer 1"),
            ChatMessage(role="user", content="Question 2")
        ]
        
        formatted = self.summarizer._format_messages_for_summary(messages)
        
        assert isinstance(formatted, str)
        assert "Question 1" in formatted
        assert "Answer 1" in formatted
        assert "Question 2" in formatted
        assert "User:" in formatted or "user:" in formatted
        assert "Assistant:" in formatted or "assistant:" in formatted
    
    def test_create_summary_prompt(self):
        """Test summary prompt creation."""
        conversation_text = "User: Hello\nAssistant: Hi there!"
        
        prompt = self.summarizer._create_summary_prompt(conversation_text)
        
        assert isinstance(prompt, str)
        assert len(prompt) > 0
        assert conversation_text in prompt
        # Should contain instructions for summarization
        assert any(word in prompt.lower() for word in ["summarize", "summary", "concise"])


class TestConversationSummarizerStatusEvents:
    """Test status event publishing in ConversationSummarizer."""
    
    def setup_method(self):
        """Set up test fixtures."""
        self.mock_llm = Mock()
        self.summarizer = ConversationSummarizer(self.mock_llm)
        self.config = ContextConfig(preserve_recent_messages=2)
    
    @pytest.mark.asyncio
    async def test_status_events_published(self):
        """Test that status events are published during summarization."""
        messages = [
            ChatMessage(role="user", content="Message 1"),
            ChatMessage(role="assistant", content="Response 1"),
            ChatMessage(role="user", content="Message 2"),
            ChatMessage(role="assistant", content="Response 2"),
            ChatMessage(role="user", content="Message 3")
        ]
        
        # Mock successful LLM response
        mock_response = Mock()
        mock_response.content = "Summary of conversation"
        self.mock_llm.complete.return_value = mock_response
        
        with patch('agent_system.context.summarizer.publish_status', new_callable=AsyncMock) as mock_publish:
            await self.summarizer.summarize_conversation(messages, self.config)
        
        # Should publish start, progress, and end events
        assert mock_publish.call_count >= 3
        
        # Check for start event
        start_calls = [call for call in mock_publish.call_args_list 
                      if len(call[1]) > 0 and call[1].get('phase') == 'start']
        assert len(start_calls) >= 1
        
        # Check for end event
        end_calls = [call for call in mock_publish.call_args_list 
                    if len(call[1]) > 0 and call[1].get('phase') == 'end']
        assert len(end_calls) >= 1
    
    @pytest.mark.asyncio
    async def test_error_status_event_published(self):
        """Test that error status events are published on failures."""
        messages = [
            ChatMessage(role="user", content="Test message"),
            ChatMessage(role="assistant", content="Test response"),
            ChatMessage(role="user", content="Another message")
        ]
        
        # Mock LLM failure
        self.mock_llm.complete.side_effect = Exception("LLM error")
        
        with patch('agent_system.context.summarizer.publish_status', new_callable=AsyncMock) as mock_publish:
            with patch.object(self.summarizer, '_create_fallback_summary',
                            new_callable=AsyncMock, return_value="Fallback"):
                await self.summarizer._create_summary(messages)
        
        # Should publish error event
        error_calls = [call for call in mock_publish.call_args_list 
                      if len(call[1]) > 0 and call[1].get('phase') == 'error']
        assert len(error_calls) >= 1


class TestConversationSummarizerEdgeCases:
    """Test edge cases and error conditions."""
    
    def setup_method(self):
        """Set up test fixtures."""
        # Test with mock LLM client to avoid make_llm() issues
        from unittest.mock import Mock
        mock_llm = Mock()
        self.summarizer = ConversationSummarizer(mock_llm)
        self.config = ContextConfig(preserve_recent_messages=2)
    
    @pytest.mark.asyncio
    async def test_no_llm_client(self):
        """Test behavior when no LLM client is provided."""
        messages = [
            ChatMessage(role="user", content="Message 1"),
            ChatMessage(role="assistant", content="Response 1"),
            ChatMessage(role="user", content="Message 2")
        ]
        
        # Should create fallback summary when no LLM available
        with patch('agent_system.context.summarizer.publish_status', new_callable=AsyncMock):
            summary = await self.summarizer._create_summary(messages)
        
        assert isinstance(summary, str)
        assert len(summary) > 0
    
    @pytest.mark.asyncio
    async def test_empty_messages_list(self):
        """Test behavior with empty messages list."""
        with patch('agent_system.context.summarizer.publish_status', new_callable=AsyncMock):
            result = await self.summarizer.summarize_conversation([], self.config)
        
        assert result == []
    
    @pytest.mark.asyncio
    async def test_messages_with_none_content(self):
        """Test handling of messages with None content."""
        messages = [
            ChatMessage(role="user", content=None),
            ChatMessage(role="assistant", content="Response"),
            ChatMessage(role="user", content="Message")
        ]
        
        with patch('agent_system.context.summarizer.publish_status', new_callable=AsyncMock):
            formatted = self.summarizer._format_messages_for_summary(messages)
        
        assert isinstance(formatted, str)
        # Should handle None content gracefully
        assert "Response" in formatted
        assert "Message" in formatted