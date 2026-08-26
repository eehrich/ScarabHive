"""Tests for multimodal tool content utilities."""

import base64
from pathlib import Path

from agent_system.utils.multimodal_tool_content import (
    encode_multimodal_item,
    create_injection_message_content,
    create_gemini_parts,
    EncodedMultimodalContent,
)


class TestEncodeMultimodalItem:
    """Tests for encode_multimodal_item function."""
    
    def test_encode_image_file(self, tmp_path: Path):
        """Test encoding an image file to base64."""
        # Create a test image file
        image_path = tmp_path / "test_image.png"
        image_data = b"\x89PNG\r\n\x1a\n" + b"fake image data"
        image_path.write_bytes(image_data)
        
        item = {
            "type": "image",
            "path": str(image_path),
            "mime_type": "image/png",
            "description": "Test image"
        }
        
        result = encode_multimodal_item(item)
        
        assert result is not None
        assert result.type == "image"
        assert result.mime_type == "image/png"
        assert result.description == "Test image"
        # Verify base64 encoding
        decoded = base64.b64decode(result.data)
        assert decoded == image_data
    
    def test_encode_audio_file(self, tmp_path: Path):
        """Test encoding an audio file to base64."""
        audio_path = tmp_path / "test_audio.wav"
        audio_data = b"RIFF" + b"\x00" * 100  # Fake WAV header
        audio_path.write_bytes(audio_data)
        
        item = {
            "type": "audio",
            "path": str(audio_path),
            "mime_type": "audio/wav",
            "description": "Test audio"
        }
        
        result = encode_multimodal_item(item)
        
        assert result is not None
        assert result.type == "audio"
        assert result.mime_type == "audio/wav"
    
    def test_encode_file_not_found(self):
        """Test encoding returns None for non-existent file."""
        item = {
            "type": "image",
            "path": "/nonexistent/path/image.png",
            "mime_type": "image/png"
        }
        
        result = encode_multimodal_item(item)
        
        assert result is None
    
    def test_encode_file_too_large(self, tmp_path: Path):
        """Test encoding returns None for file exceeding size limit."""
        large_file = tmp_path / "large.png"
        # Create a 5MB file
        large_file.write_bytes(b"\x00" * (5 * 1024 * 1024))
        
        item = {
            "type": "image",
            "path": str(large_file),
            "mime_type": "image/png"
        }
        
        # With 1MB limit
        result = encode_multimodal_item(item, max_size_mb=1.0)
        
        assert result is None
    
    def test_encode_with_pydantic_model(self, tmp_path: Path):
        """Test encoding works with Pydantic model input."""
        from agent_system.llm.models import MultimodalToolContent
        
        image_path = tmp_path / "test.png"
        image_path.write_bytes(b"fake image")
        
        item = MultimodalToolContent(
            type="image",
            path=str(image_path),
            mime_type="image/png",
            description="Pydantic model test"
        )
        
        result = encode_multimodal_item(item)
        
        assert result is not None
        assert result.description == "Pydantic model test"


class TestCreateInjectionMessageContent:
    """Tests for create_injection_message_content function."""
    
    def test_create_single_image_injection(self):
        """Test creating injection content with single image."""
        encoded = EncodedMultimodalContent(
            type="image",
            mime_type="image/png",
            data="base64encodeddata",
            description="A test image"
        )
        
        content = create_injection_message_content(
            tool_name="comfyui_workflow",
            tool_call_id="call_123",
            encoded_items=[encoded]
        )
        
        assert len(content) == 2
        # First item is text prefix
        assert content[0]["type"] == "text"
        assert "📎 [TOOL OUTPUT: comfyui_workflow, call_id=call_123]" in content[0]["text"]
        assert "A test image" in content[0]["text"]
        # Second item is image
        assert content[1]["type"] == "image_url"
        assert "data:image/png;base64,base64encodeddata" in content[1]["image_url"]["url"]
    
    def test_create_multiple_images_injection(self):
        """Test creating injection content with multiple images."""
        encoded_items = [
            EncodedMultimodalContent(type="image", mime_type="image/png", data="img1data"),
            EncodedMultimodalContent(type="image", mime_type="image/jpeg", data="img2data"),
        ]
        
        content = create_injection_message_content(
            tool_name="test_tool",
            tool_call_id=None,
            encoded_items=encoded_items
        )
        
        # Text + 2 images
        assert len(content) == 3
        assert content[0]["type"] == "text"
        assert content[1]["type"] == "image_url"
        assert content[2]["type"] == "image_url"
    
    def test_create_audio_injection(self):
        """Test creating injection content with audio."""
        encoded = EncodedMultimodalContent(
            type="audio",
            mime_type="audio/wav",
            data="audiodata"
        )
        
        # Test with supports_audio=True (audio-capable model)
        content = create_injection_message_content(
            tool_name="tts_tool",
            tool_call_id="call_456",
            encoded_items=[encoded],
            supports_audio=True
        )
        
        assert len(content) == 2
        assert content[0]["type"] == "text"
        assert content[1]["type"] == "input_audio"
        assert content[1]["input_audio"]["data"] == "audiodata"
        assert content[1]["input_audio"]["format"] == "wav"
    
    def test_create_audio_injection_without_audio_support(self):
        """Test audio is added as text note when model doesn't support audio."""
        encoded = EncodedMultimodalContent(
            type="audio",
            mime_type="audio/wav",
            data="audiodata",
            description="Test audio"
        )
        
        # Test with supports_audio=False (default - non-audio model)
        content = create_injection_message_content(
            tool_name="tts_tool",
            tool_call_id="call_456",
            encoded_items=[encoded],
            supports_audio=False
        )
        
        assert len(content) == 2
        assert content[0]["type"] == "text"
        assert content[1]["type"] == "text"
        assert "audio input not supported" in content[1]["text"].lower()
    
    def test_create_video_injection_as_text_note(self):
        """Test video is added as text note (not supported by most providers)."""
        encoded = EncodedMultimodalContent(
            type="video",
            mime_type="video/mp4",
            data="videodata",
            description="A video file"
        )
        
        content = create_injection_message_content(
            tool_name="video_tool",
            tool_call_id=None,
            encoded_items=[encoded]
        )
        
        assert len(content) == 2
        assert content[0]["type"] == "text"
        # Video becomes text note
        assert content[1]["type"] == "text"
        assert "[Video file:" in content[1]["text"]


class TestCreateGeminiParts:
    """Tests for create_gemini_parts function (Gemini native format)."""
    
    def test_create_gemini_parts_with_image(self):
        """Test creating Gemini parts with image."""
        encoded = EncodedMultimodalContent(
            type="image",
            mime_type="image/png",
            data="imagebase64"
        )
        
        parts = create_gemini_parts(
            text_content='{"status": "completed"}',
            encoded_items=[encoded]
        )
        
        assert len(parts) == 2
        assert parts[0] == {"text": '{"status": "completed"}'}
        assert parts[1]["inlineData"]["mimeType"] == "image/png"
        assert parts[1]["inlineData"]["data"] == "imagebase64"
    
    def test_create_gemini_parts_multiple_items(self):
        """Test creating Gemini parts with multiple items."""
        encoded_items = [
            EncodedMultimodalContent(type="image", mime_type="image/png", data="img1"),
            EncodedMultimodalContent(type="image", mime_type="image/jpeg", data="img2"),
        ]
        
        parts = create_gemini_parts(
            text_content="Result text",
            encoded_items=encoded_items
        )
        
        assert len(parts) == 3
        assert parts[0] == {"text": "Result text"}


# TestShouldInjectMultimodal is gone with the function it tested: it
# hardcoded a provider list in core code and had no production caller — each
# client calls the injection helper matching its own wire format. The tests
# made the dead code look alive, which is why it survived so long.
