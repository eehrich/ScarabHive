"""
Comprehensive tests for HTTPX-based LLM client.

Tests cover:
1. Basic functionality (chat, chat_tools)
2. Cancellation handling (immediate, during request) 
3. Timeout behavior (connect, read, write)
4. Error handling (HTTP errors, network errors)
5. Retry logic (429 rate limits, 5xx errors)
6. Performance comparison with OpenAI client
"""

import asyncio
import json
import pytest
from unittest.mock import AsyncMock, MagicMock, Mock, patch

import httpx

from plugins.llm_openai_compat.httpx_client import HTTPXOpenAIClient, HTTPXTimeoutConfig
from agent_system.llm.models import LLMConnectionError
from agent_system.core.cancellation import CancellationToken


# Test fixtures and helper data
def create_test_client():
    """Create test client with short timeouts for fast tests."""
    timeout_config = HTTPXTimeoutConfig(
        connect=1.0,
        read=2.0, 
        write=1.0,
        pool=0.5
    )
    return HTTPXOpenAIClient(
        model="gpt-3.5-turbo",
        api_key="test-key",
        timeout_config=timeout_config,
        max_retries=2,
        retry_backoff=0.1
    )

def get_sample_messages():
    """Sample message list for testing."""
    return [
        {"role": "system", "content": "You are a helpful assistant."},
        {"role": "user", "content": "Hello, world!"}
    ]

def get_dialect_tools():
    """A tool schema carrying every keyword Gemini's function declarations reject."""
    return [
        {
            "type": "function",
            "function": {
                "name": "read_book",
                "description": "Read the catalogue.",
                "parameters": {
                    "type": "object",
                    "title": "BookArgs",
                    "additionalProperties": False,
                    "properties": {
                        "section": {"type": "string", "title": "Section",
                                    "default": "all"},
                        "limit": {"type": "integer", "format": "int32"},
                    },
                    "required": ["section"],
                },
            },
        }
    ]


async def captured_payload(client, tools=None, messages=None):
    """The payload the client really sends — its own preparation path, one mocked POST.

    Driving the request instead of reading an attribute is the point: a flag
    can be right while the payload built from it is not.
    """
    response = httpx.Response(
        200,
        json=get_mock_openai_response(),
        request=httpx.Request("POST", "https://example.invalid/chat/completions"),
    )
    with patch("httpx.AsyncClient") as mock_async_client:
        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=response)
        mock_async_client.return_value.__aenter__ = AsyncMock(return_value=mock_client)
        mock_async_client.return_value.__aexit__ = AsyncMock(return_value=None)
        await client._make_request_non_streaming(
            messages if messages is not None else get_sample_messages(),
            tools=tools if tools is not None else [])
    return mock_client.post.call_args.kwargs["json"]


def get_sample_tools():
    """Sample tool list for testing."""
    return [
        {
            "type": "function",
            "function": {
                "name": "get_weather",
                "description": "Get weather information",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "location": {"type": "string"}
                    },
                    "required": ["location"]
                }
            }
        }
    ]

def get_mock_openai_response():
    """Mock OpenAI API response."""
    return {
        "id": "chatcmpl-test",
        "object": "chat.completion",
        "created": 1677652288,
        "model": "gpt-3.5-turbo",
        "usage": {
            "prompt_tokens": 10,
            "completion_tokens": 5,
            "total_tokens": 15
        },
        "choices": [
            {
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": "Hello! How can I help you today?"
                },
                "finish_reason": "stop"
            }
        ]
    }

def get_mock_openai_tools_response():
    """Mock OpenAI API response with tool calls."""
    return {
        "id": "chatcmpl-test-tools",
        "object": "chat.completion", 
        "created": 1677652288,
        "model": "gpt-3.5-turbo",
        "usage": {
            "prompt_tokens": 15,
            "completion_tokens": 10,
            "total_tokens": 25
        },
        "choices": [
            {
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "call_test123",
                            "type": "function",
                            "function": {
                                "name": "get_weather",
                                "arguments": '{"location": "San Francisco"}'
                            }
                        }
                    ]
                },
                "finish_reason": "tool_calls"
            }
        ]
    }


class TestHTTPXOpenAIClient:
    """Test suite for HTTPX-based OpenAI client."""
    
    @pytest.fixture
    def client(self):
        """Create test client fixture."""
        return create_test_client()
    
    @pytest.fixture
    def sample_messages(self):
        """Sample messages fixture."""
        return get_sample_messages()
    
    @pytest.fixture
    def sample_tools(self):
        """Sample tools fixture."""
        return get_sample_tools()
    
    @pytest.fixture
    def mock_openai_response(self):
        """Mock OpenAI response fixture."""
        return get_mock_openai_response()
    
    @pytest.fixture
    def mock_openai_tools_response(self):
        """Mock OpenAI tools response fixture."""
        return get_mock_openai_tools_response()


class TestBasicFunctionality(TestHTTPXOpenAIClient):
    """Test basic chat and chat_tools functionality."""
    
    @pytest.mark.asyncio
    async def test_chat_success(self):
        """Test successful chat completion."""
        client = create_test_client()
        sample_messages = get_sample_messages()
        
        # Mock SSE streaming response
        sse_lines = [
            'data: {"id":"chatcmpl-123","object":"chat.completion.chunk","created":1677652288,"model":"gpt-3.5-turbo","choices":[{"index":0,"delta":{"role":"assistant","content":""},"finish_reason":null}]}',
            'data: {"id":"chatcmpl-123","object":"chat.completion.chunk","created":1677652288,"model":"gpt-3.5-turbo","choices":[{"index":0,"delta":{"content":"Hello"},"finish_reason":null}]}',
            'data: {"id":"chatcmpl-123","object":"chat.completion.chunk","created":1677652288,"model":"gpt-3.5-turbo","choices":[{"index":0,"delta":{"content":"! How"},"finish_reason":null}]}',
            'data: {"id":"chatcmpl-123","object":"chat.completion.chunk","created":1677652288,"model":"gpt-3.5-turbo","choices":[{"index":0,"delta":{"content":" can I"},"finish_reason":null}]}',
            'data: {"id":"chatcmpl-123","object":"chat.completion.chunk","created":1677652288,"model":"gpt-3.5-turbo","choices":[{"index":0,"delta":{"content":" help you"},"finish_reason":null}]}',
            'data: {"id":"chatcmpl-123","object":"chat.completion.chunk","created":1677652288,"model":"gpt-3.5-turbo","choices":[{"index":0,"delta":{"content":" today?"},"finish_reason":null}]}',
            'data: {"id":"chatcmpl-123","object":"chat.completion.chunk","created":1677652288,"model":"gpt-3.5-turbo","choices":[{"index":0,"delta":{},"finish_reason":"stop"}]}',
            'data: [DONE]'
        ]
        
        with patch("httpx.AsyncClient") as mock_async_client:
            # Mock streaming response
            mock_stream_response = AsyncMock()
            mock_stream_response.status_code = 200
            mock_stream_response.headers = {}
            
            async def mock_aiter_bytes():
                for line in sse_lines:
                    yield (line + "\n").encode("utf-8")
            mock_stream_response.aiter_bytes = mock_aiter_bytes
            mock_stream_response.__aenter__.return_value = mock_stream_response
            mock_stream_response.__aexit__.return_value = None
            
            # Mock client and stream method
            mock_client = AsyncMock()
            mock_client.__aenter__.return_value = mock_client
            mock_client.__aexit__.return_value = None
            mock_client.stream = Mock(return_value=mock_stream_response)
            mock_async_client.return_value = mock_client
            
            result = await client.chat(sample_messages)
            
            assert result == "Hello! How can I help you today?"
            mock_client.stream.assert_called_once()
            call_args = mock_client.stream.call_args
            assert call_args.args[0] == "POST"
            assert "chat/completions" in call_args.kwargs["url"]
    
    @pytest.mark.asyncio
    async def test_chat_tools_success(self, client, sample_messages, sample_tools, mock_openai_tools_response):
        """Test successful chat completion with tools."""
        
        # Mock SSE streaming response with tool calls
        sse_lines = [
            'data: {"id":"chatcmpl-123","object":"chat.completion.chunk","created":1677652288,"model":"gpt-3.5-turbo","choices":[{"index":0,"delta":{"role":"assistant","content":""},"finish_reason":null}]}',
            'data: {"id":"chatcmpl-123","object":"chat.completion.chunk","created":1677652288,"model":"gpt-3.5-turbo","choices":[{"index":0,"delta":{"tool_calls":[{"index":0,"id":"call_123","type":"function","function":{"name":"get_weather","arguments":""}}]},"finish_reason":null}]}',
            'data: {"id":"chatcmpl-123","object":"chat.completion.chunk","created":1677652288,"model":"gpt-3.5-turbo","choices":[{"index":0,"delta":{"tool_calls":[{"index":0,"function":{"arguments":"{\\"location\\""}}]},"finish_reason":null}]}',
            'data: {"id":"chatcmpl-123","object":"chat.completion.chunk","created":1677652288,"model":"gpt-3.5-turbo","choices":[{"index":0,"delta":{"tool_calls":[{"index":0,"function":{"arguments":": \\"Boston\\""}}]},"finish_reason":null}]}',
            'data: {"id":"chatcmpl-123","object":"chat.completion.chunk","created":1677652288,"model":"gpt-3.5-turbo","choices":[{"index":0,"delta":{"tool_calls":[{"index":0,"function":{"arguments":"}"}}]},"finish_reason":null}]}',
            'data: {"id":"chatcmpl-123","object":"chat.completion.chunk","created":1677652288,"model":"gpt-3.5-turbo","choices":[{"index":0,"delta":{},"finish_reason":"tool_calls"}]}',
            'data: [DONE]'
        ]
        
        with patch("httpx.AsyncClient") as mock_async_client:
            # Mock streaming response
            mock_stream_response = AsyncMock()
            mock_stream_response.status_code = 200
            mock_stream_response.headers = {}
            
            async def mock_aiter_bytes():
                for line in sse_lines:
                    yield (line + "\n").encode("utf-8")
            mock_stream_response.aiter_bytes = mock_aiter_bytes
            mock_stream_response.__aenter__.return_value = mock_stream_response
            mock_stream_response.__aexit__.return_value = None
            
            # Mock client and stream method
            mock_client = AsyncMock()
            mock_client.__aenter__.return_value = mock_client
            mock_client.__aexit__.return_value = None
            mock_client.stream = Mock(return_value=mock_stream_response)
            mock_async_client.return_value = mock_client
            
            result = await client.chat_tools(sample_messages, sample_tools)
            
            assert "assistant" in result
            assert result["assistant"]["role"] == "assistant"
            assert "tool_calls" in result["assistant"]
            assert len(result["assistant"]["tool_calls"]) == 1
            
            # Verify tools were included in request
            call_args = mock_client.stream.call_args
            request_json = call_args.kwargs["json"]
            assert "tools" in request_json
            assert request_json["tool_choice"] == "auto"
    
    @pytest.mark.asyncio
    async def test_usage_tracking(self, client, sample_messages, mock_openai_response):
        """Test that token usage is properly tracked."""
        
        # Mock SSE streaming response with usage info (Note: usage is typically in final chunk)
        sse_lines = [
            'data: {"id":"chatcmpl-123","object":"chat.completion.chunk","created":1677652288,"model":"gpt-3.5-turbo","choices":[{"index":0,"delta":{"role":"assistant","content":""},"finish_reason":null}]}',
            'data: {"id":"chatcmpl-123","object":"chat.completion.chunk","created":1677652288,"model":"gpt-3.5-turbo","choices":[{"index":0,"delta":{"content":"Test"},"finish_reason":null}]}',
            'data: {"id":"chatcmpl-123","object":"chat.completion.chunk","created":1677652288,"model":"gpt-3.5-turbo","choices":[{"index":0,"delta":{},"finish_reason":"stop"}],"usage":{"prompt_tokens":10,"completion_tokens":5,"total_tokens":15}}',
            'data: [DONE]'
        ]
        
        with patch("httpx.AsyncClient") as mock_async_client:
            # Mock streaming response
            mock_stream_response = AsyncMock()
            mock_stream_response.status_code = 200
            mock_stream_response.headers = {}
            
            async def mock_aiter_bytes():
                for line in sse_lines:
                    yield (line + "\n").encode("utf-8")
            mock_stream_response.aiter_bytes = mock_aiter_bytes
            mock_stream_response.__aenter__.return_value = mock_stream_response
            mock_stream_response.__aexit__.return_value = None
            
            # Mock client and stream method
            mock_client = AsyncMock()
            mock_client.__aenter__.return_value = mock_client
            mock_client.__aexit__.return_value = None
            mock_client.stream = Mock(return_value=mock_stream_response)
            mock_async_client.return_value = mock_client
            
            result = await client.chat_tools(sample_messages, [])
            
            assert "usage" in result
            assert result["usage"]["prompt_tokens"] == 10
            assert result["usage"]["completion_tokens"] == 5
            assert result["usage"]["total_tokens"] == 15


class TestCancellationHandling(TestHTTPXOpenAIClient):
    """Test cancellation behavior - the key improvement over OpenAI client."""
    
    @pytest.mark.asyncio
    async def test_immediate_cancellation(self, client, sample_messages):
        """Test that pre-cancelled token raises CancelledError immediately."""
        token = CancellationToken(request_id="test-request-1")
        token.cancel()
        
        with pytest.raises(asyncio.CancelledError):
            await client.chat(sample_messages, cancellation_token=token)
    
    @pytest.mark.asyncio
    async def test_cancellation_during_request(self, client, sample_messages):
        """Test cancellation while HTTP request is in progress."""
        token = CancellationToken(request_id="test-request-2")
        
        # Create a mock that simulates cancellation during streaming
        async def mock_aiter_bytes_with_cancel():
            yield b'data: {"id":"chatcmpl-123","object":"chat.completion.chunk","created":1677652288,"model":"gpt-3.5-turbo","choices":[{"index":0,"delta":{"role":"assistant","content":""},"finish_reason":null}]}\n'
            await asyncio.sleep(0.05)
            token.cancel()  # Cancel during streaming
            await asyncio.sleep(0.05)
            # This line should never be reached due to cancellation check
            yield b'data: [DONE]\n'
        
        with patch("httpx.AsyncClient") as mock_async_client:
            mock_stream_response = AsyncMock()
            mock_stream_response.status_code = 200
            mock_stream_response.headers = {}
            mock_stream_response.aiter_bytes = mock_aiter_bytes_with_cancel
            mock_stream_response.__aenter__.return_value = mock_stream_response
            mock_stream_response.__aexit__.return_value = None
            
            mock_client = AsyncMock()
            mock_client.__aenter__.return_value = mock_client
            mock_client.__aexit__.return_value = None
            mock_client.stream = Mock(return_value=mock_stream_response)
            mock_async_client.return_value = mock_client
            
            # httpx_client returns error JSON when pre-cancelled instead of raising
            result = await client.chat(sample_messages, cancellation_token=token)
            # Should return error JSON or empty string
            assert isinstance(result, str)
            assert "_llm_error" in result or result == ""
    
    @pytest.mark.asyncio
    async def test_no_cancellation_overhead(self, client, sample_messages, mock_openai_response):
        """Test that requests without cancellation tokens work normally."""
        
        # Mock SSE streaming response
        sse_lines = [
            'data: {"id":"chatcmpl-123","object":"chat.completion.chunk","created":1677652288,"model":"gpt-3.5-turbo","choices":[{"index":0,"delta":{"role":"assistant","content":""},"finish_reason":null}]}',
            'data: {"id":"chatcmpl-123","object":"chat.completion.chunk","created":1677652288,"model":"gpt-3.5-turbo","choices":[{"index":0,"delta":{"content":"Hello! How can I help you today?"},"finish_reason":null}]}',
            'data: {"id":"chatcmpl-123","object":"chat.completion.chunk","created":1677652288,"model":"gpt-3.5-turbo","choices":[{"index":0,"delta":{},"finish_reason":"stop"}]}',
            'data: [DONE]'
        ]
        
        with patch("httpx.AsyncClient") as mock_async_client:
            # Mock streaming response
            mock_stream_response = AsyncMock()
            mock_stream_response.status_code = 200
            mock_stream_response.headers = {}
            
            async def mock_aiter_bytes():
                for line in sse_lines:
                    yield (line + "\n").encode("utf-8")
            mock_stream_response.aiter_bytes = mock_aiter_bytes
            mock_stream_response.__aenter__.return_value = mock_stream_response
            mock_stream_response.__aexit__.return_value = None
            
            # Mock client and stream method
            mock_client = AsyncMock()
            mock_client.__aenter__.return_value = mock_client
            mock_client.__aexit__.return_value = None
            mock_client.stream = Mock(return_value=mock_stream_response)
            mock_async_client.return_value = mock_client
            
            # Should work fine without cancellation token
            result = await client.chat(sample_messages, cancellation_token=None)
            assert result == "Hello! How can I help you today?"


class TestTransportErrorTyping(TestHTTPXOpenAIClient):
    """Transport errors (dead endpoint) must arrive as LLMConnectionError --
    regression: bare Exception wrapping made the agent server's llm_profile
    fallback chain blind to ConnectTimeouts, and the sub-agent died despite
    a configured cross-provider fallback."""

    @pytest.mark.asyncio
    async def test_non_streaming_connect_timeout_raises_typed_error(self, sample_messages):
        client = create_test_client()
        client.capabilities = {"streaming": False}  # like deepseek-chat-nostream
        # Discriminating power: the loop runs over max(max_retries,
        # rate_limit_max_retries) -- with both equal, the assertion below
        # could not tell the two quantities apart.
        client.rate_limit_max_retries = 1
        with patch("httpx.AsyncClient") as mock_async_client:
            mock_client = AsyncMock()
            mock_client.__aenter__.return_value = mock_client
            mock_client.__aexit__.return_value = None
            mock_client.post = AsyncMock(side_effect=httpx.ConnectTimeout("timed out"))
            mock_async_client.return_value = mock_client

            with pytest.raises(LLMConnectionError) as exc_info:
                await client.chat_tools(sample_messages, tools=[])
            assert exc_info.value.model == "gpt-3.5-turbo"
            # Retries were exhausted BEFORE the typed error is raised
            assert mock_client.post.await_count == client.max_retries + 1

    @pytest.mark.asyncio
    async def test_streaming_network_error_raises_typed_error(self, sample_messages):
        # The timeout variant is covered by test_max_retries_exceeded (above) --
        # here the network-error handler, the third raise site of the mapping.
        client = create_test_client()
        with patch("httpx.AsyncClient") as mock_async_client:
            mock_client = AsyncMock()
            mock_client.__aenter__.return_value = mock_client
            mock_client.__aexit__.return_value = None
            mock_client.stream = Mock(
                side_effect=httpx.NetworkError("Connection reset by peer"))
            mock_async_client.return_value = mock_client

            with pytest.raises(LLMConnectionError):
                await client.chat(sample_messages)
            assert mock_client.stream.call_count == client.max_retries + 1


_CONTENT = 'data: {"choices":[{"index":0,"delta":{"content":"%s"},"finish_reason":null}]}'
_THINKING = 'data: {"choices":[{"index":0,"delta":{"reasoning_content":"%s"},"finish_reason":null}]}'
_USAGE_ONLY = 'data: {"choices":[],"usage":{"prompt_tokens":3,"completion_tokens":2}}'
_FINISH = 'data: {"choices":[{"index":0,"delta":{},"finish_reason":"stop"}]}'


class TestASilentUpstream(TestHTTPXOpenAIClient):
    """Keep-alive comments reset the per-chunk timeout, so only events count as progress, and the declared
    ``stream_silence_timeout`` bounds a stream that sends nothing else."""

    LIMIT = 1.0
    GAP = 0.1

    @staticmethod
    def _stream_mock(lines, gap: float, ending: str):
        """Each line after a keep-alive and ``gap`` seconds. After the lines, by ``ending``: keep-alives forever,
        nothing at all ("quiet"), or a dropped connection ("drop"). A line given as bytes is sent as it is."""
        response = AsyncMock()
        response.status_code = 200
        response.headers = {}

        async def aiter_bytes():
            for line in lines:
                await asyncio.sleep(gap)
                if isinstance(line, bytes):  # a piece of a line: nothing may come between the pieces
                    yield line
                    continue
                yield b": keep-alive\n\n"
                yield (line + "\n").encode("utf-8")
            if ending == "drop":
                raise httpx.RemoteProtocolError("peer closed connection without sending complete message body")
            while True:
                await asyncio.sleep(3600 if ending == "quiet" else gap)
                yield b": keep-alive\n\n"

        response.aiter_bytes = aiter_bytes
        response.__aenter__.return_value = response
        response.__aexit__.return_value = None
        return response

    async def _stream(self, *attempts, silence=LIMIT, read=30.0, ending="keep-alives", outer=20):
        """One scripted stream per attempt; the chunks the caller sees. Only the limit a test names is short."""
        client = HTTPXOpenAIClient(
            model="gpt-3.5-turbo", api_key="test-key", max_retries=len(attempts) - 1, retry_backoff=0,
            stream_silence_timeout=silence,
            timeout_config=HTTPXTimeoutConfig(connect=30.0, read=read, write=30.0, pool=30.0))
        streams = iter(attempts)

        async def collect():
            return [chunk async for chunk in client.chat_tools_streaming([{"role": "user", "content": "hi"}], [])]

        with patch("httpx.AsyncClient") as mock_async_client:
            mock_client = AsyncMock()
            mock_client.__aenter__.return_value = mock_client
            mock_client.__aexit__.return_value = None
            mock_client.stream = Mock(
                side_effect=lambda *a, **kw: self._stream_mock(next(streams), self.GAP, ending))
            mock_async_client.return_value = mock_client
            try:
                # a regression hangs; the outer limit turns that into a failure
                return await asyncio.wait_for(collect(), outer)
            finally:
                self.streams_opened = mock_client.stream.call_count

    @staticmethod
    def _text(chunks) -> str:
        return "".join(c["delta"] for c in chunks if c["type"] == "content_delta")

    @pytest.mark.asyncio
    async def test_only_keep_alives_end_in_an_error_after_every_attempt(self):
        with pytest.raises(LLMConnectionError, match="only keep-alives"):
            await self._stream([], [])
        assert self.streams_opened == 2

    @pytest.mark.asyncio
    async def test_without_a_declared_limit_keep_alives_hold_the_call(self):
        """None is the endpoint's own bound (DeepSeek closes its queue after 10 minutes), not ours -- and not the
        read timeout either, even when that is short."""
        with pytest.raises(asyncio.TimeoutError):
            await self._stream([], silence=None, read=self.LIMIT, outer=3 * self.LIMIT)
        assert self.streams_opened == 1

    @pytest.mark.asyncio
    async def test_a_silent_attempt_is_retried_with_a_fresh_clock(self):
        chunks = await self._stream([], [_CONTENT % "da", _FINISH, "data: [DONE]"])
        assert self._text(chunks) == "da"
        assert self.streams_opened == 2

    @pytest.mark.asyncio
    @pytest.mark.parametrize("event", [_THINKING % "hm", _USAGE_ONLY], ids=["thinking", "no choices"])
    async def test_a_stream_of_one_kind_of_event_outlasts_the_limit(self, event):
        """The limit is the silence between events, not the length of the call, and every event counts: this one
        sends only that kind of chunk for one and a half times the limit before the answer comes."""
        chunks = await self._stream([event] * 15 + [_CONTENT % "da", _FINISH, "data: [DONE]"])
        assert self._text(chunks) == "da"
        assert self.streams_opened == 1

    @pytest.mark.asyncio
    @pytest.mark.parametrize("ending", ["keep-alives", "quiet", "drop"])
    async def test_a_finished_answer_is_kept_however_the_stream_ends(self, ending):
        """Paid for and complete: however the stream ends after the finish, the answer must not be thrown away, and
        it keeps the finish reason it came with."""
        chunks = await self._stream([_CONTENT % "fertig", _FINISH], ending=ending,
                                    read=self.LIMIT if ending == "quiet" else 30.0)
        assert self._text(chunks) == "fertig"
        assert chunks[-1]["type"] == "final" and chunks[-1]["finish_reason"] == "stop"
        assert self.streams_opened == 1

    @pytest.mark.asyncio
    async def test_a_connection_dropped_before_the_finish_keeps_its_own_error(self):
        """No finish yet: the dropped connection is what the caller hears about, not a made-up stall."""
        with pytest.raises(LLMConnectionError, match="peer closed"):
            await self._stream([_CONTENT % "halb"], [_CONTENT % "halb"], ending="drop")
        assert self.streams_opened == 2

    @pytest.mark.asyncio
    async def test_a_character_split_across_two_packets_survives(self):
        """The network cuts where it likes: a multi-byte character across two packets must arrive whole."""
        line = (_CONTENT % "Grüße").encode("utf-8") + b"\n"
        cut = line.index("ü".encode("utf-8")) + 1  # inside the two bytes of the u-umlaut
        chunks = await self._stream([line[:cut], line[cut:], _FINISH, "data: [DONE]"])
        assert self._text(chunks) == "Grüße"


class TestErrorHandling(TestHTTPXOpenAIClient):
    """Test error handling and retry logic."""
    
    @pytest.mark.asyncio
    async def test_http_404_error(self, client, sample_messages):
        """Test handling of HTTP 404 client errors."""
        with patch("httpx.AsyncClient") as mock_async_client:
            # Mock 404 error in streaming response
            mock_stream_response = AsyncMock()
            mock_stream_response.status_code = 404
            mock_stream_response.headers = {}
            mock_stream_response.request = Mock()
            
            async def mock_aread():
                return b'{"error": {"message": "Model not found"}}'
            
            mock_stream_response.aread = mock_aread
            mock_stream_response.__aenter__.return_value = mock_stream_response
            mock_stream_response.__aexit__.return_value = None
            
            mock_client = AsyncMock()
            mock_client.__aenter__.return_value = mock_client
            mock_client.__aexit__.return_value = None
            mock_client.stream = Mock(return_value=mock_stream_response)
            mock_async_client.return_value = mock_client
            
            with pytest.raises(Exception) as exc_info:
                await client.chat(sample_messages)
            
            assert "404" in str(exc_info.value)
    
    @pytest.mark.asyncio  
    async def test_429_retry_logic(self, client, sample_messages, mock_openai_response):
        """Test retry logic for 429 rate limit errors."""
        call_count = [0]  # Use list to allow mutation in nested function
        
        # Mock SSE streaming response for success case
        sse_lines = [
            'data: {"id":"chatcmpl-123","object":"chat.completion.chunk","created":1677652288,"model":"gpt-3.5-turbo","choices":[{"index":0,"delta":{"role":"assistant","content":""},"finish_reason":null}]}',
            'data: {"id":"chatcmpl-123","object":"chat.completion.chunk","created":1677652288,"model":"gpt-3.5-turbo","choices":[{"index":0,"delta":{"content":"Hello! How can I help you today?"},"finish_reason":null}]}',
            'data: {"id":"chatcmpl-123","object":"chat.completion.chunk","created":1677652288,"model":"gpt-3.5-turbo","choices":[{"index":0,"delta":{},"finish_reason":"stop"}]}',
            'data: [DONE]'
        ]
        
        with patch("httpx.AsyncClient") as mock_async_client:
            def create_mock_stream(*args, **kwargs):
                call_count[0] += 1
                
                if call_count[0] == 1:
                    # First call: 429 rate limit
                    mock_429_response = AsyncMock()
                    mock_429_response.status_code = 429
                    mock_429_response.headers = {"retry-after": "0.01"}
                    mock_429_response.__aenter__.return_value = mock_429_response
                    mock_429_response.__aexit__.return_value = None
                    return mock_429_response
                else:
                    # Second call: success
                    mock_success_response = AsyncMock()
                    mock_success_response.status_code = 200
                    mock_success_response.headers = {}
                    
                    async def mock_aiter_bytes():
                        for line in sse_lines:
                            yield (line + "\n").encode("utf-8")
                    mock_success_response.aiter_bytes = mock_aiter_bytes
                    mock_success_response.__aenter__.return_value = mock_success_response
                    mock_success_response.__aexit__.return_value = None
                    return mock_success_response
            
            mock_client = AsyncMock()
            mock_client.__aenter__.return_value = mock_client
            mock_client.__aexit__.return_value = None
            mock_client.stream = Mock(side_effect=create_mock_stream)
            mock_async_client.return_value = mock_client
            
            result = await client.chat(sample_messages)
            
            assert result == "Hello! How can I help you today?"
            assert mock_client.stream.call_count == 2
    
    @pytest.mark.asyncio
    async def test_500_server_error_retry(self, client, sample_messages, mock_openai_response):
        """Test retry logic for 500 server errors."""
        with patch("httpx.AsyncClient") as mock_async_client:
            # First response: 500 server error
            mock_500_response = AsyncMock()
            mock_500_response.status_code = 500
            mock_500_response.headers = {}
            mock_500_response.request = Mock()
            
            async def mock_aread():
                return b'{"error": "Internal Server Error"}'
            
            mock_500_response.aread = mock_aread
            mock_500_response.__aenter__.return_value = mock_500_response
            mock_500_response.__aexit__.return_value = None
            
            # Second response: success
            sse_lines = [
                'data: {"id":"chatcmpl-123","object":"chat.completion.chunk","created":1677652288,"model":"gpt-3.5-turbo","choices":[{"index":0,"delta":{"role":"assistant","content":""},"finish_reason":null}]}',
                'data: {"id":"chatcmpl-123","object":"chat.completion.chunk","created":1677652288,"model":"gpt-3.5-turbo","choices":[{"index":0,"delta":{"content":"Hello! How can I help you today?"},"finish_reason":null}]}',
                'data: {"id":"chatcmpl-123","object":"chat.completion.chunk","created":1677652288,"model":"gpt-3.5-turbo","choices":[{"index":0,"delta":{},"finish_reason":"stop"}]}',
                'data: [DONE]'
            ]
            
            mock_success_response = AsyncMock()
            mock_success_response.status_code = 200
            mock_success_response.headers = {}
            
            async def mock_aiter_bytes():
                for line in sse_lines:
                    yield (line + "\n").encode("utf-8")
            mock_success_response.aiter_bytes = mock_aiter_bytes
            mock_success_response.__aenter__.return_value = mock_success_response
            mock_success_response.__aexit__.return_value = None
            
            mock_client = AsyncMock()
            mock_client.__aenter__.return_value = mock_client
            mock_client.__aexit__.return_value = None
            mock_client.stream = Mock(side_effect=[mock_500_response, mock_success_response])
            mock_async_client.return_value = mock_client
            
            result = await client.chat(sample_messages)
            
            assert result == "Hello! How can I help you today?"
            assert mock_client.stream.call_count == 2
    
    @pytest.mark.asyncio
    async def test_timeout_error_retry(self, client, sample_messages, mock_openai_response):
        """Test retry logic for timeout errors."""
        with patch("httpx.AsyncClient") as mock_async_client:
            # First request: timeout exception
            timeout_error = httpx.TimeoutException("Read timeout")
            
            # Second response: success
            sse_lines = [
                'data: {"id":"chatcmpl-123","object":"chat.completion.chunk","created":1677652288,"model":"gpt-3.5-turbo","choices":[{"index":0,"delta":{"role":"assistant","content":""},"finish_reason":null}]}',
                'data: {"id":"chatcmpl-123","object":"chat.completion.chunk","created":1677652288,"model":"gpt-3.5-turbo","choices":[{"index":0,"delta":{"content":"Hello! How can I help you today?"},"finish_reason":null}]}',
                'data: {"id":"chatcmpl-123","object":"chat.completion.chunk","created":1677652288,"model":"gpt-3.5-turbo","choices":[{"index":0,"delta":{},"finish_reason":"stop"}]}',
                'data: [DONE]'
            ]
            
            mock_success_response = AsyncMock()
            mock_success_response.status_code = 200
            mock_success_response.headers = {}
            
            async def mock_aiter_bytes():
                for line in sse_lines:
                    yield (line + "\n").encode("utf-8")
            mock_success_response.aiter_bytes = mock_aiter_bytes
            mock_success_response.__aenter__.return_value = mock_success_response
            mock_success_response.__aexit__.return_value = None
            
            # First call raises timeout, second succeeds
            def mock_stream_with_timeout(*args, **kwargs):
                if mock_client.stream.call_count == 1:
                    raise timeout_error
                return mock_success_response
            
            mock_client = AsyncMock()
            mock_client.__aenter__.return_value = mock_client
            mock_client.__aexit__.return_value = None
            mock_client.stream = Mock(side_effect=mock_stream_with_timeout)
            mock_async_client.return_value = mock_client
            
            result = await client.chat(sample_messages)
            
            assert result == "Hello! How can I help you today?"
            assert mock_client.stream.call_count == 2
    
    @pytest.mark.asyncio
    async def test_max_retries_exceeded(self, client, sample_messages):
        """Test that max retries are respected."""
        with patch("httpx.AsyncClient") as mock_async_client:
            timeout_error = httpx.TimeoutException("Read timeout")
            
            mock_client = AsyncMock()
            mock_client.__aenter__.return_value = mock_client
            mock_client.__aexit__.return_value = None
            mock_client.stream = Mock(side_effect=timeout_error)  # Always timeout
            mock_async_client.return_value = mock_client
            
            # After the retries the error must arrive TYPED: a bare
            # Exception would be invisible to the fallback chain.
            with pytest.raises(LLMConnectionError) as exc_info:
                await client.chat(sample_messages)

            assert "timed out" in str(exc_info.value).lower() or "timeout" in str(exc_info.value).lower()
            # Should try max_retries + 1 times (2 + 1 = 3)
            assert mock_client.stream.call_count == 3
    
    @pytest.mark.asyncio
    async def test_remote_protocol_error_retry(self, client, sample_messages):
        """Test retry logic for RemoteProtocolError (peer closed connection)."""
        call_count = [0]
        
        # Success response
        sse_lines = [
            'data: {"id":"chatcmpl-123","object":"chat.completion.chunk","created":1677652288,"model":"gpt-3.5-turbo","choices":[{"index":0,"delta":{"role":"assistant","content":""},"finish_reason":null}]}',
            'data: {"id":"chatcmpl-123","object":"chat.completion.chunk","created":1677652288,"model":"gpt-3.5-turbo","choices":[{"index":0,"delta":{"content":"Hello"},"finish_reason":null}]}',
            'data: {"id":"chatcmpl-123","object":"chat.completion.chunk","created":1677652288,"model":"gpt-3.5-turbo","choices":[{"index":0,"delta":{},"finish_reason":"stop"}]}',
            'data: [DONE]'
        ]
        
        with patch("httpx.AsyncClient") as mock_async_client:
            def create_mock_stream(*args, **kwargs):
                call_count[0] += 1
                
                if call_count[0] == 1:
                    # First call: stream interruption
                    async def mock_aiter_bytes_with_error():
                        yield b'data: {"id":"chatcmpl-123","object":"chat.completion.chunk","created":1677652288,"model":"gpt-3.5-turbo","choices":[{"index":0,"delta":{"role":"assistant","content":""},"finish_reason":null}]}\n'
                        raise httpx.RemoteProtocolError("peer closed connection without sending complete message body")
                    
                    mock_error_response = AsyncMock()
                    mock_error_response.status_code = 200
                    mock_error_response.headers = {}
                    mock_error_response.aiter_bytes = mock_aiter_bytes_with_error
                    mock_error_response.__aenter__.return_value = mock_error_response
                    mock_error_response.__aexit__.return_value = None
                    return mock_error_response
                else:
                    # Second call: success
                    async def mock_aiter_bytes():
                        for line in sse_lines:
                            yield (line + "\n").encode("utf-8")
                    mock_success_response = AsyncMock()
                    mock_success_response.status_code = 200
                    mock_success_response.headers = {}
                    mock_success_response.aiter_bytes = mock_aiter_bytes
                    mock_success_response.__aenter__.return_value = mock_success_response
                    mock_success_response.__aexit__.return_value = None
                    return mock_success_response
            
            mock_client = AsyncMock()
            mock_client.__aenter__.return_value = mock_client
            mock_client.__aexit__.return_value = None
            mock_client.stream = Mock(side_effect=create_mock_stream)
            mock_async_client.return_value = mock_client
            
            result = await client.chat(sample_messages)
            
            assert result == "Hello"
            assert mock_client.stream.call_count == 2
    
    @pytest.mark.asyncio
    async def test_network_error_retry(self, client, sample_messages):
        """Test retry logic for NetworkError."""
        with patch("httpx.AsyncClient") as mock_async_client:
            # First request: network error
            network_error = httpx.NetworkError("Connection reset by peer")
            
            # Second response: success
            sse_lines = [
                'data: {"id":"chatcmpl-123","object":"chat.completion.chunk","created":1677652288,"model":"gpt-3.5-turbo","choices":[{"index":0,"delta":{"role":"assistant","content":""},"finish_reason":null}]}',
                'data: {"id":"chatcmpl-123","object":"chat.completion.chunk","created":1677652288,"model":"gpt-3.5-turbo","choices":[{"index":0,"delta":{"content":"Recovered"},"finish_reason":null}]}',
                'data: {"id":"chatcmpl-123","object":"chat.completion.chunk","created":1677652288,"model":"gpt-3.5-turbo","choices":[{"index":0,"delta":{},"finish_reason":"stop"}]}',
                'data: [DONE]'
            ]
            
            async def mock_aiter_bytes():
                for line in sse_lines:
                    yield (line + "\n").encode("utf-8")
            mock_success_response = AsyncMock()
            mock_success_response.status_code = 200
            mock_success_response.headers = {}
            mock_success_response.aiter_bytes = mock_aiter_bytes
            mock_success_response.__aenter__.return_value = mock_success_response
            mock_success_response.__aexit__.return_value = None
            
            def mock_stream_with_network_error(*args, **kwargs):
                if mock_client.stream.call_count == 1:
                    raise network_error
                return mock_success_response
            
            mock_client = AsyncMock()
            mock_client.__aenter__.return_value = mock_client
            mock_client.__aexit__.return_value = None
            mock_client.stream = Mock(side_effect=mock_stream_with_network_error)
            mock_async_client.return_value = mock_client
            
            result = await client.chat(sample_messages)
            
            assert result == "Recovered"
            assert mock_client.stream.call_count == 2


class TestTimeoutConfiguration(TestHTTPXOpenAIClient):
    """Test fine-grained timeout configuration."""
    
    def test_timeout_config_creation(self):
        """Test timeout configuration object."""
        config = HTTPXTimeoutConfig(
            connect=5.0,
            read=30.0,
            write=10.0,
            pool=2.0
        )
        assert config.connect == 5.0
        assert config.read == 30.0
        assert config.write == 10.0
        assert config.pool == 2.0
    
    def test_timeout_config_defaults(self):
        """Test default timeout values."""
        config = HTTPXTimeoutConfig()
        assert config.connect == 30.0
        assert config.read == 180.0
        assert config.write == 10.0
        assert config.pool == 5.0
    
    def test_client_timeout_integration(self):
        """Test that timeout config is properly integrated into client."""
        timeout_config = HTTPXTimeoutConfig(connect=1.0, read=2.0)
        client = HTTPXOpenAIClient(
            model="gpt-3.5-turbo",
            api_key="test-key",
            timeout_config=timeout_config
        )
        
        assert client._timeout.connect == 1.0
        assert client._timeout.read == 2.0


class TestPerformanceComparison(TestHTTPXOpenAIClient):
    """Performance and reliability comparison tests."""
    
    @pytest.mark.asyncio
    async def test_concurrent_requests(self, client, sample_messages, mock_openai_response):
        """Test handling multiple concurrent requests."""
        
        # Mock SSE streaming response
        sse_lines = [
            'data: {"id":"chatcmpl-123","object":"chat.completion.chunk","created":1677652288,"model":"gpt-3.5-turbo","choices":[{"index":0,"delta":{"role":"assistant","content":""},"finish_reason":null}]}',
            'data: {"id":"chatcmpl-123","object":"chat.completion.chunk","created":1677652288,"model":"gpt-3.5-turbo","choices":[{"index":0,"delta":{"content":"Hello! How can I help you today?"},"finish_reason":null}]}',
            'data: {"id":"chatcmpl-123","object":"chat.completion.chunk","created":1677652288,"model":"gpt-3.5-turbo","choices":[{"index":0,"delta":{},"finish_reason":"stop"}]}',
            'data: [DONE]'
        ]
        
        with patch("httpx.AsyncClient") as mock_async_client:
            def create_mock_response():
                mock_stream_response = AsyncMock()
                mock_stream_response.status_code = 200
                mock_stream_response.headers = {}
                
                async def mock_aiter_bytes():
                    for line in sse_lines:
                        yield (line + "\n").encode("utf-8")
                mock_stream_response.aiter_bytes = mock_aiter_bytes
                mock_stream_response.__aenter__.return_value = mock_stream_response
                mock_stream_response.__aexit__.return_value = None
                return mock_stream_response
            
            mock_client = AsyncMock()
            mock_client.__aenter__.return_value = mock_client
            mock_client.__aexit__.return_value = None
            
            # Each call creates a new response - use regular function, not async
            def mock_stream(*args, **kwargs):
                return create_mock_response()
            
            mock_client.stream = Mock(side_effect=mock_stream)
            mock_async_client.return_value = mock_client
            
            # Run 10 concurrent requests
            tasks = [client.chat(sample_messages) for _ in range(10)]
            results = await asyncio.gather(*tasks)
            
            assert len(results) == 10
            expected_content = "Hello! How can I help you today?"
            assert all(r == expected_content for r in results)
            # Each request should create its own client instance 
            assert mock_async_client.call_count == 10
    
    @pytest.mark.asyncio
    async def test_cancellation_cleanup(self, client, sample_messages):
        """Test that cancellation properly cleans up resources."""
        token = CancellationToken(request_id="test-request-3")
        
        # Mock that cancels immediately when stream is called
        with patch("httpx.AsyncClient") as mock_async_client:
            mock_stream_response = AsyncMock()
            mock_stream_response.status_code = 200
            mock_stream_response.headers = {}
            
            async def mock_aiter_bytes_with_cancel():
                token.cancel()
                raise asyncio.CancelledError()
                yield  # Never reached
            
            mock_stream_response.aiter_bytes = mock_aiter_bytes_with_cancel
            mock_stream_response.__aenter__.return_value = mock_stream_response
            mock_stream_response.__aexit__.return_value = None
            
            mock_client = AsyncMock()
            # Note: We no longer use context manager for client, we use try/finally with aclose()
            mock_client.aclose = AsyncMock()
            mock_client.stream = Mock(return_value=mock_stream_response)
            mock_async_client.return_value = mock_client
            
            with pytest.raises(asyncio.CancelledError):
                await client.chat(sample_messages, cancellation_token=token)
            
            # Verify client was properly closed via aclose() (new approach)
            # The implementation now uses try/finally with client.aclose() instead of context manager
            mock_client.aclose.assert_called_once()


class TestGeminiMalformedRetry(TestHTTPXOpenAIClient):
    """MALFORMED_FUNCTION_CALL retry logic — decided on the answer, not the model."""

    @pytest.fixture
    def gemini_client(self):
        """A client whose config declares the Gemini function-declaration dialect."""
        return HTTPXOpenAIClient(
            model="a-model-the-client-never-reads",
            api_key="test-key",
            base_url="https://openrouter.ai/api/v1",
            max_retries=2,
            retry_backoff=0.01,  # Fast for tests
            tool_schema_dialect="gemini_function_declarations",
        )

    @pytest.mark.asyncio
    async def test_declared_dialect_sanitizes_the_tool_schemas(self, gemini_client):
        """tool_schema_dialect=gemini_function_declarations strips the rejected keywords."""
        payload = await captured_payload(gemini_client, tools=get_dialect_tools())
        params = payload["tools"][0]["function"]["parameters"]
        assert "additionalProperties" not in params
        assert "title" not in params
        assert "title" not in params["properties"]["section"]
        assert "default" not in params["properties"]["section"]
        assert "format" not in params["properties"]["limit"]
        assert params["required"] == ["section"]

    @pytest.mark.asyncio
    async def test_default_dialect_sends_the_schema_unchanged(self, client):
        """Without the key the schema goes out as it is — plain OpenAI behaviour."""
        tools = get_dialect_tools()
        payload = await captured_payload(client, tools=tools)
        assert payload["tools"] == tools

    @pytest.mark.asyncio
    async def test_unknown_dialect_fails_loudly(self):
        """A typo must not silently buy another provider's dialect."""
        with pytest.raises(ValueError, match="tool_schema_dialect"):
            HTTPXOpenAIClient(model="m", api_key="k",
                              tool_schema_dialect="gemini_functions")

    def test_is_gemini_malformed_response_detects_malformed(self, gemini_client):
        """MALFORMED_FUNCTION_CALL without tool_calls is detected."""
        response_data = {
            "choices": [{
                "message": {"role": "assistant", "content": ""},
                "finish_reason": "error",
                "native_finish_reason": "MALFORMED_FUNCTION_CALL",
            }]
        }
        assert gemini_client._is_gemini_malformed_response(response_data) is True

    def test_is_gemini_malformed_response_ignores_with_tool_calls(self, gemini_client):
        """MALFORMED with usable tool_calls returns False (use the calls)."""
        response_data = {
            "choices": [{
                "message": {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [{"id": "1", "type": "function", "function": {"name": "foo", "arguments": "{}"}}],
                },
                "finish_reason": "error",
                "native_finish_reason": "MALFORMED_FUNCTION_CALL",
            }]
        }
        assert gemini_client._is_gemini_malformed_response(response_data) is False

    def test_malformed_is_detected_by_the_answer_not_the_model(self, client):
        """A default client sees it too — the answer's shape decides.

        An endpoint that never emits this native_finish_reason never trips the
        detector, so the model gate in front of it bought nothing.
        """
        response_data = {
            "choices": [{
                "message": {"role": "assistant", "content": ""},
                "finish_reason": "error",
                "native_finish_reason": "MALFORMED_FUNCTION_CALL",
            }]
        }
        assert client._is_gemini_malformed_response(response_data) is True

    def test_internal_format_leak_is_read_only_where_that_dialect_is_spoken(self, client, gemini_client):
        """The marker is plain text: only an endpoint that speaks the dialect can leak it.

        Unlike the MALFORMED case, which rides on a field only that backend sets, this one reads the
        answer's TEXT. A model writing about this very bug would otherwise lose its answer to a retry.
        """
        leaked = {
            "choices": [{
                "message": {"role": "assistant",
                            "content": "call:default_api:read_book{section:all}"},
                "finish_reason": "stop",
            }]
        }
        assert gemini_client._is_gemini_internal_format_leak(leaked) is True
        assert client._is_gemini_internal_format_leak(leaked) is False, \
            "a model that merely writes the marker must keep its answer"
        normal = {"choices": [{"message": {"role": "assistant", "content": "Hi"},
                               "finish_reason": "stop"}]}
        assert gemini_client._is_gemini_internal_format_leak(normal) is False

    def test_is_gemini_malformed_response_ignores_normal(self, gemini_client):
        """Normal responses are not detected as MALFORMED."""
        response_data = {
            "choices": [{
                "message": {"role": "assistant", "content": "Hello"},
                "finish_reason": "stop",
            }]
        }
        assert gemini_client._is_gemini_malformed_response(response_data) is False

    @pytest.mark.asyncio
    async def test_malformed_retries_then_succeeds(self, gemini_client, sample_messages):
        """Request is retried on MALFORMED and succeeds on second attempt."""
        malformed_response = httpx.Response(
            200,
            json={
                "choices": [{
                    "message": {"role": "assistant", "content": ""},
                    "finish_reason": "error",
                    "native_finish_reason": "MALFORMED_FUNCTION_CALL",
                }],
                "usage": {"prompt_tokens": 10, "completion_tokens": 0, "total_tokens": 10},
            },
            request=httpx.Request("POST", "https://openrouter.ai/api/v1/chat/completions"),
        )
        success_response = httpx.Response(
            200,
            json=get_mock_openai_response(),
            request=httpx.Request("POST", "https://openrouter.ai/api/v1/chat/completions"),
        )

        with patch("httpx.AsyncClient") as mock_async_client:
            mock_client = AsyncMock()
            mock_client.post = AsyncMock(side_effect=[malformed_response, success_response])
            mock_async_client.return_value.__aenter__ = AsyncMock(return_value=mock_client)
            mock_async_client.return_value.__aexit__ = AsyncMock(return_value=None)

            result = await gemini_client._make_request_non_streaming(sample_messages, tools=[])
            assert result["assistant"]["content"] == "Hello! How can I help you today?"
            assert mock_client.post.call_count == 2

    @pytest.mark.asyncio
    async def test_malformed_exhausts_retries(self, gemini_client, sample_messages):
        """After max retries, MALFORMED response is returned as-is."""
        malformed_json = {
            "choices": [{
                "message": {"role": "assistant", "content": ""},
                "finish_reason": "error",
                "native_finish_reason": "MALFORMED_FUNCTION_CALL",
            }],
            "usage": {"prompt_tokens": 10, "completion_tokens": 0, "total_tokens": 10},
        }
        malformed_response = httpx.Response(
            200,
            json=malformed_json,
            request=httpx.Request("POST", "https://openrouter.ai/api/v1/chat/completions"),
        )

        with patch("httpx.AsyncClient") as mock_async_client:
            mock_client = AsyncMock()
            # max_retries=2 → 3 total attempts, all MALFORMED
            mock_client.post = AsyncMock(return_value=malformed_response)
            mock_async_client.return_value.__aenter__ = AsyncMock(return_value=mock_client)
            mock_async_client.return_value.__aexit__ = AsyncMock(return_value=None)

            result = await gemini_client._make_request_non_streaming(sample_messages, tools=[])
            # After exhausting retries, we get the empty response
            assert result["assistant"]["content"] == ""
            assert mock_client.post.call_count == 3  # 1 initial + 2 retries


class TestDeclaredRequestFields(TestHTTPXOpenAIClient):
    """parallel_tool_calls and safety_settings: config decides, not the model name."""

    @staticmethod
    def _client(**kwargs):
        return HTTPXOpenAIClient(model="a-model-the-client-never-reads",
                                 api_key="test-key", max_retries=1,
                                 retry_backoff=0.01, **kwargs)

    @pytest.mark.asyncio
    async def test_parallel_tool_calls_none_leaves_the_field_out(self, sample_tools):
        """None = omit: a backend that does not know the field refuses it."""
        payload = await captured_payload(self._client(parallel_tool_calls=None),
                                         tools=sample_tools)
        assert "parallel_tool_calls" not in payload

    @pytest.mark.asyncio
    async def test_parallel_tool_calls_false_is_sent(self, sample_tools):
        """Omitting it means the provider default (true) applies — False must go out."""
        payload = await captured_payload(self._client(parallel_tool_calls=False),
                                         tools=sample_tools)
        assert payload["parallel_tool_calls"] is False

    @pytest.mark.asyncio
    async def test_parallel_tool_calls_default_true_is_sent(self, sample_tools):
        payload = await captured_payload(self._client(), tools=sample_tools)
        assert payload["parallel_tool_calls"] is True

    @pytest.mark.asyncio
    async def test_safety_settings_are_sent_whenever_configured(self, sample_tools):
        """No endpoint gate: a model that must not carry them does not declare them."""
        payload = await captured_payload(
            self._client(safety_settings={"HARM_CATEGORY_HARASSMENT": "BLOCK_NONE"}),
            tools=sample_tools)
        assert payload["safety_settings"] == [
            {"category": "HARM_CATEGORY_HARASSMENT", "threshold": "BLOCK_NONE"}]

    @pytest.mark.asyncio
    async def test_no_safety_settings_no_field(self, sample_tools):
        payload = await captured_payload(self._client(), tools=sample_tools)
        assert "safety_settings" not in payload

    @pytest.mark.asyncio
    async def test_declared_keys_never_leak_into_the_payload(self, sample_tools):
        """The dialect keys steer the request; they are not part of it."""
        payload = await captured_payload(
            self._client(tool_schema_dialect="json_schema",
                         assistant_reasoning_field="omit",
                         reasoning_details_mode="keep_last",
                         prompt_cache_marker_style="none",
                         stream_silence_timeout=900),
            tools=sample_tools)
        for key in ("tool_schema_dialect", "assistant_reasoning_field",
                    "reasoning_details_mode", "prompt_cache_marker_style", "stream_silence_timeout"):
            assert key not in payload

    @pytest.mark.asyncio
    async def test_streaming_sends_the_same_tool_fields(self, sample_tools):
        """Both request paths build their payload separately — they must not drift.

        Every field once added to only one of them has drifted since.
        """
        client = self._client(parallel_tool_calls=False,
                              tool_schema_dialect="gemini_function_declarations")
        tools = get_dialect_tools()
        non_streaming = await captured_payload(client, tools=tools)

        response = MagicMock()

        async def aiter_bytes():
            yield b'data: [DONE]\n'

        response.aiter_bytes = aiter_bytes
        response.status_code = 200
        response.__aenter__ = AsyncMock(return_value=response)
        response.__aexit__ = AsyncMock(return_value=None)
        with patch("httpx.AsyncClient.stream", return_value=response) as stream:
            async for _ in client._make_request_streaming(get_sample_messages(), tools=tools):
                pass
        streamed = stream.call_args.kwargs["json"]

        for key in ("tools", "tool_choice", "parallel_tool_calls"):
            assert streamed[key] == non_streaming[key], key

    def test_unknown_declared_values_fail_loudly(self):
        for key, value in (("assistant_reasoning_field", "reasoning"),
                           ("reasoning_details_mode", "keep_first"),
                           ("prompt_cache_marker_style", "claude")):
            with pytest.raises(ValueError, match=key):
                self._client(**{key: value})


class TestAnthropicViaOpenRouterCaching:
    """Anthropic prompt caching, driven by prompt_cache_marker_style."""

    @pytest.fixture
    def anthropic_or_client(self):
        """A client whose config declares the Anthropic cache-marker style."""
        return HTTPXOpenAIClient(
            model="a-model-the-client-never-reads",
            api_key="sk-or-test",
            base_url="https://openrouter.ai/api/v1",
            max_retries=1,
            retry_backoff=0.01,
            prompt_cache_marker_style="anthropic",
        )

    @pytest.fixture
    def non_anthropic_or_client(self):
        """The same endpoint without the declared style."""
        return HTTPXOpenAIClient(
            model="a-model-the-client-never-reads",
            api_key="sk-or-test",
            base_url="https://openrouter.ai/api/v1",
            max_retries=1,
            retry_backoff=0.01,
        )

    @pytest.mark.asyncio
    async def test_declared_style_marks_system_and_last_tool(self, anthropic_or_client):
        """The whole cache_control injection follows the declared style."""
        payload = await captured_payload(
            anthropic_or_client,
            tools=get_dialect_tools(),
            messages=[{"role": "system", "content": "BASE"},
                      {"role": "user", "content": "hi"}])
        assert payload["messages"][0]["content"][0]["cache_control"] == {"type": "ephemeral"}
        assert payload["tools"][-1]["cache_control"] == {"type": "ephemeral"}

    @pytest.mark.asyncio
    async def test_without_the_style_nothing_is_marked(self, non_anthropic_or_client):
        """Same endpoint, no declared style: the payload carries no cache_control."""
        payload = await captured_payload(
            non_anthropic_or_client,
            tools=get_dialect_tools(),
            messages=[{"role": "system", "content": "BASE"},
                      {"role": "user", "content": "hi"}])
        assert "cache_control" not in json.dumps(payload)

    def test_cache_control_injected_on_system_message(self, anthropic_or_client):
        """System messages get cache_control content blocks."""
        msgs = [
            {"role": "system", "content": "You are a helpful assistant."},
            {"role": "user", "content": "Hello"},
        ]
        anthropic_or_client._apply_anthropic_cache_control(msgs)
        assert isinstance(msgs[0]["content"], list)
        assert msgs[0]["content"][0]["cache_control"] == {"type": "ephemeral"}
        assert msgs[0]["content"][0]["text"] == "You are a helpful assistant."
        # User message unchanged
        assert msgs[1]["content"] == "Hello"

    def test_cache_control_on_structured_system_content(self, anthropic_or_client):
        """System messages with list content get cache_control on last text block."""
        msgs = [
            {"role": "system", "content": [
                {"type": "text", "text": "Part 1"},
                {"type": "text", "text": "Part 2"},
            ]},
        ]
        anthropic_or_client._apply_anthropic_cache_control(msgs)
        assert "cache_control" not in msgs[0]["content"][0]
        assert msgs[0]["content"][1]["cache_control"] == {"type": "ephemeral"}

    def test_cache_control_only_on_last_system_message(self, anthropic_or_client):
        """Several system messages (context_engineer reminders etc.): ONLY the
        last one gets cache_control -- otherwise system+system+...+tool
        exceeds Anthropic's hard 4-marker limit (400, hit for real 2026-07-21)."""
        msgs = [
            {"role": "system", "content": "Base system prompt."},
            {"role": "system", "content": "Injected reminder A."},
            {"role": "system", "content": "Injected reminder B."},
            {"role": "user", "content": "Hi"},
        ]
        anthropic_or_client._apply_anthropic_cache_control(msgs)
        # only the last system message is marked
        assert isinstance(msgs[0]["content"], str)
        assert isinstance(msgs[1]["content"], str)
        assert isinstance(msgs[2]["content"], list)
        assert msgs[2]["content"][0]["cache_control"] == {"type": "ephemeral"}
        total = sum(
            1 for m in msgs if isinstance(m.get("content"), list)
            for p in m["content"] if isinstance(p, dict) and "cache_control" in p
        )
        assert total == 1

    def test_tool_cache_control_on_last_tool(self, anthropic_or_client):
        """cache_control added to last tool definition."""
        tools = [
            {"type": "function", "function": {"name": "a"}},
            {"type": "function", "function": {"name": "b"}},
        ]
        anthropic_or_client._apply_anthropic_tool_cache_control(tools)
        assert "cache_control" not in tools[0]
        assert tools[-1]["cache_control"] == {"type": "ephemeral"}

    def test_no_cache_control_for_non_anthropic(self, non_anthropic_or_client):
        """Non-Anthropic models via OpenRouter don't get cache_control."""
        msgs = [
            {"role": "system", "content": "You are a helpful assistant."},
        ]
        non_anthropic_or_client._postprocess_messages_for_provider(msgs)
        # Content should stay as plain string
        assert msgs[0]["content"] == "You are a helpful assistant."

    def test_reasoning_details_stripped_from_all_but_last_assistant(self, non_anthropic_or_client):
        """Historical reasoning_details (Gemini thought signatures) are dropped from
        every assistant message except the most recent one. Google validates only
        the current turn, and stale/malformed blocks (e.g. OpenRouter UUID
        placeholders) cause 'Corrupted thought signature' 400 errors.
        """
        msgs = [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "", "tool_calls": [{"id": "a", "type": "function", "function": {"name": "f"}}], "reasoning_details": [{"type": "reasoning.encrypted", "data": "OLD1", "id": "a"}]},
            {"role": "tool", "name": "f", "tool_call_id": "a", "content": "r1"},
            {"role": "assistant", "content": "", "tool_calls": [{"id": "b", "type": "function", "function": {"name": "f"}}], "reasoning_details": [{"type": "reasoning.encrypted", "data": "OLD2", "id": "b"}]},
            {"role": "tool", "name": "f", "tool_call_id": "b", "content": "r2"},
            {"role": "assistant", "content": "", "tool_calls": [{"id": "c", "type": "function", "function": {"name": "f"}}], "reasoning_details": [{"type": "reasoning.encrypted", "data": "CURRENT", "id": "c"}]},
            {"role": "tool", "name": "f", "tool_call_id": "c", "content": "r3"},
        ]
        non_anthropic_or_client._postprocess_messages_for_provider(msgs)
        # First two assistants: reasoning_details removed
        assert "reasoning_details" not in msgs[2]
        assert "reasoning_details" not in msgs[4]
        # Last assistant: reasoning_details preserved verbatim
        assert msgs[6]["reasoning_details"] == [{"type": "reasoning.encrypted", "data": "CURRENT", "id": "c"}]
        # tool_calls untouched everywhere
        assert msgs[2]["tool_calls"][0]["id"] == "a"
        assert msgs[6]["tool_calls"][0]["id"] == "c"

    def test_detect_body_400_signature_issue(self, non_anthropic_or_client):
        """Body-level error code 400 is detected regardless of message detail.
        OpenRouter often strips the underlying Google error to a generic
        'Provider returned error', so the detector must trigger on the 400
        alone - the caller gates on whether RD exists to strip/bypass."""
        client = non_anthropic_or_client
        # Generic 400 (no metadata.raw) - still detected
        assert client._detect_body_400_signature_issue(
            {"error": {"code": 400, "message": "Provider returned error"}}
        ) is not None
        # 400 with metadata.raw - detected, message includes raw
        result = client._detect_body_400_signature_issue({
            "error": {
                "code": 400,
                "message": "Provider returned error",
                "metadata": {"raw": "{\"error\":{\"message\":\"Corrupted thought signature.\"}}"},
            }
        })
        assert result is not None
        assert "Corrupted thought signature" in result
        # 429 not detected as 400
        assert client._detect_body_400_signature_issue(
            {"error": {"code": 429, "message": "Too many requests"}}
        ) is None
        # No error key
        assert client._detect_body_400_signature_issue({"choices": []}) is None

    def test_inject_signature_bypass_replaces_data_in_encrypted_blocks(self, non_anthropic_or_client):
        """Bypass injection replaces `data` in every `reasoning.encrypted`
        block across all assistant messages. Structure (type, format, id, index)
        stays intact so OpenRouter still translates to Google's native format."""
        client = non_anthropic_or_client
        payload = {"messages": [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "hi"},
            {"role": "assistant", "tool_calls": [{"id": "a"}], "reasoning_details": [
                {"type": "reasoning.encrypted", "data": "REAL_SIG_A", "format": "google-gemini-v1", "id": "a", "index": 0},
            ]},
            {"role": "tool", "tool_call_id": "a", "content": "r1"},
            {"role": "assistant", "tool_calls": [{"id": "b"}], "reasoning_details": [
                {"type": "reasoning.text", "text": "internal thinking", "format": "google-gemini-v1"},
                {"type": "reasoning.encrypted", "data": "REAL_SIG_B", "format": "google-gemini-v1", "id": "b", "index": 1},
            ]},
            {"role": "tool", "tool_call_id": "b", "content": "r2"},
        ]}
        n = client._inject_signature_bypass(payload)
        assert n == 2
        # Encrypted blocks now carry the bypass token
        rd_a = payload["messages"][2]["reasoning_details"][0]
        assert rd_a["data"] == "skip_thought_signature_validator"
        assert rd_a["id"] == "a"
        assert rd_a["type"] == "reasoning.encrypted"
        assert rd_a["index"] == 0
        # text block left alone
        assert payload["messages"][4]["reasoning_details"][0]["text"] == "internal thinking"
        assert "data" not in payload["messages"][4]["reasoning_details"][0]
        # Encrypted block on second assistant also patched
        assert payload["messages"][4]["reasoning_details"][1]["data"] == "skip_thought_signature_validator"
        # Idempotent: running again patches nothing more
        assert client._inject_signature_bypass(payload) == 0

    def test_inject_signature_bypass_handles_missing_rd(self, non_anthropic_or_client):
        """No-op on payloads with no reasoning_details."""
        payload = {"messages": [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "answer"},
        ]}
        assert non_anthropic_or_client._inject_signature_bypass(payload) == 0

    def test_reasoning_details_kept_when_only_one_assistant(self, non_anthropic_or_client):
        """Single assistant message keeps its reasoning_details (it IS the current turn)."""
        msgs = [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "", "tool_calls": [{"id": "a", "type": "function", "function": {"name": "f"}}], "reasoning_details": [{"type": "reasoning.encrypted", "data": "X"}]},
            {"role": "tool", "name": "f", "tool_call_id": "a", "content": "r"},
        ]
        non_anthropic_or_client._postprocess_messages_for_provider(msgs)
        assert msgs[2]["reasoning_details"] == [{"type": "reasoning.encrypted", "data": "X"}]

    # --- reasoning_details_mode (config-driven, no model-name detection) ------

    @staticmethod
    def _rd_client(mode=None):
        kw = {} if mode is None else {"reasoning_details_mode": mode}
        return HTTPXOpenAIClient(
            model="openai/gpt-5.6-terra",
            api_key="sk-or-test",
            base_url="https://openrouter.ai/api/v1",
            **kw,
        )

    @staticmethod
    def _rd_two_assistants():
        return [
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "", "tool_calls": [{"id": "a", "type": "function", "function": {"name": "f"}}], "reasoning_details": [{"type": "reasoning.encrypted", "id": "rs_1", "data": "X"}]},
            {"role": "tool", "name": "f", "tool_call_id": "a", "content": "r"},
            {"role": "assistant", "content": "", "tool_calls": [{"id": "b", "type": "function", "function": {"name": "g"}}], "reasoning_details": [{"type": "reasoning.encrypted", "id": "rs_2", "data": "Y"}]},
        ]

    def test_reasoning_mode_default_is_keep_last(self):
        """No config → keep_last: only the most recent assistant keeps reasoning_details."""
        c = self._rd_client()
        assert c.reasoning_details_mode == "keep_last"
        msgs = self._rd_two_assistants()
        c._postprocess_messages_for_provider(msgs)
        assert "reasoning_details" not in msgs[1]   # older stripped
        assert "reasoning_details" in msgs[3]        # latest kept

    def test_reasoning_mode_keep_all_preserves_chain(self):
        """keep_all → every assistant message keeps reasoning_details (OpenAI chain)."""
        c = self._rd_client("keep_all")
        msgs = self._rd_two_assistants()
        c._postprocess_messages_for_provider(msgs)
        assert "reasoning_details" in msgs[1]
        assert "reasoning_details" in msgs[3]

    def test_reasoning_mode_strip_drops_all(self):
        """strip → no assistant message keeps reasoning_details."""
        c = self._rd_client("strip")
        msgs = self._rd_two_assistants()
        c._postprocess_messages_for_provider(msgs)
        assert all("reasoning_details" not in m for m in msgs if m["role"] == "assistant")

    # --- rd_orphaned: history-mutation invalidation (reasoning_artifacts) ----

    def test_keep_all_strips_orphaned_message(self):
        """keep_all: a message flagged rd_orphaned (its chain predecessors were
        removed by compaction) must lose its reasoning_details — sending a
        partial chain is exactly the 'could not be verified' 400. Later,
        unflagged messages keep theirs (fresh chain).

        A CLOSED turn: an orphaned turn whose tool_calls are still open is the
        one exception (see test_reasoning_details_prefix_stability.py)."""
        c = self._rd_client("keep_all")
        msgs = self._rd_two_assistants()
        msgs[1].pop("tool_calls")
        msgs[1]["content"] = "done"
        msgs[1]["rd_orphaned"] = True  # older turn survived a mutation flagged
        c._postprocess_messages_for_provider(msgs)
        assert "reasoning_details" not in msgs[1]   # orphaned → stripped
        assert "reasoning_details" in msgs[3]        # fresh chain → kept
        # flag must never reach the provider
        assert all("rd_orphaned" not in m for m in msgs)

    def test_keep_all_without_orphan_keeps_everything(self):
        c = self._rd_client("keep_all")
        msgs = self._rd_two_assistants()
        c._postprocess_messages_for_provider(msgs)
        assert "reasoning_details" in msgs[1]
        assert "reasoning_details" in msgs[3]

    def test_keep_last_ignores_orphan_on_latest(self):
        """keep_last (Gemini): the latest signature is required for the open
        tool round-trip even right after a mutation — the flag is ignored,
        only popped."""
        c = self._rd_client()  # default keep_last
        msgs = self._rd_two_assistants()
        msgs[3]["rd_orphaned"] = True
        c._postprocess_messages_for_provider(msgs)
        assert "reasoning_details" in msgs[3]        # latest kept despite flag
        assert "reasoning_details" not in msgs[1]    # older stripped as usual
        assert all("rd_orphaned" not in m for m in msgs)

    def test_orphan_flag_survives_sanitize_then_popped(self):
        """rd_orphaned must survive _sanitize_message_for_api (whitelisted) so
        postprocess — which runs after serialization — can see it, and then be
        removed."""
        c = self._rd_client("keep_all")
        raw = {"role": "assistant", "content": "", "rd_orphaned": True,
               "reasoning_details": [{"data": "X"}], "estimated_tokens": 42}
        clean = HTTPXOpenAIClient._sanitize_message_for_api(raw)
        assert clean.get("rd_orphaned") is True      # whitelisted
        assert "estimated_tokens" not in clean         # junk still filtered
        msgs = [clean]
        c._postprocess_messages_for_provider(msgs)
        assert "rd_orphaned" not in msgs[0]           # popped before payload

    # --- OpenAI encrypted-reasoning cross-backend 400 (A+B fix) ---------------

    def test_detect_openai_encrypted_reasoning_400(self, non_anthropic_or_client):
        """Detects the OpenAI 'encrypted content for item rs_…' 400, and NOT
        the Gemini signature 400 / non-400 bodies."""
        client = non_anthropic_or_client
        # The real failure shape: OpenRouter wraps OpenAI's message in metadata.raw
        result = client._detect_openai_encrypted_reasoning_400({
            "error": {
                "code": 400,
                "message": "Provider returned error",
                "metadata": {"raw": "{\"error\":{\"message\":\"The encrypted content for item rs_0666abc could not be verified.\"}}"},
            }
        })
        assert result is not None
        assert "encrypted content" in result
        # message-only form (no metadata.raw)
        assert client._detect_openai_encrypted_reasoning_400({
            "error": {"code": 400, "message": "The encrypted content for item rs_9 could not be verified"}
        }) is not None
        # Gemini signature 400 must NOT match (different recovery path)
        assert client._detect_openai_encrypted_reasoning_400({
            "error": {"code": 400, "message": "Corrupted thought signature"}
        }) is None
        # 429 / no-error bodies
        assert client._detect_openai_encrypted_reasoning_400(
            {"error": {"code": 429, "message": "rate limit"}}
        ) is None
        assert client._detect_openai_encrypted_reasoning_400({"choices": []}) is None

    def test_strip_reasoning_details_removes_from_all_assistants(self, non_anthropic_or_client):
        """Recovery drops reasoning_details from every assistant message and
        returns the count; user/tool/system messages are untouched."""
        client = non_anthropic_or_client
        payload = {"messages": [
            {"role": "system", "content": "sys"},
            {"role": "assistant", "content": "a1", "reasoning_details": [{"type": "reasoning.encrypted", "data": "X"}]},
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "a2", "reasoning_details": [{"type": "reasoning.text", "text": "t"}]},
            {"role": "tool", "name": "f", "tool_call_id": "a", "content": "r"},
        ]}
        n = client._strip_reasoning_details(payload)
        assert n == 2
        assert all("reasoning_details" not in m for m in payload["messages"])
        # No-op when nothing to strip
        assert client._strip_reasoning_details(
            {"messages": [{"role": "assistant", "content": "x"}]}
        ) == 0

    def test_openrouter_has_default_app_headers(self, anthropic_or_client):
        """OpenRouter clients get default X-Title and HTTP-Referer at init."""
        assert anthropic_or_client._headers["X-Title"] == "ScarabHive"
        assert "HTTP-Referer" in anthropic_or_client._headers

    def test_set_app_title_sets_unique_referer(self, anthropic_or_client):
        """set_app_title creates per-agent referer for unique OpenRouter app_id."""
        anthropic_or_client.set_app_title("coding_agent")
        assert anthropic_or_client._headers["X-Title"] == "coding_agent"
        assert anthropic_or_client._headers["HTTP-Referer"].endswith("/coding_agent")

    def test_set_app_title_noop_on_non_openrouter(self):
        """set_app_title is a no-op for non-OpenRouter clients."""
        client = HTTPXOpenAIClient(
            model="gpt-5",
            api_key="sk-test",
            base_url="https://api.openai.com/v1",
        )
        client.set_app_title("coding_agent")
        assert "X-Title" not in client._headers


class TestContentFilterFallback:
    """Provider content-filter blocks (Gemini PROHIBITED_CONTENT etc.) must
    surface as assistant.error so the server-side fallback-profile mechanism
    can switch to llm_profile_fallbacks. Retrying the same model is pointless —
    the filter is deterministic per content."""

    @pytest.fixture
    def gemini_client(self):
        return HTTPXOpenAIClient(
            model="google/gemini-3.1-pro-preview",
            api_key="test-key",
            base_url="https://openrouter.ai/api/v1",
        )

    def test_content_filter_surfaces_as_error(self, gemini_client):
        """finish_reason=content_filter without tool_calls → assistant.error set."""
        response_data = {
            "choices": [{
                "message": {
                    "role": "assistant",
                    "content": "{\"partial\": \"garbled JSON before filter cut",
                },
                "finish_reason": "content_filter",
                "native_finish_reason": "PROHIBITED_CONTENT",
            }]
        }
        result = gemini_client._format_response(response_data)
        assistant = result["assistant"]
        assert "error" in assistant
        assert assistant["error"]["type"] == "content_filter_prohibited_content"
        assert "blocked" in assistant["error"]["message"].lower()
        assert assistant["content"] == ""

    def test_content_filter_with_tool_calls_keeps_them(self, gemini_client):
        """If tool_calls present despite content_filter, use them (don't error)."""
        response_data = {
            "choices": [{
                "message": {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [{
                        "id": "1", "type": "function",
                        "function": {"name": "f", "arguments": "{}"},
                    }],
                },
                "finish_reason": "content_filter",
                "native_finish_reason": "PROHIBITED_CONTENT",
            }]
        }
        result = gemini_client._format_response(response_data)
        assistant = result["assistant"]
        assert "error" not in assistant
        assert assistant.get("tool_calls")

    def test_content_filter_native_reason_fallback(self, gemini_client):
        """native_finish_reason missing → still surfaced with generic suffix."""
        response_data = {
            "choices": [{
                "message": {"role": "assistant", "content": ""},
                "finish_reason": "content_filter",
            }]
        }
        result = gemini_client._format_response(response_data)
        assistant = result["assistant"]
        assert "error" in assistant
        assert assistant["error"]["type"] == "content_filter_content_filter"

    def test_normal_finish_reason_no_error(self, gemini_client):
        """finish_reason=stop has no error injected."""
        response_data = {
            "choices": [{
                "message": {"role": "assistant", "content": "hello"},
                "finish_reason": "stop",
            }]
        }
        result = gemini_client._format_response(response_data)
        assert "error" not in result["assistant"]
        assert result["assistant"]["content"] == "hello"

    def test_an_unparseable_response_is_an_error_not_an_empty_answer(
            self, gemini_client):
        """A client-side parse failure used to return bare empty content.

        The agent loop cannot tell that from "the model said nothing": it
        retries the same model, files a content issue, and never reaches the
        fallback chain — while the actual cause is an unexpected response
        shape from a new gateway backend.
        """
        class Exploding(dict):
            def get(self, *args, **kwargs):
                raise TypeError("unexpected shape")

        result = gemini_client._format_response(Exploding())
        assistant = result["assistant"]
        assert assistant["content"] == ""
        assert assistant.get("error"), "parse failure looked like an empty answer"
        assert assistant["error"]["type"] == "response_format_error"
        assert "unexpected shape" in assistant["error"]["message"]


class TestEncryptedReasoningRecovery:
    """Two-stage recovery for the encrypted-reasoning 400.

    Root cause (empirical, via replay against a pinned provider): OpenRouter's
    bridge occasionally delivers a defective encrypted blob for turns with
    several parallel tool_calls -- rejected deterministically, all other
    items of the chain keep verifying. Stage 0 therefore removes ONLY the
    item named in the error (payload + original session), stage 1 strips
    everything.
    """

    @staticmethod
    def _client():
        return HTTPXOpenAIClient(
            model="openai/gpt-5.6-terra", api_key="sk-or-test",
            base_url="https://openrouter.ai/api/v1",
            reasoning_details_mode="keep_all",
        )

    @staticmethod
    def _payload_and_session():
        def mk(rs_id, data):
            return {"role": "assistant", "content": "",
                    "tool_calls": [{"id": f"c_{rs_id}", "type": "function", "function": {"name": "f"}}],
                    "reasoning_details": [
                        {"type": "reasoning.summary", "format": "openai-responses-v1", "index": 0, "summary": "s"},
                        {"type": "reasoning.encrypted", "format": "openai-responses-v1", "index": 1, "id": rs_id, "data": data},
                    ]}
        session = [
            {"role": "user", "content": "hi"},
            mk("rs_aaa", "A"), {"role": "tool", "tool_call_id": "c_rs_aaa", "content": "r"},
            mk("rs_bbb", "B"), {"role": "tool", "tool_call_id": "c_rs_bbb", "content": "r"},
            mk("rs_ccc", "C"), {"role": "tool", "tool_call_id": "c_rs_ccc", "content": "r"},
        ]
        import copy
        payload = {"model": "openai/gpt-5.6-terra", "messages": copy.deepcopy(session)}
        return payload, session

    DETAIL = ('The encrypted content for item rs_bbb could not be verified. '
              'Reason: Encrypted content could not be decrypted or parsed.')

    def test_stage0_targeted_strip_payload_and_session(self):
        c = self._client()
        payload, session = self._payload_and_session()
        reason = c._recover_encrypted_reasoning(self.DETAIL, payload, session, 0)
        assert reason and "targeted" in reason and "rs_bbb" in reason
        # only the rs_bbb message loses its reasoning_details -- on both sides
        for msgs in (payload["messages"], session):
            assert "reasoning_details" in msgs[1]      # rs_aaa stays
            assert "reasoning_details" not in msgs[3]  # rs_bbb gone
            assert "reasoning_details" in msgs[5]      # rs_ccc stays

    def test_stage1_full_strip(self):
        c = self._client()
        payload, session = self._payload_and_session()
        reason = c._recover_encrypted_reasoning(self.DETAIL, payload, session, 1)
        assert reason and "full strip" in reason
        for msgs in (payload["messages"], session):
            assert all("reasoning_details" not in m for m in msgs if m.get("role") == "assistant")

    def test_stage2_no_more_recovery(self):
        c = self._client()
        payload, session = self._payload_and_session()
        assert c._recover_encrypted_reasoning(self.DETAIL, payload, session, 2) is None

    def test_missing_item_id_falls_back_to_full_strip(self):
        c = self._client()
        payload, session = self._payload_and_session()
        reason = c._recover_encrypted_reasoning("encrypted content bad", payload, session, 0)
        assert reason and "full strip" in reason

    def test_unknown_item_id_falls_back_to_full_strip(self):
        c = self._client()
        payload, session = self._payload_and_session()
        detail = "The encrypted content for item rs_zzz could not be verified."
        reason = c._recover_encrypted_reasoning(detail, payload, session, 0)
        assert reason and "full strip" in reason

    def test_strip_reasoning_details_by_item_id(self):
        payload, _ = self._payload_and_session()
        n = HTTPXOpenAIClient._strip_reasoning_details(payload, item_id="rs_ccc")
        assert n == 1
        assert "reasoning_details" in payload["messages"][1]
        assert "reasoning_details" not in payload["messages"][5]


class TestCacheBreakpoints:
    """GPT-5.6-Cache-Breakpoints: Sentinel-Split (OpenAI) vs Strip (Fremd-Provider)."""

    def _client(self, pck=None):
        c = create_test_client()
        c.prompt_cache_key = pck
        return c

    def test_with_key_splits_into_marked_parts(self):
        from agent_system.llm.cache_key import CACHE_BP_SENTINEL
        msgs = [{"role": "user",
                 "content": "stabil" + CACHE_BP_SENTINEL + "variabel"}]
        self._client(pck="auto")._apply_cache_breakpoints(msgs)
        parts = msgs[0]["content"]
        assert [p["text"] for p in parts] == ["stabil", "variabel"]
        assert parts[0]["prompt_cache_breakpoint"] == {"mode": "explicit"}
        assert "prompt_cache_breakpoint" not in parts[1]

    def test_without_key_strips_sentinel(self):
        # deepseek fallback and the like: foreign APIs know neither marker nor
        # field -- the content stays a plain string without sentinel leftovers.
        from agent_system.llm.cache_key import CACHE_BP_SENTINEL
        msgs = [{"role": "user",
                 "content": "zeile1\n" + CACHE_BP_SENTINEL + "zeile2"}]
        self._client(pck=None)._apply_cache_breakpoints(msgs)
        assert msgs[0]["content"] == "zeile1\nzeile2"

    def test_content_without_sentinel_untouched(self):
        msgs = [{"role": "user", "content": "normaler task"},
                {"role": "assistant", "content": None}]
        self._client(pck="auto")._apply_cache_breakpoints(msgs)
        assert msgs[0]["content"] == "normaler task"
        assert msgs[1]["content"] is None


class TestAnthropicCacheControl:
    """Anthropic (via OpenRouter) prompt caching: system + tools + multi-turn
    conversation tail, all within the hard 4-block cache_control limit."""

    def _client(self, mode=None):
        c = create_test_client()
        c.prompt_cache_marker_style = "anthropic"
        c.assistant_reasoning_field = "omit"
        c.prompt_cache_mode = mode
        c.reasoning_details_mode = "keep_last"
        return c

    @staticmethod
    def _count(msgs, tools=None):
        n = 0
        for t in (tools or []):
            if isinstance(t, dict) and "cache_control" in t:
                n += 1
        for m in msgs:
            content = m.get("content")
            if isinstance(content, list):
                n += sum(1 for p in content
                         if isinstance(p, dict) and "cache_control" in p)
            elif "cache_control" in m:
                n += 1
        return n

    def _conversation(self):
        return [
            {"role": "system", "content": "BASE"},
            {"role": "system", "content": "OKF CONTEXT"},
            {"role": "system", "content": "MEMORY"},
            {"role": "user", "content": "erste frage"},
            {"role": "assistant", "content": "antwort"},
            {"role": "tool", "content": "grosser ssh output"},
            {"role": "user", "content": "zweite frage"},
        ]

    def test_multi_turn_marks_system_tail_tool_within_limit(self):
        msgs = self._conversation()
        tools = [{"function": {"name": "a"}}, {"function": {"name": "ssh"}}]
        c = self._client("multi_turn")
        c._postprocess_messages_for_provider(msgs)
        c._apply_anthropic_tool_cache_control(tools)
        c._cap_anthropic_cache_control(msgs, tools)
        # last leading system + conversation tail + last tool = 3, <= 4
        assert self._count(msgs, tools) == 3
        assert "cache_control" in str(msgs[2]["content"])   # last system
        assert "cache_control" in str(msgs[-1]["content"])  # tail
        assert "cache_control" in tools[-1]

    def test_only_last_system_marked_never_exceeds_limit(self):
        # Regression: marking EVERY system message + tools blew the 4-limit
        # (HTTP 400). Many system injections must still yield 1 system marker.
        msgs = [{"role": "system", "content": f"sys{i}"} for i in range(6)]
        msgs.append({"role": "user", "content": "frage"})
        c = self._client("multi_turn")
        c._postprocess_messages_for_provider(msgs)
        system_markers = sum(
            1 for m in msgs if m.get("role") == "system"
            and isinstance(m.get("content"), list)
            and any("cache_control" in p for p in m["content"]))
        assert system_markers == 1

    def test_auto_turn1_no_history_skips_tail(self):
        msgs = [{"role": "system", "content": "BASE"},
                {"role": "user", "content": "frage"}]
        self._client(None)._postprocess_messages_for_provider(msgs)
        assert "cache_control" not in str(msgs[1]["content"])  # no tail
        assert "cache_control" in str(msgs[0]["content"])      # system yes

    def test_auto_turn2_with_history_marks_tail(self):
        msgs = [{"role": "system", "content": "BASE"},
                {"role": "user", "content": "a"},
                {"role": "assistant", "content": "b"},
                {"role": "user", "content": "c"}]
        self._client(None)._postprocess_messages_for_provider(msgs)
        assert "cache_control" in str(msgs[-1]["content"])  # reactive tail

    def test_task_sequence_and_off_skip_tail(self):
        for mode in ("task_sequence", "one_shot", "off"):
            msgs = self._conversation()
            self._client(mode)._postprocess_messages_for_provider(msgs)
            assert "cache_control" not in str(msgs[-1]["content"]), mode

    def test_cap_trims_to_four_keeping_latest(self):
        over = [{"role": "system",
                 "content": [{"type": "text", "text": f"s{i}",
                              "cache_control": {"type": "ephemeral"}}]}
                for i in range(6)]
        HTTPXOpenAIClient._cap_anthropic_cache_control(over, None)
        assert self._count(over) == 4
        # earliest two dropped, latest four kept
        assert all("cache_control" not in over[i]["content"][0] for i in (0, 1))
        assert all("cache_control" in over[i]["content"][0] for i in range(2, 6))

    def test_cap_counts_tools_and_messages_together(self):
        tools = [{"function": {"name": "a"}, "cache_control": {"type": "ephemeral"}}]
        msgs = [{"role": "system",
                 "content": [{"type": "text", "text": f"s{i}",
                              "cache_control": {"type": "ephemeral"}}]}
                for i in range(4)]
        HTTPXOpenAIClient._cap_anthropic_cache_control(msgs, tools)
        assert self._count(msgs, tools) == 4  # 1 tool + 4 msgs = 5 -> 4


if __name__ == "__main__":
    # Run tests with pytest when executed directly
    pytest.main([__file__, "-v"])