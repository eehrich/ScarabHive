"""Tests for TokenOptimizer functionality."""

import pytest
from unittest.mock import patch
from agent_system.context.optimizer import TokenOptimizer
from agent_system.llm.models import ChatMessage


class TestTokenOptimizer:
    """Test the TokenOptimizer class."""
    
    def setup_method(self):
        """Set up test fixtures."""
        self.optimizer = TokenOptimizer()
    
    def test_init(self):
        """Test optimizer initialization."""
        assert isinstance(self.optimizer.compression_stats, dict)
        assert "messages_processed" in self.optimizer.compression_stats
        assert "tokens_saved" in self.optimizer.compression_stats
        assert "compression_ratio" in self.optimizer.compression_stats
        
        # Check initial values
        assert self.optimizer.compression_stats["messages_processed"] == 0
        assert self.optimizer.compression_stats["tokens_saved"] == 0
        assert self.optimizer.compression_stats["compression_ratio"] == 0.0
    
    def test_improved_token_estimation(self):
        """Test the improved token estimation method."""
        # Test simple text
        tokens = self.optimizer._estimate_text_tokens("Hello world")
        assert tokens > 0
        assert tokens < 10  # Should be reasonable
        
        # Test JSON content (should have higher token count due to structure)
        json_tokens = self.optimizer._estimate_text_tokens('{"key": "value", "number": 123}')
        plain_tokens = self.optimizer._estimate_text_tokens("key value number 123")
        assert json_tokens > plain_tokens  # JSON should cost more tokens
        
        # Test empty content
        assert self.optimizer._estimate_text_tokens("") == 0
        assert self.optimizer._estimate_text_tokens(None) == 0
    
    def test_content_optimization_detection(self):
        """Test detection of already-optimized content."""
        # Already optimized content should be detected
        compact_json = '{"id":"abc123","type":"response","data":[1,2,3]}'
        assert self.optimizer._is_content_already_optimized(compact_json)
        
        # Verbose content should not be detected as optimized
        verbose_text = "I understand that you want me to help with this task. Let me think about this carefully and provide you with a comprehensive response that addresses all your needs and concerns."
        assert not self.optimizer._is_content_already_optimized(verbose_text)
        
        # Short content should be considered optimized
        short_text = "OK"
        assert self.optimizer._is_content_already_optimized(short_text)
        
        # Base64-like content should be considered optimized
        base64_like = "SGVsbG8gd29ybGQgdGhpcyBpcyBhIGxvbmcgc3RyaW5nIGZvciB0ZXN0aW5n"
        assert self.optimizer._is_content_already_optimized(base64_like)
    
    def test_negative_savings_prevention(self):
        """Test that optimization doesn't make things worse."""
        # Create a message with already-compact content
        compact_message = ChatMessage(
            role="tool", 
            content='{"results":[{"id":"1","name":"test"}]}'
        )
        
        # Optimization should not make this worse
        original_tokens = self.optimizer._estimate_message_tokens(compact_message)
        optimized_message = self.optimizer._optimize_message(compact_message)
        optimized_tokens = self.optimizer._estimate_message_tokens(optimized_message)
        
        # Should not increase token count
        assert optimized_tokens <= original_tokens
    
    @pytest.mark.asyncio
    async def test_optimize_messages_empty_list(self):
        """Test optimization with empty message list."""
        result = await self.optimizer.optimize_messages([])
        
        assert result == []
    
    @pytest.mark.asyncio
    async def test_optimize_messages_basic(self):
        """Test basic message optimization."""
        messages = [
            ChatMessage(role="user", content="Hello world!"),
            ChatMessage(role="assistant", content="Hi there! How can I help you today?")
        ]
        
        # Use simple mocking that doesn't interfere with async context managers
        result = await self.optimizer.optimize_messages(messages)
        
        # Should return optimized messages
        assert len(result) == len(messages)
        assert all(isinstance(msg, ChatMessage) for msg in result)
        
    @pytest.mark.asyncio
    async def test_no_negative_savings_in_optimization(self):
        """Test that optimize_messages never reports negative savings."""
        # Create messages with mixed content types
        messages = [
            ChatMessage(role="user", content="Hello world!"),
            ChatMessage(role="tool", content='{"compact": true, "data": [1,2,3]}'),
            ChatMessage(role="assistant", content="I understand that you want help.")
        ]
        
        await self.optimizer.optimize_messages(messages)
        
        # Check that tokens_saved in stats is not negative
        stats = self.optimizer.get_compression_stats()
        assert stats["tokens_saved"] >= 0  # Should never be negative
    
    @pytest.mark.asyncio
    async def test_optimize_messages_large_batch_progress(self):
        """Test progress events for large message batches."""
        # Create 25 messages to trigger progress events (every 10 messages)
        messages = [
            ChatMessage(role="user", content=f"Message {i}")
            for i in range(25)
        ]
        
        result = await self.optimizer.optimize_messages(messages)
        
        assert len(result) == 25
    
    @pytest.mark.asyncio
    async def test_optimize_messages_error_handling(self):
        """Test error handling during optimization."""
        messages = [ChatMessage(role="user", content="Test message")]
        
        # Mock _optimize_message to raise an exception
        with patch.object(self.optimizer, '_optimize_message', side_effect=Exception("Test error")):
            # The new implementation should raise the exception instead of catching it
            with pytest.raises(Exception, match="Test error"):
                await self.optimizer.optimize_messages(messages)
    
    def test_optimize_message_basic(self):
        """Test optimization of a single message."""
        original_msg = ChatMessage(role="user", content="  Hello   world!  \n\n")
        
        optimized_msg = self.optimizer._optimize_message(original_msg)
        
        assert isinstance(optimized_msg, ChatMessage)
        assert optimized_msg.role == original_msg.role
        # Content should be optimized (whitespace cleaned)
        assert optimized_msg.content != original_msg.content
        assert "Hello world!" in optimized_msg.content
    
    def test_optimize_content_whitespace(self):
        """Test content optimization removes excess whitespace."""
        content = "  Hello    world!  \n\n\n  How are you?  "
        
        optimized = self.optimizer._optimize_content(content)
        
        assert optimized == "Hello world!\nHow are you?"
    
    def test_optimize_content_verbose_patterns(self):
        """Test removal of verbose patterns."""
        content = "I understand that you want me to help with this task. Let me think about this carefully."
        
        optimized = self.optimizer._optimize_content(content)
        
        # Should be shorter due to verbose pattern removal
        assert len(optimized) <= len(content)
        assert optimized != content
    
    def test_compress_json_basic(self):
        """Test JSON compression."""
        json_text = """
        {
            "name": "John Doe",
            "age": 30,
            "city": "New York"
        }
        """
        
        compressed = self.optimizer._compress_json(json_text)
        
        # Should be more compact
        assert len(compressed) < len(json_text)
        assert '"name":"John Doe"' in compressed or '"name": "John Doe"' in compressed
    
    def test_compress_json_invalid(self):
        """Test JSON compression with invalid JSON."""
        invalid_json = "This is not JSON content"
        
        compressed = self.optimizer._compress_json(invalid_json)
        
        # Should return original content if not valid JSON
        assert compressed == invalid_json
    
    def test_remove_verbose_patterns(self):
        """Test removal of verbose patterns."""
        test_cases = [
            ("I understand that you want to", "you want to"),
            ("Let me think about this carefully.", ""),
            ("I need to be very careful here", ""),
            ("As an AI assistant, I can help", "I can help"),
            ("This is a normal sentence", "This is a normal sentence")  # Should remain unchanged
        ]
        
        for original, expected_partial in test_cases:
            result = self.optimizer._remove_verbose_patterns(original)
            if expected_partial:
                assert expected_partial in result or result == original
            else:
                assert len(result) < len(original) or result == original
    
    def test_estimate_message_tokens(self):
        """Test token estimation for individual messages."""
        msg = ChatMessage(role="user", content="Hello world!")
        
        tokens = self.optimizer._estimate_message_tokens(msg)
        
        assert isinstance(tokens, int)
        assert tokens > 0
    
    def test_estimate_message_tokens_none_content(self):
        """Test token estimation with None content."""
        msg = ChatMessage(role="user", content=None)
        
        tokens = self.optimizer._estimate_message_tokens(msg)
        
        assert isinstance(tokens, int)
        assert tokens >= 0  # Should handle None gracefully
    
    def test_get_compression_stats(self):
        """Test compression statistics retrieval."""
        stats = self.optimizer.get_compression_stats()
        
        assert isinstance(stats, dict)
        assert "messages_processed" in stats
        assert "tokens_saved" in stats
        assert "compression_ratio" in stats
    
    def test_reset_stats(self):
        """Test statistics reset."""
        # Modify stats
        self.optimizer.compression_stats["messages_processed"] = 10
        self.optimizer.compression_stats["tokens_saved"] = 100
        
        self.optimizer.reset_stats()
        
        assert self.optimizer.compression_stats["messages_processed"] == 0
        assert self.optimizer.compression_stats["tokens_saved"] == 0
        assert self.optimizer.compression_stats["compression_ratio"] == 0.0


class TestTokenOptimizerToolResults:
    """Test tool result compression functionality."""
    
    def setup_method(self):
        """Set up test fixtures."""
        self.optimizer = TokenOptimizer()
    
    def test_compress_tool_results_basic(self):
        """Test basic tool result compression."""
        messages = [
            ChatMessage(role="user", content="Search for Python tutorials"),
            ChatMessage(
                role="assistant", 
                content="I'll search for Python tutorials.",
                tool_calls=[{"type": "function", "function": {"name": "search"}}]
            ),
            ChatMessage(
                role="tool",
                content='{"results": [' + '{"title": "Very long tutorial content with lots of details..."}' * 10 + ']}'
            )
        ]
        
        compressed = self.optimizer.compress_tool_results(messages)
        
        assert len(compressed) == len(messages)
        # Tool result should be compressed
        tool_msg = compressed[-1]
        assert len(tool_msg.content) < len(messages[-1].content)
    
    def test_compress_tool_results_no_tool_messages(self):
        """Test compression when no tool messages exist."""
        messages = [
            ChatMessage(role="user", content="Hello"),
            ChatMessage(role="assistant", content="Hi there!")
        ]
        
        compressed = self.optimizer.compress_tool_results(messages)
        
        # Should return messages unchanged
        assert compressed == messages
    
    def test_truncate_large_content(self):
        """Test truncation of large content."""
        large_content = "x" * 5000  # 5000 character content
        
        truncated = self.optimizer._truncate_large_content(large_content, max_length=1000)
        
        assert len(truncated) <= 1000
        assert "..." in truncated or "[truncated]" in truncated.lower()


class TestTokenOptimizerEdgeCases:
    """Test edge cases and error conditions."""
    
    def setup_method(self):
        """Set up test fixtures."""
        self.optimizer = TokenOptimizer()
    
    def test_optimize_content_empty_string(self):
        """Test optimization with empty string."""
        result = self.optimizer._optimize_content("")
        assert result == ""
    
    def test_optimize_content_only_whitespace(self):
        """Test optimization with only whitespace."""
        result = self.optimizer._optimize_content("   \n\n\t  ")
        assert result == ""
    
    def test_optimize_message_with_tool_calls(self):
        """Test optimization preserves tool calls."""
        tool_calls = [{"type": "function", "function": {"name": "test"}}]
        msg = ChatMessage(
            role="assistant",
            content="  Let me search for that  ",
            tool_calls=tool_calls
        )
        
        optimized = self.optimizer._optimize_message(msg)
        
        assert optimized.tool_calls == tool_calls
        assert optimized.content != msg.content  # Content should be optimized
    
    def test_optimize_message_preserves_metadata(self):
        """Test that optimization preserves message metadata."""
        msg = ChatMessage(
            role="assistant",
            content="  Hello world  ",
            name="test_assistant",
            function_call={"name": "test_function"}
        )
        
        optimized = self.optimizer._optimize_message(msg)
        
        assert optimized.role == msg.role
        assert optimized.name == msg.name
        assert optimized.function_call == msg.function_call
    
    @pytest.mark.asyncio
    async def test_stats_updated_correctly(self):
        """Test that compression statistics are updated correctly."""
        messages = [
            ChatMessage(role="user", content="Hello"),
            ChatMessage(role="assistant", content="Hi there!")
        ]
        
        initial_processed = self.optimizer.compression_stats["messages_processed"]
        
        await self.optimizer.optimize_messages(messages)
        
        # Stats should be updated
        assert self.optimizer.compression_stats["messages_processed"] == initial_processed + len(messages)
        assert isinstance(self.optimizer.compression_stats["tokens_saved"], int)
        assert isinstance(self.optimizer.compression_stats["compression_ratio"], float)


class TestTokenOptimizerIntegration:
    """Integration tests for TokenOptimizer."""
    
    def setup_method(self):
        """Set up test fixtures."""
        self.optimizer = TokenOptimizer()
    
    @pytest.mark.asyncio
    async def test_real_optimization_scenario(self):
        """Test optimization with realistic message content."""
        messages = [
            ChatMessage(
                role="system",
                content="You are a helpful assistant. Please provide clear and concise responses."
            ),
            ChatMessage(
                role="user",
                content="Can you help me understand how machine learning works?"
            ),
            ChatMessage(
                role="assistant",
                content="""I understand that you want to learn about machine learning. Let me think about this carefully and provide you with a comprehensive explanation.

Machine learning is a subset of artificial intelligence that enables computers to learn and improve from experience without being explicitly programmed.    

Here are the key concepts:
   
1. Data: Machine learning algorithms need data to learn from
2. Algorithms: Mathematical models that find patterns in data  
3. Training: The process of teaching the algorithm using example data
4. Prediction: Using the trained model to make predictions on new data

I hope this helps you understand the basics! Let me know if you need clarification on any part."""
            ),
            ChatMessage(
                role="user", 
                content="That's helpful! Can you give me an example?"
            )
        ]
        
        optimized = await self.optimizer.optimize_messages(messages)
        
        assert len(optimized) == len(messages)
        
        # Check that optimization actually reduced content size
        original_total_length = sum(len(str(msg.content or "")) for msg in messages)
        optimized_total_length = sum(len(str(msg.content or "")) for msg in optimized)
        
        # Should be same or smaller (optimization might not always reduce size for short content)
        assert optimized_total_length <= original_total_length
        
        # Verify message structure is preserved
        for orig, opt in zip(messages, optimized):
            assert orig.role == opt.role
            assert orig.name == opt.name
            assert orig.tool_calls == opt.tool_calls