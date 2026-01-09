"""Tests for openai_utils content normalization."""
from agent_system.llm import openai_utils


class TestNormalizeContentItem:
    """Test normalize_content_item function."""

    def test_text_item_passthrough(self):
        """Text items pass through unchanged."""
        item = {"type": "text", "text": "Hello world"}
        result = openai_utils.normalize_content_item(item)
        assert result == item

    def test_text_file_converted_to_text(self):
        """text_file items are converted to text with filename header."""
        item = {
            "type": "text_file",
            "name": "example.py",
            "content": "print('hello')"
        }
        result = openai_utils.normalize_content_item(item)
        
        assert result["type"] == "text"
        assert "[File: example.py]" in result["text"]
        assert "print('hello')" in result["text"]

    def test_text_file_without_name(self):
        """text_file without name uses default 'file'."""
        item = {
            "type": "text_file",
            "content": "some content"
        }
        result = openai_utils.normalize_content_item(item)
        
        assert result["type"] == "text"
        assert "[File: file]" in result["text"]

    def test_image_url_only_keeps_url_and_detail(self):
        """image_url items only keep url and optional detail keys."""
        item = {
            "type": "image_url",
            "image_url": {
                "url": "https://example.com/img.png",
                "detail": "high",
                "name": "extra_field",
                "other": "ignored"
            }
        }
        result = openai_utils.normalize_content_item(item)
        
        assert result["type"] == "image_url"
        assert result["image_url"]["url"] == "https://example.com/img.png"
        assert result["image_url"]["detail"] == "high"
        assert "name" not in result["image_url"]
        assert "other" not in result["image_url"]

    def test_image_url_without_detail(self):
        """image_url without detail key."""
        item = {
            "type": "image_url",
            "image_url": {"url": "data:image/png;base64,abc123"}
        }
        result = openai_utils.normalize_content_item(item)
        
        assert result["type"] == "image_url"
        assert result["image_url"] == {"url": "data:image/png;base64,abc123"}

    def test_audio_returns_none(self):
        """Audio items return None (filtered out)."""
        item = {"type": "audio", "audio": {"data": "base64data"}}
        result = openai_utils.normalize_content_item(item)
        assert result is None

    def test_video_returns_none(self):
        """Video items return None (filtered out)."""
        item = {"type": "video", "video": {"url": "http://example.com/v.mp4"}}
        result = openai_utils.normalize_content_item(item)
        assert result is None

    def test_unknown_type_passthrough(self):
        """Unknown types pass through unchanged."""
        item = {"type": "custom", "data": "value"}
        result = openai_utils.normalize_content_item(item)
        assert result == item


class TestNormalizeContentList:
    """Test normalize_content_list function."""

    def test_filters_audio_and_video(self):
        """Audio and video items are filtered out."""
        content = [
            {"type": "text", "text": "Hello"},
            {"type": "audio", "audio": {"data": "base64"}},
            {"type": "text", "text": "World"},
            {"type": "video", "video": {"url": "http://v.mp4"}}
        ]
        result = openai_utils.normalize_content_list(content)
        
        assert len(result) == 2
        assert result[0]["text"] == "Hello"
        assert result[1]["text"] == "World"

    def test_converts_text_files(self):
        """text_file items are converted."""
        content = [
            {"type": "text_file", "name": "test.txt", "content": "contents"}
        ]
        result = openai_utils.normalize_content_list(content)
        
        assert len(result) == 1
        assert result[0]["type"] == "text"
        assert "[File: test.txt]" in result[0]["text"]

    def test_normalizes_images(self):
        """Image items are normalized to remove extra keys."""
        content = [
            {
                "type": "image_url",
                "image_url": {
                    "url": "https://img.jpg",
                    "name": "should_be_removed"
                }
            }
        ]
        result = openai_utils.normalize_content_list(content)
        
        assert len(result) == 1
        assert "name" not in result[0]["image_url"]


class TestNormalizeMessageContent:
    """Test normalize_message_content function."""

    def test_string_content_unchanged(self):
        """String content passes through unchanged."""
        result = openai_utils.normalize_message_content("Hello")
        assert result == "Hello"

    def test_list_content_single_text_simplified_to_string(self):
        """List with single text item is simplified to string."""
        content = [
            {"type": "text", "text": "Hello"}
        ]
        result = openai_utils.normalize_message_content(content)
        # Single text item gets simplified to string
        assert result == "Hello"

    def test_list_content_filters_audio(self):
        """List content filters audio, single text item becomes string."""
        content = [
            {"type": "text", "text": "Hello"},
            {"type": "audio", "audio": {"data": "base64"}}
        ]
        result = openai_utils.normalize_message_content(content)
        # After filtering audio, only one text item remains -> string
        assert result == "Hello"

    def test_list_content_multiple_items_stays_list(self):
        """List with multiple non-audio items stays as list."""
        content = [
            {"type": "text", "text": "Hello"},
            {"type": "image_url", "image_url": {"url": "https://img.jpg"}}
        ]
        result = openai_utils.normalize_message_content(content)
        assert isinstance(result, list)
        assert len(result) == 2

    def test_none_content_becomes_empty_string(self):
        """None content becomes empty string."""
        result = openai_utils.normalize_message_content(None)
        assert result == ""

    def test_empty_list_becomes_empty_string(self):
        """Empty list becomes empty string."""
        result = openai_utils.normalize_message_content([])
        assert result == ""


class TestNormalizeMessages:
    """Test normalize_messages function."""

    def test_normalizes_all_message_contents(self):
        """All message contents are normalized."""
        messages = [
            {"role": "user", "content": [
                {"type": "text", "text": "Hello"},
                {"type": "audio", "audio": {"data": "base64"}}
            ]},
            {"role": "assistant", "content": "Response"}
        ]
        result = openai_utils.normalize_messages(messages)
        
        assert len(result) == 2
        assert len(result[0]["content"]) == 1
        assert result[1]["content"] == "Response"

    def test_preserves_other_message_fields(self):
        """Other message fields like role, name, tool_calls are preserved."""
        messages = [
            {
                "role": "assistant",
                "content": "Response",
                "tool_calls": [{"id": "123", "type": "function"}]
            }
        ]
        result = openai_utils.normalize_messages(messages)
        
        assert result[0]["tool_calls"] == [{"id": "123", "type": "function"}]
