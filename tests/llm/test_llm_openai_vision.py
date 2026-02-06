"""Tests for OpenAI client vision support."""

import pytest
from unittest.mock import Mock, AsyncMock
from agent_system.llm.openai_client import OpenAIAsyncClient
from agent_system.llm.models import ChatMessage


class TestOpenAIVisionMessageFormatting:
    """Test that OpenAI client properly formats vision messages."""
    
    @pytest.fixture
    def mock_openai_client(self):
        """Create a mock OpenAI client."""
        client = OpenAIAsyncClient(
            model="gpt-5",
            api_key="test-key"
        )
        # Mock the internal OpenAI client
        client._client = Mock()
        client._client.chat = Mock()
        client._client.chat.completions = Mock()
        return client
    
    def test_text_only_message_serialization(self, mock_openai_client):
        """Test that text-only messages serialize correctly."""
        msg = ChatMessage(role="user", content="Hello")
        serialized = msg.model_dump()
        
        assert serialized["role"] == "user"
        assert serialized["content"] == "Hello"
        assert "tool_calls" not in serialized or serialized["tool_calls"] is None
    
    def test_multimodal_message_serialization(self):
        """Test that multimodal messages serialize to correct format."""
        msg = ChatMessage(
            role="user",
            content=[
                {"type": "text", "text": "What's in this image?"},
                {
                    "type": "image_url",
                    "image_url": "https://example.com/image.jpg",
                    "detail": "high"
                }
            ]
        )
        serialized = msg.model_dump()
        
        assert serialized["role"] == "user"
        assert isinstance(serialized["content"], list)
        assert len(serialized["content"]) == 2
        
        # Check text content
        assert serialized["content"][0]["type"] == "text"
        assert serialized["content"][0]["text"] == "What's in this image?"
        
        # Check image content
        assert serialized["content"][1]["type"] == "image_url"
        assert serialized["content"][1]["image_url"] == "https://example.com/image.jpg"
        assert serialized["content"][1]["detail"] == "high"
    
    def test_base64_image_message_serialization(self):
        """Test base64 image message serialization."""
        msg = ChatMessage(
            role="user",
            content=[
                {"type": "text", "text": "Describe this"},
                {
                    "type": "image_url",
                    "image_url": "data:image/jpeg;base64,/9j/4AAQSkZJRg=="
                }
            ]
        )
        serialized = msg.model_dump()
        
        assert serialized["content"][1]["type"] == "image_url"
        assert "base64" in serialized["content"][1]["image_url"]
    
    @pytest.mark.asyncio
    async def test_vision_message_passed_to_openai_sdk(self, mock_openai_client):
        """Test that vision messages are correctly passed to OpenAI SDK."""
        # Setup mock response
        mock_response = Mock()
        mock_response.choices = [Mock()]
        mock_response.choices[0].message = Mock()
        mock_response.choices[0].message.content = "This is a cat."
        mock_response.choices[0].message.tool_calls = None
        
        mock_openai_client._client.chat.completions.create = AsyncMock(return_value=mock_response)
        
        # Create vision message
        messages = [
            ChatMessage(
                role="user",
                content=[
                    {"type": "text", "text": "What animal is this?"},
                    {
                        "type": "image_url",
                        "image_url": "https://example.com/cat.jpg"
                    }
                ]
            )
        ]
        
        # Call chat
        result = await mock_openai_client.chat(messages)
        
        # Verify result
        assert result == "This is a cat."
        
        # Verify OpenAI SDK was called with correct format
        mock_openai_client._client.chat.completions.create.assert_called_once()
        call_args = mock_openai_client._client.chat.completions.create.call_args
        
        assert "messages" in call_args[1]
        sent_messages = call_args[1]["messages"]
        assert len(sent_messages) == 1
        assert sent_messages[0]["role"] == "user"
        assert isinstance(sent_messages[0]["content"], list)
        assert len(sent_messages[0]["content"]) == 2
    
    def test_multiple_images_serialization(self):
        """Test serialization of message with multiple images."""
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
        serialized = msg.model_dump()
        
        assert len(serialized["content"]) == 3
        assert serialized["content"][1]["type"] == "image_url"
        assert serialized["content"][2]["type"] == "image_url"
    
    def test_mixed_conversation_serialization(self):
        """Test serialization of conversation with mixed message types."""
        messages = [
            ChatMessage(role="system", content="You are a helpful assistant."),
            ChatMessage(
                role="user",
                content=[
                    {"type": "text", "text": "What's in this image?"},
                    {"type": "image_url", "image_url": "https://example.com/img.jpg"}
                ]
            ),
            ChatMessage(role="assistant", content="This is a photo of a dog."),
            ChatMessage(role="user", content="What breed is it?")
        ]
        
        serialized = [m.model_dump() for m in messages]
        
        # System message
        assert serialized[0]["content"] == "You are a helpful assistant."
        
        # Vision message
        assert isinstance(serialized[1]["content"], list)
        assert len(serialized[1]["content"]) == 2
        
        # Text responses
        assert serialized[2]["content"] == "This is a photo of a dog."
        assert serialized[3]["content"] == "What breed is it?"


class TestOpenAIVisionIntegration:
    """Integration tests for OpenAI vision support."""
    
    def test_vision_message_is_multimodal(self):
        """Test that vision messages are detected as multimodal."""
        msg = ChatMessage(
            role="user",
            content=[
                {"type": "text", "text": "What's this?"},
                {"type": "image_url", "image_url": "url"}
            ]
        )
        
        assert msg.is_multimodal()
        assert msg.has_images()
        assert msg.count_images() == 1
    
    def test_text_message_not_multimodal(self):
        """Test that text-only messages are not multimodal."""
        msg = ChatMessage(role="user", content="Hello")
        
        assert not msg.is_multimodal()
        assert not msg.has_images()
        assert msg.count_images() == 0
    
    def test_extract_text_from_vision_message(self):
        """Test extracting text from vision message."""
        msg = ChatMessage(
            role="user",
            content=[
                {"type": "text", "text": "First part"},
                {"type": "image_url", "image_url": "url"},
                {"type": "text", "text": "Second part"}
            ]
        )
        
        text = msg.get_text_content()
        assert "First part" in text
        assert "Second part" in text


class TestOpenAIVisionCompatibility:
    """Test backward compatibility with existing code."""
    
    def test_existing_text_messages_still_work(self):
        """Test that existing text-only code still works."""
        messages = [
            ChatMessage(role="system", content="You are helpful"),
            ChatMessage(role="user", content="Hello"),
            ChatMessage(role="assistant", content="Hi there")
        ]
        
        # Should serialize without errors
        serialized = [m.model_dump() for m in messages]
        
        assert all(isinstance(m["content"], str) for m in serialized)
        assert serialized[0]["content"] == "You are helpful"
        assert serialized[1]["content"] == "Hello"
        assert serialized[2]["content"] == "Hi there"
    
    def test_tool_call_messages_still_work(self):
        """Test that tool call messages still work."""
        msg = ChatMessage(
            role="assistant",
            content="",
            tool_calls=[
                {
                    "id": "call_123",
                    "type": "function",
                    "function": {
                        "name": "get_weather",
                        "arguments": '{"location": "NYC"}'
                    }
                }
            ]
        )
        
        serialized = msg.model_dump()
        
        assert serialized["role"] == "assistant"
        assert serialized["tool_calls"] is not None
        assert len(serialized["tool_calls"]) == 1
    
    def test_tool_response_messages_still_work(self):
        """Test that tool response messages still work."""
        msg = ChatMessage(
            role="tool",
            content="Weather is 72F",
            tool_call_id="call_123"
        )
        
        serialized = msg.model_dump()
        
        assert serialized["role"] == "tool"
        assert serialized["content"] == "Weather is 72F"
        assert serialized["tool_call_id"] == "call_123"
