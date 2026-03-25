"""Tests for Anthropic Claude client."""
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from agent_system.llm.models import ChatMessage


@pytest.fixture
def anthropic_client():
    """Create an AnthropicAsyncClient instance for testing."""
    # Import inside the patch context to ensure module-level patch works
    with patch('anthropic.AsyncAnthropic') as mock_anthropic:
        mock_instance = MagicMock()
        mock_anthropic.return_value = mock_instance
        
        from agent_system.llm.anthropic_client import AnthropicAsyncClient
        
        client = AnthropicAsyncClient(
            model="claude-sonnet-4-20250514",
            api_key="test-api-key",
            context_window=200000,
            request_timeout=180
        )
        client._mock = mock_instance
        return client


class TestAnthropicClientMessageConversion:
    """Test message format conversion from ChatMessage to Anthropic format."""

    def test_convert_simple_user_message(self, anthropic_client):
        """Test conversion of simple user message."""
        messages = [
            ChatMessage(role="user", content="Hello, how are you?")
        ]
        
        system_prompt, converted = anthropic_client._convert_messages(messages)
        
        assert system_prompt is None
        assert len(converted) == 1
        assert converted[0]["role"] == "user"
        assert converted[0]["content"] == "Hello, how are you?"

    def test_convert_system_message(self, anthropic_client):
        """Test that system messages are extracted with cache_control."""
        messages = [
            ChatMessage(role="system", content="You are a helpful assistant."),
            ChatMessage(role="user", content="Hello")
        ]
        
        system_prompt, converted = anthropic_client._convert_messages(messages)
        
        # With enable_prompt_caching=True (default), system prompt becomes cached content blocks
        assert isinstance(system_prompt, list)
        assert len(system_prompt) == 1
        assert system_prompt[0]["type"] == "text"
        assert system_prompt[0]["text"] == "You are a helpful assistant."
        assert system_prompt[0]["cache_control"] == {"type": "ephemeral"}
        assert len(converted) == 1  # Only user message
        assert converted[0]["role"] == "user"
        assert converted[0]["content"] == "Hello"

    def test_convert_multiple_system_messages(self, anthropic_client):
        """Test that multiple system messages are concatenated with cache_control."""
        messages = [
            ChatMessage(role="system", content="You are helpful."),
            ChatMessage(role="system", content="Be concise."),
            ChatMessage(role="user", content="Hello")
        ]
        
        system_prompt, converted = anthropic_client._convert_messages(messages)
        
        assert isinstance(system_prompt, list)
        assert len(system_prompt) == 1
        assert system_prompt[0]["text"] == "You are helpful.\nBe concise."
        assert system_prompt[0]["cache_control"] == {"type": "ephemeral"}
        assert len(converted) == 1

    def test_convert_assistant_message(self, anthropic_client):
        """Test conversion of assistant message."""
        messages = [
            ChatMessage(role="assistant", content="I'm doing well, thank you!")
        ]
        
        system_prompt, converted = anthropic_client._convert_messages(messages)
        
        assert len(converted) == 1
        assert converted[0]["role"] == "assistant"
        assert converted[0]["content"] == "I'm doing well, thank you!"

    def test_convert_tool_response(self, anthropic_client):
        """Test conversion of tool response message."""
        messages = [
            ChatMessage(
                role="tool",
                content='{"temperature": 22, "condition": "sunny"}',
                tool_call_id="toolu_123",
                name="get_weather"
            )
        ]
        
        system_prompt, converted = anthropic_client._convert_messages(messages)
        
        assert len(converted) == 1
        assert converted[0]["role"] == "user"  # Tool results go to user role
        assert len(converted[0]["content"]) == 1
        assert converted[0]["content"][0]["type"] == "tool_result"
        assert converted[0]["content"][0]["tool_use_id"] == "toolu_123"

    def test_convert_assistant_with_tool_calls(self, anthropic_client):
        """Test conversion of assistant message with tool calls."""
        messages = [
            ChatMessage(
                role="assistant",
                content="Let me check the weather.",
                tool_calls=[
                    {
                        "id": "toolu_123",
                        "type": "function",
                        "function": {
                            "name": "get_weather",
                            "arguments": '{"location": "Paris"}'
                        }
                    }
                ]
            )
        ]
        
        system_prompt, converted = anthropic_client._convert_messages(messages)
        
        assert len(converted) == 1
        assert converted[0]["role"] == "assistant"
        content_blocks = converted[0]["content"]
        assert len(content_blocks) == 2
        
        # First block is text
        assert content_blocks[0]["type"] == "text"
        assert content_blocks[0]["text"] == "Let me check the weather."
        
        # Second block is tool_use
        assert content_blocks[1]["type"] == "tool_use"
        assert content_blocks[1]["id"] == "toolu_123"
        assert content_blocks[1]["name"] == "get_weather"
        assert content_blocks[1]["input"] == {"location": "Paris"}


class TestAnthropicClientToolConversion:
    """Test tool schema conversion to Anthropic format."""

    def test_convert_simple_tool(self, anthropic_client):
        """Test conversion of simple OpenAI tool schema."""
        tools = [
            {
                "type": "function",
                "function": {
                    "name": "get_weather",
                    "description": "Get weather for a location",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "location": {
                                "type": "string",
                                "description": "City name"
                            }
                        },
                        "required": ["location"]
                    }
                }
            }
        ]
        
        anthropic_tools = anthropic_client._convert_tools(tools)
        
        assert len(anthropic_tools) == 1
        assert anthropic_tools[0]["name"] == "get_weather"
        assert anthropic_tools[0]["description"] == "Get weather for a location"
        assert "input_schema" in anthropic_tools[0]
        assert anthropic_tools[0]["input_schema"]["type"] == "object"

    def test_convert_multiple_tools(self, anthropic_client):
        """Test conversion of multiple tools."""
        tools = [
            {
                "type": "function",
                "function": {
                    "name": "tool1",
                    "description": "First tool",
                    "parameters": {"type": "object", "properties": {}}
                }
            },
            {
                "type": "function",
                "function": {
                    "name": "tool2",
                    "description": "Second tool",
                    "parameters": {"type": "object", "properties": {}}
                }
            }
        ]
        
        anthropic_tools = anthropic_client._convert_tools(tools)
        
        assert len(anthropic_tools) == 2
        assert anthropic_tools[0]["name"] == "tool1"
        assert anthropic_tools[1]["name"] == "tool2"


class TestAnthropicImageConversion:
    """Test image content conversion using anthropic_utils."""

    def test_convert_base64_image(self):
        """Test conversion of base64 image."""
        from agent_system.llm import anthropic_utils
        
        item = {
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": "image/png",
                "data": "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
            }
        }
        
        result = anthropic_utils._convert_image_content(item)
        
        assert result["type"] == "image"
        assert result["source"]["type"] == "base64"
        assert result["source"]["media_type"] == "image/png"

    def test_convert_data_url_image(self):
        """Test conversion of data URL image."""
        from agent_system.llm import anthropic_utils
        
        item = {
            "type": "image_url",
            "image_url": {
                "url": "data:image/jpeg;base64,/9j/4AAQSkZJRgABAQAAAQABAAD"
            }
        }
        
        result = anthropic_utils._convert_image_content(item)
        
        assert result["type"] == "image"
        assert result["source"]["type"] == "base64"
        assert result["source"]["media_type"] == "image/jpeg"

    def test_convert_url_image(self):
        """Test conversion of URL image."""
        from agent_system.llm import anthropic_utils
        
        item = {
            "type": "image_url",
            "image_url": {
                "url": "https://example.com/image.png"
            }
        }
        
        result = anthropic_utils._convert_image_content(item)
        
        assert result["type"] == "image"
        assert result["source"]["type"] == "url"
        assert result["source"]["url"] == "https://example.com/image.png"


class TestAnthropicClientUsageExtraction:
    """Test usage info extraction."""

    def test_extract_basic_usage(self, anthropic_client):
        """Test extraction of basic usage info."""
        usage = MagicMock()
        usage.input_tokens = 100
        usage.output_tokens = 50
        
        result = anthropic_client._extract_usage(usage)
        
        assert result["prompt_tokens"] == 100
        assert result["completion_tokens"] == 50
        assert result["total_tokens"] == 150

    def test_extract_cached_usage(self, anthropic_client):
        """Test extraction of cached token usage."""
        usage = MagicMock()
        usage.input_tokens = 100
        usage.output_tokens = 50
        usage.cache_read_input_tokens = 80
        usage.cache_creation_input_tokens = 20
        
        result = anthropic_client._extract_usage(usage)
        
        assert result["prompt_tokens"] == 100
        assert result["prompt_tokens_details"]["cached_tokens"] == 80
        assert result["prompt_tokens_details"]["cache_creation_tokens"] == 20


class TestAnthropicClientStreaming:
    """Test streaming chat functionality."""

    @pytest.mark.asyncio
    async def test_streaming_text_response(self, anthropic_client):
        """Test streaming text response."""
        # Create mock stream events
        text_event = MagicMock()
        text_event.type = "content_block_delta"
        text_event.delta = MagicMock()
        text_event.delta.type = "text_delta"
        text_event.delta.text = "Hello"
        
        final_message = MagicMock()
        final_message.usage = MagicMock()
        final_message.usage.input_tokens = 10
        final_message.usage.output_tokens = 5
        
        # Create async iterator helper
        async def async_iter_events():
            yield text_event
        
        # Create mock stream context manager
        mock_stream = MagicMock()
        mock_stream.__aenter__ = AsyncMock(return_value=mock_stream)
        mock_stream.__aexit__ = AsyncMock(return_value=None)
        mock_stream.__aiter__ = lambda self: async_iter_events()
        mock_stream.get_final_message = AsyncMock(return_value=final_message)
        
        anthropic_client._client.messages.stream = MagicMock(return_value=mock_stream)
        
        messages = [ChatMessage(role="user", content="Hi")]
        tools = []
        
        chunks = []
        async for chunk in anthropic_client.chat_tools_streaming(messages, tools):
            chunks.append(chunk)
        
        # Should have content delta and final
        assert any(c.get("type") == "content_delta" for c in chunks)
        assert any(c.get("type") == "final" for c in chunks)


class TestAnthropicClientSchemaClean:
    """Test JSON schema cleaning."""

    def test_clean_schema_removes_unsupported(self, anthropic_client):
        """Test that unsupported schema fields are removed."""
        schema = {
            "type": "object",
            "properties": {
                "name": {"type": "string"}
            },
            "additionalProperties": False,  # Should be removed
            "$schema": "http://json-schema.org/draft-07/schema#"  # Should be removed
        }
        
        cleaned = anthropic_client._clean_schema(schema)
        
        assert "additionalProperties" not in cleaned
        assert "$schema" not in cleaned
        assert cleaned["type"] == "object"
        assert "properties" in cleaned

    def test_clean_nested_schema(self, anthropic_client):
        """Test cleaning of nested schemas."""
        schema = {
            "type": "object",
            "properties": {
                "items": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "additionalProperties": True
                    }
                }
            }
        }
        
        cleaned = anthropic_client._clean_schema(schema)
        
        # Check nested additionalProperties is removed
        assert "additionalProperties" not in cleaned["properties"]["items"]["items"]


class TestAnthropicPromptCaching:
    """Test prompt caching cache_control injection."""

    @pytest.fixture
    def client_no_caching(self):
        """Create an AnthropicAsyncClient with prompt caching disabled."""
        with patch('anthropic.AsyncAnthropic') as mock_anthropic:
            mock_anthropic.return_value = MagicMock()
            from agent_system.llm.anthropic_client import AnthropicAsyncClient
            return AnthropicAsyncClient(
                model="claude-sonnet-4-20250514",
                api_key="test-api-key",
                enable_prompt_caching=False,
            )

    def test_system_prompt_cached_by_default(self, anthropic_client):
        """System prompt becomes content block with cache_control when caching enabled."""
        messages = [
            ChatMessage(role="system", content="You are helpful."),
            ChatMessage(role="user", content="Hi"),
        ]
        system_prompt, _ = anthropic_client._convert_messages(messages)
        assert isinstance(system_prompt, list)
        assert system_prompt[0]["cache_control"] == {"type": "ephemeral"}

    def test_system_prompt_plain_when_caching_disabled(self, client_no_caching):
        """System prompt stays a plain string when caching is disabled."""
        messages = [
            ChatMessage(role="system", content="You are helpful."),
            ChatMessage(role="user", content="Hi"),
        ]
        system_prompt, _ = client_no_caching._convert_messages(messages)
        assert system_prompt == "You are helpful."

    def test_tool_cache_control_on_last_tool(self, anthropic_client):
        """cache_control is added to the last tool definition only."""
        tools = [
            {"type": "function", "function": {"name": "tool_a", "description": "A", "parameters": {"type": "object", "properties": {}}}},
            {"type": "function", "function": {"name": "tool_b", "description": "B", "parameters": {"type": "object", "properties": {}}}},
        ]
        converted = anthropic_client._convert_tools(tools)
        assert "cache_control" not in converted[0]
        assert converted[-1]["cache_control"] == {"type": "ephemeral"}

    def test_tool_no_cache_control_when_disabled(self, client_no_caching):
        """No cache_control on tools when caching is disabled."""
        tools = [
            {"type": "function", "function": {"name": "tool_a", "description": "A", "parameters": {"type": "object", "properties": {}}}},
        ]
        converted = client_no_caching._convert_tools(tools)
        assert "cache_control" not in converted[0]
