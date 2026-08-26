"""Tests for ollama_utils content normalization."""
from plugins_llm.llm_ollama import ollama_utils


class TestExtractBase64FromDataUrl:
    """Test extract_base64_from_data_url function."""

    def test_valid_data_url(self):
        """Valid data URL extracts base64 data."""
        data_url = "data:image/jpeg;base64,/9j/4AAQSkZJRg=="
        result = ollama_utils.extract_base64_from_data_url(data_url)
        assert result == "/9j/4AAQSkZJRg=="

    def test_png_data_url(self):
        """PNG data URL extracts base64 data."""
        data_url = "data:image/png;base64,iVBORw0KGgo="
        result = ollama_utils.extract_base64_from_data_url(data_url)
        assert result == "iVBORw0KGgo="

    def test_regular_url_returns_none(self):
        """Regular URL returns None."""
        url = "https://example.com/image.jpg"
        result = ollama_utils.extract_base64_from_data_url(url)
        assert result is None

    def test_invalid_data_url_returns_none(self):
        """Invalid data URL without comma returns None."""
        data_url = "data:image/jpeg;base64"
        result = ollama_utils.extract_base64_from_data_url(data_url)
        assert result is None


class TestExtractImagesFromContent:
    """Test extract_images_from_content function."""

    def test_string_content(self):
        """String content returns text and empty images."""
        text, images = ollama_utils.extract_images_from_content("Hello world")
        assert text == "Hello world"
        assert images == []

    def test_text_item(self):
        """Text item extracts text."""
        content = [{"type": "text", "text": "Hello"}]
        text, images = ollama_utils.extract_images_from_content(content)
        assert text == "Hello"
        assert images == []

    def test_multiple_text_items(self):
        """Multiple text items are joined."""
        content = [
            {"type": "text", "text": "Hello"},
            {"type": "text", "text": "World"}
        ]
        text, images = ollama_utils.extract_images_from_content(content)
        assert text == "Hello\nWorld"
        assert images == []

    def test_text_file_converted(self):
        """text_file items are converted to text with header."""
        content = [{"type": "text_file", "name": "test.py", "content": "print('hi')"}]
        text, images = ollama_utils.extract_images_from_content(content)
        assert "[File: test.py]" in text
        assert "print('hi')" in text
        assert images == []

    def test_image_url_data_url(self):
        """image_url with data URL extracts base64."""
        content = [{
            "type": "image_url",
            "image_url": {"url": "data:image/png;base64,abc123"}
        }]
        text, images = ollama_utils.extract_images_from_content(content)
        assert text == ""
        assert images == ["abc123"]

    def test_image_url_regular_url_skipped(self):
        """image_url with regular URL is skipped (Ollama only supports base64)."""
        content = [{
            "type": "image_url",
            "image_url": {"url": "https://example.com/img.jpg"}
        }]
        text, images = ollama_utils.extract_images_from_content(content)
        assert text == ""
        assert images == []  # Regular URLs not supported

    def test_anthropic_style_image(self):
        """Anthropic-style base64 image is extracted."""
        content = [{
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": "image/jpeg",
                "data": "xyz789"
            }
        }]
        text, images = ollama_utils.extract_images_from_content(content)
        assert text == ""
        assert images == ["xyz789"]

    def test_audio_skipped(self):
        """Audio items are skipped."""
        content = [
            {"type": "text", "text": "Hello"},
            {"type": "audio", "audio": {"data": "base64"}}
        ]
        text, images = ollama_utils.extract_images_from_content(content)
        assert text == "Hello"
        assert images == []

    def test_video_skipped(self):
        """Video items are skipped."""
        content = [
            {"type": "text", "text": "Hello"},
            {"type": "video", "video": {}}
        ]
        text, images = ollama_utils.extract_images_from_content(content)
        assert text == "Hello"
        assert images == []

    def test_mixed_content(self):
        """Mixed text and images are properly extracted."""
        content = [
            {"type": "text", "text": "Describe this:"},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,img1"}},
            {"type": "text", "text": "And this:"},
            {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,img2"}}
        ]
        text, images = ollama_utils.extract_images_from_content(content)
        assert "Describe this:" in text
        assert "And this:" in text
        assert images == ["img1", "img2"]

    def test_image_url_as_string(self):
        """image_url can be a plain string."""
        content = [{
            "type": "image_url",
            "image_url": "data:image/png;base64,stringformat"
        }]
        text, images = ollama_utils.extract_images_from_content(content)
        assert images == ["stringformat"]


class TestNormalizeMessage:
    """Test normalize_message function."""

    def test_simple_text_message(self):
        """Simple text message."""
        msg = {"role": "user", "content": "Hello"}
        result = ollama_utils.normalize_message(msg)
        
        assert result["role"] == "user"
        assert result["content"] == "Hello"
        assert "images" not in result

    def test_message_with_images(self):
        """Message with images extracts them to images field."""
        msg = {
            "role": "user",
            "content": [
                {"type": "text", "text": "What is this?"},
                {"type": "image_url", "image_url": {"url": "data:image/png;base64,abc"}}
            ]
        }
        result = ollama_utils.normalize_message(msg)
        
        assert result["role"] == "user"
        assert result["content"] == "What is this?"
        assert result["images"] == ["abc"]

    def test_message_preserves_tool_calls(self):
        """Message preserves tool_calls."""
        msg = {
            "role": "assistant",
            "content": "",
            "tool_calls": [{"id": "123", "function": {"name": "test"}}]
        }
        result = ollama_utils.normalize_message(msg)
        
        assert result["tool_calls"] == [{"id": "123", "function": {"name": "test"}}]

    def test_message_preserves_tool_call_id(self):
        """Message preserves tool_call_id for tool responses."""
        msg = {
            "role": "tool",
            "content": "Result",
            "tool_call_id": "call_123"
        }
        result = ollama_utils.normalize_message(msg)
        
        assert result["tool_call_id"] == "call_123"

    def test_message_preserves_name(self):
        """Message preserves name field."""
        msg = {
            "role": "user",
            "content": "Hello",
            "name": "John"
        }
        result = ollama_utils.normalize_message(msg)
        
        assert result["name"] == "John"


class TestNormalizeMessages:
    """Test normalize_messages function."""

    def test_normalizes_all_messages(self):
        """All messages are normalized."""
        messages = [
            {"role": "user", "content": "Hello"},
            {"role": "assistant", "content": "Hi there"},
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "Look at this"},
                    {"type": "image_url", "image_url": {"url": "data:image/png;base64,xyz"}}
                ]
            }
        ]
        result = ollama_utils.normalize_messages(messages)
        
        assert len(result) == 3
        assert result[0]["content"] == "Hello"
        assert result[1]["content"] == "Hi there"
        assert result[2]["content"] == "Look at this"
        assert result[2]["images"] == ["xyz"]
