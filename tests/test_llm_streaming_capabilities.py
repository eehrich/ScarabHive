"""Test that streaming can be disabled via model capabilities."""

import pytest
from unittest.mock import AsyncMock, patch
from agent_system.llm.httpx_client import HTTPXOpenAIClient
from agent_system.llm.openai_client import OpenAIAsyncClient
from agent_system.llm.ollama_client import OllamaNativeAsyncClient
from agent_system.config.models import ModelCapabilitiesConfig


class TestHTTPXClientCapabilities:
    """Test HTTPX client streaming capabilities."""

    def test_streaming_enabled_by_default(self):
        """Test that streaming is enabled by default when no capabilities provided."""
        client = HTTPXOpenAIClient(
            model="gpt-5-mini",
            api_key="test-key"
        )
        assert client.supports_streaming() is True

    def test_streaming_enabled_explicitly(self):
        """Test that streaming works when explicitly enabled in capabilities."""
        caps = ModelCapabilitiesConfig(streaming=True)
        client = HTTPXOpenAIClient(
            model="gpt-5-mini",
            api_key="test-key",
            capabilities=caps
        )
        assert client.supports_streaming() is True

    def test_streaming_disabled_in_capabilities(self):
        """Test that streaming is disabled when set to False in capabilities."""
        caps = ModelCapabilitiesConfig(streaming=False)
        client = HTTPXOpenAIClient(
            model="gpt-5-mini",
            api_key="test-key",
            capabilities=caps
        )
        assert client.supports_streaming() is False

    def test_capabilities_config_from_dict(self):
        """Test that ModelCapabilitiesConfig can be created from dict (YAML loading)."""
        # This simulates how YAML config is loaded
        config_dict = {
            "tools": True,
            "streaming": False,
            "json_mode": True
        }
        caps = ModelCapabilitiesConfig(**config_dict)
        
        assert caps.streaming is False
        assert caps.tools is True
        assert caps.json_mode is True

    def test_empty_capabilities_object(self):
        """Test that empty capabilities dict defaults streaming to True."""
        caps = ModelCapabilitiesConfig()
        client = HTTPXOpenAIClient(
            model="gpt-5-mini",
            api_key="test-key",
            capabilities=caps
        )
        assert client.supports_streaming() is True  # Default is True

    @pytest.mark.asyncio
    async def test_non_streaming_request_used_when_disabled(self):
        """Test that non-streaming request method is used when streaming is disabled."""
        capabilities = ModelCapabilitiesConfig(streaming=False)
        client = HTTPXOpenAIClient(
            api_key="test_key",
            model="gpt-5-mini",
            capabilities=capabilities
        )
        
        # Mock the non-streaming method to verify it gets called
        mock_response = {"assistant": {"role": "assistant", "content": "Test response"}}
        
        with patch.object(client, '_make_request_non_streaming', new_callable=AsyncMock) as mock_non_streaming:
            mock_non_streaming.return_value = mock_response
            
            result = await client._make_request(
                messages=[{"role": "user", "content": "test"}],
                tools=[]
            )
            
            # Verify non-streaming method was called
            mock_non_streaming.assert_called_once()
            assert result == mock_response

    @pytest.mark.asyncio
    async def test_streaming_request_used_when_enabled(self):
        """Test that streaming request method is used when streaming is enabled."""
        capabilities = ModelCapabilitiesConfig(streaming=True)
        client = HTTPXOpenAIClient(
            api_key="test_key",
            model="gpt-4",
            capabilities=capabilities
        )
        
        # Mock the streaming method to verify it gets called
        async def mock_streaming_gen(*args, **kwargs):
            yield {"type": "content_delta", "delta": "Test"}
            yield {"type": "final", "assistant": {"role": "assistant", "content": "Test response"}}
        
        with patch.object(client, '_make_request_streaming', side_effect=mock_streaming_gen):
            result = await client._make_request(
                messages=[{"role": "user", "content": "test"}],
                tools=[]
            )
            
            # Verify we got the final result
            assert result["assistant"]["content"] == "Test response"


class TestOpenAIClientCapabilities:
    """Test OpenAI SDK client streaming capabilities."""

    def test_streaming_enabled_by_default(self):
        """Test that streaming is enabled by default."""
        client = OpenAIAsyncClient(
            model="gpt-4",
            api_key="test-key"
        )
        assert client.supports_streaming() is True

    def test_streaming_disabled_in_capabilities(self):
        """Test that streaming can be disabled via capabilities."""
        caps = ModelCapabilitiesConfig(streaming=False)
        client = OpenAIAsyncClient(
            model="gpt-4",
            api_key="test-key",
            capabilities=caps
        )
        assert client.supports_streaming() is False

    def test_streaming_enabled_explicitly(self):
        """Test that streaming can be explicitly enabled."""
        caps = ModelCapabilitiesConfig(streaming=True)
        client = OpenAIAsyncClient(
            model="gpt-4",
            api_key="test-key",
            capabilities=caps
        )
        assert client.supports_streaming() is True


class TestOllamaClientCapabilities:
    """Test Ollama client streaming capabilities."""

    def test_streaming_enabled_by_default(self):
        """Test that streaming is enabled by default."""
        client = OllamaNativeAsyncClient(
            model="llama2"
        )
        assert client.supports_streaming() is True

    def test_streaming_disabled_in_capabilities(self):
        """Test that streaming can be disabled via capabilities."""
        caps = ModelCapabilitiesConfig(streaming=False)
        client = OllamaNativeAsyncClient(
            model="llama2",
            capabilities=caps
        )
        assert client.supports_streaming() is False

    def test_streaming_enabled_explicitly(self):
        """Test that streaming can be explicitly enabled."""
        caps = ModelCapabilitiesConfig(streaming=True)
        client = OllamaNativeAsyncClient(
            model="llama2",
            capabilities=caps
        )
        assert client.supports_streaming() is True

