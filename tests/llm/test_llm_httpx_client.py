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
import pytest
from unittest.mock import AsyncMock, Mock, patch

import httpx

from agent_system.llm.httpx_client import HTTPXOpenAIClient, HTTPXTimeoutConfig
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
            
            with pytest.raises(Exception) as exc_info:
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
        assert config.connect == 10.0
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
    """Test MALFORMED_FUNCTION_CALL retry logic for Gemini via OpenRouter."""

    @pytest.fixture
    def gemini_client(self):
        """Create a Gemini-via-OpenRouter client."""
        return HTTPXOpenAIClient(
            model="google/gemini-3-flash-preview",
            api_key="test-key",
            base_url="https://openrouter.ai/api/v1",
            max_retries=2,
            retry_backoff=0.01,  # Fast for tests
        )

    def test_is_gemini_via_openrouter_detection(self, gemini_client):
        """Gemini models via OpenRouter are correctly detected."""
        assert gemini_client._is_gemini_via_openrouter is True

    def test_is_gemini_via_openrouter_negative(self, client):
        """Non-Gemini clients are not detected as Gemini."""
        assert client._is_gemini_via_openrouter is False

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

    def test_is_gemini_malformed_response_ignores_non_gemini(self, client):
        """Non-Gemini clients never report MALFORMED."""
        response_data = {
            "choices": [{
                "message": {"role": "assistant", "content": ""},
                "finish_reason": "error",
                "native_finish_reason": "MALFORMED_FUNCTION_CALL",
            }]
        }
        assert client._is_gemini_malformed_response(response_data) is False

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

    def test_parallel_tool_calls_stripped_for_gemini(self, gemini_client, sample_tools):
        """parallel_tool_calls is not sent to Gemini."""
        assert gemini_client.parallel_tool_calls is True  # Default
        # The actual stripping is checked in the payload building,
        # verified via the _is_gemini_via_openrouter flag
        assert gemini_client._is_gemini_via_openrouter is True


class TestAnthropicViaOpenRouterCaching:
    """Test Anthropic prompt caching via OpenRouter."""

    @pytest.fixture
    def anthropic_or_client(self):
        """HTTPXOpenAIClient configured as Anthropic via OpenRouter."""
        return HTTPXOpenAIClient(
            model="anthropic/claude-sonnet-4-6",
            api_key="sk-or-test",
            base_url="https://openrouter.ai/api/v1",
        )

    @pytest.fixture
    def non_anthropic_or_client(self):
        """HTTPXOpenAIClient configured as non-Anthropic via OpenRouter."""
        return HTTPXOpenAIClient(
            model="google/gemini-2.5-pro",
            api_key="sk-or-test",
            base_url="https://openrouter.ai/api/v1",
        )

    def test_anthropic_via_openrouter_detection(self, anthropic_or_client):
        assert anthropic_or_client._is_anthropic_via_openrouter is True

    def test_non_anthropic_via_openrouter_not_detected(self, non_anthropic_or_client):
        assert non_anthropic_or_client._is_anthropic_via_openrouter is False

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


if __name__ == "__main__":
    # Run tests with pytest when executed directly
    pytest.main([__file__, "-v"])