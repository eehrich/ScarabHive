"""Tests for multimodal message support."""

from agent_system.llm.models import (
    ChatMessage,
    ContentType,
    ImageDetail,
    ImageSource,
    ImageContent,
    TextContent,
    AudioContent,
    VideoContent,
)


class TestContentTypes:
    """Test content type definitions."""
    
    def test_content_type_enum(self):
        """Test ContentType enum values."""
        assert ContentType.TEXT == "text"
        assert ContentType.IMAGE == "image"
        assert ContentType.IMAGE_URL == "image_url"
        assert ContentType.AUDIO == "audio"
        assert ContentType.VIDEO == "video"
    
    def test_image_detail_enum(self):
        """Test ImageDetail enum values."""
        assert ImageDetail.AUTO == "auto"
        assert ImageDetail.LOW == "low"
        assert ImageDetail.HIGH == "high"


class TestImageSource:
    """Test ImageSource model."""
    
    def test_base64_image_source(self):
        """Test base64 image source."""
        source = ImageSource(
            type="base64",
            media_type="image/jpeg",
            data="base64encodeddata"
        )
        assert source.type == "base64"
        assert source.media_type == "image/jpeg"
        assert source.data == "base64encodeddata"
        assert source.url is None
    
    def test_url_image_source(self):
        """Test URL image source."""
        source = ImageSource(
            type="url",
            url="https://example.com/image.jpg"
        )
        assert source.type == "url"
        assert source.url == "https://example.com/image.jpg"
        assert source.data is None


class TestContentModels:
    """Test content model classes."""
    
    def test_text_content(self):
        """Test TextContent model."""
        text = TextContent(type="text", text="Hello world")
        assert text.type == "text"
        assert text.text == "Hello world"
    
    def test_image_content_anthropic_format(self):
        """Test ImageContent with Anthropic format."""
        image = ImageContent(
            type="image",
            source=ImageSource(
                type="base64",
                media_type="image/jpeg",
                data="base64data"
            )
        )
        assert image.type == "image"
        assert image.source is not None
        assert image.source.type == "base64"
        assert image.source.media_type == "image/jpeg"
    
    def test_image_content_openai_format(self):
        """Test ImageContent with OpenAI format."""
        image = ImageContent(
            type="image_url",
            image_url="https://example.com/image.jpg",
            detail=ImageDetail.HIGH
        )
        assert image.type == "image_url"
        assert image.image_url == "https://example.com/image.jpg"
        assert image.detail == ImageDetail.HIGH
    
    def test_audio_content(self):
        """Test AudioContent model."""
        audio = AudioContent(
            type="audio",
            source=ImageSource(
                type="base64",
                media_type="audio/wav",
                data="base64audio"
            )
        )
        assert audio.type == "audio"
        assert audio.source.media_type == "audio/wav"
    
    def test_video_content(self):
        """Test VideoContent model."""
        video = VideoContent(
            type="video",
            source=ImageSource(
                type="base64",
                media_type="video/mp4",
                data="base64video"
            )
        )
        assert video.type == "video"
        assert video.source.media_type == "video/mp4"


class TestChatMessageBackwardCompatibility:
    """Test backward compatibility of ChatMessage."""
    
    def test_text_only_message(self):
        """Test traditional text-only message."""
        msg = ChatMessage(role="user", content="Hello")
        assert msg.role == "user"
        assert msg.content == "Hello"
        assert not msg.is_multimodal()
        assert msg.get_text_content() == "Hello"
    
    def test_tool_call_message(self):
        """Test message with tool calls."""
        msg = ChatMessage(
            role="assistant",
            content="",
            tool_calls=[
                {
                    "id": "call_123",
                    "type": "function",
                    "function": {"name": "get_weather", "arguments": "{}"}
                }
            ]
        )
        assert msg.role == "assistant"
        assert msg.tool_calls is not None
        assert len(msg.tool_calls) == 1
    
    def test_tool_response_message(self):
        """Test message with tool call response."""
        msg = ChatMessage(
            role="tool",
            content="Weather: 72F",
            tool_call_id="call_123"
        )
        assert msg.role == "tool"
        assert msg.tool_call_id == "call_123"
        assert msg.content == "Weather: 72F"


class TestMultimodalMessages:
    """Test multimodal message support."""
    
    def test_text_and_image_message(self):
        """Test message with text and image."""
        msg = ChatMessage(
            role="user",
            content=[
                {"type": "text", "text": "What's in this image?"},
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": "image/jpeg",
                        "data": "base64imagedata"
                    }
                }
            ]
        )
        assert msg.role == "user"
        assert msg.is_multimodal()
        assert msg.has_images()
        assert msg.count_images() == 1
        assert msg.get_text_content() == "What's in this image?"
    
    def test_multiple_images_message(self):
        """Test message with multiple images."""
        msg = ChatMessage(
            role="user",
            content=[
                {"type": "text", "text": "Compare these images"},
                {
                    "type": "image_url",
                    "image_url": "https://example.com/image1.jpg"
                },
                {
                    "type": "image_url",
                    "image_url": "https://example.com/image2.jpg"
                }
            ]
        )
        assert msg.is_multimodal()
        assert msg.has_images()
        assert msg.count_images() == 2
    
    def test_openai_format_image(self):
        """Test OpenAI format image message."""
        msg = ChatMessage(
            role="user",
            content=[
                TextContent(type="text", text="Describe this image"),
                ImageContent(
                    type="image_url",
                    image_url={"url": "https://example.com/image.jpg", "detail": "high"}
                )
            ]
        )
        assert msg.is_multimodal()
        assert msg.has_images()
        assert msg.count_images() == 1
    
    def test_anthropic_format_image(self):
        """Test Anthropic format image message."""
        msg = ChatMessage(
            role="user",
            content=[
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": "image/png",
                        "data": "iVBORw0KGgo..."
                    }
                },
                {"type": "text", "text": "What animal is this?"}
            ]
        )
        assert msg.is_multimodal()
        assert msg.has_images()
        assert msg.count_images() == 1
        assert msg.get_text_content() == "What animal is this?"
    
    def test_audio_message(self):
        """Test message with audio content."""
        msg = ChatMessage(
            role="user",
            content=[
                {"type": "text", "text": "Transcribe this audio"},
                {
                    "type": "audio",
                    "source": {
                        "type": "base64",
                        "media_type": "audio/wav",
                        "data": "audiodata"
                    }
                }
            ]
        )
        assert msg.is_multimodal()
        assert not msg.has_images()
        assert msg.count_images() == 0
    
    def test_video_message(self):
        """Test message with video content."""
        msg = ChatMessage(
            role="user",
            content=[
                {"type": "text", "text": "Analyze this video"},
                {
                    "type": "video",
                    "source": {
                        "type": "base64",
                        "media_type": "video/mp4",
                        "data": "videodata"
                    }
                }
            ]
        )
        assert msg.is_multimodal()
        assert not msg.has_images()


class TestMessageUtilityMethods:
    """Test utility methods on ChatMessage."""
    
    def test_get_text_content_from_string(self):
        """Test get_text_content with string content."""
        msg = ChatMessage(role="user", content="Hello world")
        assert msg.get_text_content() == "Hello world"
    
    def test_get_text_content_from_list(self):
        """Test get_text_content with list content."""
        msg = ChatMessage(
            role="user",
            content=[
                {"type": "text", "text": "First part"},
                {"type": "image_url", "image_url": "url"},
                {"type": "text", "text": "Second part"}
            ]
        )
        assert msg.get_text_content() == "First part Second part"
    
    def test_get_text_content_empty(self):
        """Test get_text_content with no content."""
        msg = ChatMessage(role="assistant", content=None)
        assert msg.get_text_content() == ""
    
    def test_is_multimodal_text_only(self):
        """Test is_multimodal returns False for text-only."""
        msg = ChatMessage(role="user", content="Just text")
        assert not msg.is_multimodal()
    
    def test_is_multimodal_with_image(self):
        """Test is_multimodal returns True with image."""
        msg = ChatMessage(
            role="user",
            content=[
                {"type": "text", "text": "Text"},
                {"type": "image_url", "image_url": "url"}
            ]
        )
        assert msg.is_multimodal()
    
    def test_has_images_false(self):
        """Test has_images returns False when no images."""
        msg = ChatMessage(role="user", content="No images here")
        assert not msg.has_images()
    
    def test_has_images_true(self):
        """Test has_images returns True with images."""
        msg = ChatMessage(
            role="user",
            content=[
                {"type": "image_url", "image_url": "url"}
            ]
        )
        assert msg.has_images()
    
    def test_count_images_zero(self):
        """Test count_images with no images."""
        msg = ChatMessage(role="user", content="Text only")
        assert msg.count_images() == 0
    
    def test_count_images_multiple(self):
        """Test count_images with multiple images."""
        msg = ChatMessage(
            role="user",
            content=[
                {"type": "image_url", "image_url": "url1"},
                {"type": "text", "text": "Between"},
                {"type": "image", "source": {"type": "base64", "data": "data"}},
                {"type": "image_url", "image_url": "url2"}
            ]
        )
        assert msg.count_images() == 3


class TestMessageSerialization:
    """Test message serialization/deserialization."""
    
    def test_serialize_text_message(self):
        """Test serializing text-only message."""
        msg = ChatMessage(role="user", content="Hello")
        data = msg.model_dump()
        assert data["role"] == "user"
        assert data["content"] == "Hello"
    
    def test_serialize_multimodal_message(self):
        """Test serializing multimodal message."""
        msg = ChatMessage(
            role="user",
            content=[
                {"type": "text", "text": "Question"},
                {"type": "image_url", "image_url": "url"}
            ]
        )
        data = msg.model_dump()
        assert data["role"] == "user"
        assert isinstance(data["content"], list)
        assert len(data["content"]) == 2
    
    def test_deserialize_text_message(self):
        """Test deserializing text-only message."""
        data = {"role": "user", "content": "Hello"}
        msg = ChatMessage(**data)
        assert msg.role == "user"
        assert msg.content == "Hello"
    
    def test_deserialize_multimodal_message(self):
        """Test deserializing multimodal message."""
        data = {
            "role": "user",
            "content": [
                {"type": "text", "text": "Question"},
                {"type": "image_url", "image_url": "https://example.com/img.jpg"}
            ]
        }
        msg = ChatMessage(**data)
        assert msg.role == "user"
        assert isinstance(msg.content, list)
        assert msg.is_multimodal()
