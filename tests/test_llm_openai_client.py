"""Unit tests for OpenAIAsyncClient."""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from agent_system.llm.openai_client import OpenAIAsyncClient
from agent_system.llm.models import ChatMessage


@pytest.fixture
def mock_openai():
    """Mock the AsyncOpenAI class at import time."""
    mock_class = MagicMock()
    mock_instance = MagicMock()
    mock_class.return_value = mock_instance
    
    with patch("openai.AsyncOpenAI", mock_class):
        yield mock_class, mock_instance


@pytest.fixture
def openai_client(mock_openai):
    """Create an OpenAIAsyncClient with mocked OpenAI SDK."""
    mock_class, mock_instance = mock_openai
    client = OpenAIAsyncClient(
        model="gpt-4",
        api_key="test-key",
        base_url="https://api.openai.com/v1"
    )
    return client, mock_instance


class TestOpenAIClientInitialization:
    """Test client initialization and configuration."""

    def test_basic_initialization(self, mock_openai):
        """Test basic client initialization."""
        mock_class, _ = mock_openai
        client = OpenAIAsyncClient(
            model="gpt-4",
            api_key="test-key"
        )
        
        assert client.model == "gpt-4"
        mock_class.assert_called_once()
        call_kwargs = mock_class.call_args[1]
        assert call_kwargs["api_key"] == "test-key"

    def test_initialization_with_base_url(self, mock_openai):
        """Test client initialization with custom base URL."""
        mock_class, _ = mock_openai
        client = OpenAIAsyncClient(
            model="gpt-3.5-turbo",
            api_key="test-key",
            base_url="https://custom.api.com/v1"
        )
        
        assert client.model == "gpt-3.5-turbo"
        call_kwargs = mock_class.call_args[1]
        assert call_kwargs["api_key"] == "test-key"
        assert call_kwargs["base_url"] == "https://custom.api.com/v1"

    def test_initialization_with_timeout(self, mock_openai):
        """Test client initialization with custom timeout."""
        mock_class, _ = mock_openai
        client = OpenAIAsyncClient(
            model="gpt-4",
            api_key="test-key",
            timeout=120.0
        )
        
        call_kwargs = mock_class.call_args[1]
        assert call_kwargs["timeout"] == 120.0

    def test_initialization_with_retry_params(self, mock_openai):
        """Test client initialization with retry parameters."""
        mock_class, _ = mock_openai
        client = OpenAIAsyncClient(
            model="gpt-4",
            api_key="test-key",
            max_attempts=3,
            base_backoff=1.0,
            backoff_cap=60.0
        )
        
        # Client should be created successfully
        assert client.model == "gpt-4"


class TestOpenAIClientChat:
    """Test chat functionality."""

    @pytest.mark.asyncio
    async def test_chat_simple_message(self, openai_client):
        """Test sending a simple chat message."""
        client, mock_instance = openai_client
        
        # Mock the chat completion response
        mock_response = MagicMock()
        mock_response.choices = [MagicMock()]
        mock_response.choices[0].message.content = "Hello! How can I help?"
        mock_response.choices[0].message.tool_calls = None
        
        mock_chat = MagicMock()
        mock_chat.completions = MagicMock()
        mock_chat.completions.create = AsyncMock(return_value=mock_response)
        mock_instance.chat = mock_chat
        
        messages = [
            ChatMessage(role="user", content="Hello")
        ]
        
        result = await client.chat(messages)
        
        assert result == "Hello! How can I help?"
        mock_chat.completions.create.assert_called_once()

    @pytest.mark.asyncio
    async def test_chat_with_system_message(self, openai_client):
        """Test chat with system message."""
        client, mock_instance = openai_client
        
        mock_response = MagicMock()
        mock_response.choices = [MagicMock()]
        mock_response.choices[0].message.content = "I'm a helpful assistant."
        mock_response.choices[0].message.tool_calls = None
        
        mock_chat = MagicMock()
        mock_chat.completions = MagicMock()
        mock_chat.completions.create = AsyncMock(return_value=mock_response)
        mock_instance.chat = mock_chat
        
        messages = [
            ChatMessage(role="system", content="You are a helpful assistant."),
            ChatMessage(role="user", content="Who are you?")
        ]
        
        result = await client.chat(messages)
        
        assert result == "I'm a helpful assistant."
        call_args = mock_chat.completions.create.call_args[1]
        assert len(call_args["messages"]) == 2
        assert call_args["messages"][0]["role"] == "system"


class TestOpenAIClientRetry:
    """Test retry logic."""

    @pytest.mark.asyncio
    async def test_retry_on_rate_limit(self, openai_client):
        """Test retry on rate limit error."""
        client, mock_instance = openai_client
        
        # First call raises RateLimitError, second succeeds
        from openai import RateLimitError
        
        mock_response = MagicMock()
        mock_response.choices = [MagicMock()]
        mock_response.choices[0].message.content = "Success after retry"
        mock_response.choices[0].message.tool_calls = None
        
        mock_chat = MagicMock()
        mock_chat.completions = MagicMock()
        
        # Mock the error
        error_response = MagicMock()
        error_response.status_code = 429
        
        mock_chat.completions.create = AsyncMock(
            side_effect=[
                RateLimitError("Rate limit", response=error_response, body=None),
                mock_response
            ]
        )
        mock_instance.chat = mock_chat
        
        messages = [ChatMessage(role="user", content="Test")]
        
        # Should succeed after retry
        result = await client.chat(messages)
        assert result == "Success after retry"
        assert mock_chat.completions.create.call_count == 2


class TestOpenAIClientMessageMapping:
    """Test message format mapping."""

    @pytest.mark.asyncio
    async def test_message_with_name(self, openai_client):
        """Test message with name field."""
        client, mock_instance = openai_client
        
        mock_response = MagicMock()
        mock_response.choices = [MagicMock()]
        mock_response.choices[0].message.content = "OK"
        mock_response.choices[0].message.tool_calls = None
        
        mock_chat = MagicMock()
        mock_chat.completions = MagicMock()
        mock_chat.completions.create = AsyncMock(return_value=mock_response)
        mock_instance.chat = mock_chat
        
        messages = [
            ChatMessage(role="tool", content="Result", name="get_weather", tool_call_id="call_123")
        ]
        
        await client.chat(messages)
        
        call_args = mock_chat.completions.create.call_args[1]
        sent_message = call_args["messages"][0]
        assert sent_message["role"] == "tool"
        assert sent_message["content"] == "Result"
        assert sent_message["name"] == "get_weather"
        assert sent_message["tool_call_id"] == "call_123"

    @pytest.mark.asyncio
    async def test_message_with_tool_calls(self, openai_client):
        """Test assistant message with tool_calls."""
        client, mock_instance = openai_client
        
        mock_response = MagicMock()
        mock_response.choices = [MagicMock()]
        mock_response.choices[0].message.content = "Done"
        mock_response.choices[0].message.tool_calls = None
        
        mock_chat = MagicMock()
        mock_chat.completions = MagicMock()
        mock_chat.completions.create = AsyncMock(return_value=mock_response)
        mock_instance.chat = mock_chat
        
        tool_calls = [{"id": "call_1", "function": {"name": "test"}}]
        messages = [
            ChatMessage(role="assistant", content=None, tool_calls=tool_calls)
        ]
        
        await client.chat(messages)
        
        call_args = mock_chat.completions.create.call_args[1]
        sent_message = call_args["messages"][0]
        assert sent_message["role"] == "assistant"
        assert sent_message["tool_calls"] == tool_calls


class TestOpenAIClientCancellation:
    """Test cancellation support."""

    @pytest.mark.asyncio
    async def test_cancellation_with_token(self, openai_client):
        """Test chat with cancellation token."""
        client, mock_instance = openai_client
        
        # Create a mock cancellation token with is_cancelled attribute
        cancellation_token = MagicMock()
        cancellation_token.is_cancelled = False
        
        mock_response = MagicMock()
        mock_response.choices = [MagicMock()]
        mock_response.choices[0].message.content = "Response"
        mock_response.choices[0].message.tool_calls = None
        
        mock_chat = MagicMock()
        mock_chat.completions = MagicMock()
        mock_chat.completions.create = AsyncMock(return_value=mock_response)
        mock_instance.chat = mock_chat
        
        messages = [ChatMessage(role="user", content="Test")]
        
        result = await client.chat(messages, cancellation_token=cancellation_token)
        
        assert result == "Response"
