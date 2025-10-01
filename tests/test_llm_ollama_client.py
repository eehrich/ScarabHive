"""Unit tests for OllamaNativeAsyncClient."""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from agent_system.llm.ollama_client import OllamaNativeAsyncClient
from agent_system.llm.models import ChatMessage


class TestOllamaClientInitialization:
    """Test client initialization and configuration."""

    def test_basic_initialization(self):
        """Test basic client initialization."""
        with patch("httpx.AsyncClient"):
            client = OllamaNativeAsyncClient(
                model="llama2"
            )
            
            assert client.model == "llama2"
            assert client._base == "http://127.0.0.1:11434"
            assert client._options == {}
            assert client._timeout == 60.0
            assert client.verify is True

    def test_initialization_with_base_url(self):
        """Test client initialization with custom base URL."""
        with patch("httpx.AsyncClient"):
            client = OllamaNativeAsyncClient(
                model="mistral",
                base_url="http://custom-host:8080/"
            )
            
            assert client.model == "mistral"
            # Should strip trailing slash
            assert client._base == "http://custom-host:8080"

    def test_initialization_with_options(self):
        """Test client initialization with custom options."""
        with patch("httpx.AsyncClient"):
            options = {
                "num_ctx": 4096,
                "temperature": 0.8,
                "top_p": 0.9
            }
            
            client = OllamaNativeAsyncClient(
                model="llama2",
                options=options
            )
            
            assert client._options == options

    def test_initialization_with_timeout(self):
        """Test client initialization with custom timeout."""
        with patch("httpx.AsyncClient"):
            client = OllamaNativeAsyncClient(
                model="llama2",
                timeout=120.0
            )
            
            assert client._timeout == 120.0

    def test_initialization_with_verify_false(self):
        """Test client initialization with SSL verification disabled."""
        with patch("httpx.AsyncClient"):
            client = OllamaNativeAsyncClient(
                model="llama2",
                verify=False
            )
            
            assert client.verify is False


class TestOllamaClientMessageMapping:
    """Test message format mapping."""

    def test_map_simple_user_message(self):
        """Test mapping simple user message."""
        with patch("httpx.AsyncClient"):
            client = OllamaNativeAsyncClient(model="llama2")
            
            messages = [
                ChatMessage(role="user", content="Hello")
            ]
            
            mapped = client._map_messages(messages)
            
            assert len(mapped) == 1
            assert mapped[0]["role"] == "user"
            assert mapped[0]["content"] == "Hello"

    def test_map_system_and_user_messages(self):
        """Test mapping system and user messages."""
        with patch("httpx.AsyncClient"):
            client = OllamaNativeAsyncClient(model="llama2")
            
            messages = [
                ChatMessage(role="system", content="You are helpful."),
                ChatMessage(role="user", content="Hi")
            ]
            
            mapped = client._map_messages(messages)
            
            assert len(mapped) == 2
            assert mapped[0]["role"] == "system"
            assert mapped[0]["content"] == "You are helpful."
            assert mapped[1]["role"] == "user"
            assert mapped[1]["content"] == "Hi"

    def test_map_tool_message(self):
        """Test mapping tool message."""
        with patch("httpx.AsyncClient"):
            client = OllamaNativeAsyncClient(model="llama2")
            
            messages = [
                ChatMessage(
                    role="tool",
                    content="Weather data",
                    name="get_weather",
                    tool_call_id="call_123"
                )
            ]
            
            mapped = client._map_messages(messages)
            
            assert len(mapped) == 1
            assert mapped[0]["role"] == "tool"
            assert mapped[0]["content"] == "Weather data"
            assert mapped[0]["tool_name"] == "get_weather"
            assert mapped[0]["tool_call_id"] == "call_123"

    def test_map_assistant_message_with_tool_calls(self):
        """Test mapping assistant message with tool calls."""
        with patch("httpx.AsyncClient"):
            client = OllamaNativeAsyncClient(model="llama2")
            
            tool_calls = [{"id": "call_1", "function": {"name": "test"}}]
            messages = [
                ChatMessage(role="assistant", content=None, tool_calls=tool_calls)
            ]
            
            mapped = client._map_messages(messages)
            
            assert len(mapped) == 1
            assert mapped[0]["role"] == "assistant"
            assert mapped[0]["tool_calls"] == tool_calls

    def test_map_message_without_content(self):
        """Test mapping message without content (None)."""
        with patch("httpx.AsyncClient"):
            client = OllamaNativeAsyncClient(model="llama2")
            
            messages = [
                ChatMessage(role="assistant", content=None)
            ]
            
            mapped = client._map_messages(messages)
            
            assert len(mapped) == 1
            assert mapped[0]["role"] == "assistant"
            assert "content" not in mapped[0]


class TestOllamaClientChat:
    """Test chat functionality."""

    @pytest.mark.asyncio
    async def test_chat_simple_message(self):
        """Test sending a simple chat message."""
        with patch("httpx.AsyncClient") as mock_async_client_class:
            client = OllamaNativeAsyncClient(model="llama2")
            
            # Mock the async context manager and post response
            mock_client_instance = MagicMock()
            mock_response = MagicMock()
            mock_response.json.return_value = {
                "message": {"role": "assistant", "content": "Hello! How can I help?"}
            }
            mock_response.raise_for_status = MagicMock()
            
            mock_client_instance.__aenter__ = AsyncMock(return_value=mock_client_instance)
            mock_client_instance.__aexit__ = AsyncMock()
            mock_client_instance.post = AsyncMock(return_value=mock_response)
            
            mock_async_client_class.return_value = mock_client_instance
            
            messages = [
                ChatMessage(role="user", content="Hello")
            ]
            
            result = await client.chat(messages)
            
            assert result == "Hello! How can I help?"
            mock_client_instance.post.assert_called_once()

    @pytest.mark.asyncio
    async def test_chat_with_options(self):
        """Test chat request includes options."""
        with patch("httpx.AsyncClient") as mock_async_client_class:
            client = OllamaNativeAsyncClient(
                model="llama2",
                options={"num_ctx": 4096, "temperature": 0.7}
            )
            
            mock_client_instance = MagicMock()
            mock_response = MagicMock()
            mock_response.json.return_value = {
                "message": {"role": "assistant", "content": "Response"}
            }
            mock_response.raise_for_status = MagicMock()
            
            mock_client_instance.__aenter__ = AsyncMock(return_value=mock_client_instance)
            mock_client_instance.__aexit__ = AsyncMock()
            mock_client_instance.post = AsyncMock(return_value=mock_response)
            
            mock_async_client_class.return_value = mock_client_instance
            
            messages = [ChatMessage(role="user", content="Test")]
            
            await client.chat(messages)
            
            # Check that options were included in request body
            call_args = mock_client_instance.post.call_args
            body = call_args[1]["json"]
            assert "options" in body
            assert body["options"]["num_ctx"] == 4096
            assert body["options"]["temperature"] == 0.7

    @pytest.mark.asyncio
    async def test_chat_url_construction(self):
        """Test that correct URL is constructed."""
        with patch("httpx.AsyncClient") as mock_async_client_class:
            client = OllamaNativeAsyncClient(
                model="llama2",
                base_url="http://localhost:11434"
            )
            
            mock_client_instance = MagicMock()
            mock_response = MagicMock()
            mock_response.json.return_value = {
                "message": {"role": "assistant", "content": "OK"}
            }
            mock_response.raise_for_status = MagicMock()
            
            mock_client_instance.__aenter__ = AsyncMock(return_value=mock_client_instance)
            mock_client_instance.__aexit__ = AsyncMock()
            mock_client_instance.post = AsyncMock(return_value=mock_response)
            
            mock_async_client_class.return_value = mock_client_instance
            
            messages = [ChatMessage(role="user", content="Test")]
            
            await client.chat(messages)
            
            # Check URL
            call_args = mock_client_instance.post.call_args
            url = call_args[0][0]
            assert url == "http://localhost:11434/api/chat"

    @pytest.mark.asyncio
    async def test_chat_request_body_structure(self):
        """Test that request body has correct structure."""
        with patch("httpx.AsyncClient") as mock_async_client_class:
            client = OllamaNativeAsyncClient(model="llama2")
            
            mock_client_instance = MagicMock()
            mock_response = MagicMock()
            mock_response.json.return_value = {
                "message": {"role": "assistant", "content": "OK"}
            }
            mock_response.raise_for_status = MagicMock()
            
            mock_client_instance.__aenter__ = AsyncMock(return_value=mock_client_instance)
            mock_client_instance.__aexit__ = AsyncMock()
            mock_client_instance.post = AsyncMock(return_value=mock_response)
            
            mock_async_client_class.return_value = mock_client_instance
            
            messages = [
                ChatMessage(role="system", content="Be helpful."),
                ChatMessage(role="user", content="Test")
            ]
            
            await client.chat(messages)
            
            call_args = mock_client_instance.post.call_args
            body = call_args[1]["json"]
            
            assert body["model"] == "llama2"
            assert body["stream"] is False
            assert len(body["messages"]) == 2
            assert body["messages"][0]["role"] == "system"
            assert body["messages"][1]["role"] == "user"

    @pytest.mark.asyncio
    async def test_chat_response_with_empty_content(self):
        """Test handling response with empty content."""
        with patch("httpx.AsyncClient") as mock_async_client_class:
            client = OllamaNativeAsyncClient(model="llama2")
            
            mock_client_instance = MagicMock()
            mock_response = MagicMock()
            
            # Response with empty content
            mock_response.json.return_value = {
                "message": {
                    "role": "assistant",
                    "content": ""
                }
            }
            mock_response.raise_for_status = MagicMock()
            
            mock_client_instance.__aenter__ = AsyncMock(return_value=mock_client_instance)
            mock_client_instance.__aexit__ = AsyncMock()
            mock_client_instance.post = AsyncMock(return_value=mock_response)
            
            mock_async_client_class.return_value = mock_client_instance
            
            messages = [ChatMessage(role="user", content="Test")]
            
            result = await client.chat(messages)
            
            # Should return empty string
            assert result == ""


class TestOllamaClientVerifyParameter:
    """Test SSL verification parameter."""

    @pytest.mark.asyncio
    async def test_verify_parameter_passed_to_client(self):
        """Test that verify parameter is passed to httpx client."""
        with patch("httpx.AsyncClient") as mock_async_client_class:
            client = OllamaNativeAsyncClient(
                model="llama2",
                verify=False
            )
            
            mock_client_instance = MagicMock()
            mock_response = MagicMock()
            mock_response.json.return_value = {
                "message": {"role": "assistant", "content": "OK"}
            }
            mock_response.raise_for_status = MagicMock()
            
            mock_client_instance.__aenter__ = AsyncMock(return_value=mock_client_instance)
            mock_client_instance.__aexit__ = AsyncMock()
            mock_client_instance.post = AsyncMock(return_value=mock_response)
            
            mock_async_client_class.return_value = mock_client_instance
            
            messages = [ChatMessage(role="user", content="Test")]
            
            await client.chat(messages)
            
            # Check that AsyncClient was called with verify=False
            mock_async_client_class.assert_called()
            call_kwargs = mock_async_client_class.call_args[1]
            assert call_kwargs["verify"] is False


class TestOllamaClientCancellation:
    """Test cancellation support."""

    @pytest.mark.asyncio
    async def test_cancellation_token(self):
        """Test chat with cancellation token."""
        with patch("httpx.AsyncClient") as mock_async_client_class:
            client = OllamaNativeAsyncClient(model="llama2")
            
            cancellation_token = MagicMock()
            cancellation_token.is_cancelled = False
            
            mock_client_instance = MagicMock()
            mock_response = MagicMock()
            mock_response.json.return_value = {
                "message": {"role": "assistant", "content": "Response"}
            }
            mock_response.raise_for_status = MagicMock()
            
            mock_client_instance.__aenter__ = AsyncMock(return_value=mock_client_instance)
            mock_client_instance.__aexit__ = AsyncMock()
            mock_client_instance.post = AsyncMock(return_value=mock_response)
            
            mock_async_client_class.return_value = mock_client_instance
            
            messages = [ChatMessage(role="user", content="Test")]
            
            result = await client.chat(messages, cancellation_token=cancellation_token)
            
            assert result == "Response"
