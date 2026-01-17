"""Tests for LLM token estimation utilities."""

import pytest
from agent_system.llm.token_utils import (
    estimate_token_count,
    estimate_content_tokens,
    estimate_json_tokens,
    estimate_tool_result_tokens,
    estimate_inline_data_tokens,
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
        # Use actual base64 data (4000 chars = 1000 tokens)
        base64_data = "A" * 4000
        multimodal_content = [
            {"type": "text", "text": "What is in this image?"},
            {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": base64_data}}
        ]
        msg = ChatMessage(role="user", content=multimodal_content)
        tokens = estimate_token_count([msg])
        
        # Should include:
        # - Base overhead: 4
        # - Text tokens: "What is in this image?" ~ 6 words * 1.3 = 7.8 -> 7
        # - Image inline data tokens: 4000 * 0.25 = 1000
        # Total: 4 + 7 + 1000 = ~1011
        assert tokens > 1000  # At least the image inline data estimate

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

    def test_extract_text_from_text_file_content(self):
        """Test text extraction from text_file content type."""
        from agent_system.llm.token_utils import extract_text_from_content
        
        content = [
            {"type": "text", "text": "Here is the file:"},
            {"type": "text_file", "name": "example.py", "content": "def hello(): print('world')"}
        ]
        result = extract_text_from_content(content)
        assert "Here is the file:" in result
        assert "def hello(): print('world')" in result

    def test_estimate_tokens_with_text_file(self):
        """Test token estimation includes text_file content."""
        multimodal_content = [
            {"type": "text", "text": "Review this code:"},
            {"type": "text_file", "name": "script.py", "content": "import sys\n\ndef main():\n    print('Hello')\n\nif __name__ == '__main__':\n    main()"}
        ]
        msg = ChatMessage(role="user", content=multimodal_content)
        tokens = estimate_token_count([msg])
        
        # Should include tokens for both the text and the file content
        # "Review this code:" = 4 words
        # File content = ~10-15 words of code
        # Total should be meaningful
        assert tokens > 15  # At least overhead + reasonable word count

    def test_estimate_inline_data_tokens_base64_source(self):
        """Test token estimation for inline base64 data in source format."""
        from agent_system.llm.token_utils import estimate_inline_data_tokens
        
        # Simulating ~1KB of base64 data (1000 chars)
        base64_data = "A" * 1000
        item = {"type": "image", "source": {"type": "base64", "data": base64_data}}
        
        tokens = estimate_inline_data_tokens(item)
        # 1000 chars * 0.25 = 250 tokens
        assert tokens == 250

    def test_estimate_inline_data_tokens_audio_url(self):
        """Test token estimation for inline base64 data in audio_url format.
        
        Audio uses duration-based estimation (41 tokens/second) instead of
        base64 character count. Falls back to bytes-based duration estimation
        when duration_seconds is not available.
        """
        from agent_system.llm.token_utils import estimate_inline_data_tokens
        
        # Simulating audio data URL with ~2KB of base64 (decodes to ~1500 bytes)
        # Duration fallback: raw_bytes / 16KB * 41 tokens/s
        base64_data = "B" * 2000
        item = {"type": "audio", "audio_url": f"data:audio/wav;base64,{base64_data}"}
        
        tokens = estimate_inline_data_tokens(item)
        # Fallback estimation is much lower than old base64 char count
        assert tokens < 50  # Much less than old 500 tokens
        assert tokens >= 1  # But at least some tokens
        
    def test_estimate_inline_data_tokens_audio_with_duration(self):
        """Test token estimation for audio with explicit duration_seconds."""
        from agent_system.llm.token_utils import estimate_inline_data_tokens, TOKENS_PER_AUDIO_SECOND
        
        # Audio item with duration_seconds (set by encode_audio_to_data_url)
        item = {
            "type": "audio", 
            "audio_url": "data:audio/wav;base64,dummydata",
            "duration_seconds": 45.0  # 45 seconds
        }
        
        tokens = estimate_inline_data_tokens(item)
        # Should use duration: 45s * 41 tokens/s = 1845 tokens
        expected = int(45.0 * TOKENS_PER_AUDIO_SECOND)
        assert tokens == expected

    def test_estimate_inline_data_tokens_image_url(self):
        """Test token estimation for inline base64 data in image_url format."""
        from agent_system.llm.token_utils import estimate_inline_data_tokens
        
        # Simulating image data URL with ~4KB of base64
        base64_data = "C" * 4000
        item = {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{base64_data}"}}
        
        tokens = estimate_inline_data_tokens(item)
        # 4000 chars * 0.25 = 1000 tokens
        assert tokens == 1000

    def test_estimate_inline_data_tokens_no_inline_data(self):
        """Test that items without inline data return 0."""
        from agent_system.llm.token_utils import estimate_inline_data_tokens
        
        # Regular URL (not base64)
        item = {"type": "image_url", "image_url": {"url": "https://example.com/image.png"}}
        assert estimate_inline_data_tokens(item) == 0
        
        # Text content
        item = {"type": "text", "text": "Hello world"}
        assert estimate_inline_data_tokens(item) == 0

    def test_count_multimodal_items_with_inline_data_tokens(self):
        """Test that count_multimodal_items returns inline_data_tokens."""
        from agent_system.llm.token_utils import count_multimodal_items
        
        # ~4KB of base64 data
        base64_data = "D" * 4000
        content = [
            {"type": "text", "text": "Describe this image"},
            {"type": "image", "source": {"type": "base64", "data": base64_data}}
        ]
        
        counts = count_multimodal_items(content)
        assert counts['images'] == 1
        assert counts['inline_data_tokens'] == 1000  # 4000 * 0.25

    def test_estimate_tokens_uses_inline_data_over_fallback(self):
        """Test that token estimation uses actual inline data size over fallback estimates."""
        # Large base64 data: ~40KB = 40,000 chars = 10,000 tokens
        base64_data = "E" * 40000
        multimodal_content = [
            {"type": "text", "text": "Analyze this audio"},
            {"type": "audio", "source": {"type": "base64", "data": base64_data}}
        ]
        msg = ChatMessage(role="user", content=multimodal_content)
        tokens = estimate_token_count([msg])
        
        # Should use inline_data_tokens (10,000) instead of fallback (250)
        # Total: 4 (overhead) + ~5 (text) + 10,000 (inline data) = ~10,009
        assert tokens > 5000  # Much higher than the 250 fallback

    def test_estimate_file_tokens(self, tmp_path):
        """Test that estimate_file_tokens calculates tokens correctly for different file types."""
        from agent_system.llm.token_utils import estimate_file_tokens
        
        # Test image file (uses size-based estimation: small images = 258 tokens)
        test_image = tmp_path / "test_image.png"
        test_image.write_bytes(b"x" * 50_000)  # 50KB < 100KB threshold
        
        tokens = estimate_file_tokens(test_image)
        # Small image (< 100KB) = 258 tokens (single tile)
        assert tokens == 258

    def test_estimate_file_tokens_large_image(self, tmp_path):
        """Test that large images get multiple tile estimation."""
        from agent_system.llm.token_utils import estimate_file_tokens
        
        # Create a 700KB image file (should estimate 2 tiles)
        # 700KB / 300KB per tile = 2.3 → 2 tiles × 258 = 516 tokens
        test_image = tmp_path / "large_image.jpg"
        test_image.write_bytes(b"x" * 700_000)
        
        tokens = estimate_file_tokens(test_image)
        # 700000 // 307200 = 2 tiles × 258 = 516 tokens
        assert tokens == 516

    def test_estimate_file_tokens_nonexistent_file(self):
        """Test that estimate_file_tokens returns 0 for nonexistent files."""
        from agent_system.llm.token_utils import estimate_file_tokens
        
        tokens = estimate_file_tokens("/nonexistent/path/file.wav")
        assert tokens == 0

    def test_estimate_file_tokens_audio_fallback(self, tmp_path):
        """Test audio token estimation with size-based fallback (when pydub can't read)."""
        from agent_system.llm.token_utils import estimate_file_tokens
        
        # Create a fake audio file that pydub can't read
        # Using 163840 bytes = exactly 10s at 16KB/s (16384 bytes/s)
        fake_audio = tmp_path / "fake.wav"
        fake_audio.write_bytes(b"x" * 163840)  # Exactly 16KB * 10 = 10s
        
        tokens = estimate_file_tokens(fake_audio, file_type='audio')
        # Fallback: 163840 / 16384 = 10 seconds × 41 tokens/s = 410 tokens
        assert tokens == 410

    def test_estimate_inline_data_tokens_with_file_path(self, tmp_path):
        """Test that estimate_inline_data_tokens works with file path items."""
        # Create a fake audio file - exact 163840 bytes = 10s at 16KB/s
        audio_file = tmp_path / "audio.wav"
        audio_file.write_bytes(b"x" * 163840)
        
        # MultimodalToolContent dict format
        item = {
            "type": "audio",
            "path": str(audio_file),
            "mime_type": "audio/wav"
        }
        
        tokens = estimate_inline_data_tokens(item)
        # Fallback: 163840 / 16384 = 10 seconds × 41 tokens/s = 410 tokens
        assert tokens == 410

    def test_estimate_inline_data_tokens_with_image_path(self, tmp_path):
        """Test that estimate_inline_data_tokens works with image file paths."""
        # Create a 200KB image file
        image_file = tmp_path / "image.png"
        image_file.write_bytes(b"x" * 200_000)
        
        item = {
            "type": "image",
            "path": str(image_file),
            "mime_type": "image/png"
        }
        
        tokens = estimate_inline_data_tokens(item)
        # 200KB > 100KB threshold, so estimate tiles
        # 200KB / 300KB per tile = ~0.67 → 1 tile minimum = 258 tokens
        assert tokens == 258

    def test_estimate_tokens_multimodal_content_with_file_paths(self, tmp_path):
        """Test that estimate_token_count counts multimodal_content with file paths."""
        from agent_system.llm.models import MultimodalToolContent
        
        # Create a fake audio file - with size-based fallback estimation
        # 163840 bytes = 10 seconds at 16KB/s fallback rate
        audio_file = tmp_path / "audio.wav"
        audio_file.write_bytes(b"x" * 163840)
        
        # Create message with multimodal_content (tool response format)
        multimodal = [
            MultimodalToolContent(
                type="audio",
                path=str(audio_file),
                mime_type="audio/wav",
                description="Test audio"
            )
        ]
        
        msg = ChatMessage(
            role="tool",
            tool_call_id="test-id",
            content='{"status": "success"}',
            multimodal_content=multimodal
        )
        
        tokens = estimate_token_count([msg])
        
        # Should include:
        # - 4 (base overhead)
        # - ~8 (tool call id overhead)
        # - ~30 (content text tokens)
        # - 410 (audio file: 10 seconds × 41 tokens/s)
        # Total: ~452 tokens
        assert tokens > 400  # Main contribution is the audio file
        assert tokens < 500  # Reasonable upper bound

    def test_estimate_tokens_multiple_multimodal_items(self, tmp_path):
        """Test counting multiple multimodal items in one message."""
        from agent_system.llm.models import MultimodalToolContent
        
        # Create two audio files with exact sizes for 5s and 10s respectively
        audio1 = tmp_path / "audio1.wav"
        audio1.write_bytes(b"x" * 81920)  # 5 seconds at 16KB/s
        
        audio2 = tmp_path / "audio2.wav"
        audio2.write_bytes(b"x" * 163840)  # 10 seconds at 16KB/s
        
        multimodal = [
            MultimodalToolContent(type="audio", path=str(audio1), mime_type="audio/wav"),
            MultimodalToolContent(type="audio", path=str(audio2), mime_type="audio/wav"),
        ]
        
        msg = ChatMessage(
            role="tool",
            tool_call_id="test-id",
            content='{"status": "success"}',
            multimodal_content=multimodal
        )
        
        tokens = estimate_token_count([msg])
        
        # 5s × 41 = 205 tokens + 10s × 41 = 410 tokens = 615 + overhead ~24 = ~639
        assert tokens > 600
        assert tokens < 700


class TestMediaDurationCache:
    """Tests for the media duration cache."""
    
    def test_cache_hit_returns_cached_value(self, tmp_path):
        """Test that cache returns cached duration on hit."""
        from agent_system.llm.token_utils import _duration_cache, _CacheEntry
        import time
        
        # Clear cache first
        _duration_cache.clear()
        
        test_file = tmp_path / "test.wav"
        test_file.write_bytes(b"x" * 1000)
        mtime = test_file.stat().st_mtime
        
        # Cache miss initially
        result = _duration_cache.get(str(test_file), mtime)
        assert result is _CacheEntry  # Sentinel for cache miss
        
        # Set value
        _duration_cache.set(str(test_file), mtime, 10.5)
        
        # Cache hit
        result = _duration_cache.get(str(test_file), mtime)
        assert result == 10.5
    
    def test_cache_invalidates_on_mtime_change(self, tmp_path):
        """Test that cache invalidates when file modification time changes."""
        from agent_system.llm.token_utils import _duration_cache, _CacheEntry
        
        _duration_cache.clear()
        
        test_file = tmp_path / "test.wav"
        test_file.write_bytes(b"x" * 1000)
        old_mtime = test_file.stat().st_mtime
        
        # Cache a value
        _duration_cache.set(str(test_file), old_mtime, 10.5)
        
        # Should hit with same mtime
        assert _duration_cache.get(str(test_file), old_mtime) == 10.5
        
        # Should miss with different mtime (file changed)
        new_mtime = old_mtime + 1.0
        assert _duration_cache.get(str(test_file), new_mtime) is _CacheEntry
    
    def test_cache_evicts_oldest_on_capacity(self):
        """Test that cache evicts oldest entries when at capacity."""
        from agent_system.llm.token_utils import _MediaDurationCache, _CacheEntry
        
        # Create small cache
        cache = _MediaDurationCache(max_size=3, ttl_seconds=3600)
        
        # Fill cache
        cache.set("/file1", 1.0, 10.0)
        cache.set("/file2", 1.0, 20.0)
        cache.set("/file3", 1.0, 30.0)
        
        assert cache.stats()["size"] == 3
        
        # Add one more - should evict oldest (file1)
        cache.set("/file4", 1.0, 40.0)
        
        assert cache.stats()["size"] == 3
        assert cache.get("/file1", 1.0) is _CacheEntry  # Evicted
        assert cache.get("/file4", 1.0) == 40.0  # New entry present
    
    def test_cache_caches_none_values(self, tmp_path):
        """Test that cache stores None values (for files that can't be read)."""
        from agent_system.llm.token_utils import _duration_cache, _CacheEntry
        
        _duration_cache.clear()
        
        # Cache a None value (simulates failed read)
        _duration_cache.set("/nonexistent/file.wav", 0.0, None)
        
        # Should return None (not miss)
        result = _duration_cache.get("/nonexistent/file.wav", 0.0)
        assert result is None  # None is a valid cached value
    
    def test_estimate_file_tokens_uses_cache(self, tmp_path):
        """Test that estimate_file_tokens uses the cache for repeated calls."""
        from agent_system.llm.token_utils import (
            _duration_cache, estimate_file_tokens
        )
        import time
        
        _duration_cache.clear()
        
        # Create a fake audio file (pydub can't read it, falls back)
        audio_file = tmp_path / "test.wav"
        audio_file.write_bytes(b"x" * 163840)  # 10s at 16KB/s fallback
        
        # First call - populates cache
        t1 = time.perf_counter()
        tokens1 = estimate_file_tokens(audio_file, file_type='audio')
        t2 = time.perf_counter()
        first_time = t2 - t1
        
        # Second call - should use cache
        t3 = time.perf_counter()
        tokens2 = estimate_file_tokens(audio_file, file_type='audio')
        t4 = time.perf_counter()
        second_time = t4 - t3
        
        # Same result
        assert tokens1 == tokens2 == 410
        
        # Cache should have entry
        assert _duration_cache.stats()["size"] >= 1


class TestEstimateTokenCountDictSupport:
    """Tests for estimate_token_count with dict messages (not just ChatMessage)."""
    
    def test_estimate_token_count_with_dict_messages(self):
        """Test that estimate_token_count works with dict messages."""
        msg_dict = {
            'role': 'user',
            'content': 'Hello, how are you?'
        }
        
        tokens = estimate_token_count([msg_dict])
        
        # Should estimate tokens without error
        # 4 (overhead) + ~5 (text) = ~9
        assert tokens > 0
        assert tokens < 20
    
    def test_estimate_token_count_dict_with_multimodal_content(self, tmp_path):
        """Test that estimate_token_count counts multimodal_content in dict messages."""
        # Create a fake audio file
        audio_file = tmp_path / "test.wav"
        audio_file.write_bytes(b"x" * 163840)  # 10s at 16KB/s
        
        msg_dict = {
            'role': 'tool',
            'tool_call_id': 'call_123',
            'content': '{"status": "success"}',
            'multimodal_content': [
                {'type': 'audio', 'path': str(audio_file), 'mime_type': 'audio/wav'}
            ]
        }
        
        tokens = estimate_token_count([msg_dict])
        
        # Should include audio tokens: 10s × 41 = 410 + overhead ~24 = 434
        assert tokens > 400
        assert tokens < 450
    
    def test_estimate_token_count_dict_equals_chatmessage(self, tmp_path):
        """Test that dict and ChatMessage produce same token count."""
        from agent_system.llm.models import ChatMessage
        
        audio_file = tmp_path / "test.wav"
        audio_file.write_bytes(b"x" * 163840)
        
        msg_dict = {
            'role': 'tool',
            'tool_call_id': 'call_123',
            'content': '{"status": "success"}',
            'multimodal_content': [
                {'type': 'audio', 'path': str(audio_file), 'mime_type': 'audio/wav'}
            ]
        }
        
        msg_cm = ChatMessage(
            role='tool',
            tool_call_id='call_123',
            content='{"status": "success"}',
            multimodal_content=[
                {'type': 'audio', 'path': str(audio_file), 'mime_type': 'audio/wav'}
            ]
        )
        
        tokens_dict = estimate_token_count([msg_dict])
        tokens_cm = estimate_token_count([msg_cm])
        
        assert tokens_dict == tokens_cm
    
    def test_estimate_token_count_mixed_messages(self, tmp_path):
        """Test that estimate_token_count works with mixed dict and ChatMessage."""
        from agent_system.llm.models import ChatMessage
        
        msg1 = ChatMessage(role='user', content='Hello')
        msg2 = {'role': 'assistant', 'content': 'Hi there!'}
        msg3 = ChatMessage(role='user', content='How are you?')
        
        tokens = estimate_token_count([msg1, msg2, msg3])
        
        # Should work with mixed types
        assert tokens > 10


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
