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
            
            async def mock_aiter_lines():
                for line in sse_lines:
                    yield line
            
            mock_stream_response.aiter_lines = mock_aiter_lines
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
            
            async def mock_aiter_lines():
                for line in sse_lines:
                    yield line
            
            mock_stream_response.aiter_lines = mock_aiter_lines
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
            
            async def mock_aiter_lines():
                for line in sse_lines:
                    yield line
            
            mock_stream_response.aiter_lines = mock_aiter_lines
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
        async def mock_aiter_lines_with_cancel():
            yield 'data: {"id":"chatcmpl-123","object":"chat.completion.chunk","created":1677652288,"model":"gpt-3.5-turbo","choices":[{"index":0,"delta":{"role":"assistant","content":""},"finish_reason":null}]}'
            await asyncio.sleep(0.05)
            token.cancel()  # Cancel during streaming
            await asyncio.sleep(0.05)
            # This line should never be reached due to cancellation check
            yield 'data: [DONE]'
        
        with patch("httpx.AsyncClient") as mock_async_client:
            mock_stream_response = AsyncMock()
            mock_stream_response.status_code = 200
            mock_stream_response.headers = {}
            mock_stream_response.aiter_lines = mock_aiter_lines_with_cancel
            mock_stream_response.__aenter__.return_value = mock_stream_response
            mock_stream_response.__aexit__.return_value = None
            
            mock_client = AsyncMock()
            mock_client.__aenter__.return_value = mock_client
            mock_client.__aexit__.return_value = None
            mock_client.stream = Mock(return_value=mock_stream_response)
            mock_async_client.return_value = mock_client
            
            with pytest.raises(asyncio.CancelledError):
                await client.chat(sample_messages, cancellation_token=token)
    
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
            
            async def mock_aiter_lines():
                for line in sse_lines:
                    yield line
            
            mock_stream_response.aiter_lines = mock_aiter_lines
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
                    
                    async def mock_aiter_lines():
                        for line in sse_lines:
                            yield line
                    
                    mock_success_response.aiter_lines = mock_aiter_lines
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
            
            async def mock_aiter_lines():
                for line in sse_lines:
                    yield line
            
            mock_success_response.aiter_lines = mock_aiter_lines
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
            
            async def mock_aiter_lines():
                for line in sse_lines:
                    yield line
            
            mock_success_response.aiter_lines = mock_aiter_lines
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
                
                async def mock_aiter_lines():
                    for line in sse_lines:
                        yield line
                
                mock_stream_response.aiter_lines = mock_aiter_lines
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
            
            async def mock_aiter_lines_with_cancel():
                token.cancel()
                raise asyncio.CancelledError()
                yield  # Never reached
            
            mock_stream_response.aiter_lines = mock_aiter_lines_with_cancel
            mock_stream_response.__aenter__.return_value = mock_stream_response
            mock_stream_response.__aexit__.return_value = None
            
            mock_client = AsyncMock()
            mock_client.__aenter__.return_value = mock_client
            mock_client.__aexit__.return_value = None
            mock_client.stream = Mock(return_value=mock_stream_response)
            mock_async_client.return_value = mock_client
            
            with pytest.raises(asyncio.CancelledError):
                await client.chat(sample_messages, cancellation_token=token)
            
            # Verify client context manager was properly exited
            mock_client.__aexit__.assert_called_once()


if __name__ == "__main__":
    # Run tests with pytest when executed directly
    pytest.main([__file__, "-v"])