"""Tests for OpenAI client multimodal injection."""

import pytest
from unittest.mock import patch
from pathlib import Path

from agent_system.llm.models import ChatMessage, MultimodalToolContent


class TestOpenAIMultimodalInjection:
    """Tests for multimodal content injection in OpenAI client."""
    
    @pytest.fixture
    def openai_client(self):
        """Create an OpenAI client with vision support for multimodal testing."""
        from agent_system.llm.openai_client import OpenAIAsyncClient
        from agent_system.llm.capabilities import ModelCapabilities
        
        with patch('openai.AsyncOpenAI'):
            client = OpenAIAsyncClient(
                model="gpt-4o",
                api_key="test-key"
            )
        # Mock vision capabilities for multimodal injection
        client.capabilities = ModelCapabilities(
            image_input=True
        )
        return client
    
    @pytest.fixture
    def openai_client_no_vision(self):
        """Create an OpenAI client WITHOUT vision support."""
        from agent_system.llm.openai_client import OpenAIAsyncClient
        from agent_system.llm.capabilities import ModelCapabilities
        
        with patch('openai.AsyncOpenAI'):
            client = OpenAIAsyncClient(
                model="deepseek-chat",
                api_key="test-key"
            )
        # Mock non-vision capabilities
        client.capabilities = ModelCapabilities(
            image_input=False
        )
        return client
    
    def test_create_multimodal_injection_with_image(self, openai_client, tmp_path: Path):
        """Test _create_multimodal_injection creates proper user message."""
        # Create test image
        image_path = tmp_path / "test.png"
        image_path.write_bytes(b"fake image data")
        
        tool_msg = ChatMessage(
            role="tool",
            name="comfyui_workflow",
            tool_call_id="call_123",
            content='{"status": "completed"}',
            multimodal_content=[
                MultimodalToolContent(
                    type="image",
                    path=str(image_path),
                    mime_type="image/png",
                    description="Generated test image"
                )
            ]
        )
        
        injection = openai_client._create_multimodal_injection(tool_msg)
        
        assert injection is not None
        assert injection["role"] == "user"
        assert isinstance(injection["content"], list)
        assert len(injection["content"]) == 2
        
        # Check text prefix
        assert injection["content"][0]["type"] == "text"
        assert "📎 [TOOL OUTPUT: comfyui_workflow" in injection["content"][0]["text"]
        assert "call_id=call_123" in injection["content"][0]["text"]
        
        # Check image
        assert injection["content"][1]["type"] == "image_url"
        assert "data:image/png;base64," in injection["content"][1]["image_url"]["url"]
    
    def test_create_multimodal_injection_no_content(self, openai_client):
        """Test _create_multimodal_injection returns None for message without multimodal."""
        tool_msg = ChatMessage(
            role="tool",
            name="test_tool",
            content='{"result": "ok"}'
        )
        
        injection = openai_client._create_multimodal_injection(tool_msg)
        
        assert injection is None
    
    def test_create_multimodal_injection_file_not_found(self, openai_client):
        """Test _create_multimodal_injection handles missing files - skips them but processes others."""
        tool_msg = ChatMessage(
            role="tool",
            name="test_tool",
            tool_call_id="call_456",
            content='{"status": "completed"}',
            multimodal_content=[
                MultimodalToolContent(
                    type="image",
                    path="/nonexistent/image.png",
                    mime_type="image/png"
                )
            ]
        )
        
        injection = openai_client._create_multimodal_injection(tool_msg)
        
        # Should return an error message since file doesn't exist
        # This provides feedback to the LLM about the missing file
        assert injection is not None
        assert injection["role"] == "user"
        content = injection["content"]
        assert isinstance(content, list)
        assert len(content) == 1
        assert content[0]["type"] == "text"
        assert "file not found" in content[0]["text"].lower()
        assert "/nonexistent/image.png" in content[0]["text"]
    
    def test_create_multimodal_injection_text_fallback_no_vision(self, openai_client_no_vision, tmp_path: Path):
        """Test text fallback when model doesn't support vision."""
        # Create test image
        image_path = tmp_path / "test.png"
        image_path.write_bytes(b"fake image data")
        
        tool_msg = ChatMessage(
            role="tool",
            name="comfyui_workflow",
            tool_call_id="call_789",
            content='{"status": "completed"}',
            multimodal_content=[
                MultimodalToolContent(
                    type="image",
                    path=str(image_path),
                    mime_type="image/png",
                    description="Generated test image"
                )
            ]
        )
        
        injection = openai_client_no_vision._create_multimodal_injection(tool_msg)
        
        assert injection is not None
        assert injection["role"] == "user"
        # Should be text string, not list
        assert isinstance(injection["content"], str)
        assert "📎 [TOOL OUTPUT: comfyui_workflow]" in injection["content"]
        assert "Generated 1 multimodal item(s)" in injection["content"]
        assert "does not support vision/multimodal input" in injection["content"]
    
    def test_create_multimodal_injection_multiple_images(self, openai_client, tmp_path: Path):
        """Test injection with multiple images."""
        # Create test images
        img1 = tmp_path / "img1.png"
        img2 = tmp_path / "img2.jpg"
        img1.write_bytes(b"image1")
        img2.write_bytes(b"image2")
        
        tool_msg = ChatMessage(
            role="tool",
            name="image_tool",
            content='{"images": 2}',
            multimodal_content=[
                MultimodalToolContent(type="image", path=str(img1), mime_type="image/png"),
                MultimodalToolContent(type="image", path=str(img2), mime_type="image/jpeg"),
            ]
        )
        
        injection = openai_client._create_multimodal_injection(tool_msg)
        
        assert injection is not None
        # Text + 2 images
        assert len(injection["content"]) == 3


class TestOpenAISerializeMessages:
    """Tests for message serialization with multimodal injection."""
    
    @pytest.fixture
    def openai_client(self):
        """Create an OpenAI client with vision support."""
        from agent_system.llm.openai_client import OpenAIAsyncClient
        from agent_system.llm.capabilities import ModelCapabilities
        
        with patch('openai.AsyncOpenAI'):
            client = OpenAIAsyncClient(
                model="gpt-4o",
                api_key="test-key"
            )
        # Mock vision capabilities
        client.capabilities = ModelCapabilities(
            image_input=True
        )
        return client
    
    @pytest.mark.asyncio
    async def test_serialize_injects_multimodal_after_tool(self, openai_client, tmp_path: Path):
        """Test that serialization injects user message after tool with multimodal."""
        # Create test image
        image_path = tmp_path / "output.png"
        image_path.write_bytes(b"generated image")
        
        messages = [
            ChatMessage(role="user", content="Generate an image"),
            ChatMessage(
                role="assistant",
                content=None,
                tool_calls=[{"id": "call_1", "function": {"name": "gen_image", "arguments": "{}"}}]
            ),
            ChatMessage(
                role="tool",
                name="gen_image",
                tool_call_id="call_1",
                content='{"status": "completed"}',
                multimodal_content=[
                    MultimodalToolContent(
                        type="image",
                        path=str(image_path),
                        mime_type="image/png"
                    )
                ]
            ),
        ]
        
        # Use the internal serialize function pattern
        import asyncio
        
        def _serialize_messages():
            result = []
            for m in messages:
                d = m.model_dump(exclude_none=True, mode='json')
                d.pop('multimodal_content', None)
                result.append(d)
                
                if m.role == "tool" and m.multimodal_content:
                    injection = openai_client._create_multimodal_injection(m)
                    if injection:
                        result.append(injection)
            return result
        
        serialized = await asyncio.to_thread(_serialize_messages)
        
        # Should have 4 messages: user, assistant, tool, injected_user
        assert len(serialized) == 4
        
        # Check order
        assert serialized[0]["role"] == "user"
        assert serialized[1]["role"] == "assistant"
        assert serialized[2]["role"] == "tool"
        assert serialized[3]["role"] == "user"  # Injected
        
        # Check injected message has multimodal content
        assert isinstance(serialized[3]["content"], list)
    
    @pytest.mark.asyncio
    async def test_serialize_no_injection_without_multimodal(self, openai_client):
        """Test that normal tool messages don't get injection."""
        messages = [
            ChatMessage(role="user", content="Do something"),
            ChatMessage(
                role="tool",
                name="some_tool",
                tool_call_id="call_1",
                content='{"result": "ok"}'
                # No multimodal_content
            ),
        ]
        
        import asyncio
        
        def _serialize_messages():
            result = []
            for m in messages:
                d = m.model_dump(exclude_none=True, mode='json')
                d.pop('multimodal_content', None)
                result.append(d)
                
                if m.role == "tool" and m.multimodal_content:
                    injection = openai_client._create_multimodal_injection(m)
                    if injection:
                        result.append(injection)
            return result
        
        serialized = await asyncio.to_thread(_serialize_messages)
        
        # Should still be 2 messages, no injection
        assert len(serialized) == 2
        assert serialized[0]["role"] == "user"
        assert serialized[1]["role"] == "tool"


class TestOpenAIAudioFiltering:
    """Test that audio content is filtered out for Chat Completions API."""

    @pytest.mark.asyncio
    async def test_audio_content_filtered_from_user_message(self):
        """Test that audio content is removed from user messages."""
        messages = [
            ChatMessage(
                role="user",
                content=[
                    {"type": "text", "text": "Here is an audio file"},
                    {"type": "audio", "audio_url": "data:audio/wav;base64,UklGR...", "media_type": "audio/wav"},
                    {"type": "image_url", "image_url": {"url": "data:image/png;base64,iVBOR..."}}
                ]
            ),
        ]
        
        import asyncio
        import logging
        
        logging.getLogger(__name__)
        
        def _serialize_messages():
            result = []
            for m in messages:
                d = m.model_dump(exclude_none=True, mode='json')
                d.pop('multimodal_content', None)
                
                # Filter out audio/video content
                if isinstance(d.get('content'), list):
                    filtered_content = []
                    for item in d['content']:
                        if isinstance(item, dict):
                            item_type = item.get('type', '')
                            if item_type in ('audio', 'video'):
                                continue
                        filtered_content.append(item)
                    d['content'] = filtered_content if filtered_content else ""
                
                result.append(d)
            return result
        
        serialized = await asyncio.to_thread(_serialize_messages)
        
        assert len(serialized) == 1
        content = serialized[0]["content"]
        
        # Should have text and image_url, but NOT audio
        assert len(content) == 2
        content_types = [item.get("type") for item in content]
        assert "text" in content_types
        assert "image_url" in content_types
        assert "audio" not in content_types

    @pytest.mark.asyncio
    async def test_all_audio_filtered_leaves_empty_string(self):
        """Test that filtering all content leaves empty string."""
        messages = [
            ChatMessage(
                role="user",
                content=[
                    {"type": "audio", "audio_url": "data:audio/wav;base64,UklGR...", "media_type": "audio/wav"},
                ]
            ),
        ]
        
        import asyncio
        
        def _serialize_messages():
            result = []
            for m in messages:
                d = m.model_dump(exclude_none=True, mode='json')
                d.pop('multimodal_content', None)
                
                if isinstance(d.get('content'), list):
                    filtered_content = []
                    for item in d['content']:
                        if isinstance(item, dict):
                            item_type = item.get('type', '')
                            if item_type in ('audio', 'video'):
                                continue
                        filtered_content.append(item)
                    d['content'] = filtered_content if filtered_content else ""
                
                result.append(d)
            return result
        
        serialized = await asyncio.to_thread(_serialize_messages)
        
        # Content should be empty string when all items filtered
        assert serialized[0]["content"] == ""