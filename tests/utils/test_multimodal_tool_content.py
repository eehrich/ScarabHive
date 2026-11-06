"""Tests for multimodal tool content utilities."""

import base64
import json
import logging
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_system.utils.multimodal_tool_content import (
    create_multimodal_injection,
    encode_multimodal_item,
    create_injection_message_content,
    create_gemini_parts,
    EncodedMultimodalContent,
)


class TestAnEarlierTurnKeepsTheImageItsToolReturned:
    """Every request encodes the history's media again from disk. A tool that
    rendered again to the same path showed the newer image in the older turn
    too, a deleted file turned the turn into "not found", and the prompt cache
    broke there each time."""

    @pytest.fixture(autouse=True)
    def frozen(self, monkeypatch):
        from collections import OrderedDict
        from agent_system.utils import multimodal_tool_content as mtc
        monkeypatch.setattr(mtc, "_FROZEN", OrderedDict())
        monkeypatch.setattr(mtc, "_frozen_size", 0)

    def _tool_message(self, image):
        from agent_system.llm.models import ChatMessage
        from agent_system.servers.agent.components.tool_call_contract import pop_multimodal_content
        items = pop_multimodal_content(
            {"_multimodal_content": [{"type": "image", "path": str(image), "mime_type": "image/png"}]}, "render")
        assert items and items[0].sha256, "the tool result recorded no content hash"
        return ChatMessage(role="tool", tool_call_id="c1", name="render", content="ok",
                           multimodal_content=items)

    def _images(self, msg):
        injection = create_multimodal_injection(msg, supports_vision=True)
        return json.dumps(injection["content"])

    def test_a_rerender_to_the_same_path_does_not_reach_the_older_turn(self, tmp_path):
        image = tmp_path / "cover.png"
        image.write_bytes(b"\x89PNG first render")
        msg = self._tool_message(image)
        first = self._images(msg)
        image.write_bytes(b"\x89PNG second render, another picture")
        assert self._images(msg) == first

    def test_a_deleted_file_still_shows_in_its_turn(self, tmp_path):
        image = tmp_path / "cover.png"
        image.write_bytes(b"\x89PNG first render")
        msg = self._tool_message(image)
        first = self._images(msg)
        image.unlink()
        assert self._images(msg) == first

    def test_a_file_changed_before_its_first_request_is_not_shown(self, tmp_path):
        """Not frozen yet (a restart in between): the new picture would still be the wrong one."""
        image = tmp_path / "cover.png"
        image.write_bytes(b"\x89PNG first render")
        msg = self._tool_message(image)
        image.write_bytes(b"\x89PNG another picture")
        assert create_multimodal_injection(msg, supports_vision=True) is None

    def test_a_frozen_file_still_counts_in_the_context(self, tmp_path):
        """Counted as 0 it was never compacted although every request carries it."""
        from agent_system.llm.token_utils import TOKENS_PER_IMAGE, estimate_media_tokens
        image = tmp_path / "cover.png"
        image.write_bytes(b"\x89PNG first render")
        msg = self._tool_message(image)
        self._images(msg)
        image.unlink()
        item = msg.multimodal_content[0]
        assert estimate_media_tokens(item.model_dump()) == TOKENS_PER_IMAGE
        assert estimate_media_tokens(item) == TOKENS_PER_IMAGE

    def test_a_model_without_vision_is_not_sent_to_a_file_that_is_gone(self, tmp_path):
        image = tmp_path / "cover.png"
        image.write_bytes(b"\x89PNG first render")
        msg = self._tool_message(image)
        self._images(msg)
        image.unlink()
        note = create_multimodal_injection(msg, supports_vision=False)["content"]
        assert "Missing files" in note and f"File not found: {image}" in note

    def test_a_file_too_large_to_send_is_not_hashed(self, tmp_path, monkeypatch):
        from agent_system.utils import multimodal_tool_content as mtc
        monkeypatch.setattr(mtc, "DEFAULT_MAX_IMAGE_SIZE_MB", 0.00001)
        image = tmp_path / "big.png"
        image.write_bytes(b"\x89PNG" + b"x" * 100)
        assert mtc.content_sha256(image, "image") is None
        image.write_bytes(b"\x89PNG")
        assert mtc.content_sha256(image, "image")


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


HOST_PATHS = [r"\\evil.example\share\x.png", r"\\?\UNC\evil.example\share\x.png",
              r"\??\UNC\evil.example\share\x.png", "//evil.example/share/x.png"]


@pytest.fixture
def touched(monkeypatch):
    """Every path the file system is asked about -- exists(), stat(), open, read."""
    seen: list = []

    def spy(owner, name):
        real = getattr(owner, name)

        def wrapper(target, *args, **kwargs):
            seen.append(str(target))
            return real(target, *args, **kwargs)
        monkeypatch.setattr(owner, name, wrapper)

    for name in ("exists", "stat", "open", "read_bytes"):
        spy(Path, name)
    spy(os, "stat")
    return seen


class TestHostPathsAreNeverTouched:
    """exists()/stat() on a share makes Windows sign in to the host (NTLM leak),
    and this runs on every LLM request for every path in the history."""

    @pytest.mark.parametrize("hostile", HOST_PATHS)
    def test_encode_skips_a_host_path_without_stat(self, touched, hostile):
        item = {"type": "image", "path": hostile, "mime_type": "image/png"}
        assert encode_multimodal_item(item) is None
        assert not [p for p in touched if "evil" in p]

    @pytest.mark.parametrize("supports_vision", [True, False])
    @pytest.mark.parametrize("hostile", HOST_PATHS)
    def test_injection_skips_a_host_path_and_keeps_the_local_file(
            self, tmp_path, touched, caplog, hostile, supports_vision):
        image = tmp_path / "ok.png"
        image.write_bytes(b"\x89PNG\r\n\x1a\nlocal")
        msg = SimpleNamespace(name="shot", tool_call_id="c1", multimodal_content=[
            {"type": "image", "path": hostile, "mime_type": "image/png"},
            {"type": "image", "path": str(image), "mime_type": "image/png"},
        ])

        with caplog.at_level(logging.WARNING):
            result = create_multimodal_injection(msg, supports_vision=supports_vision)

        assert not [p for p in touched if "evil" in p]
        assert str(image) in touched, "spy saw nothing -- the test would be vacuous"
        refused = [r for r in caplog.records if "evil" in r.getMessage()]
        assert len(refused) == 1 and refused[0].levelno == logging.WARNING
        text = json.dumps(result)
        assert "not found" in text  # reported like a missing file
        if supports_vision:
            images = [c for c in result["content"] if c.get("type") == "image_url"]
            assert len(images) == 1
            assert base64.b64encode(image.read_bytes()).decode() in images[0]["image_url"]["url"]

    def test_a_local_file_still_encodes_under_the_spy(self, tmp_path, touched):
        image = tmp_path / "ok.png"
        image.write_bytes(b"\x89PNG\r\n\x1a\nlocal")
        result = encode_multimodal_item({"type": "image", "path": str(image), "mime_type": "image/png"})
        assert result is not None
        assert base64.b64decode(result.data) == image.read_bytes()
        assert str(image) in touched


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
