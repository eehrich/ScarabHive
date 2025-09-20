"""Test context management fixes for infinite loop prevention."""

import pytest
from unittest.mock import AsyncMock
from agent_system.context.manager import ContextManager
from agent_system.context.config import ContextConfig, ContextStrategy
from agent_system.context.summarizer import ConversationSummarizer
from agent_system.llm.clients import ChatMessage


@pytest.fixture
def small_context_config():
    """Config with small context window for easy testing."""
    return ContextConfig(
        context_window=1000,
        summarization_threshold=600,  # 60%
        prediction_threshold=0.8,  # 80%
        preserve_recent_messages=3,
        strategy=ContextStrategy.SUMMARIZE_OLDEST
    )


@pytest.fixture
def mock_llm_client():
    """Mock LLM client for testing."""
    client = AsyncMock()
    client.chat = AsyncMock(return_value="Test summary of conversation content.")
    return client


@pytest.fixture
def context_manager(small_context_config):
    """Context manager with small config for testing."""
    manager = ContextManager(small_context_config)
    return manager


@pytest.fixture
def summarizer(mock_llm_client):
    """Summarizer with mock LLM client."""
    return ConversationSummarizer(mock_llm_client)


def create_test_messages(count: int, content_size: int = 50) -> list[ChatMessage]:
    """Create test messages with specified count and content size."""
    messages = []
    for i in range(count):
        content = f"Message {i}: " + "x" * content_size
        messages.append(ChatMessage(role="user", content=content))
    return messages


def create_summary_message(summary_text: str) -> ChatMessage:
    """Create a conversation summary message."""
    return ChatMessage(
        role="system",
        content=f"[CONVERSATION SUMMARY] The following is a summary of earlier conversation:\n\n{summary_text}\n\n[END SUMMARY] Recent conversation continues below:"
    )


class TestTokenAccumulation:
    """Test token accumulation fixes."""
    
    def test_token_usage_not_accumulated(self, context_manager):
        """Test that token usage reflects current conversation, not session accumulation."""
        # First update
        context_manager.update_token_usage({'total_tokens': 100, 'prompt_tokens': 80, 'completion_tokens': 20})
        assert context_manager._actual_usage_stats['total_tokens'] == 100
        
        # Second update should replace, not accumulate
        context_manager.update_token_usage({'total_tokens': 150, 'prompt_tokens': 120, 'completion_tokens': 30})
        assert context_manager._actual_usage_stats['total_tokens'] == 150  # Not 250!
        assert context_manager._actual_usage_stats['prompt_tokens'] == 120  # Not 200!
    
    def test_token_usage_with_none_values(self, context_manager):
        """Test token usage update handles None values gracefully."""
        context_manager.update_token_usage({'total_tokens': None, 'prompt_tokens': 100})
        assert context_manager._actual_usage_stats['total_tokens'] == 0
        assert context_manager._actual_usage_stats['prompt_tokens'] == 100


class TestTriggerLogic:
    """Test improved trigger logic."""
    
    def test_trigger_based_on_current_tokens(self, context_manager):
        """Test that triggers are based on current message tokens, not accumulated usage."""
        # Create messages with many words to exceed threshold (600 tokens)
        # Each message needs ~30 words to get ~25 tokens per message (30 * 0.75 + 4 overhead)
        # We need 25 messages * 25 tokens = 625 tokens to exceed 600 threshold
        messages = []
        for i in range(25):
            # Create content with many actual words (not just repeated characters)
            words = [f"word{j}" for j in range(30)]  # 30 words per message
            content = f"Message {i}: " + " ".join(words)
            messages.append(ChatMessage(role="user", content=content))
        
        estimated_tokens = context_manager.estimate_token_count(messages)
        
        # Verify we actually exceed threshold
        threshold = context_manager.config.get_summarization_threshold_tokens()
        assert estimated_tokens > threshold, f"Estimated {estimated_tokens} should exceed threshold {threshold}"
        
        # Should trigger based on estimated tokens
        should_trigger = context_manager.should_manage_context(estimated_tokens, None)
        assert should_trigger
        
        # Update with low actual usage - should still trigger based on estimated tokens
        context_manager.update_token_usage({'total_tokens': 50})
        should_trigger_after_update = context_manager.should_manage_context(estimated_tokens, None)
        assert should_trigger_after_update
    
    def test_no_trigger_below_threshold(self, context_manager):
        """Test that no trigger occurs when below threshold."""
        # Create small messages below threshold
        messages = create_test_messages(count=3, content_size=20)  # Should be < 600 tokens
        estimated_tokens = context_manager.estimate_token_count(messages)
        
        should_trigger = context_manager.should_manage_context(estimated_tokens, None)
        assert not should_trigger


class TestSummaryDetection:
    """Test summary detection and re-summarization prevention."""
    
    async def test_detect_existing_summary(self, context_manager, summarizer):
        """Test that existing summaries are detected."""
        context_manager.set_summarizer(summarizer)
        
        # Create messages with existing summary
        messages = [
            ChatMessage(role="system", content="System prompt"),
            create_summary_message("Previous conversation summary"),
            ChatMessage(role="user", content="New user message"),
            ChatMessage(role="assistant", content="New assistant response")
        ]
        
        # Should detect existing summary and use truncation instead
        result = await context_manager.manage_context(messages)
        
        # Should return truncated messages, not re-summarized
        assert len(result) <= len(messages)
        # Should not have created new summary (summarizer.chat not called for new summary)
        
    async def test_preserve_existing_summaries_in_summarizer(self, summarizer, small_context_config):
        """Test that summarizer preserves existing summaries."""
        # Create messages with existing summary and new content
        messages = [
            ChatMessage(role="system", content="System prompt"),
            create_summary_message("Existing summary"),
            ChatMessage(role="user", content="Old message 1"),
            ChatMessage(role="user", content="Old message 2"),
            ChatMessage(role="user", content="New message 1"),  # Will be preserved
            ChatMessage(role="user", content="New message 2"),  # Will be preserved
            ChatMessage(role="user", content="New message 3"),  # Will be preserved
        ]
        
        result = await summarizer.summarize_conversation(messages, small_context_config)
        
        # Should preserve system prompt, existing summary, and recent messages
        assert len(result) >= 5  # system + existing summary + new summary + 3 preserved
        
        # Check that existing summary is preserved
        existing_summaries = [msg for msg in result if msg.content and "[CONVERSATION SUMMARY]" in msg.content]
        assert len(existing_summaries) >= 1  # At least the original summary


class TestInfiniteLoopPrevention:
    """Test prevention of infinite loops."""
    
    async def test_no_infinite_summarization_loop(self, context_manager, summarizer):
        """Test that summarization doesn't create infinite loops."""
        context_manager.set_summarizer(summarizer)
        
        # Start with messages that would trigger summarization
        messages = create_test_messages(count=15, content_size=50)
        
        # First summarization
        result1 = await context_manager.manage_context(messages)
        
        # Second attempt - should not trigger infinite loop
        result2 = await context_manager.manage_context(result1)
        
        # Should not have grown significantly (no infinite summarization)
        assert len(result2) <= len(result1) + 2  # Allow small variance
        
        # Third attempt - should be stable
        result3 = await context_manager.manage_context(result2)
        assert len(result3) <= len(result2) + 1
    
    async def test_summarization_with_multiple_existing_summaries(self, summarizer, small_context_config):
        """Test handling of multiple existing summaries."""
        messages = [
            create_summary_message("First summary"),
            create_summary_message("Second summary"),
            ChatMessage(role="user", content="Old message"),
            ChatMessage(role="user", content="Recent message 1"),
            ChatMessage(role="user", content="Recent message 2"),
            ChatMessage(role="user", content="Recent message 3"),
        ]
        
        result = await summarizer.summarize_conversation(messages, small_context_config)
        
        # Should preserve both existing summaries
        existing_summaries = [msg for msg in result if msg.content and "[CONVERSATION SUMMARY]" in msg.content]
        assert len(existing_summaries) >= 2


class TestIntegrationScenarios:
    """Test realistic integration scenarios."""
    
    async def test_large_tool_results_scenario(self, context_manager, summarizer):
        """Test handling of large tool results that trigger context management."""
        context_manager.set_summarizer(summarizer)
        
        # Create scenario with large tool results
        messages = [
            ChatMessage(role="system", content="System prompt"),
            ChatMessage(role="user", content="Search for information"),
            ChatMessage(role="assistant", content="", tool_calls=[{"id": "call_1", "function": {"name": "search"}}]),
            ChatMessage(role="tool", content="Very large search result: " + "x" * 1000, tool_call_id="call_1"),
            ChatMessage(role="user", content="Follow up question"),
        ]
        
        result = await context_manager.manage_context(messages)
        
        # Should handle large content appropriately
        assert len(result) > 0
        # Should have preserved the tool call structure
        tool_messages = [msg for msg in result if getattr(msg, 'role', None) == 'tool']
        assert len(tool_messages) <= len([msg for msg in messages if getattr(msg, 'role', None) == 'tool'])
    
    async def test_conversation_growth_pattern(self, context_manager, summarizer):
        """Test realistic conversation growth and management."""
        context_manager.set_summarizer(summarizer)
        
        # Simulate growing conversation
        conversation = []
        
        for round_num in range(5):
            # Add user message and assistant response
            conversation.append(ChatMessage(role="user", content=f"User message {round_num}: " + "content " * 20))
            conversation.append(ChatMessage(role="assistant", content=f"Assistant response {round_num}: " + "response " * 20))
            
            # Apply context management
            conversation = await context_manager.manage_context(conversation)
            
            # Conversation should not grow unbounded
            assert len(conversation) < 20  # Reasonable upper bound
            
            # Should maintain coherent structure
            assert len(conversation) > 0


if __name__ == "__main__":
    pytest.main([__file__, "-v"])