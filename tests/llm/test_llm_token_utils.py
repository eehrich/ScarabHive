"""Tests for LLM token estimation utilities."""

import pytest
from agent_system.llm.token_utils import (
    CHARS_PER_TOKEN,
    JSON_CHARS_PER_TOKEN,
    TOKENS_PER_AUDIO_SECOND,
    TOKENS_PER_IMAGE,
    TOKENS_PER_VIDEO_SECOND,
    estimate_token_count,
    estimate_content_tokens,
    estimate_json_tokens,
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

    IMAGE_PAYLOAD = "A" * 1_000_044  # a 750 KB picture as base64

    @pytest.mark.parametrize("item", [
        {"type": "image_url", "image_url": {"url": "data:image/png;base64," + IMAGE_PAYLOAD}},
        {"type": "image_url", "image_url": "data:image/png;base64," + IMAGE_PAYLOAD},
        {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": IMAGE_PAYLOAD}},
        {"type": "image", "inline_data": {"mime_type": "image/png", "data": IMAGE_PAYLOAD}},
        {"type": "image", "source": {"media_type": "image/png", "data": IMAGE_PAYLOAD}},
    ], ids=["image_url_dict", "image_url_string", "source", "inline_data", "source_without_type"])
    @pytest.mark.parametrize("as_chat_message", [False, True], ids=["dict", "chat_message"])
    def test_an_inline_image_costs_one_image_not_its_base64_length(self, item, as_chat_message):
        """0.25 tokens per base64 character made this picture 250,015 tokens in one
        form and 262 in another; the provider bills one image either way."""
        from agent_system.llm.token_utils import TOKENS_PER_IMAGE

        text = {"type": "text", "text": "What is in this image?"}
        with_image = {"role": "user", "content": [text, item]}
        without = {"role": "user", "content": [text]}
        if as_chat_message:
            with_image, without = ChatMessage(**with_image), ChatMessage(**without)
            assert not isinstance(with_image.content[1], dict), "fixture stayed a dict"

        assert (estimate_token_count([with_image])
                == estimate_token_count([without]) + TOKENS_PER_IMAGE)

    # 20 seconds at 16 KB/s: 327,680 bytes. Not 10 seconds — that is the default
    # of an audio item without a payload, and a lost payload would look right.
    AUDIO_BYTES = 20 * 16 * 1024
    AUDIO_B64 = "A" * (-(-AUDIO_BYTES // 3) * 4)

    @pytest.mark.parametrize("item", [
        {"type": "audio", "source": {"type": "base64", "media_type": "audio/wav", "data": AUDIO_B64}},
        {"type": "audio", "inline_data": {"mime_type": "audio/wav", "data": AUDIO_B64}},
        {"type": "audio", "inline_data": {"mime_type": "audio/wav", "data": b"x" * AUDIO_BYTES}},
        {"type": "audio", "audio_url": "data:audio/wav;base64," + AUDIO_B64},
    ], ids=["source", "inline_data_base64", "inline_data_bytes", "audio_url"])
    @pytest.mark.parametrize("as_chat_message", [False, True], ids=["dict", "chat_message"])
    def test_an_inline_audio_payload_is_counted_by_its_bytes_in_every_form(self, item, as_chat_message):
        from agent_system.llm.token_utils import TOKENS_PER_AUDIO_SECOND, estimate_media_tokens

        if as_chat_message:
            item = ChatMessage(role="user", content=[item]).content[0]
            assert not isinstance(item, dict), "fixture stayed a dict"

        assert estimate_media_tokens(item) == 20 * TOKENS_PER_AUDIO_SECOND

    def test_audio_as_writer_audio_sends_it_is_counted_by_its_bytes(self):
        """AudioContent(source=ImageSource(...)), the writer audio scorer's form."""
        from agent_system.llm.models import AudioContent, ImageSource
        from agent_system.llm.token_utils import TOKENS_PER_AUDIO_SECOND, estimate_media_tokens

        item = AudioContent(source=ImageSource(media_type="audio/wav", data=self.AUDIO_B64))
        msg = ChatMessage(role="user", content=[item])

        assert estimate_media_tokens(item) == 20 * TOKENS_PER_AUDIO_SECOND
        assert estimate_token_count([msg]) == 4 + 20 * TOKENS_PER_AUDIO_SECOND

    def test_an_audio_duration_wins_over_its_bytes(self):
        from agent_system.llm.token_utils import TOKENS_PER_AUDIO_SECOND, estimate_media_tokens

        item = {"type": "audio", "audio_url": "data:audio/wav;base64," + self.AUDIO_B64,
                "duration_seconds": 45.0}
        assert estimate_media_tokens(item) == int(45.0 * TOKENS_PER_AUDIO_SECOND)

    @pytest.mark.parametrize("as_chat_message", [False, True], ids=["dict", "chat_message"])
    def test_a_video_url_payload_is_counted_by_its_bytes(self, as_chat_message):
        """Never measured before: a video counted 30 seconds at the AUDIO rate."""
        from agent_system.llm.token_utils import TOKENS_PER_VIDEO_SECOND, estimate_media_tokens

        raw = 5 * 100 * 1024  # 5 seconds at 100 KB/s
        item = {"type": "video", "video_url": "data:video/mp4;base64," + "A" * (-(-raw // 3) * 4)}
        if as_chat_message:
            item = ChatMessage(role="user", content=[item]).content[0]
            assert not isinstance(item, dict), "fixture stayed a dict"

        assert estimate_media_tokens(item) == 5 * TOKENS_PER_VIDEO_SECOND

    @pytest.mark.parametrize("item, expected", [
        ({"inline_data": {"mime_type": "audio/wav", "data": AUDIO_B64}},
         20 * TOKENS_PER_AUDIO_SECOND),
        ({"source": {"media_type": "video/mp4", "data": "A" * (-(-5 * 100 * 1024 // 3) * 4)}},
         5 * TOKENS_PER_VIDEO_SECOND),
        ({"media_type": "audio/wav", "source": {"data": AUDIO_B64}}, 20 * TOKENS_PER_AUDIO_SECOND),
        ({"inline_data": {"mime_type": "image/png", "data": "QUJD"}}, TOKENS_PER_IMAGE),
    ], ids=["inline_data_audio", "source_video", "mime_on_the_item", "inline_data_image"])
    def test_an_item_without_a_type_is_read_by_its_mime(self, item, expected):
        """Gemini's native inline_data carries no ``type``, and extract_inline_media
        reads such items; without the mime they cost nothing."""
        from agent_system.llm.token_utils import estimate_media_tokens

        assert estimate_media_tokens(item) == expected

    def test_every_media_item_in_a_mixed_message_counts(self):
        """The fallback per image applied only when NO item had a payload: next to a
        payload image, a remote image was free."""
        from agent_system.llm.token_utils import TOKENS_PER_IMAGE

        msg = ChatMessage(role="user", content=[
            {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "A" * 40_000}},
            {"type": "image_url", "image_url": {"url": "https://example.com/image.png"}},
        ])
        assert estimate_token_count([msg]) == 4 + 2 * TOKENS_PER_IMAGE

    def test_a_file_image_and_the_same_bytes_inline_cost_the_same(self, tmp_path):
        """A file used to count 258 per 300 KB tile, growing without bound."""
        import base64
        from agent_system.llm.token_utils import TOKENS_PER_IMAGE, estimate_media_tokens

        raw = b"\x89PNG" + b"x" * 3_000_000
        image = tmp_path / "large.png"
        image.write_bytes(raw)
        as_file = {"type": "image", "path": str(image), "mime_type": "image/png"}
        inline = {"type": "image_url", "image_url": {
            "url": "data:image/png;base64," + base64.b64encode(raw).decode()}}

        assert estimate_media_tokens(as_file) == estimate_media_tokens(inline) == TOKENS_PER_IMAGE

    @pytest.mark.parametrize("item", [
        {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "QUJDREVGRw=="}},
        {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,QUJDREVGRw=="}},
        {"type": "image_url", "image_url": "data:image/webp;base64,QUJDREVGRw=="},
        {"type": "image", "inline_data": {"mime_type": "image/gif", "data": "QUJDREVGRw=="}},
        {"type": "audio", "audio_url": "data:audio/mpeg;base64,QUJDREVGRw=="},
        {"type": "video", "video_url": "data:video/mp4;base64,QUJDREVGRw=="},
        {"media_type": "audio/wav", "source": {"data": "QUJDREVGRw=="}},
    ], ids=["source", "image_url_dict", "image_url_string", "inline_data", "audio_url", "video_url",
            "mime_on_the_item"])
    def test_the_payload_reader_agrees_with_extract_inline_media(self, item):
        """Two readers of the same wire shapes, one decoding, one not: a shape only
        one of them knows is media the estimate or the store cannot see."""
        from agent_system.llm.token_utils import inline_payload
        from agent_system.utils.multimodal_tool_content import extract_inline_media

        raw, mime, _ = extract_inline_media(item)
        found = inline_payload(item)

        assert found is not None, "the non-decoding reader does not know this shape"
        assert abs(found[0] * 3 // 4 - len(raw)) <= 2
        assert found[1] == mime

    def test_a_non_media_item_costs_nothing(self):
        from agent_system.llm.token_utils import estimate_media_tokens

        assert estimate_media_tokens({"type": "text", "text": "Hello world"}) == 0
        assert estimate_media_tokens("plain string") == 0

    def test_a_type_that_is_no_plain_string_is_still_read(self):
        """``type`` is not promised to be a str: a ContentType member names the
        medium, an unhashable value names none and must not raise."""
        from agent_system.llm.models import ContentType
        from agent_system.llm.token_utils import TOKENS_PER_IMAGE, estimate_media_tokens

        assert estimate_media_tokens({"type": ContentType.IMAGE_URL,
                                      "image_url": "https://example.com/a.png"}) == TOKENS_PER_IMAGE
        assert estimate_media_tokens({"type": ["image"], "image_url": "https://example.com/a.png"}) == 0

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

    def test_estimate_media_tokens_with_file_path(self, tmp_path):
        """A path item is counted from its file."""
        from agent_system.llm.token_utils import estimate_media_tokens

        # Create a fake audio file - exact 163840 bytes = 10s at 16KB/s
        audio_file = tmp_path / "audio.wav"
        audio_file.write_bytes(b"x" * 163840)

        # MultimodalToolContent dict format
        item = {
            "type": "audio",
            "path": str(audio_file),
            "mime_type": "audio/wav"
        }

        tokens = estimate_media_tokens(item)
        # Fallback: 163840 / 16384 = 10 seconds × 41 tokens/s = 410 tokens
        assert tokens == 410

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
