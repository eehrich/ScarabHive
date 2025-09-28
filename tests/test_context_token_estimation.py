"""Tests for improved token estimation logic in ContextManager."""

import pytest
from src.agent_system.context.manager import ContextManager
from src.agent_system.context.config import ContextConfig
from src.agent_system.llm.clients import ChatMessage


class TestContextTokenEstimation:
    """Test the enhanced token estimation methods."""

    @pytest.fixture
    def context_manager(self):
        """Create a ContextManager for testing."""
        config = ContextConfig(context_window=4096)
        return ContextManager(config)

    def test_estimate_content_tokens_natural_language(self, context_manager):
        """Test token estimation for natural language content."""
        # Natural language should use ~0.75 tokens per word
        content = "Hello, how are you doing today? I hope everything is going well."
        tokens = context_manager._estimate_content_tokens(content)
        word_count = len(content.split())  # 12 words
        expected = int(word_count * 0.75)  # ~9 tokens
        assert abs(tokens - expected) <= 2  # Allow small variance

    def test_estimate_content_tokens_code(self, context_manager):
        """Test token estimation for code content."""
        # Code should use ~1.2 tokens per word (more dense due to symbols)
        content = """
def hello_world():
    print("Hello, world!")
    return True
"""
        tokens = context_manager._estimate_content_tokens(content)
        word_count = len(content.split())  # 6 words
        expected = int(word_count * 1.2)  # ~7 tokens
        assert abs(tokens - expected) <= 3

    def test_estimate_content_tokens_json(self, context_manager):
        """Test token estimation for JSON/structured content."""
        content = '{"name": "test", "values": [1, 2, 3], "active": true}'
        tokens = context_manager._estimate_content_tokens(content)
        word_count = len(content.split())  # 7 words (structural chars removed in estimation)
        expected = int(word_count * 1.1)  # ~8 tokens for JSON
        assert abs(tokens - expected) <= 2

    def test_estimate_json_tokens(self, context_manager):
        """Test JSON-specific token estimation."""
        json_content = '{"user": "john", "data": {"items": [1, 2, 3]}, "active": true}'
        tokens = context_manager._estimate_json_tokens(json_content)

        # Should account for structural characters
        structural_chars = json_content.count('{') + json_content.count('}') + \
                          json_content.count('[') + json_content.count(']') + \
                          json_content.count('"') + json_content.count(':') + \
                          json_content.count(',')

        # Should be more than just length/4 due to structure
        assert tokens > len(json_content) // 4
        assert tokens >= structural_chars  # At least one token per structural char

    def test_estimate_tool_result_tokens_json(self, context_manager):
        """Test tool result estimation for JSON responses."""
        json_response = '{"status": "success", "results": [{"id": 1, "name": "test"}]}'
        tokens = context_manager._estimate_tool_result_tokens(json_response)

        # Should detect as JSON and use JSON estimation
        json_tokens = context_manager._estimate_json_tokens(json_response)
        assert tokens == json_tokens

    def test_estimate_tool_result_tokens_html(self, context_manager):
        """Test tool result estimation for HTML content."""
        html_content = '<div class="test"><p>Hello world</p><span>More text</span></div>'
        tokens = context_manager._estimate_tool_result_tokens(html_content)

        # Should use HTML ratio (1.4 tokens per word)
        word_count = len(html_content.split())  # 4 words
        expected = int(word_count * 1.4)  # ~5 tokens
        assert abs(tokens - expected) <= 2

    def test_estimate_tool_result_tokens_plain_text(self, context_manager):
        """Test tool result estimation for plain text."""
        text_content = "This is a simple plain text response from a tool."
        tokens = context_manager._estimate_tool_result_tokens(text_content)

        # Should use natural language ratio (0.75 tokens per word)
        word_count = len(text_content.split())  # 10 words
        expected = int(word_count * 0.75)  # ~7 tokens
        assert abs(tokens - expected) <= 1

    def test_is_code_content_detection(self, context_manager):
        """Test code content detection."""
        # Should detect code
        code_samples = [
            "def function_name():",
            "if (condition) { return true; }",
            "const variable = () => {}",
            "class MyClass { constructor() {} }"
        ]

        for code in code_samples:
            assert context_manager._is_code_content(code), f"Should detect as code: {code}"

        # Should not detect as code
        text_samples = [
            "Hello, how are you?",
            "This is just normal text.",
            "No code here, just words."
        ]

        for text in text_samples:
            assert not context_manager._is_code_content(text), f"Should not detect as code: {text}"

    def test_is_structured_data_detection(self, context_manager):
        """Test structured data detection."""
        # Should detect structured data
        structured_samples = [
            '{"key": "value"}',
            '[1, 2, 3]',
            '<xml>content</xml>',
            '- item1\n- item2'
        ]

        for data in structured_samples:
            assert context_manager._is_structured_data(data), f"Should detect as structured: {data}"

        # Should not detect as structured
        text_samples = [
            "Plain text",
            "Some {words} in brackets",
            "Text with <emphasis> but not XML"
        ]

        for text in text_samples:
            assert not context_manager._is_structured_data(text), f"Should not detect as structured: {text}"

    def test_full_message_estimation(self, context_manager):
        """Test full message token estimation with various message types."""
        messages = [
            ChatMessage(role="user", content="Hello, can you help me with a task?"),
            ChatMessage(
                role="assistant",
                content="I'll help you with that.",
                tool_calls=[{
                    "id": "call_123",
                    "function": {
                        "name": "search_web",
                        "arguments": '{"query": "python programming", "limit": 5}'
                    },
                    "type": "function"
                }]
            ),
            ChatMessage(
                role="tool",
                content='{"results": [{"title": "Python Tutorial", "url": "example.com"}]}',
                tool_call_id="call_123"
            )
        ]

        total_tokens = context_manager.estimate_token_count(messages)

        # Should be reasonable (not too high or too low)
        assert total_tokens > 20, "Should estimate significant tokens for this conversation"
        assert total_tokens < 200, "Should not over-estimate for this small conversation"

        # Each message should contribute some tokens
        assert total_tokens > len(messages) * 4, "Each message should contribute at least base overhead"

    def test_empty_content_handling(self, context_manager):
        """Test handling of empty or None content."""
        assert context_manager._estimate_content_tokens("") == 0
        assert context_manager._estimate_content_tokens(None) == 0
        assert context_manager._estimate_json_tokens("") == 0
        assert context_manager._estimate_tool_result_tokens("") == 0

    def test_comparison_with_old_method(self, context_manager):
        """Test that new method gives different (hopefully better) estimates than old simple method."""
        messages = [
            ChatMessage(role="user", content="Write a Python function to calculate factorial."),
            ChatMessage(
                role="assistant",
                content="Here's a Python function:\n\ndef factorial(n):\n    if n <= 1:\n        return 1\n    return n * factorial(n-1)"
            )
        ]

        new_estimate = context_manager.estimate_token_count(messages)

        # Old method simulation (simple char count / 3.5)
        total_chars = sum(len(str(msg.content or "")) + 100 for msg in messages)
        old_estimate = int(total_chars / 3.5)

        # New method should be different (and hopefully more accurate)
        assert new_estimate != old_estimate, "New estimation should differ from old simple method"

        # Both should be in reasonable range for this content
        assert 20 < new_estimate < 150
        assert 20 < old_estimate < 150