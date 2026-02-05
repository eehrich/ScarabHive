"""Tests for SessionService including token estimation."""

import pytest
from agent_system.services.session_service import _estimate_message_tokens


class TestEstimateMessageTokens:
    """Tests for the _estimate_message_tokens helper function."""

    def test_simple_text_content(self):
        """Test token estimation for simple text content."""
        msg = {"role": "user", "content": "Hello, how are you?"}
        tokens = _estimate_message_tokens(msg)
        # ~20 chars / 4 = ~5 tokens
        assert 3 <= tokens <= 10

    def test_empty_content(self):
        """Test token estimation for empty content."""
        msg = {"role": "assistant", "content": ""}
        tokens = _estimate_message_tokens(msg)
        # Empty but at least 1 token
        assert tokens >= 1

    def test_none_content(self):
        """Test token estimation for None content."""
        msg = {"role": "assistant", "content": None}
        tokens = _estimate_message_tokens(msg)
        assert tokens >= 1

    def test_long_text_content(self):
        """Test token estimation for longer text."""
        # 400 chars should be ~100 tokens
        long_text = "a" * 400
        msg = {"role": "user", "content": long_text}
        tokens = _estimate_message_tokens(msg)
        assert 90 <= tokens <= 110

    def test_multimodal_content_with_text(self):
        """Test token estimation for multimodal content with text."""
        msg = {
            "role": "user",
            "content": [
                {"type": "text", "text": "What is in this image?"}
            ]
        }
        tokens = _estimate_message_tokens(msg)
        # ~25 chars / 4 = ~6 tokens
        assert 4 <= tokens <= 12

    def test_multimodal_content_with_image(self):
        """Test token estimation for multimodal content with image."""
        msg = {
            "role": "user",
            "content": [
                {"type": "text", "text": "Describe this"},
                {"type": "image_url", "image_url": {"url": "data:image/png;base64,..."}}
            ]
        }
        tokens = _estimate_message_tokens(msg)
        # ~15 chars text + 1000 for image = ~1003
        assert tokens >= 1000

    def test_multimodal_multiple_images(self):
        """Test token estimation for multiple images."""
        msg = {
            "role": "user",
            "content": [
                {"type": "image", "data": "..."},
                {"type": "image_url", "image_url": {"url": "..."}},
            ]
        }
        tokens = _estimate_message_tokens(msg)
        # 2 images = 2000 tokens
        assert tokens >= 2000

    def test_tool_calls_in_assistant_message(self):
        """Test token estimation includes tool calls."""
        msg = {
            "role": "assistant",
            "content": "Let me search for that.",
            "tool_calls": [
                {
                    "id": "call_abc123",
                    "type": "function",
                    "function": {
                        "name": "web_search",
                        "arguments": '{"query": "weather in Berlin"}'
                    }
                }
            ]
        }
        tokens = _estimate_message_tokens(msg)
        # Should include both content and tool calls
        # Content: ~25 chars = ~6 tokens
        # Tool call JSON: ~100 chars = ~25 tokens
        assert tokens >= 30

    def test_multiple_tool_calls(self):
        """Test token estimation with multiple tool calls."""
        msg = {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "call_1",
                    "type": "function",
                    "function": {"name": "tool1", "arguments": '{"a": 1}'}
                },
                {
                    "id": "call_2",
                    "type": "function",
                    "function": {"name": "tool2", "arguments": '{"b": 2}'}
                }
            ]
        }
        tokens = _estimate_message_tokens(msg)
        # Two tool calls should add significant tokens
        assert tokens >= 40

    def test_reasoning_content(self):
        """Test token estimation includes reasoning content."""
        msg = {
            "role": "assistant",
            "content": "The answer is 42.",
            "reasoning_content": "Let me think about this step by step. First I need to consider..."
        }
        tokens = _estimate_message_tokens(msg)
        # Content (~17 chars = ~4 tokens) + reasoning (~65 chars = ~16 tokens)
        assert tokens >= 15

    def test_tool_result_message(self):
        """Test token estimation for tool result messages."""
        msg = {
            "role": "tool",
            "content": '{"result": "The weather in Berlin is sunny, 22°C"}',
            "tool_call_id": "call_abc123"
        }
        tokens = _estimate_message_tokens(msg)
        assert tokens >= 10

    def test_system_message(self):
        """Test token estimation for system messages."""
        system_prompt = "You are a helpful assistant. " * 50  # ~1500 chars
        msg = {"role": "system", "content": system_prompt}
        tokens = _estimate_message_tokens(msg)
        # ~1500 chars / 4 = ~375 tokens
        assert 350 <= tokens <= 400

    def test_content_as_list_with_strings(self):
        """Test multimodal content that's a list of strings (edge case)."""
        msg = {
            "role": "user",
            "content": ["Hello", "World", "How are you?"]
        }
        tokens = _estimate_message_tokens(msg)
        assert tokens >= 5

    def test_minimum_token_count(self):
        """Test that minimum token count is 1."""
        msg = {"role": "user", "content": ""}
        tokens = _estimate_message_tokens(msg)
        assert tokens >= 1

    def test_missing_content_key(self):
        """Test message without content key."""
        msg = {"role": "assistant", "tool_calls": []}
        tokens = _estimate_message_tokens(msg)
        assert tokens >= 1
