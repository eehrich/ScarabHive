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

    def test_http_server_initialization(self, mock_system_config, mock_server_config):
        """Test HTTP server initialization with default config."""
        server = HTTPServer("http_server", mock_system_config, mock_server_config)
        assert server.name == "http_server"
        assert server.host == "127.0.0.1"
        assert server.port == 9000
        assert server.wrapped_server is None

    def test_port_zero_means_a_free_port(self, mock_system_config):
        from agent_system.config.models import ToolServerConfig

        server = HTTPServer("http_server", mock_system_config, ToolServerConfig(type="http_server", port=0))
        assert server.port == 0

    def test_http_server_initialization_with_config(self, mock_system_config, mock_server_config):
        """Test HTTP server initialization with custom config."""
        from agent_system.config.models import ToolServerConfig, AgentConfig
        
        # Create ToolServerConfig with custom host and port
        server_config = ToolServerConfig(type="http_server", enabled=True, agent_config=AgentConfig())
        server_config.host = "0.0.0.0"
        server_config.port = 8080
        
        server = HTTPServer("http_server", mock_system_config, server_config)
        assert server.host == "0.0.0.0"
        assert server.port == 8080

    @pytest.mark.asyncio
    async def test_offers_agents_no_tool(self, mock_system_config, mock_server_config):
        """No tools, and the registry must not fabricate its catch-all tool for
        the plugin either: a tool here could only fail in the running system."""
        from agent_system.plugins.tool_adapter import PluginToolAdapter

        server = HTTPServer("http_server", mock_system_config, mock_server_config)
        assert server.get_tools() == []
        assert await PluginToolAdapter("http_server", server).list_tools() == []

    def test_create_fastapi_app_without_wrapped_server(self, mock_system_config, mock_server_config):
        """Test creating FastAPI app without wrapped server raises error."""
        server = HTTPServer("http_server", mock_system_config, mock_server_config)
        with pytest.raises(ValueError, match="No server wrapped"):
            server.create_fastapi_app()

    def test_create_fastapi_app_with_wrapped_server(self, mock_system_config, mock_server_config):
        """Test creating FastAPI app with wrapped server."""
        # Create mock wrapped server (use regular Mock for FastAPI tests)
        mock_server = Mock()
        mock_server.name = "test_server"

        # Create HTTP server and wrap it
        server = HTTPServer("http_server", mock_system_config, mock_server_config)
        server.wrap_server(mock_server)

        # Create FastAPI app
        app = server.create_fastapi_app()

        # Verify app was created
        assert isinstance(app, FastAPI)
        assert app.title == "Tool server: test_server"

        # Test health endpoint
        client = TestClient(app)
        response = client.get("/health")
        assert response.status_code == 200
        assert response.json() == {"status": "ok", "server": "test_server"}

    @pytest.mark.asyncio
    async def test_create_fastapi_app_call_endpoint(self, mock_system_config, mock_server_config):
        """Test FastAPI app call endpoint."""
        # Create mock wrapped server (use regular Mock but make call async)
        mock_server = Mock()
        mock_server.name = "test_server"
        # Make the call method async
        async def async_call(*args, **kwargs):
            return {"result": "test_response"}
        mock_server.call_with_status = async_call

        # Create HTTP server and wrap it
        server = HTTPServer("http_server", mock_system_config, mock_server_config)
        server.wrap_server(mock_server)

        # Create FastAPI app and test client
        app = server.create_fastapi_app()
        client = TestClient(app, base_url="http://127.0.0.1:9000")

        # Test call endpoint
        response = client.post("/call", json={"tool": "test_tool", "params": {"key": "value"}})
        assert response.status_code == 200
        assert response.json() == {"result": "test_response"}


class TestHTTPPluginFactory:
    """Test the HTTP server plugin factory function."""

    def test_plugin_factory_basic(self, mock_system_config, mock_server_config):
        """Test basic plugin factory creation."""
        server = PLUGIN_FACTORY("http_server", mock_system_config, mock_server_config)
        assert isinstance(server, HTTPServer)
        assert server.name == "http_server"

    def test_plugin_factory_with_config(self, mock_system_config, mock_server_config):
        """Test plugin factory with configuration."""
        from agent_system.config.models import ToolServerConfig, AgentConfig
        
        # Create ToolServerConfig with custom host and port
        server_config = ToolServerConfig(type="http_server", enabled=True, agent_config=AgentConfig())
        server_config.host = "0.0.0.0"
        server_config.port = 8080
        
        server = PLUGIN_FACTORY("http_server", mock_system_config, server_config)
        assert server.host == "0.0.0.0"
        assert server.port == 8080

    def test_plugin_factory_ssl_verify(self, mock_system_config, mock_server_config):
        """Test plugin factory SSL verification handling.

        The HTTP server plugin wraps other tool servers and does not perform
        outbound HTTP requests itself. The test asserts that providing
        ssl_verify in the system config does not cause an error and that any
        ssl-related setting is not unexpectedly required.
        """
        # The plugin may not expose `ssl_verify` attribute; ensure no exception
        # and that behavior is stable when system config toggles ssl_verify.
        mock_system_config.ssl_verify = False
        server = PLUGIN_FACTORY("http_server", mock_system_config, mock_server_config)
        # If the plugin exposes ssl_verify, it should reflect the system setting;
        # otherwise, just ensure attribute access doesn't raise.
        if hasattr(server, 'ssl_verify'):
            assert server.ssl_verify is False


class TestHTTPCLI:
    """Test the HTTP server CLI interface."""

    def test_build_parser_basic_args(self, mock_system_config, mock_server_config):
        """Test building parser with basic arguments."""
        parser = build_parser()
        args = parser.parse_args(["--server-name", "test_server"])

        assert args.server_name == "test_server"
        assert args.host == "127.0.0.1"
        assert args.port == 9000
        assert args.no_ssl_verify is False

    def test_build_parser_all_args(self, mock_system_config, mock_server_config):
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

    def test_build_parser_defaults(self, mock_system_config, mock_server_config):
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

    def test_cli_main_builds_both_servers_with_the_factory_signature(self):
        """The CLI must build the wrapped server and the adapter the way the tool
        registry does: (name, system_config, server_config), the entry taken from
        plugins.servers. It passed ``ssl_verify=`` to both and never started."""
        from agent_system.config.models import AgentSystemConfig, PluginsConfig, ToolServerConfig

        system_config = AgentSystemConfig(plugins=PluginsConfig(servers={
            "my_dt": ToolServerConfig(type="stub_type", enabled=True, marker="from-config"),
        }))
        built = {}

        def factory(name, sys_cfg, srv_cfg):
            built.update(name=name, sys_cfg=sys_cfg, srv_cfg=srv_cfg)
            server = Mock()
            server.name = name
            return server

        started = []

        def fake_run(coro):
            started.append(coro)
            coro.close()

        with patch("plugins.http_server.__main__.load_settings", return_value=system_config), \
                patch("plugins.http_server.__main__.discover_all_plugins", return_value={"stub_type": factory}), \
                patch("plugins.http_server.__main__.asyncio.run", side_effect=fake_run), \
                patch("sys.argv", ["http_server", "--server-name", "my_dt", "--port", "9123"]):
            cli_main()

        assert built["name"] == "my_dt"
        assert built["sys_cfg"] is system_config
        assert built["srv_cfg"].marker == "from-config"
        assert len(started) == 1

    def test_call_endpoint_hands_the_wrapped_server_a_status(self, mock_system_config, mock_server_config):
        """/call goes through call_with_status: plugins read params['_status'],
        a bare call() answered every real tool with "Call failed: '_status'"."""
        from agent_system.tools.base import ToolServer

        class NeedsStatus(ToolServer):
            async def echo(self, params):
                return {"status_seen": params["_status"] is not None, "value": params["value"]}

        wrapped = NeedsStatus("needs_status", mock_system_config, mock_server_config)
        server = HTTPServer("http_server", mock_system_config, mock_server_config)
        server.wrap_server(wrapped)
        client = TestClient(server.create_fastapi_app(), base_url="http://127.0.0.1:9000")
        r = client.post("/call", json={"tool": "echo", "params": {"value": 7}})
        assert r.json() == {"status_seen": True, "value": 7}





class TestServeLifecycle:
    @pytest.mark.asyncio
    async def test_serve_starts_and_stops_the_wrapped_plugin(self, mock_system_config, mock_server_config):
        """A plugin that opens its resources in start_plugin() only works when
        someone calls it; the tool registry does, so the adapter must too."""
        order = []
        wrapped = Mock()
        wrapped.name = "w"
        wrapped.start_plugin = AsyncMock(side_effect=lambda: order.append("start"))
        wrapped.stop_plugin = AsyncMock(side_effect=lambda: order.append("stop"))
        server = HTTPServer("http_server", mock_system_config, mock_server_config)
        server.wrap_server(wrapped)

        async def fake_serve(self_):
            order.append("serve")

        with patch("uvicorn.Server.serve", fake_serve):
            await server.serve()
        assert order == ["start", "serve", "stop"]


class TestDispatch:
    def test_plugin_without_call_with_status_is_called_plainly(self, mock_system_config, mock_server_config):
        """Hybrid plugins (todo) have call() only; the registry falls back to it,
        and so must the adapter."""

        class CallOnly:
            name = "call_only"

            async def call(self, tool, params):
                return {"tool": tool, "params": params}

        server = HTTPServer("http_server", mock_system_config, mock_server_config)
        server.wrap_server(CallOnly())
        client = TestClient(server.create_fastapi_app(), base_url="http://127.0.0.1:9000")
        r = client.post("/call", json={"tool": "todo", "params": {"operation": "summary"}})
        assert r.json() == {"tool": "todo", "params": {"operation": "summary"}}
