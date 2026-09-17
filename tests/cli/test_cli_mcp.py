"""Tests for CLI MCP external server management commands."""

import json
import pytest
from unittest.mock import patch, AsyncMock

from agent_system.agent_cli import main
from agent_system.tools.integration import ToolServerIntegration
from agent_system.config.models import AgentSystemConfig, PluginsConfig, ToolServerConfig, RemoteMCPConfig


@pytest.fixture
def mock_config():
    """Mock AgentSystemConfig with MCP settings using current models."""
    plugins = PluginsConfig(
        plugin_dirs=["src/plugins"],
        servers={},
        default_config=ToolServerConfig()
    )
    config = AgentSystemConfig(plugins=plugins)
    return config


@pytest.fixture
def mock_tool_integration():
    """Mock tool integration with test servers."""
    integration = AsyncMock(spec=ToolServerIntegration)
    
    # Mock server configuration using new RemoteMCPConfig model
    test_server = RemoteMCPConfig(
        url="http://localhost:8001/mcp",
        enabled=True,
        description="Test tool server",
    )

    # Provide mcp_system.servers-like structure if needed by callers
    integration.server_config = type('ToolServerConfig', (), {
        'servers': {'test_server': test_server}
    })()

    # No client plugin is loaded in this CLI test, so nothing is connected.
    # Without this the spec'd AsyncMock would hand back a truthy provider whose
    # pool answers every lookup, and every server would look connected.
    integration.external_provider = None

    # Configure runtime and management server mappings using RemoteMCPConfig
    integration.configured_external_servers = {'test_server': test_server}
    integration.all_configured_external_servers = {'test_server': test_server}
    integration.server_config.servers = {'test_server': test_server}

    # Add missing list_servers method for CLI commands
    integration.list_servers = AsyncMock(return_value=[
        {
            'name': 'test_server',
            'connected': False,
            'url': 'http://localhost:8001/mcp',
            'description': 'Test tool server',
            'enabled': True
        }
    ])
    
    # (Do not include extra disabled servers here; tests that need them will add them)
    
    integration.client_manager = AsyncMock()
    integration.client_manager.get_client.return_value = None  # Not connected by default
    
    return integration


class TestCLIMCP:
    """Test CLI MCP external server management commands."""

    @patch('agent_system.agent_cli.load_settings')
    @patch('agent_system.agent_cli.ToolServerIntegration')
    @patch('builtins.print')
    def test_mcp_list_no_servers(self, mock_print, mock_integration_class, mock_load_settings, mock_config):
        """Test mcp list command when no servers are configured."""
        mock_load_settings.return_value = mock_config
        
        # Mock integration with no external servers
        mock_integration = AsyncMock()
        mock_integration.server_config.servers = {}
        mock_integration.configured_external_servers = {}  # Runtime enabled servers
        mock_integration.all_configured_external_servers = {}  # CLI management (all servers)
        mock_integration_class.return_value = mock_integration
        
        # Test the command
        with patch('sys.argv', ['cli', 'mcp', 'list']):
            main()
        
        # Should print message about no servers
        printed_args = [call.args[0] for call in mock_print.call_args_list]
        assert any("No external MCP servers configured" in arg for arg in printed_args)

    @patch('agent_system.agent_cli.load_settings')
    @patch('agent_system.agent_cli.ToolServerIntegration')
    @patch('builtins.print')
    def test_mcp_list_json_format(self, mock_print, mock_integration_class, mock_load_settings, mock_config, mock_tool_integration):
        """Test mcp list command with JSON format."""
        mock_load_settings.return_value = mock_config
        mock_integration_class.return_value = mock_tool_integration
        
        # Call the _mcp_list_servers helper directly to avoid full CLI bootstrapping
        from agent_system.agent_cli import _mcp_list_servers
        args_obj = type('Args', (), {'out_format': 'json'})()
        import asyncio
        asyncio.run(_mcp_list_servers(mock_tool_integration, args_obj))
        
        # Should print JSON array
        printed_args = [call.args[0] for call in mock_print.call_args_list]
        json_output = None
        for arg in printed_args:
            try:
                json_output = json.loads(arg)
                break
            except Exception:
                continue
        
        assert json_output is not None
        assert isinstance(json_output, list)
        assert len(json_output) == 1
        assert json_output[0]['name'] == 'test_server'
        assert not json_output[0]['connected']

    @patch('agent_system.agent_cli.load_settings')
    @patch('agent_system.agent_cli.ToolServerIntegration')
    def test_mcp_without_action_prints_help(self, mock_integration_class, mock_load_settings, mock_config, capsys):
        """A bare `mcp` used to connect everything and then print nothing."""
        mock_load_settings.return_value = mock_config

        with patch('sys.argv', ['cli', 'mcp']):
            main()

        assert 'list,status,test,tools' in capsys.readouterr().out
        mock_integration_class.assert_not_called()

    @patch('agent_system.agent_cli.load_settings')
    @patch('agent_system.agent_cli.ToolServerIntegration')
    def test_mcp_tools_lists_through_the_tool_service(self, mock_integration_class, mock_load_settings, mock_config, mock_tool_integration, capsys):
        mock_load_settings.return_value = mock_config
        mock_integration_class.return_value = mock_tool_integration
        # The shape ToolService.list_tools returns
        listing = {"server": "test_server", "available_tools": ["hello", "secret"],
                   "effective_tools": ["hello"],
                   "filtering": {"blocked_tools": ["secret"]}}

        with patch('agent_system.agent_cli.ToolService.list_tools', AsyncMock(return_value=listing)) as list_tools, \
                patch('sys.argv', ['cli', 'mcp', 'tools', 'test_server', '--format', 'json']):
            main()

        list_tools.assert_awaited_once_with('test_server', include_filtering=True)
        assert json.loads(capsys.readouterr().out) == listing
        mock_tool_integration.shutdown.assert_awaited()

        # The default table read a key the service does not have and
        # reported every server as having no tools.
        with patch('agent_system.agent_cli.ToolService.list_tools', AsyncMock(return_value=listing)), \
                patch('sys.argv', ['cli', 'mcp', 'tools', 'test_server']):
            main()
        table = capsys.readouterr().out
        assert "secret [BLOCKED]" in table
        assert "No tools available" not in table

    @patch('agent_system.agent_cli.load_settings')
    @patch('agent_system.agent_cli.ToolServerIntegration')
    def test_mcp_tools_table_shows_an_error_as_an_error(self, mock_integration_class, mock_load_settings, mock_config, mock_tool_integration, capsys):
        mock_load_settings.return_value = mock_config
        mock_integration_class.return_value = mock_tool_integration
        error = {"error": "Server nope not found in configuration"}

        with patch('agent_system.agent_cli.ToolService.list_tools', AsyncMock(return_value=error)), \
                patch('sys.argv', ['cli', 'mcp', 'tools', 'nope']), \
                pytest.raises(SystemExit) as exit_info:
            main()

        assert exit_info.value.code == 1
        assert json.loads(capsys.readouterr().out) == error

    @patch('agent_system.agent_cli.load_settings')  
    @patch('agent_system.agent_cli.ToolServerIntegration')
    @patch('builtins.print')
    def test_mcp_status_specific_server(self, mock_print, mock_integration_class, mock_load_settings, mock_config, mock_tool_integration):
        """Test mcp status command for specific server."""
        mock_load_settings.return_value = mock_config
        mock_integration_class.return_value = mock_tool_integration
        
        # Test the command
        with patch('sys.argv', ['cli', 'mcp', 'status', 'test_server']):
            main()
        
        # Should print server status JSON
        printed_args = [call.args[0] for call in mock_print.call_args_list]
        status_output = None
        for arg in printed_args:
            try:
                status_output = json.loads(arg)
                if 'name' in status_output:
                    break
            except Exception:
                continue
        
        assert status_output is not None
        assert status_output['name'] == 'test_server'
        assert not status_output['connected']
        assert 'url' in status_output

    @patch('agent_system.agent_cli.load_settings')
    @patch('agent_system.agent_cli.ToolServerIntegration')
    @patch('builtins.print')
    def test_mcp_test_server_disabled(self, mock_print, mock_integration_class, mock_load_settings, mock_config, mock_tool_integration):
        """Test mcp test command with disabled server."""
        mock_load_settings.return_value = mock_config
        
        # Mock disabled server
        disabled_server = RemoteMCPConfig(
            url="http://localhost:8002/mcp",
            enabled=False,
            description="Disabled test server",
        )
        
        mock_tool_integration.server_config.servers['disabled_server'] = disabled_server
        # Ensure configured_external_servers contains the disabled entry for CLI lookup
        mock_tool_integration.configured_external_servers['disabled_server'] = disabled_server
        mock_tool_integration.all_configured_external_servers['disabled_server'] = disabled_server
        mock_integration_class.return_value = mock_tool_integration
        
        # A failed test exits 1 -- it used to print its error and exit 0
        with patch('sys.argv', ['cli', 'mcp', 'test', 'disabled_server']), \
                pytest.raises(SystemExit) as exit_info:
            main()
        assert exit_info.value.code == 1

        # Should print error about disabled server
        printed_args = [call.args[0] for call in mock_print.call_args_list]
        error_output = None
        for arg in printed_args:
            try:
                error_output = json.loads(arg)
                if 'error' in error_output:
                    break
            except Exception:
                continue
        
        assert error_output is not None
        assert 'error' in error_output
        assert 'is disabled' in error_output['error']

    @patch('agent_system.agent_cli.load_settings')
    @patch('agent_system.agent_cli.ToolServerIntegration')
    def test_mcp_status_of_an_unknown_server_is_an_error(self, mock_integration_class, mock_load_settings, mock_config, mock_tool_integration, capsys):
        """The service answers None for an unknown name; the CLI printed `null`."""
        mock_load_settings.return_value = mock_config
        mock_integration_class.return_value = mock_tool_integration

        with patch('sys.argv', ['cli', 'mcp', 'status', 'nope']), pytest.raises(SystemExit) as exit_info:
            main()

        assert exit_info.value.code == 1
        assert "not found" in json.loads(capsys.readouterr().out)["error"]

    def test_mcp_help_command(self, capsys):
        """Test mcp help command shows correct usage."""
        with patch('sys.argv', ['cli', 'mcp', '--help']):
            with pytest.raises(SystemExit):
                main()
        
        captured = capsys.readouterr()
        assert 'Action to perform on external MCP servers' in captured.out
        assert 'list,status,test,tools' in captured.out