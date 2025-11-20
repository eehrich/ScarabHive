"""Tests for HTTP Server Plugin."""

import pytest
from unittest.mock import AsyncMock, patch, Mock
from fastapi.testclient import TestClient
from fastapi import FastAPI

from plugins.http_server.server import HTTPServer
from plugins.http_server.plugin import PLUGIN_FACTORY
from plugins.http_server.__main__ import build_parser, cli_main


class TestHTTPServer:
    """Test the HTTP server MCP adapter."""

    def test_http_server_initialization(self, mock_system_config, mock_mcp_config):
        """Test HTTP server initialization with default config."""
        server = HTTPServer("http_server", mock_system_config, mock_mcp_config)
        assert server.name == "http_server"
        assert server.host == "127.0.0.1"
        assert server.port == 9000
        assert server.wrapped_server is None

    def test_http_server_initialization_with_config(self, mock_system_config, mock_mcp_config):
        """Test HTTP server initialization with custom config."""
        from agent_system.config.models import MCPConfig, AgentConfig
        
        # Create MCPConfig with custom host and port
        mcp_config = MCPConfig(type="http_server", enabled=True, agent_config=AgentConfig())
        mcp_config.host = "0.0.0.0"
        mcp_config.port = 8080
        
        server = HTTPServer("http_server", mock_system_config, mcp_config)
        assert server.host == "0.0.0.0"
        assert server.port == 8080

    def test_http_server_schema(self, mock_system_config, mock_mcp_config):
        """Test HTTP server schema generation."""
        server = HTTPServer("http_server", mock_system_config, mock_mcp_config)
        tools = server.get_tools()

        assert len(tools) == 1
        assert tools[0]["type"] == "function"
        assert tools[0]["function"]["name"] == "http_server_ops"
        assert "health" in tools[0]["function"]["parameters"]["properties"]["operation"]["enum"]
        assert "call" in tools[0]["function"]["parameters"]["properties"]["operation"]["enum"]

    # get_default_action() removed in modernization; dispatcher handles routing.
    # Old default-action test removed as obsolete.

    @pytest.mark.asyncio
    async def test_http_server_call_without_wrapped_server(self, mock_system_config, mock_mcp_config):
        """Test HTTP server call without wrapped server."""
        server = HTTPServer("http_server", mock_system_config, mock_mcp_config)
        result = await server.call("http_server_ops", {"operation": "call", "tool": "test_tool"})
        assert result["error"] == "No server wrapped - use wrap_server() first"

    @pytest.mark.asyncio
    async def test_http_server_call_with_wrapped_server(self, mock_system_config, mock_mcp_config):
        """Test HTTP server call with wrapped server."""
        # Create mock wrapped server

        mock_server = AsyncMock()
        mock_server.call.return_value = {"result": "success"}

        # Create HTTP server and wrap it
        server = HTTPServer("http_server", mock_system_config, mock_mcp_config)
        server.wrap_server(mock_server)

        # Test the call via http_server_ops
        result = await server.call("http_server_ops", {
            "operation": "call", 
            "tool": "test_tool",
            "params": {"param": "value"}
        })

        # Verify the call was forwarded
        mock_server.call.assert_called_once_with("test_tool", {"param": "value"})
        assert result == {"result": "success"}

    def test_create_fastapi_app_without_wrapped_server(self, mock_system_config, mock_mcp_config):
        """Test creating FastAPI app without wrapped server raises error."""
        server = HTTPServer("http_server", mock_system_config, mock_mcp_config)
        with pytest.raises(ValueError, match="No server wrapped"):
            server.create_fastapi_app()

    def test_create_fastapi_app_with_wrapped_server(self, mock_system_config, mock_mcp_config):
        """Test creating FastAPI app with wrapped server."""
        # Create mock wrapped server (use regular Mock for FastAPI tests)
        mock_server = Mock()
        mock_server.name = "test_server"

        # Create HTTP server and wrap it
        server = HTTPServer("http_server", mock_system_config, mock_mcp_config)
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
    async def test_create_fastapi_app_call_endpoint(self, mock_system_config, mock_mcp_config):
        """Test FastAPI app call endpoint."""
        # Create mock wrapped server (use regular Mock but make call async)
        mock_server = Mock()
        mock_server.name = "test_server"
        # Make the call method async
        async def async_call(*args, **kwargs):
            return {"result": "test_response"}
        mock_server.call = async_call

        # Create HTTP server and wrap it
        server = HTTPServer("http_server", mock_system_config, mock_mcp_config)
        server.wrap_server(mock_server)

        # Create FastAPI app and test client
        app = server.create_fastapi_app()
        client = TestClient(app)

        # Test call endpoint
        response = client.post("/call", json={"tool": "test_tool", "params": {"key": "value"}})
        assert response.status_code == 200
        assert response.json() == {"result": "test_response"}


class TestHTTPPluginFactory:
    """Test the HTTP server plugin factory function."""

    def test_plugin_factory_basic(self, mock_system_config, mock_mcp_config):
        """Test basic plugin factory creation."""
        server = PLUGIN_FACTORY("http_server", mock_system_config, mock_mcp_config)
        assert isinstance(server, HTTPServer)
        assert server.name == "http_server"

    def test_plugin_factory_with_config(self, mock_system_config, mock_mcp_config):
        """Test plugin factory with configuration."""
        from agent_system.config.models import MCPConfig, AgentConfig
        
        # Create MCPConfig with custom host and port
        mcp_config = MCPConfig(type="http_server", enabled=True, agent_config=AgentConfig())
        mcp_config.host = "0.0.0.0"
        mcp_config.port = 8080
        
        server = PLUGIN_FACTORY("http_server", mock_system_config, mcp_config)
        assert server.host == "0.0.0.0"
        assert server.port == 8080

    def test_plugin_factory_ssl_verify(self, mock_system_config, mock_mcp_config):
        """Test plugin factory SSL verification handling.

        The HTTP server plugin wraps other MCP servers and does not perform
        outbound HTTP requests itself. The test asserts that providing
        ssl_verify in the system config does not cause an error and that any
        ssl-related setting is not unexpectedly required.
        """
        # The plugin may not expose `ssl_verify` attribute; ensure no exception
        # and that behavior is stable when system config toggles ssl_verify.
        mock_system_config.ssl_verify = False
        server = PLUGIN_FACTORY("http_server", mock_system_config, mock_mcp_config)
        # If the plugin exposes ssl_verify, it should reflect the system setting;
        # otherwise, just ensure attribute access doesn't raise.
        if hasattr(server, 'ssl_verify'):
            assert server.ssl_verify is False


class TestHTTPCLI:
    """Test the HTTP server CLI interface."""

    def test_build_parser_basic_args(self, mock_system_config, mock_mcp_config):
        """Test building parser with basic arguments."""
        parser = build_parser()
        args = parser.parse_args(["--server-name", "test_server"])

        assert args.server_name == "test_server"
        assert args.host == "127.0.0.1"
        assert args.port == 9000
        assert args.no_ssl_verify is False

    def test_build_parser_all_args(self, mock_system_config, mock_mcp_config):
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

    def test_build_parser_defaults(self, mock_system_config, mock_mcp_config):
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
        # Mock plugin discovery (use regular Mocks for CLI testing)
        mock_factory = Mock()
        mock_server = Mock()
        mock_server.name = "test_server"
        mock_factory.return_value = mock_server
        mock_discover.return_value = {"test_server": mock_factory}

        # Mock the HTTPServer to avoid creating real coroutines
        with patch('plugins.http_server.__main__.HTTPServer') as mock_http_server_class:
            mock_http_server_instance = Mock()
            # Make serve return None (not a coroutine) since asyncio.run is mocked
            mock_http_server_instance.serve = Mock(return_value=None)
            mock_http_server_class.return_value = mock_http_server_instance

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
    from agent_system.plugins import discover_all_plugins

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
