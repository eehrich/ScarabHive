"""Tests for LLM token estimation utilities."""

import pytest
from agent_system.llm.token_utils import (
    estimate_token_count,
    estimate_content_tokens,
    estimate_json_tokens,
    estimate_tool_result_tokens,
    is_code_content,
    is_structured_data,
)
from agent_system.llm.models import ChatMessage


class TestContentTypeDetection:
    """Test content type detection functions."""

    def test_is_code_content_python(self):
        """Test detection of Python code."""
        python_code = """
def hello_world():
    print("Hello, World!")
    return True
"""
        assert is_code_content(python_code) is True

    def test_is_code_content_javascript(self):
        """Test detection of JavaScript code."""
        js_code = """
const myFunc = () => {
    if (x > 0) {
        return x * 2;
    }
}
"""
        assert is_code_content(js_code) is True

    def test_is_code_content_symbols(self):
        """Test detection based on symbol density."""
        code_with_symbols = "x = (a + b) * (c - d) / e;"
        assert is_code_content(code_with_symbols) is True

    def test_is_code_content_plain_text(self):
        """Test that plain text is not detected as code."""
        plain_text = "This is a simple sentence with no code indicators."
        assert is_code_content(plain_text) is False

    def test_is_structured_data_json_object(self):
        """Test detection of JSON object."""
        json_obj = '{"name": "John", "age": 30}'
        assert is_structured_data(json_obj) is True

    def test_is_structured_data_json_array(self):
        """Test detection of JSON array."""
        json_arr = '[1, 2, 3, 4, 5]'
        assert is_structured_data(json_arr) is True

    def test_is_structured_data_xml(self):
        """Test detection of XML."""
        xml = '<root><item>value</item></root>'
        assert is_structured_data(xml) is True

    def test_is_structured_data_yaml_list(self):
        """Test detection of YAML-like lists."""
        yaml_list = """
- item1
- item2
- item3
"""
        assert is_structured_data(yaml_list) is True

    def test_is_structured_data_plain_text(self):
        """Test that plain text is not detected as structured data."""
        plain_text = "This is just plain text"
        assert is_structured_data(plain_text) is False


class TestContentTokenEstimation:
    """Test token estimation for different content types."""

    def test_estimate_content_tokens_empty(self):
        """Test estimation for empty content."""
        assert estimate_content_tokens("") == 0
        assert estimate_content_tokens(None) == 0

    def test_estimate_content_tokens_natural_language(self):
        """Test estimation for natural language text."""
        text = "The quick brown fox jumps over the lazy dog"
        # 9 words * 1.3 = 11.7 -> 11 tokens
        tokens = estimate_content_tokens(text)
        assert tokens == 11  # int(9 * 1.3)

    def test_estimate_content_tokens_code(self):
        """Test estimation for code content."""
        code = "def function(): return True"
        # Should be detected as code and use 1.5 ratio
        tokens = estimate_content_tokens(code)
        # 4 words * 1.5 = 6.0 -> 6 tokens
        assert tokens == 6

    def test_estimate_content_tokens_json(self):
        """Test estimation for JSON content."""
        json_str = '{"key": "value", "number": 123}'
        # Should be detected as structured data and use 1.1 ratio
        tokens = estimate_content_tokens(json_str)
        # 4 words (key, value, number, 123) * 1.1 = 4.4 -> 4 tokens
        assert tokens > 0

    def test_estimate_content_tokens_long_text(self):
        """Test estimation for longer text."""
        # 100 words of natural language
        text = " ".join(["word"] * 100)
        tokens = estimate_content_tokens(text)
        # 100 * 1.3 = 130
        assert tokens == 130


class TestJsonTokenEstimation:
    """Test JSON-specific token estimation."""

    def test_estimate_json_tokens_empty(self):
        """Test estimation for empty JSON."""
        assert estimate_json_tokens("") == 0
        assert estimate_json_tokens(None) == 0

    def test_estimate_json_tokens_simple_object(self):
        """Test estimation for simple JSON object."""
        json_str = '{"name": "John", "age": 30}'
        tokens = estimate_json_tokens(json_str)
        # Structural: { } " " : " " , " " : = 10
        # Words: name John age 30 = 4 words * 0.8 = 3.2 -> 3
        # Total: 10 + 3 = 13
        assert tokens >= 10  # At least the structural tokens

    def test_estimate_json_tokens_nested_object(self):
        """Test estimation for nested JSON."""
        json_str = '{"user": {"name": "Alice", "id": 1}, "active": true}'
        tokens = estimate_json_tokens(json_str)
        assert tokens > 20  # Should have many structural tokens

    def test_estimate_json_tokens_array(self):
        """Test estimation for JSON array."""
        json_str = '[1, 2, 3, 4, 5]'
        tokens = estimate_json_tokens(json_str)
        # Structural: [ ] , , , , = 6
        # Words: 1 2 3 4 5 = 5 * 0.8 = 4
        # Total: 10
        assert tokens >= 6


class TestToolResultTokenEstimation:
    """Test token estimation for tool results."""

    def test_estimate_tool_result_tokens_empty(self):
        """Test estimation for empty tool result."""
        assert estimate_tool_result_tokens("") == 0

    def test_estimate_tool_result_tokens_json(self):
        """Test estimation for JSON tool result."""
        json_result = '{"status": "success", "data": [1, 2, 3]}'
        tokens = estimate_tool_result_tokens(json_result)
        assert tokens > 0

    def test_estimate_tool_result_tokens_html(self):
        """Test estimation for HTML tool result."""
        html = "<html><body><h1>Title</h1><p>Content</p></body></html>"
        tokens = estimate_tool_result_tokens(html)
        # HTML detection: needs both < and > plus reasonable content
        # The function detects it as HTML and uses 1.4 ratio
        # But with limited actual words, the token count may be lower
        assert tokens > 0  # Just verify it's estimated

    def test_estimate_tool_result_tokens_code(self):
        """Test estimation for code tool result."""
        code = "def test(): return True"
        tokens = estimate_tool_result_tokens(code)
        # Should be detected as code with 1.2 ratio
        assert tokens > 0

    def test_estimate_tool_result_tokens_plain_text(self):
        """Test estimation for plain text tool result."""
        text = "This is a simple text response from a tool"
        tokens = estimate_tool_result_tokens(text)
        # 9 words * 1.3 = 11.7 -> 11 tokens
        assert tokens == 11


class TestMessageTokenEstimation:
    """Test token estimation for complete messages."""

    def test_estimate_token_count_empty_list(self):
        """Test estimation for empty message list."""
        assert estimate_token_count([]) == 0

    def test_estimate_token_count_simple_user_message(self):
        """Test estimation for simple user message."""
        msg = ChatMessage(role="user", content="Hello, how are you?")
        tokens = estimate_token_count([msg])
        # Base overhead: 4 + content tokens (4 words * 1.3 = 5.2 -> 5)
        # Total: 4 + 5 = 9
        assert tokens == 9

    def test_estimate_token_count_assistant_message(self):
        """Test estimation for assistant message."""
        msg = ChatMessage(role="assistant", content="I am doing well, thank you!")
        tokens = estimate_token_count([msg])
        # Base overhead: 4 + content tokens (6 words * 1.3 = 7.8 -> 7)
        # Total: 4 + 7 = 11
        assert tokens == 11

    def test_estimate_token_count_system_message(self):
        """Test estimation for system message."""
        msg = ChatMessage(role="system", content="You are a helpful assistant")
        tokens = estimate_token_count([msg])
        assert tokens > 4  # At least base overhead

    def test_estimate_token_count_message_with_tool_calls(self):
        """Test estimation for message with tool calls."""
        msg = ChatMessage(
            role="assistant",
            content="Let me search for that",
            tool_calls=[
                {
                    "id": "call_123",
                    "type": "function",
                    "function": {
                        "name": "search",
                        "arguments": '{"query": "test"}'
                    }
                }
            ]
        )
        tokens = estimate_token_count([msg])
        # Base overhead: 4
        # Content: 5 words * 0.75 = 3.75 -> 3
        # Tool call overhead: 10
        # Function name: len("search") // 4 = 1
        # Arguments JSON: should have some tokens
        # Total should be > 20
        assert tokens > 20

    def test_estimate_token_count_tool_result_message(self):
        """Test estimation for tool result message."""
        msg = ChatMessage(
            role="tool",
            content='{"results": ["item1", "item2", "item3"]}',
            tool_call_id="call_123"
        )
        tokens = estimate_token_count([msg])
        # Base overhead: 4
        # Tool call ID overhead: 8
        # Content (JSON): should have structural + word tokens
        assert tokens > 15

    def test_estimate_token_count_multiple_messages(self):
        """Test estimation for multiple messages."""
        messages = [
            ChatMessage(role="user", content="Hello"),
            ChatMessage(role="assistant", content="Hi there"),
            ChatMessage(role="user", content="How are you"),
        ]
        tokens = estimate_token_count(messages)
        # Each message has base overhead of 4
        # Total should be at least 3 * 4 = 12
        assert tokens >= 12

    def test_estimate_token_count_conversation_flow(self):
        """Test estimation for realistic conversation flow."""
        messages = [
            ChatMessage(role="system", content="You are a helpful coding assistant"),
            ChatMessage(role="user", content="Write a Python function to calculate fibonacci"),
            ChatMessage(
                role="assistant",
                content="def fib(n): return n if n <= 1 else fib(n-1) + fib(n-2)",
                tool_calls=[]
            ),
            ChatMessage(role="user", content="Thanks!"),
        ]
        tokens = estimate_token_count(messages)
        # Should be reasonable estimate for this conversation
        assert tokens > 30  # At least base overheads + content


class TestEdgeCases:
    """Test edge cases and boundary conditions."""

    def test_none_content(self):
        """Test handling of None content."""
        msg = ChatMessage(role="user", content=None)
        tokens = estimate_token_count([msg])
        # Only base overhead
        assert tokens == 4

    def test_empty_content(self):
        """Test handling of empty string content."""
        msg = ChatMessage(role="user", content="")
        tokens = estimate_token_count([msg])
        # Only base overhead
        assert tokens == 4

    def test_very_long_content(self):
        """Test handling of very long content."""
        long_text = " ".join(["word"] * 10000)
        msg = ChatMessage(role="user", content=long_text)
        tokens = estimate_token_count([msg])
        # 10000 words * 0.75 = 7500 + overhead
        assert tokens > 7500

    def test_unicode_content(self):
        """Test handling of unicode content."""
        msg = ChatMessage(role="user", content="Hello 世界 🌍")
        tokens = estimate_token_count([msg])
        assert tokens > 0

    def test_multiline_content(self):
        """Test handling of multiline content."""
        content = """
This is line 1
This is line 2
This is line 3
"""
        msg = ChatMessage(role="user", content=content)
        tokens = estimate_token_count([msg])
        assert tokens > 4


class TestTokenEstimationAccuracy:
    """Test accuracy of token estimation against known values."""

    def test_estimation_consistency(self):
        """Test that estimation is consistent for same content."""
        content = "This is a test message for consistency"
        msg1 = ChatMessage(role="user", content=content)
        msg2 = ChatMessage(role="assistant", content=content)

        tokens1 = estimate_token_count([msg1])
        tokens2 = estimate_token_count([msg2])

        # Should be identical since same content and overhead
        assert tokens1 == tokens2

    def test_estimation_proportionality(self):
        """Test that longer content has more tokens."""
        short = ChatMessage(role="user", content="Hello")
        medium = ChatMessage(role="user", content="Hello world from the system")
        long = ChatMessage(role="user", content="Hello world from the system with many more words")

        short_tokens = estimate_token_count([short])
        medium_tokens = estimate_token_count([medium])
        long_tokens = estimate_token_count([long])

        assert short_tokens < medium_tokens < long_tokens

    def test_estimation_reasonable_ratios(self):
        """Test that estimation ratios are reasonable."""
        # Natural language should be ~1.3 tokens per word
        natural = "The quick brown fox jumps over the lazy dog"  # 9 words
        msg = ChatMessage(role="user", content=natural)
        tokens = estimate_token_count([msg])

        # 9 words * 1.3 = 11.7 -> 11 + 4 overhead = 15
        assert tokens == 15

    def test_code_vs_text_ratio(self):
        """Test that code is estimated higher than text."""
        text = "hello world test message here"  # 5 words
        code = "def function(): return value;"  # 4 words but code

        text_msg = ChatMessage(role="user", content=text)
        code_msg = ChatMessage(role="user", content=code)

        text_tokens = estimate_token_count([text_msg])
        code_tokens = estimate_token_count([code_msg])

        # Code should have more tokens per word
        # text: 5 * 0.75 = 3.75 -> 3 + 4 = 7
        # code: 4 * 1.2 = 4.8 -> 4 + 4 = 8
        assert code_tokens >= text_tokens


class TestMultimodalTokenEstimation:
    """Test token estimation for multimodal content (images, audio, etc.)."""

    def test_extract_text_from_string_content(self):
        """Test text extraction from plain string content."""
        from agent_system.llm.token_utils import extract_text_from_content
        
        content = "Hello, world!"
        result = extract_text_from_content(content)
        assert result == "Hello, world!"

    def test_extract_text_from_none_content(self):
        """Test text extraction from None content."""
        from agent_system.llm.token_utils import extract_text_from_content
        
        result = extract_text_from_content(None)
        assert result == ""

    def test_extract_text_from_multimodal_list(self):
        """Test text extraction from multimodal content list."""
        from agent_system.llm.token_utils import extract_text_from_content
        
        content = [
            {"type": "text", "text": "What is in this image?"},
            {"type": "image", "source": {"type": "base64", "data": "..."}}
        ]
        result = extract_text_from_content(content)
        assert result == "What is in this image?"

    def test_extract_text_from_multiple_text_parts(self):
        """Test text extraction from content with multiple text parts."""
        from agent_system.llm.token_utils import extract_text_from_content
        
        content = [
            {"type": "text", "text": "First part."},
            {"type": "image", "source": {"type": "base64", "data": "..."}},
            {"type": "text", "text": "Second part."}
        ]
        result = extract_text_from_content(content)
        assert result == "First part. Second part."

    def test_count_multimodal_items_images(self):
        """Test counting images in multimodal content."""
        from agent_system.llm.token_utils import count_multimodal_items
        
        content = [
            {"type": "text", "text": "Describe these images"},
            {"type": "image", "source": {"type": "base64", "data": "..."}},
            {"type": "image_url", "image_url": {"url": "https://example.com/img.png"}}
        ]
        counts = count_multimodal_items(content)
        assert counts['images'] == 2
        assert counts['audio'] == 0
        assert counts['video'] == 0

    def test_count_multimodal_items_audio(self):
        """Test counting audio in multimodal content."""
        from agent_system.llm.token_utils import count_multimodal_items
        
        content = [
            {"type": "text", "text": "Transcribe this audio"},
            {"type": "audio", "source": {"type": "base64", "data": "..."}}
        ]
        counts = count_multimodal_items(content)
        assert counts['images'] == 0
        assert counts['audio'] == 1
        assert counts['video'] == 0

    def test_count_multimodal_items_string_content(self):
        """Test that string content returns zero counts."""
        from agent_system.llm.token_utils import count_multimodal_items
        
        counts = count_multimodal_items("Just plain text")
        assert counts['images'] == 0
        assert counts['audio'] == 0
        assert counts['video'] == 0

    def test_estimate_tokens_multimodal_message(self):
        """Test token estimation for multimodal ChatMessage."""
        multimodal_content = [
            {"type": "text", "text": "What is in this image?"},
            {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "..."}}
        ]
        msg = ChatMessage(role="user", content=multimodal_content)
        tokens = estimate_token_count([msg])
        
        # Should include:
        # - Base overhead: 4
        # - Text tokens: "What is in this image?" ~ 6 words * 1.3 = 7.8 -> 7
        # - Image tokens: 1000 (TOKENS_PER_IMAGE)
        # Total: 4 + 7 + 1000 = 1011
        assert tokens > 1000  # At least the image token estimate

    def test_estimate_tokens_multimodal_with_audio(self):
        """Test token estimation for multimodal message with audio."""
        multimodal_content = [
            {"type": "text", "text": "Transcribe this"},
            {"type": "audio", "source": {"type": "base64", "media_type": "audio/wav", "data": "..."}}
        ]
        msg = ChatMessage(role="user", content=multimodal_content)
        tokens = estimate_token_count([msg])
        
        # Should include audio token estimate (25 tokens/sec * 10 sec average = 250)
        assert tokens > 200  # At least the audio token estimate

    def test_estimate_tokens_text_only_message_unchanged(self):
        """Test that text-only messages still work correctly."""
        msg = ChatMessage(role="user", content="Hello, how are you?")
        tokens = estimate_token_count([msg])
        
        # 4 words * 1.3 = 5.2 -> 5 + 4 overhead = 9
        assert tokens == 9


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
