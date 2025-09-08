from pathlib import Path
import pytest
import json
from unittest.mock import AsyncMock, Mock, patch
from io import StringIO
import sys

from agent_system.mcp.plugins import discover_all_plugins
from plugins.llm_router.server import LLMRouterServer
from plugins.llm_router.__main__ import main, build_parser, cli_main


@pytest.mark.asyncio
async def test_llm_router_plugin_discovered():
    repo_root = Path(__file__).resolve().parents[1]
    default_dir = repo_root / 'plugins'
    if not default_dir.exists():
        alt = repo_root / 'src' / 'plugins'
        if alt.exists():
            default_dir = alt
    plugins = discover_all_plugins([default_dir])
    assert 'llm_router' in plugins
    factory = plugins['llm_router']
    inst = factory('llm_router', {})
    assert inst is not None


class TestLLMRouterCLI:
    """Test the LLM router plugin CLI functionality."""

    @pytest.mark.asyncio
    async def test_build_parser_basic_args(self):
        """Test basic argument parsing."""
        parser = build_parser()
        args = parser.parse_args(['--message', 'Hello world'])

        assert args.message == 'Hello world'
        assert args.provider == 'openai'
        assert args.model is None
        assert args.server is False
        assert args.port == 8081

    @pytest.mark.asyncio
    async def test_build_parser_all_args(self):
        """Test parsing with all arguments."""
        parser = build_parser()
        args = parser.parse_args([
            '--message', 'Test message',
            '--provider', 'ollama',
            '--model', 'llama2',
            '--default-provider', 'ollama',
            '--default-model', 'llama3',
            '--server',
            '--port', '9001'
        ])

        assert args.message == 'Test message'
        assert args.provider == 'ollama'
        assert args.model == 'llama2'
        assert args.default_provider == 'ollama'
        assert args.default_model == 'llama3'
        assert args.server is True
        assert args.port == 9001

    @pytest.mark.asyncio
    async def test_build_parser_prompt_alias(self):
        """Test that --prompt works as alias for --message."""
        parser = build_parser()
        args = parser.parse_args(['--prompt', 'Hello with prompt'])

        assert args.message == 'Hello with prompt'

    @pytest.mark.asyncio
    async def test_build_parser_defaults(self):
        """Test default values."""
        parser = build_parser()
        args = parser.parse_args([])

        assert args.message is None
        assert args.provider == 'openai'
        assert args.model is None
        assert args.default_provider == 'openai'
        assert args.default_model == 'gpt-4o-mini'
        assert args.server is False
        assert args.port == 8081

    @pytest.mark.asyncio
    async def test_main_function_output(self, capsys):
        """Test main function output."""
        main(['--message', 'Test message', '--provider', 'ollama'])

        captured = capsys.readouterr()
        assert "LLM Router MCP Server" in captured.out

        # Parse the JSON output
        lines = captured.out.strip().split('\n')
        json_line = [line for line in lines if line.startswith('{')][0]
        output = json.loads(json_line)

        assert output['description'] == 'LLM Router MCP Server'
        assert output['message'] == 'Test message'
        assert output['provider'] == 'ollama'
        assert output['server_mode'] is False


class TestLLMRouterServer:
    """Test the LLMRouterServer class functionality."""

    @pytest.mark.asyncio
    async def test_llm_router_server_initialization(self):
        """Test LLM router server initialization."""
        server = LLMRouterServer("llm_router", {}, True)
        assert server.name == "llm_router"
        assert server.ssl_verify is True
        assert server.default_provider == "openai"
        assert server.default_model == "gpt-4o-mini"

    @pytest.mark.asyncio
    async def test_llm_router_server_initialization_with_config(self):
        """Test LLM router server initialization with config."""
        config = {
            "default_provider": "ollama",
            "model": "llama3",
            "openai_api_key": "test_key"
        }
        server = LLMRouterServer("llm_router", config, False)
        assert server.name == "llm_router"
        assert server.ssl_verify is False
        # Since openai_api_key is provided, it should use "openai" as default
        assert server.default_provider == "openai"
        assert server.default_model == "llama3"
        assert server.openai_api_key == "test_key"

    @pytest.mark.asyncio
    async def test_llm_router_server_schema(self):
        """Test LLM router server schema structure."""
        server = LLMRouterServer("llm_router", {}, True)
        schema = server.get_schema()

        assert schema["type"] == "function"
        assert schema["function"]["name"] == "llm_router"
        assert "description" in schema["function"]

        params = schema["function"]["parameters"]
        assert params["type"] == "object"
        assert "action" in params["properties"]
        assert "messages" in params["properties"]
        assert "message" in params["properties"]
        assert "provider" in params["properties"]
        assert "model" in params["properties"]

    @pytest.mark.asyncio
    async def test_llm_router_server_default_action(self):
        """Test LLM router server default action."""
        server = LLMRouterServer("llm_router", {}, True)
        assert server.get_default_action() == "chat"

    @pytest.mark.asyncio
    async def test_llm_router_server_invalid_action(self):
        """Test LLM router server with invalid action."""
        server = LLMRouterServer("llm_router", {}, True)

        with pytest.raises(ValueError, match="Unknown tool"):
            await server.call("invalid_action", {"message": "Hello"})

    @pytest.mark.asyncio
    async def test_llm_router_server_missing_message(self):
        """Test LLM router server with missing message."""
        server = LLMRouterServer("llm_router", {}, True)

        result = await server.call("chat", {})
        assert result["error"] == "No message or messages provided"

    @pytest.mark.asyncio
    async def test_llm_router_server_with_message(self):
        """Test LLM router server with message parameter."""
        server = LLMRouterServer("llm_router", {}, True)

        # Mock the LLM client and its chat method
        mock_client = AsyncMock()
        mock_client.chat.return_value = "Mocked response"

        with patch('plugins.llm_router.server.make_llm', return_value=mock_client) as mock_make_llm:
            result = await server.call("chat", {"message": "Hello world"})

            # Verify the client was created with correct parameters
            mock_make_llm.assert_called_once_with(
                "openai", "gpt-4o-mini", None, None, None, None, None, ssl_verify=True
            )

            # Verify chat was called
            mock_client.chat.assert_called_once()

            # Verify response structure
            assert result["content"] == "Mocked response"
            assert result["provider"] == "openai"
            assert result["model"] == "gpt-4o-mini"

    @pytest.mark.asyncio
    async def test_llm_router_server_with_messages_array(self):
        """Test LLM router server with messages array parameter."""
        server = LLMRouterServer("llm_router", {}, True)

        # Mock the LLM client and its chat method
        mock_client = AsyncMock()
        mock_client.chat.return_value = "Mocked response"

        with patch('plugins.llm_router.server.make_llm', return_value=mock_client) as mock_make_llm:
            messages = [
                {"role": "system", "content": "You are a helpful assistant"},
                {"role": "user", "content": "Hello world"}
            ]
            result = await server.call("chat", {"messages": messages})

            # Verify chat was called with correct message objects
            mock_client.chat.assert_called_once()
            call_args = mock_client.chat.call_args[0][0]

            assert len(call_args) == 2
            assert call_args[0].role == "system"
            assert call_args[0].content == "You are a helpful assistant"
            assert call_args[1].role == "user"
            assert call_args[1].content == "Hello world"

    @pytest.mark.asyncio
    async def test_llm_router_server_with_custom_provider_model(self):
        """Test LLM router server with custom provider and model."""
        server = LLMRouterServer("llm_router", {}, True)

        # Mock the LLM client and its chat method
        mock_client = AsyncMock()
        mock_client.chat.return_value = "Custom response"

        with patch('plugins.llm_router.server.make_llm', return_value=mock_client) as mock_make_llm:
            result = await server.call("chat", {
                "message": "Hello",
                "provider": "ollama",
                "model": "llama3"
            })

            # Verify the client was created with custom parameters
            mock_make_llm.assert_called_once_with(
                "ollama", "llama3", None, None, None, None, None, ssl_verify=True
            )

            # Verify response contains custom provider/model
            assert result["content"] == "Custom response"
            assert result["provider"] == "ollama"
            assert result["model"] == "llama3"

    @pytest.mark.asyncio
    async def test_llm_router_server_error_handling(self):
        """Test LLM router server error handling."""
        server = LLMRouterServer("llm_router", {}, True)

        # Mock the LLM client to raise an exception
        mock_client = AsyncMock()
        mock_client.chat.side_effect = Exception("API Error")

        with patch('plugins.llm_router.server.make_llm', return_value=mock_client) as mock_make_llm:
            result = await server.call("chat", {"message": "Hello"})

            # Verify error response structure
            assert result["error"] == "Chat failed with provider 'openai': API Error"
            assert result["provider"] == "openai"
            assert result["model"] == "gpt-4o-mini"
            assert "content" not in result


class TestLLMRouterPluginFactory:
    """Test the LLM router plugin factory function."""

    @pytest.mark.asyncio
    async def test_plugin_factory_basic(self):
        """Test basic plugin factory functionality."""
        from plugins.llm_router.plugin import PLUGIN_FACTORY

        server = PLUGIN_FACTORY("llm_router")
        assert server.name == "llm_router"
        assert server.ssl_verify is True

    @pytest.mark.asyncio
    async def test_plugin_factory_with_config(self):
        """Test plugin factory with configuration."""
        from plugins.llm_router.plugin import PLUGIN_FACTORY

        # Test with OpenAI API key - should use openai provider
        config = {"default_provider": "ollama", "model": "llama3", "openai_api_key": "test_key"}
        server = PLUGIN_FACTORY("llm_router", config, False)
        assert server.name == "llm_router"
        assert server.ssl_verify is False
        assert server.default_provider == "openai"  # Should use openai when key is available
        assert server.default_model == "llama3"

    @pytest.mark.asyncio
    async def test_plugin_factory_name_parameter(self):
        """Test plugin factory with custom name."""
        from plugins.llm_router.plugin import PLUGIN_FACTORY

        server = PLUGIN_FACTORY("custom_llm_router")
        assert server.name == "custom_llm_router"
