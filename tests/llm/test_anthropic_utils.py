"""Tests for anthropic_utils content normalization."""
import pytest
from agent_system.llm import anthropic_utils


class TestConvertImageContent:
    """Test _convert_image_content function."""

    def test_base64_image_direct_format(self):
        """Base64 image in Anthropic's direct format."""
        item = {
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": "image/png",
                "data": "iVBORw0KGgoAAAANSUhEUgAAAAE="
            }
        }
        result = anthropic_utils._convert_image_content(item)
        
        assert result["type"] == "image"
        assert result["source"]["type"] == "base64"
        assert result["source"]["media_type"] == "image/png"
        assert result["source"]["data"] == "iVBORw0KGgoAAAANSUhEUgAAAAE="

    def test_url_image(self):
        """URL image conversion."""
        item = {
            "type": "image_url",
            "image_url": {"url": "https://example.com/image.jpg"}
        }
        result = anthropic_utils._convert_image_content(item)
        
        assert result["type"] == "image"
        assert result["source"]["type"] == "url"
        assert result["source"]["url"] == "https://example.com/image.jpg"

    def test_data_url_image(self):
        """Data URL image is converted to base64 format."""
        item = {
            "type": "image_url",
            "image_url": {"url": "data:image/jpeg;base64,/9j/4AAQ"}
        }
        result = anthropic_utils._convert_image_content(item)
        
        assert result["type"] == "image"
        assert result["source"]["type"] == "base64"
        assert result["source"]["media_type"] == "image/jpeg"
        assert result["source"]["data"] == "/9j/4AAQ"

    def test_invalid_data_url_returns_none(self):
        """Invalid data URL returns None."""
        item = {
            "type": "image_url",
            "image_url": {"url": "data:invalid"}
        }
        result = anthropic_utils._convert_image_content(item)
        assert result is None

    def test_url_as_string(self):
        """image_url can be a plain string."""
        item = {
            "type": "image_url",
            "image_url": "https://example.com/img.png"
        }
        result = anthropic_utils._convert_image_content(item)
        
        assert result["type"] == "image"
        assert result["source"]["type"] == "url"
        assert result["source"]["url"] == "https://example.com/img.png"

    def test_unknown_type_returns_none(self):
        """Unknown image type returns None."""
        item = {"type": "video", "video": {}}
        result = anthropic_utils._convert_image_content(item)
        assert result is None


class TestNormalizeContentItem:
    """Test normalize_content_item function."""

    def test_text_dict(self):
        """Text dict passes through."""
        item = {"type": "text", "text": "Hello"}
        result = anthropic_utils.normalize_content_item(item)
        assert result == {"type": "text", "text": "Hello"}

    def test_text_file_converted(self):
        """text_file is converted to text with filename header."""
        item = {
            "type": "text_file",
            "name": "example.py",
            "content": "print('hello')"
        }
        result = anthropic_utils.normalize_content_item(item)
        
        assert result["type"] == "text"
        assert "[File: example.py]" in result["text"]
        assert "print('hello')" in result["text"]

    def test_image_converted(self):
        """Image types are converted via _convert_image_content."""
        item = {
            "type": "image_url",
            "image_url": {"url": "https://example.com/img.png"}
        }
        result = anthropic_utils.normalize_content_item(item)
        
        assert result["type"] == "image"
        assert result["source"]["type"] == "url"

    def test_audio_returns_none(self):
        """Audio items return None (filtered out)."""
        item = {"type": "audio", "audio": {"data": "base64"}}
        result = anthropic_utils.normalize_content_item(item)
        assert result is None

    def test_video_returns_none(self):
        """Video items return None (filtered out)."""
        item = {"type": "video", "video": {}}
        result = anthropic_utils.normalize_content_item(item)
        assert result is None


class TestNormalizeContentList:
    """Test normalize_content_list function."""

    def test_filters_audio_and_video(self):
        """Audio and video items are filtered out."""
        content = [
            {"type": "text", "text": "Hello"},
            {"type": "audio", "audio": {"data": "base64"}},
            {"type": "text", "text": "World"},
            {"type": "video", "video": {}}
        ]
        result = anthropic_utils.normalize_content_list(content)
        
        assert len(result) == 2
        assert result[0]["text"] == "Hello"
        assert result[1]["text"] == "World"

    def test_converts_strings_to_text_blocks(self):
        """String items become text blocks."""
        content = ["Hello", "World"]
        result = anthropic_utils.normalize_content_list(content)
        
        assert len(result) == 2
        assert result[0] == {"type": "text", "text": "Hello"}
        assert result[1] == {"type": "text", "text": "World"}

    def test_converts_text_files(self):
        """text_file items are converted."""
        content = [
            {"type": "text_file", "name": "test.txt", "content": "contents"}
        ]
        result = anthropic_utils.normalize_content_list(content)
        
        assert len(result) == 1
        assert result[0]["type"] == "text"
        assert "[File: test.txt]" in result[0]["text"]

    def test_converts_images(self):
        """Image items are converted to Anthropic format."""
        content = [
            {
                "type": "image_url",
                "image_url": {"url": "https://img.jpg"}
            }
        ]
        result = anthropic_utils.normalize_content_list(content)
        
        assert len(result) == 1
        assert result[0]["type"] == "image"
        assert result[0]["source"]["type"] == "url"

    def test_mixed_content(self):
        """Mixed content types are handled."""
        content = [
            "Plain string",
            {"type": "text", "text": "Text dict"},
            {"type": "text_file", "name": "f.txt", "content": "file content"},
            {"type": "image_url", "image_url": {"url": "https://img.png"}},
            {"type": "audio", "audio": {}}  # Should be filtered
        ]
        result = anthropic_utils.normalize_content_list(content)
        
        assert len(result) == 4
        assert result[0] == {"type": "text", "text": "Plain string"}
        assert result[1] == {"type": "text", "text": "Text dict"}
        assert "[File: f.txt]" in result[2]["text"]
        assert result[3]["type"] == "image"

    def test_empty_list(self):
        """Empty list returns empty list."""
        result = anthropic_utils.normalize_content_list([])
        assert result == []

    def test_all_filtered_returns_empty(self):
        """If all items are filtered, returns empty list."""
        content = [
            {"type": "audio", "audio": {}},
            {"type": "video", "video": {}}
        ]
        result = anthropic_utils.normalize_content_list(content)
        assert result == []
