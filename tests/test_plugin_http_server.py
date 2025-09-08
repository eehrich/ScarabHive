"""Tests for HTTP Server Plugin."""

import pytest
from unittest.mock import AsyncMock, patch
from fastapi.testclient import TestClient
from fastapi import FastAPI

from plugins.http_server.server import HTTPServer
from plugins.http_server.plugin import create_plugin
from plugins.http_server.__main__ import build_parser, cli_main


class TestHTTPServer:
    """Test the HTTP server MCP adapter."""

    def test_http_server_initialization(self):
        """Test HTTP server initialization with default config."""
        server = HTTPServer("http_server", {}, True)
        assert server.name == "http_server"
        assert server.host == "127.0.0.1"
        assert server.port == 9000
        assert server.wrapped_server is None

    def test_http_server_initialization_with_config(self):
        """Test HTTP server initialization with custom config."""
        config = {"host": "0.0.0.0", "port": 8080}
        server = HTTPServer("http_server", config, True)
        assert server.host == "0.0.0.0"
        assert server.port == 8080

    def test_http_server_schema(self):
        """Test HTTP server schema generation."""
        server = HTTPServer("http_server", {}, True)
        schema = server.get_schema()

        assert schema["type"] == "function"
        assert schema["function"]["name"] == "http_server"
        assert "health" in schema["function"]["parameters"]["properties"]["action"]["enum"]
        assert "call" in schema["function"]["parameters"]["properties"]["action"]["enum"]

    def test_http_server_default_action(self):
        """Test HTTP server default action."""
        server = HTTPServer("http_server", {}, True)
        assert server.get_default_action() == "health"

    @pytest.mark.asyncio
    async def test_http_server_call_without_wrapped_server(self):
        """Test HTTP server call without wrapped server."""
        server = HTTPServer("http_server", {}, True)
        result = await server.call("test_tool", {})
        assert result["error"] == "No server wrapped - use wrap_server() first"

    @pytest.mark.asyncio
    async def test_http_server_call_with_wrapped_server(self):
        """Test HTTP server call with wrapped server."""
        # Create mock wrapped server
        mock_server = AsyncMock()
        mock_server.call.return_value = {"result": "success"}

        # Create HTTP server and wrap it
        server = HTTPServer("http_server", {}, True)
        server.wrap_server(mock_server)

        # Test the call
        result = await server.call("test_tool", {"param": "value"})

        # Verify the call was forwarded
        mock_server.call.assert_called_once_with("test_tool", {"param": "value"})
        assert result == {"result": "success"}

    def test_create_fastapi_app_without_wrapped_server(self):
        """Test creating FastAPI app without wrapped server raises error."""
        server = HTTPServer("http_server", {}, True)
        with pytest.raises(ValueError, match="No server wrapped"):
            server.create_fastapi_app()

    def test_create_fastapi_app_with_wrapped_server(self):
        """Test creating FastAPI app with wrapped server."""
        # Create mock wrapped server
        mock_server = AsyncMock()
        mock_server.name = "test_server"

        # Create HTTP server and wrap it
        server = HTTPServer("http_server", {}, True)
        server.wrap_server(mock_server)

        # Create FastAPI app
        app = server.create_fastapi_app()

        # Verify app was created
        assert isinstance(app, FastAPI)
        assert app.title == "MCP Server: test_server"

        # Test health endpoint
        client = TestClient(app)
        response = client.get("/health")
        assert response.status_code == 200
        assert response.json() == {"status": "ok", "server": "test_server"}

    @pytest.mark.asyncio
    async def test_create_fastapi_app_call_endpoint(self):
        """Test FastAPI app call endpoint."""
        # Create mock wrapped server
        mock_server = AsyncMock()
        mock_server.name = "test_server"
        mock_server.call.return_value = {"result": "test_response"}

        # Create HTTP server and wrap it
        server = HTTPServer("http_server", {}, True)
        server.wrap_server(mock_server)

        # Create FastAPI app and test client
        app = server.create_fastapi_app()
        client = TestClient(app)

        # Test call endpoint
        response = client.post("/call", json={"tool": "test_tool", "params": {"key": "value"}})
        assert response.status_code == 200
        assert response.json() == {"result": "test_response"}

        # Verify the call was forwarded correctly
        mock_server.call.assert_called_once_with("test_tool", {"key": "value"})

    @pytest.mark.asyncio
    async def test_create_fastapi_app_call_endpoint_with_provider_override(self):
        """Test FastAPI app call endpoint with provider/model override."""
        # Create mock wrapped server
        mock_server = AsyncMock()
        mock_server.name = "test_server"
        mock_server.call.return_value = {"result": "ollama_response"}

        # Create HTTP server and wrap it
        server = HTTPServer("http_server", {}, True)
        server.wrap_server(mock_server)

        # Create FastAPI app and test client
        app = server.create_fastapi_app()
        client = TestClient(app)

        # Test call endpoint with provider/model override
        response = client.post("/call", json={
            "tool": "chat",
            "params": {"message": "Hello"},
            "provider": "ollama",
            "model": "llama3"
        })
        assert response.status_code == 200
        assert response.json() == {"result": "ollama_response"}

        # Verify the call was made with provider/model in params
        mock_server.call.assert_called_once_with("chat", {
            "message": "Hello",
            "provider": "ollama",
            "model": "llama3"
        })

    @pytest.mark.asyncio
    async def test_create_fastapi_app_call_endpoint_error_handling(self):
        """Test FastAPI app call endpoint error handling."""
        # Create mock wrapped server that raises API key error
        mock_server = AsyncMock()
        mock_server.name = "test_server"
        mock_server.call.side_effect = ValueError("OPENAI_API_KEY is required when provider=openai")

        # Create HTTP server and wrap it
        server = HTTPServer("http_server", {}, True)
        server.wrap_server(mock_server)

        # Create FastAPI app and test client
        app = server.create_fastapi_app()
        client = TestClient(app)

        # Test call endpoint with API key error
        response = client.post("/call", json={"tool": "chat", "params": {"message": "Hello"}})
        assert response.status_code == 200
        result = response.json()
        assert "error" in result
        assert "API key configuration" in result["error"]
        assert "Ollama" in result["suggestion"]


class TestHTTPPluginFactory:
    """Test the HTTP server plugin factory function."""

    def test_plugin_factory_basic(self):
        """Test basic plugin factory creation."""
        server = create_plugin("http_server")
        assert isinstance(server, HTTPServer)
        assert server.name == "http_server"

    def test_plugin_factory_with_config(self):
        """Test plugin factory with configuration."""
        config = {"host": "0.0.0.0", "port": 8080}
        server = create_plugin("http_server", config)
        assert server.host == "0.0.0.0"
        assert server.port == 8080

    def test_plugin_factory_ssl_verify(self):
        """Test plugin factory SSL verification setting."""
        server = create_plugin("http_server", {}, ssl_verify=False)
        assert server.ssl_verify is False


class TestHTTPCLI:
    """Test the HTTP server CLI interface."""

    def test_build_parser_basic_args(self):
        """Test building parser with basic arguments."""
        parser = build_parser()
        args = parser.parse_args(["--server-name", "test_server"])

        assert args.server_name == "test_server"
        assert args.host == "127.0.0.1"
        assert args.port == 9000
        assert args.no_ssl_verify is False

    def test_build_parser_all_args(self):
        """Test building parser with all arguments."""
        parser = build_parser()
        args = parser.parse_args([
            "--server-name", "test_server",
            "--host", "0.0.0.0",
            "--port", "8080",
            "--no-ssl-verify"
        ])

        assert args.server_name == "test_server"
        assert args.host == "0.0.0.0"
        assert args.port == 8080
        assert args.no_ssl_verify is True

    def test_build_parser_defaults(self):
        """Test parser default values."""
        parser = build_parser()
        args = parser.parse_args(["--server-name", "test_server"])

        assert args.host == "127.0.0.1"
        assert args.port == 9000
        assert not args.no_ssl_verify

    @patch('plugins.http_server.__main__.discover_all_plugins')
    @patch('argparse.ArgumentParser.parse_args')
    def test_cli_main_server_not_found(self, mock_parse, mock_discover):
        """Test CLI when requested server is not found."""
        # Setup mocks
        mock_discover.return_value = {"available_server": None}
        mock_args = mock_parse.return_value
        mock_args.server_name = "nonexistent_server"

        # The function should exit with sys.exit(1), which we catch
        with pytest.raises(SystemExit) as exc_info:
            cli_main()

        assert exc_info.value.code == 1

    @patch('plugins.http_server.__main__.discover_all_plugins')
    @patch('plugins.http_server.__main__.asyncio.run')
    def test_cli_main_success(self, mock_asyncio_run, mock_discover):
        """Test successful CLI execution."""
        # Mock plugin discovery
        mock_factory = AsyncMock()
        mock_server = AsyncMock()
        mock_server.name = "test_server"
        mock_factory.return_value = mock_server
        mock_discover.return_value = {"test_server": mock_factory}

        with patch('argparse.ArgumentParser.parse_args') as mock_parse:
            mock_args = mock_parse.return_value
            mock_args.server_name = "test_server"
            mock_args.host = "127.0.0.1"
            mock_args.port = 9000
            mock_args.no_ssl_verify = False

            cli_main()

            # Verify asyncio.run was called (server started)
            mock_asyncio_run.assert_called_once()


def test_http_server_plugin_discovered():
    """Test that the HTTP server plugin is discoverable."""
    from pathlib import Path
    from agent_system.mcp.plugins import discover_all_plugins

    repo_root = Path(__file__).resolve().parents[1]
    default_dir = repo_root / 'plugins'
    if not default_dir.exists():
        alt = repo_root / 'src' / 'plugins'
        if alt.exists():
            default_dir = alt

    plugins = discover_all_plugins([default_dir])

    # HTTP server should be discoverable
    assert "http_server" in plugins
    assert callable(plugins["http_server"])
