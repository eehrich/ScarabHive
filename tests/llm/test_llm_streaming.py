"""Tests for LLM streaming functionality."""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from agent_system.llm.httpx_client import HTTPXOpenAIClient, HTTPXTimeoutConfig
from agent_system.llm.openai_client import OpenAIAsyncClient
from agent_system.llm.models import ChatMessage


@pytest.fixture
def httpx_client():
    """Create HTTPXOpenAIClient for testing."""
    config = HTTPXTimeoutConfig(connect=10.0, read=180.0, write=10.0, pool=5.0)
    return HTTPXOpenAIClient(
        model="gpt-4o-mini",
        api_key="test-key",
        timeout_config=config,
        max_retries=1
    )


@pytest.fixture
def openai_client():
    """Create OpenAIAsyncClient for testing."""
    return OpenAIAsyncClient(
        model="gpt-4o-mini",
        api_key="test-key",
        timeout=60.0
    )


class TestHTTPXClientStreaming:
    """Test HTTPXOpenAIClient streaming functionality."""
    
    def test_supports_streaming(self, httpx_client):
        """HTTPXOpenAIClient should support streaming."""
        assert httpx_client.supports_streaming() is True
    
    @pytest.mark.asyncio
    async def test_streaming_content_only(self, httpx_client):
        """Test streaming with content-only response."""
        messages = [ChatMessage(role="user", content="Hello")]
        tools = []
        
        # Mock SSE stream response - now as bytes with newlines (for aiter_bytes)
        mock_stream_lines = [
            'data: {"choices":[{"delta":{"content":"Hello"}}]}',
            'data: {"choices":[{"delta":{"content":" there"}}]}',
            'data: {"choices":[{"delta":{"content":"!"}}]}',
            'data: [DONE]'
        ]
        mock_bytes = ('\n'.join(mock_stream_lines) + '\n').encode('utf-8')
        
        async def mock_aiter_bytes():
            yield mock_bytes
        
        mock_response = MagicMock()
        mock_response.aiter_bytes = mock_aiter_bytes
        mock_response.status_code = 200  # Add status code
        mock_response.__aenter__ = AsyncMock(return_value=mock_response)
        mock_response.__aexit__ = AsyncMock(return_value=None)
        
        # Patch httpx.AsyncClient.stream method
        with patch('httpx.AsyncClient.stream', return_value=mock_response):
            chunks = []
            async for chunk in httpx_client.chat_tools_streaming(messages, tools):
                chunks.append(chunk)
            
            # Should have content deltas + final
            assert len(chunks) == 4
            assert chunks[0]["type"] == "content_delta"
            assert chunks[0]["delta"] == "Hello"
            assert chunks[0]["accumulated"] == "Hello"
            
            assert chunks[1]["type"] == "content_delta"
            assert chunks[1]["delta"] == " there"
            assert chunks[1]["accumulated"] == "Hello there"
            
            assert chunks[2]["type"] == "content_delta"
            assert chunks[2]["delta"] == "!"
            assert chunks[2]["accumulated"] == "Hello there!"
            
            assert chunks[3]["type"] == "final"
            assert chunks[3]["assistant"]["content"] == "Hello there!"
    
    @pytest.mark.asyncio
    async def test_streaming_with_tool_calls(self, httpx_client):
        """Test streaming with tool call response."""
        messages = [ChatMessage(role="user", content="What's the weather?")]
        tools = [{"type": "function", "function": {"name": "get_weather", "parameters": {}}}]
        
        # Mock SSE stream with tool call deltas - now as bytes with newlines (for aiter_bytes)
        mock_stream_lines = [
            'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"id":"call_123","function":{"name":"get_weather"}}]}}]}',
            'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"function":{"arguments":"{\\"location\\""}}]}}]}',
            'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"function":{"arguments":":\\"Berlin\\"}"}}]}}]}',
            'data: [DONE]'
        ]
        mock_bytes = ('\n'.join(mock_stream_lines) + '\n').encode('utf-8')
        
        async def mock_aiter_bytes():
            yield mock_bytes
        
        mock_response = MagicMock()
        mock_response.aiter_bytes = mock_aiter_bytes
        mock_response.status_code = 200  # Add status code
        mock_response.__aenter__ = AsyncMock(return_value=mock_response)
        mock_response.__aexit__ = AsyncMock(return_value=None)
        
        # Patch httpx.AsyncClient.stream method
        with patch('httpx.AsyncClient.stream', return_value=mock_response):
            chunks = []
            async for chunk in httpx_client.chat_tools_streaming(messages, tools):
                chunks.append(chunk)
                print(f"Chunk {len(chunks)}: {chunk}")
            
            # Should have tool call deltas + final
            assert len(chunks) >= 1, f"Expected chunks but got: {chunks}"
            
            # Debug: print all chunks
            print(f"\nAll chunks ({len(chunks)}):")
            for i, c in enumerate(chunks):
                print(f"  [{i}] {c}")
            
            # Find tool_call_delta chunks
            tool_deltas = [c for c in chunks if c.get("type") == "tool_call_delta"]
            assert len(tool_deltas) >= 1, f"Expected tool_call_delta chunks but got: {chunks}"
            
            assert tool_deltas[0]["type"] == "tool_call_delta"
            assert tool_deltas[0]["index"] == 0
            assert tool_deltas[0]["accumulated"]["id"] == "call_123"
            assert tool_deltas[0]["accumulated"]["function"]["name"] == "get_weather"
            
            assert chunks[1]["type"] == "tool_call_delta"
            assert '{"location"' in chunks[1]["accumulated"]["function"]["arguments"]
            
            assert chunks[2]["type"] == "tool_call_delta"
            assert chunks[2]["accumulated"]["function"]["arguments"] == '{"location":"Berlin"}'
            
            assert chunks[3]["type"] == "final"
            assert len(chunks[3]["assistant"]["tool_calls"]) == 1
            assert chunks[3]["assistant"]["tool_calls"][0]["function"]["name"] == "get_weather"
            assert chunks[3]["assistant"]["tool_calls"][0]["function"]["arguments"] == '{"location":"Berlin"}'


class TestOpenAIClientStreaming:
    """Test OpenAIAsyncClient streaming functionality."""
    
    def test_supports_streaming(self, openai_client):
        """OpenAIAsyncClient should support streaming."""
        assert openai_client.supports_streaming() is True
    
    @pytest.mark.asyncio
    async def test_streaming_content_only(self, openai_client):
        """Test streaming with content-only response."""
        messages = [ChatMessage(role="user", content="Hello")]
        tools = []
        
        # Mock streaming chunks from OpenAI SDK
        class MockDelta:
            def __init__(self, content=None, tool_calls=None):
                self.content = content
                self.tool_calls = tool_calls
        
        class MockChoice:
            def __init__(self, delta):
                self.delta = delta
        
        class MockChunk:
            def __init__(self, choices):
                self.choices = choices
        
        async def mock_create(**kwargs):
            """Async function that returns an async generator (like real SDK)."""
            class AsyncStream:
                def __init__(self):
                    self._chunks = [
                        MockChunk([MockChoice(MockDelta(content="Hello"))]),
                        MockChunk([MockChoice(MockDelta(content=" world"))]),
                        MockChunk([MockChoice(MockDelta(content="!"))]),
                    ]
                    self._index = 0
                
                def __aiter__(self):
                    return self
                
                async def __anext__(self):
                    if self._index >= len(self._chunks):
                        raise StopAsyncIteration
                    chunk = self._chunks[self._index]
                    self._index += 1
                    return chunk
            
            return AsyncStream()
        
        # Patch the create method to return our async function
        with patch.object(openai_client._client.chat.completions, 'create', side_effect=mock_create):
            chunks = []
            async for chunk in openai_client.chat_tools_streaming(messages, tools):
                chunks.append(chunk)
            
            # Should have content deltas + final
            assert len(chunks) == 4
            assert chunks[0]["type"] == "content_delta"
            assert chunks[0]["delta"] == "Hello"
            
            assert chunks[1]["type"] == "content_delta"
            assert chunks[1]["delta"] == " world"
            
            assert chunks[2]["type"] == "content_delta"
            assert chunks[2]["delta"] == "!"
            
            assert chunks[3]["type"] == "final"
            assert chunks[3]["assistant"]["content"] == "Hello world!"


class TestBaseLLMClientFallback:
    """Test BaseLLMClient streaming fallback."""
    
    @pytest.mark.asyncio
    async def test_default_streaming_fallback(self):
        """Test that base class provides non-streaming fallback."""
        from agent_system.llm.models import LLMClient
        
        class MockLLMClient(LLMClient):
            async def chat(self, messages, cancellation_token=None):
                return "Test response"
            
            async def chat_tools(self, messages, tools, cancellation_token=None, status_scope=None):
                return {"assistant": {"role": "assistant", "content": "Test response"}}
        
        client = MockLLMClient()
        
        # Should not support streaming by default
        assert client.supports_streaming() is False
        
        # Should fall back to non-streaming
        messages = [ChatMessage(role="user", content="Hello")]
        tools = []
        
        chunks = []
        async for chunk in client.chat_tools_streaming(messages, tools):
            chunks.append(chunk)
        
        # Should only yield final result
        assert len(chunks) == 1
        assert chunks[0]["type"] == "final"
        assert chunks[0]["assistant"]["content"] == "Test response"
