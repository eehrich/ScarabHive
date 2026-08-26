"""Unit tests for OpenAIAsyncClient."""

import pytest
import httpx
from unittest.mock import AsyncMock, MagicMock, patch
from plugins_llm.llm_openai.openai_client import OpenAIAsyncClient
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
        OpenAIAsyncClient(
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
    
    @pytest.mark.asyncio
    async def test_streaming_remote_protocol_error_retry(self, openai_client):
        """Test retry logic for RemoteProtocolError during streaming."""
        client, mock_instance = openai_client
        
        # Create mock chunks for successful response
        def create_success_chunks():
            chunk1 = MagicMock()
            chunk1.choices = [MagicMock()]
            chunk1.choices[0].delta = MagicMock()
            chunk1.choices[0].delta.content = "Hello"
            chunk1.choices[0].delta.tool_calls = None
            chunk1.usage = None
            
            chunk2 = MagicMock()
            chunk2.choices = [MagicMock()]
            chunk2.choices[0].delta = MagicMock()
            chunk2.choices[0].delta.content = " World"
            chunk2.choices[0].delta.tool_calls = None
            chunk2.usage = MagicMock()
            chunk2.usage.prompt_tokens = 10
            chunk2.usage.completion_tokens = 5
            chunk2.usage.total_tokens = 15
            
            return [chunk1, chunk2]
        
        # First stream: raises RemoteProtocolError mid-stream
        async def failing_stream():
            chunk = MagicMock()
            chunk.choices = [MagicMock()]
            chunk.choices[0].delta = MagicMock()
            chunk.choices[0].delta.content = "Hel"
            chunk.choices[0].delta.tool_calls = None
            chunk.usage = None
            yield chunk
            raise httpx.RemoteProtocolError("peer closed connection without sending complete message body")
        
        # Second stream: succeeds
        async def success_stream():
            for chunk in create_success_chunks():
                yield chunk
        
        mock_chat = MagicMock()
        mock_chat.completions = MagicMock()
        mock_chat.completions.create = AsyncMock(side_effect=[failing_stream(), success_stream()])
        mock_instance.chat = mock_chat
        
        messages = [ChatMessage(role="user", content="Test")]
        result = []
        
        async for chunk in client.chat_tools_streaming(messages, []):
            result.append(chunk)
        
        # Should have final result after retry
        assert any(c.get("type") == "final" for c in result)
        final = next(c for c in result if c.get("type") == "final")
        assert "Hello World" in final["assistant"]["content"]
        assert mock_chat.completions.create.call_count == 2
    
    @pytest.mark.asyncio
    async def test_streaming_network_error_retry(self, openai_client):
        """Test retry logic for NetworkError during streaming."""
        client, mock_instance = openai_client
        
        # Success chunks
        def create_chunks():
            chunk = MagicMock()
            chunk.choices = [MagicMock()]
            chunk.choices[0].delta = MagicMock()
            chunk.choices[0].delta.content = "Recovered"
            chunk.choices[0].delta.tool_calls = None
            chunk.usage = None
            return [chunk]
        
        async def success_stream():
            for chunk in create_chunks():
                yield chunk
        
        mock_chat = MagicMock()
        mock_chat.completions = MagicMock()
        # First: NetworkError, Second: success
        mock_chat.completions.create = AsyncMock(
            side_effect=[
                httpx.NetworkError("Connection reset by peer"),
                success_stream()
            ]
        )
        mock_instance.chat = mock_chat
        
        messages = [ChatMessage(role="user", content="Test")]
        result = []
        
        async for chunk in client.chat_tools_streaming(messages, []):
            result.append(chunk)
        
        # Should succeed after retry
        assert any(c.get("type") == "final" for c in result)
        final = next(c for c in result if c.get("type") == "final")
        assert "Recovered" in final["assistant"]["content"]
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


class TestOpenAIClientGeminiStreaming:
    """Test Gemini-specific streaming features."""

    @pytest.mark.asyncio
    async def test_gemini_reasoning_delta_streaming(self, openai_client):
        """Test that Gemini's delta.reasoning is converted to thinking_delta events."""
        client, mock_instance = openai_client
        
        # Create mock streaming chunks with reasoning deltas
        class MockChunk:
            def __init__(self, reasoning=None, content=None, usage=None):
                self.choices = []
                if reasoning or content:
                    delta = MagicMock()
                    delta.reasoning = reasoning  # Gemini thinking tokens
                    delta.content = content
                    delta.tool_calls = None
                    choice = MagicMock()
                    choice.delta = delta
                    self.choices.append(choice)
                self.usage = usage
        
        # Mock chunks: reasoning tokens + content + usage
        chunks = [
            MockChunk(reasoning="Let me think..."),
            MockChunk(reasoning=" analyzing the problem"),
            MockChunk(content="The answer is 42"),
            MockChunk(usage=MagicMock(prompt_tokens=10, completion_tokens=20, total_tokens=30))
        ]
        
        async def mock_stream():
            for chunk in chunks:
                yield chunk
        
        mock_stream_obj = MagicMock()
        mock_stream_obj.__aiter__ = lambda self: mock_stream()
        
        mock_chat = MagicMock()
        mock_chat.completions = MagicMock()
        mock_chat.completions.create = AsyncMock(return_value=mock_stream_obj)
        mock_instance.chat = mock_chat
        
        messages = [ChatMessage(role="user", content="Test")]
        tools = []
        
        # Collect all events
        events = []
        async for event in client.chat_tools_streaming(messages, tools):
            events.append(event)
        
        # Verify thinking_delta events for reasoning
        thinking_deltas = [e for e in events if e.get("type") == "thinking_delta"]
        assert len(thinking_deltas) == 2
        assert thinking_deltas[0]["delta"] == "Let me think..."
        assert thinking_deltas[1]["delta"] == " analyzing the problem"
        
        # Verify content deltas
        content_deltas = [e for e in events if e.get("type") == "content_delta"]
        assert len(content_deltas) == 1
        assert content_deltas[0]["delta"] == "The answer is 42"
        
        # Verify final event
        final_event = [e for e in events if e.get("type") == "final"][0]
        assert final_event["assistant"]["content"] == "The answer is 42"
        assert "usage" in final_event


class TestOpenAIClientRetryExhaustion:
    """Test retry exhaustion handling."""

    @pytest.mark.asyncio
    async def test_chat_retry_exhaustion_raises_typed_error(self, openai_client):
        """Exhausted 429 retries raise LLMRateLimitError (mirrors
        _chat_tools_chat_completions). The old contract returned an error
        string AS the answer, so callers could never trigger a fallback
        profile -- and the loop even slept a full backoff after the LAST
        attempt with no request following it."""
        client, mock_instance = openai_client

        from openai import RateLimitError

        from agent_system.llm.models import LLMRateLimitError

        # Mock asyncio.sleep to avoid delays
        with patch('asyncio.sleep', new_callable=AsyncMock):
            # Mock error response
            error_response = MagicMock()
            error_response.status_code = 429

            # All attempts fail
            mock_chat = MagicMock()
            mock_chat.completions = MagicMock()
            mock_chat.completions.create = AsyncMock(
                side_effect=RateLimitError("Rate limit", response=error_response, body=None)
            )
            mock_instance.chat = mock_chat

            messages = [ChatMessage(role="user", content="Test")]

            with pytest.raises(LLMRateLimitError):
                await client.chat(messages)

    @pytest.mark.asyncio
    async def test_chat_tools_retry_exhaustion_none_response(self, openai_client):
        """Test that exhausted retries in streaming return error payload."""
        client, mock_instance = openai_client
        
        # Mock asyncio.sleep to avoid delays
        with patch('asyncio.sleep', new_callable=AsyncMock):
            # Mock chunks that fail immediately
            async def failing_stream(*args, **kwargs):
                raise httpx.RemoteProtocolError("peer closed connection")
                yield  # Never reached
            
            mock_chat = MagicMock()
            mock_chat.completions = MagicMock()
            mock_chat.completions.create = AsyncMock(side_effect=failing_stream)
            mock_instance.chat = mock_chat
            
            messages = [ChatMessage(role="user", content="Test")]
            
            # Collect all events
            events = []
            async for event in client.chat_tools_streaming(messages, []):
                events.append(event)
            
            # Should yield final event with error, not raise AttributeError
            assert len(events) == 1
            final_event = events[0]
            assert final_event["type"] == "final"
            assert "error" in final_event["assistant"]
            assert "Stream failed after" in final_event["assistant"]["error"]["message"]
            # Verify multiple retry attempts were made
            assert mock_chat.completions.create.call_count >= 2


class TestOpenAIClientStreamingUsageTracking:
    """Test usage tracking in streaming mode."""

    @pytest.mark.asyncio
    async def test_streaming_usage_tracking(self, openai_client):
        """Test that usage information is tracked and returned in streaming mode."""
        client, mock_instance = openai_client
        
        # Create mock streaming chunks with usage in final chunk
        class MockChunk:
            def __init__(self, content=None, usage=None):
                self.choices = []
                if content:
                    delta = MagicMock()
                    delta.content = content
                    delta.tool_calls = None
                    choice = MagicMock()
                    choice.delta = delta
                    self.choices.append(choice)
                self.usage = usage
        
        # Mock chunks: content chunks + final chunk with usage
        chunks = [
            MockChunk(content="Hello"),
            MockChunk(content=" world"),
            MockChunk(content="!"),
            MockChunk(usage=MagicMock(prompt_tokens=10, completion_tokens=20, total_tokens=30))
        ]
        
        async def mock_stream():
            for chunk in chunks:
                yield chunk
        
        mock_stream_obj = MagicMock()
        mock_stream_obj.__aiter__ = lambda self: mock_stream()
        
        mock_chat = MagicMock()
        mock_chat.completions = MagicMock()
        mock_chat.completions.create = AsyncMock(return_value=mock_stream_obj)
        mock_instance.chat = mock_chat
        
        messages = [ChatMessage(role="user", content="Test")]
        tools = []
        
        # Collect all events
        events = []
        async for event in client.chat_tools_streaming(messages, tools):
            events.append(event)
        
        # Verify content deltas
        content_deltas = [e for e in events if e.get("type") == "content_delta"]
        assert len(content_deltas) == 3
        assert content_deltas[0]["delta"] == "Hello"
        assert content_deltas[1]["delta"] == " world"
        assert content_deltas[2]["delta"] == "!"
        
        # Verify final event has usage
        final_event = [e for e in events if e.get("type") == "final"][0]
        assert "usage" in final_event
        assert final_event["usage"]["prompt_tokens"] == 10
        assert final_event["usage"]["completion_tokens"] == 20
        assert final_event["usage"]["total_tokens"] == 30
        assert final_event["assistant"]["content"] == "Hello world!"

    @pytest.mark.asyncio
    async def test_streaming_without_usage(self, openai_client):
        """Test that streaming works correctly when no usage data is provided."""
        client, mock_instance = openai_client
        
        # Create mock streaming chunks WITHOUT usage
        class MockChunk:
            def __init__(self, content=None):
                self.choices = []
                if content:
                    delta = MagicMock()
                    delta.content = content
                    delta.tool_calls = None
                    choice = MagicMock()
                    choice.delta = delta
                    self.choices.append(choice)
                self.usage = None  # No usage data
        
        chunks = [
            MockChunk(content="Test"),
            MockChunk(content=" response"),
        ]
        
        async def mock_stream():
            for chunk in chunks:
                yield chunk
        
        mock_stream_obj = MagicMock()
        mock_stream_obj.__aiter__ = lambda self: mock_stream()
        
        mock_chat = MagicMock()
        mock_chat.completions = MagicMock()
        mock_chat.completions.create = AsyncMock(return_value=mock_stream_obj)
        mock_instance.chat = mock_chat
        
        messages = [ChatMessage(role="user", content="Test")]
        tools = []
        
        # Collect all events
        events = []
        async for event in client.chat_tools_streaming(messages, tools):
            events.append(event)
        
        # Verify final event does NOT have usage
        final_event = [e for e in events if e.get("type") == "final"][0]
        assert "usage" not in final_event
        assert final_event["assistant"]["content"] == "Test response"

