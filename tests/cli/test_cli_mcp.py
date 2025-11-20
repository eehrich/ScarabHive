"""Tests for CLI MCP external server management commands."""

import json
import pytest
from unittest.mock import patch, AsyncMock

from agent_system.agent_cli import main
from agent_system.mcp.integration import MCPIntegration
from agent_system.config.models import AgentSystemConfig, PluginsConfig, MCPServersConfig, MCPConfig, RemoteMCPConfig


@pytest.fixture
def mock_config():
    """Mock AgentSystemConfig with MCP settings using current models."""
    plugins = PluginsConfig(
        plugin_dirs=["src/plugins"],
        servers={},
        default_config=MCPConfig()
    )
    config = AgentSystemConfig(plugins=plugins)
    return config


@pytest.fixture
def mock_mcp_integration():
    """Mock MCP integration with test servers."""
    integration = AsyncMock(spec=MCPIntegration)
    
    # Mock server configuration using new RemoteMCPConfig model
    test_server = RemoteMCPConfig(
        url="http://localhost:8001/mcp",
        enabled=True,
        description="Test MCP server",
    )

    # Provide mcp_system.servers-like structure if needed by callers
    integration.mcp_config = type('MCPConfig', (), {
        'servers': {'test_server': test_server}
    })()

    # Configure runtime and management server mappings using RemoteMCPConfig
    integration.configured_external_servers = {'test_server': test_server}
    integration.all_configured_external_servers = {'test_server': test_server}
    integration.mcp_config.servers = {'test_server': test_server}

    # Add missing list_servers method for CLI commands
    integration.list_servers = AsyncMock(return_value=[
        {
            'name': 'test_server',
            'connected': False,
            'url': 'http://localhost:8001/mcp',
            'description': 'Test MCP server',
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
    @patch('agent_system.agent_cli.MCPIntegration')
    @patch('builtins.print')
    def test_mcp_list_no_servers(self, mock_print, mock_integration_class, mock_load_settings, mock_config):
        """Test mcp list command when no servers are configured."""
        mock_load_settings.return_value = mock_config
        
        # Mock integration with no external servers
        mock_integration = AsyncMock()
        mock_integration.mcp_config.servers = {}
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
    @patch('agent_system.agent_cli.MCPIntegration')
    @patch('builtins.print')
    def test_mcp_list_json_format(self, mock_print, mock_integration_class, mock_load_settings, mock_config, mock_mcp_integration):
        """Test mcp list command with JSON format."""
        mock_load_settings.return_value = mock_config
        mock_integration_class.return_value = mock_mcp_integration
        
        # Call the _mcp_list_servers helper directly to avoid full CLI bootstrapping
        from agent_system.agent_cli import _mcp_list_servers
        args_obj = type('Args', (), {'out_format': 'json', 'no_probe': True})()
        import asyncio
        asyncio.run(_mcp_list_servers(mock_mcp_integration, args_obj))
        
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
    @patch('agent_system.agent_cli.MCPIntegration')
    @patch('builtins.print')
    def test_mcp_connect_server_not_found(self, mock_print, mock_integration_class, mock_load_settings, mock_config, mock_mcp_integration):
        """Test mcp connect command with nonexistent server."""
        mock_load_settings.return_value = mock_config
        mock_integration_class.return_value = mock_mcp_integration
        
        # Test the command
        with patch('sys.argv', ['cli', 'mcp', 'connect', 'nonexistent']):
            main()
        
        # Should print error message
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
        assert 'not found in configuration' in error_output['error']

    @patch('agent_system.agent_cli.load_settings')
    @patch('agent_system.agent_cli.MCPIntegration')
    @patch('builtins.print')
    def test_mcp_connect_missing_server_name(self, mock_print, mock_integration_class, mock_load_settings, mock_config):
        """Test mcp connect command without server name."""
        mock_load_settings.return_value = mock_config
        mock_integration_class.return_value = AsyncMock()
        
        # Test the command
        with patch('sys.argv', ['cli', 'mcp', 'connect']):
            main()
        
        # Should print error message
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
        assert 'server name required' in error_output['error']

    @patch('agent_system.agent_cli.load_settings')  
    @patch('agent_system.agent_cli.MCPIntegration')
    @patch('builtins.print')
    def test_mcp_status_specific_server(self, mock_print, mock_integration_class, mock_load_settings, mock_config, mock_mcp_integration):
        """Test mcp status command for specific server."""
        mock_load_settings.return_value = mock_config
        mock_integration_class.return_value = mock_mcp_integration
        
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
    @patch('agent_system.agent_cli.MCPIntegration')
    @patch('builtins.print')
    def test_mcp_test_server_disabled(self, mock_print, mock_integration_class, mock_load_settings, mock_config, mock_mcp_integration):
        """Test mcp test command with disabled server."""
        mock_load_settings.return_value = mock_config
        
        # Mock disabled server
        disabled_server = RemoteMCPConfig(
            url="http://localhost:8002/mcp",
            enabled=False,
            description="Disabled test server",
        )
        
        mock_mcp_integration.mcp_config.servers['disabled_server'] = disabled_server
        # Ensure configured_external_servers contains the disabled entry for CLI lookup
        mock_mcp_integration.configured_external_servers['disabled_server'] = disabled_server
        mock_mcp_integration.all_configured_external_servers['disabled_server'] = disabled_server
        mock_integration_class.return_value = mock_mcp_integration
        
        # Test the command
        with patch('sys.argv', ['cli', 'mcp', 'test', 'disabled_server']):
            main()
        
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

    def test_mcp_help_command(self, capsys):
        """Test mcp help command shows correct usage."""
        with patch('sys.argv', ['cli', 'mcp', '--help']):
            with pytest.raises(SystemExit):
                main()
        
        captured = capsys.readouterr()
        assert 'Action to perform on external MCP servers' in captured.out
        assert 'list,connect,disconnect,status,test' in captured.out
        assert '--format' in captured.out
        assert '--timeout' in captured.out