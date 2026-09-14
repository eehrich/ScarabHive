"""Tests for LLM token estimation utilities."""

import pytest
from agent_system.llm.token_utils import (
    CHARS_PER_TOKEN,
    JSON_CHARS_PER_TOKEN,
    estimate_token_count,
    estimate_content_tokens,
    estimate_json_tokens,
    estimate_inline_data_tokens,
    estimate_tools_token_count,
)
from agent_system.llm.models import ChatMessage


class TestContentTokenEstimation:
    """Characters per token, fitted to production prompt_tokens (see CHARS_PER_TOKEN)."""

    def test_estimate_content_tokens_empty(self):
        """Test estimation for empty content."""
        assert estimate_content_tokens("") == 0
        assert estimate_content_tokens(None) == 0

    @pytest.mark.parametrize("text", [
        "The quick brown fox jumps over the lazy dog",
        "def function(): return True",
        '{"key": "value", "number": 123}',
        "Die Verwaltungsgerichtsbarkeit prüft Rechtsstreitigkeiten öffentlich-rechtlicher Art.",
    ])
    def test_every_kind_of_text_is_counted_by_its_characters(self, text):
        assert estimate_content_tokens(text) == int(len(text) / CHARS_PER_TOKEN)

    def test_one_code_word_does_not_rescore_the_whole_text(self):
        """Scored by content type, a single "from " or "class " switched the class of a
        whole text and moved its estimate by 20 %."""
        prose = " ".join(f"plain note number {i} about the project" for i in range(200))
        with_code_words = prose + " loaded from yaml, the class names"
        added = estimate_content_tokens(with_code_words) - estimate_content_tokens(prose)
        assert added <= len(" loaded from yaml, the class names") / CHARS_PER_TOKEN + 1


class TestJsonTokenEstimation:
    """Test JSON-specific token estimation."""

    def test_estimate_json_tokens_empty(self):
        """Test estimation for empty JSON."""
        assert estimate_json_tokens("") == 0
        assert estimate_json_tokens(None) == 0

    @pytest.mark.parametrize("json_str", [
        '{"name": "John", "age": 30}',
        '{"user": {"name": "Alice", "id": 1}, "active": true}',
        '[1, 2, 3, 4, 5]',
    ])
    def test_json_is_counted_denser_than_text(self, json_str):
        assert estimate_json_tokens(json_str) == int(len(json_str) / JSON_CHARS_PER_TOKEN)
        assert estimate_json_tokens(json_str) >= estimate_content_tokens(json_str)


class TestMessageTokenEstimation:
    """Test token estimation for complete messages."""

    def test_estimate_token_count_empty_list(self):
        """Test estimation for empty message list."""
        assert estimate_token_count([]) == 0

    def test_estimate_token_count_simple_user_message(self):
        """Test estimation for simple user message."""
        msg = ChatMessage(role="user", content="Hello, how are you?")
        tokens = estimate_token_count([msg])
        # Base overhead 4 + 19 characters / 3.3 = 5
        assert tokens == 9

    def test_estimate_token_count_assistant_message(self):
        """Test estimation for assistant message."""
        msg = ChatMessage(role="assistant", content="I am doing well, thank you!")
        tokens = estimate_token_count([msg])
        # Base overhead 4 + 27 characters / 3.3 = 8
        assert tokens == 12

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

    @pytest.mark.parametrize("content", [
        "This is a simple text response from a tool",
        '{"status": "success", "data": [1, 2, 3], "note": "done"}',
        "def load(path):\n    import json\n    return json.load(open(path))",
    ])
    def test_a_tool_result_counts_its_content_once(self, content):
        """Counted twice, tool-heavy production requests read 0.82 real tokens per
        estimated token against 1.36 for text; once, both 1.36-1.39."""
        as_text = estimate_token_count([ChatMessage(role="assistant", content=content)])
        as_result = estimate_token_count([ChatMessage(role="tool", content=content,
                                                      tool_call_id="call_1")])
        assert as_result == as_text + 8  # the tool call id overhead, nothing more

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
        natural = "The quick brown fox jumps over the lazy dog"  # 43 characters
        msg = ChatMessage(role="user", content=natural)
        tokens = estimate_token_count([msg])

        # 43 / 3.3 = 13 + 4 overhead
        assert tokens == 17

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
        from agent_system.llm.token_utils import _duration_cache
        
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
        t2 - t1
        
        # Second call - should use cache
        t3 = time.perf_counter()
        tokens2 = estimate_file_tokens(audio_file, file_type='audio')
        t4 = time.perf_counter()
        t4 - t3
        
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


class TestToolsTokenEstimation:
    """Test token estimation for tool definitions/schemas."""

    def test_estimate_tools_token_count_empty(self):
        """Test estimation for empty tools list."""
        assert estimate_tools_token_count([]) == 0

    def test_estimate_tools_token_count_none(self):
        """Test estimation for None tools."""
        assert estimate_tools_token_count(None) == 0

    def test_estimate_tools_token_count_single_tool(self):
        """Test estimation for a single tool with name, description, and parameters."""
        tools = [{
            "type": "function",
            "function": {
                "name": "get_weather",
                "description": "Get the current weather for a location",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "location": {
                            "type": "string",
                            "description": "The city and state, e.g. San Francisco, CA"
                        },
                        "unit": {
                            "type": "string",
                            "enum": ["celsius", "fahrenheit"]
                        }
                    },
                    "required": ["location"]
                }
            }
        }]
        tokens = estimate_tools_token_count(tools)
        # Should have: 10 base + name tokens + description tokens + params JSON tokens
        assert tokens > 30
        assert tokens < 300  # Reasonable upper bound for a single tool

    def test_a_description_counts_at_the_text_rate(self):
        without = estimate_tools_token_count([{"type": "function", "function": {}}])
        with_description = estimate_tools_token_count(
            [{"type": "function", "function": {"description": "x" * 330}}])

        assert without == 10
        assert with_description - without == estimate_content_tokens("x" * 330) == 100

    def test_estimate_tools_token_count_multiple_tools(self):
        """Test estimation scales with number of tools."""
        tools = [
            {
                "type": "function",
                "function": {
                    "name": f"tool_{i}",
                    "description": f"This is tool number {i} that does something useful",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "input": {"type": "string", "description": "The input value"}
                        },
                        "required": ["input"]
                    }
                }
            }
            for i in range(10)
        ]
        tokens = estimate_tools_token_count(tools)
        # 10 tools should be significantly more than 1 tool
        single_tool_tokens = estimate_tools_token_count(tools[:1])
        assert tokens > single_tool_tokens * 5  # Not exactly 10x due to varying name lengths

    def test_estimate_tools_token_count_no_parameters(self):
        """Test estimation for tool without parameters."""
        tools = [{
            "type": "function",
            "function": {
                "name": "ping",
                "description": "Ping the server"
            }
        }]
        tokens = estimate_tools_token_count(tools)
        # Minimal tool: 10 base + name + description
        assert tokens > 10
        assert tokens < 30

    def test_estimate_tools_token_count_complex_schema(self):
        """Test estimation for tool with complex nested parameters."""
        tools = [{
            "type": "function",
            "function": {
                "name": "create_document",
                "description": "Create a new document with structured content including title, body, metadata, and tags",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "title": {"type": "string", "description": "Document title"},
                        "body": {"type": "string", "description": "Document body content"},
                        "metadata": {
                            "type": "object",
                            "properties": {
                                "author": {"type": "string"},
                                "created_at": {"type": "string", "format": "date-time"},
                                "category": {"type": "string", "enum": ["tech", "science", "art"]},
                                "priority": {"type": "integer", "minimum": 1, "maximum": 5}
                            },
                            "required": ["author"]
                        },
                        "tags": {
                            "type": "array",
                            "items": {"type": "string"},
                            "maxItems": 10
                        }
                    },
                    "required": ["title", "body"]
                }
            }
        }]
        tokens = estimate_tools_token_count(tools)
        # Complex schema should be significantly more tokens
        assert tokens > 80

    def test_estimate_tools_token_count_empty_function(self):
        """Test estimation for tool with empty function definition."""
        tools = [{"type": "function", "function": {}}]
        tokens = estimate_tools_token_count(tools)
        # Should still count base overhead
        assert tokens == 10

    def test_estimate_tools_token_count_non_function_tool(self):
        """Test estimation for tool without function key."""
        tools = [{"type": "other_type"}]
        tokens = estimate_tools_token_count(tools)
        # Base overhead only, skips function parsing
        assert tokens == 10

    def test_estimate_tools_token_count_with_strict(self):
        """Test estimation includes strict mode flag."""
        tools_no_strict = [{
            "type": "function",
            "function": {
                "name": "test",
                "description": "A test tool"
            }
        }]
        tools_strict = [{
            "type": "function",
            "function": {
                "name": "test",
                "description": "A test tool",
                "strict": True
            }
        }]
        tokens_no_strict = estimate_tools_token_count(tools_no_strict)
        tokens_strict = estimate_tools_token_count(tools_strict)
        assert tokens_strict == tokens_no_strict + 2

    def test_estimate_token_count_with_tools(self):
        """Test that estimate_token_count includes tool tokens when tools parameter is provided."""
        messages = [ChatMessage(role="user", content="Hello")]
        tools = [{
            "type": "function",
            "function": {
                "name": "get_info",
                "description": "Get information about a topic",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "topic": {"type": "string"}
                    },
                    "required": ["topic"]
                }
            }
        }]
        
        tokens_without_tools = estimate_token_count(messages)
        tokens_with_tools = estimate_token_count(messages, tools=tools)
        
        # With tools should be strictly more than without
        assert tokens_with_tools > tokens_without_tools
        
        # The difference should equal the tool token estimation
        tool_tokens = estimate_tools_token_count(tools)
        assert tokens_with_tools == tokens_without_tools + tool_tokens

    def test_estimate_token_count_tools_none(self):
        """Test that estimate_token_count works when tools is None (default)."""
        messages = [ChatMessage(role="user", content="Hello")]
        tokens_default = estimate_token_count(messages)
        tokens_none = estimate_token_count(messages, tools=None)
        assert tokens_default == tokens_none

    def test_realistic_agent_tools(self):
        """Test estimation with a realistic set of agent tools (20+ tools)."""
        tools = []
        descriptions = [
            "Search the web for information using DuckDuckGo",
            "Read the contents of a file from the workspace",
            "Write content to a file in the workspace",
            "Execute a terminal command and return output",
            "Create a new directory in the workspace",
            "List files in a directory",
            "Search for text patterns in files using regex",
            "Get the current date and time",
            "Manage a todo list with add, remove, and list operations",
            "Store and retrieve persistent memories across sessions",
            "Scrape and parse web page content from a URL",
            "Execute SQL queries against a SQLite database",
            "Send a message to a sub-agent for parallel processing",
            "Summarize the current conversation context",
            "Check the current context window utilization stats",
            "Search code repositories for relevant snippets",
            "Compile and run code in a sandboxed environment",
            "Generate images using ComfyUI workflows",
            "Record and manage audio conversations",
            "Query financial market data and trading signals",
        ]
        for i, desc in enumerate(descriptions):
            tools.append({
                "type": "function",
                "function": {
                    "name": f"tool_{i}_{desc.split()[0].lower()}",
                    "description": desc,
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "query": {"type": "string", "description": "The input query"},
                            "options": {
                                "type": "object",
                                "properties": {
                                    "limit": {"type": "integer", "default": 10},
                                    "format": {"type": "string", "enum": ["json", "text", "html"]}
                                }
                            }
                        },
                        "required": ["query"]
                    }
                }
            })
        
        tokens = estimate_tools_token_count(tools)
        # 20 tools with moderate schemas should be in the thousands
        assert tokens > 1000
        assert tokens < 20000  # But not unreasonably large


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
