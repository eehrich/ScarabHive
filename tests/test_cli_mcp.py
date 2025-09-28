"""Tests for CLI MCP external server management commands."""

import json
import pytest
from unittest.mock import patch, AsyncMock

from src.agent_system.cli import main
from src.agent_system.mcp.integration import MCPIntegration
from src.agent_system.mcp.config import MCPServerConfig


@pytest.fixture
def mock_config():
    """Mock configuration with MCP settings."""
    config = type('Config', (), {})()
    config.mcp = type('MCPConfig', (), {
        'config_file': 'config/mcp.yaml',
        'enabled': True,
        'expose_local_server': True,
        'local_server_port': 8000,
        'local_server_host': 'localhost',
        'default_timeout': 30.0,
        'max_concurrent_requests': 10,
        'plugin_dirs': ['src/plugins'],
        'enabled_servers': [],
        'servers': {},
        'model_dump': lambda self=None: {
            'enabled': True,
            'expose_local_server': True,
            'local_server_port': 8000,
            'local_server_host': 'localhost',
            'default_timeout': 30.0,
            'max_concurrent_requests': 10,
            'plugin_dirs': ['src/plugins'],
            'enabled_servers': [],
            'servers': {}
        },
        '__dict__': {
            'enabled': True,
            'expose_local_server': True,
            'local_server_port': 8000,
            'local_server_host': 'localhost',
            'default_timeout': 30.0,
            'max_concurrent_requests': 10,
            'plugin_dirs': ['src/plugins'],
            'enabled_servers': [],
            'servers': {}
        }
    })()
    return config


@pytest.fixture
def mock_mcp_integration():
    """Mock MCP integration with test servers."""
    integration = AsyncMock(spec=MCPIntegration)
    
    # Mock server configuration
    test_server = MCPServerConfig(
        name="test_server",
        url="http://localhost:8001/mcp",
        enabled=True,
        description="Test MCP server"
    )
    
    integration.mcp_config = type('MCPConfig', (), {
        'servers': {'test_server': test_server}
    })()
    
    # Fix: Add configured_external_servers for runtime operations and CLI management
    integration.configured_external_servers = {
        'test_server': {
            'url': 'http://localhost:8001/mcp',
            'enabled': True,
            'description': 'Test MCP server'
        }
    }
    integration.all_configured_external_servers = {
        'test_server': {
            'url': 'http://localhost:8001/mcp',
            'enabled': True,
            'description': 'Test MCP server'
        }
    }
    
    integration.client_manager = AsyncMock()
    integration.client_manager.get_client.return_value = None  # Not connected by default
    
    return integration


class TestCLIMCP:
    """Test CLI MCP external server management commands."""

    @patch('src.agent_system.cli.load_settings')
    @patch('src.agent_system.cli.MCPIntegration')
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

    @patch('src.agent_system.cli.load_settings')
    @patch('src.agent_system.cli.MCPIntegration')
    @patch('builtins.print')
    def test_mcp_list_json_format(self, mock_print, mock_integration_class, mock_load_settings, mock_config, mock_mcp_integration):
        """Test mcp list command with JSON format."""
        mock_load_settings.return_value = mock_config
        mock_integration_class.return_value = mock_mcp_integration
        
        # Test the command
        with patch('sys.argv', ['cli', 'mcp', 'list', '--format', 'json']):
            main()
        
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

    @patch('src.agent_system.cli.load_settings')
    @patch('src.agent_system.cli.MCPIntegration')
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

    @patch('src.agent_system.cli.load_settings')
    @patch('src.agent_system.cli.MCPIntegration')
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

    @patch('src.agent_system.cli.load_settings')  
    @patch('src.agent_system.cli.MCPIntegration')
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
        assert 'address' in status_output

    @patch('src.agent_system.cli.load_settings')
    @patch('src.agent_system.cli.MCPIntegration')
    @patch('builtins.print')
    def test_mcp_test_server_disabled(self, mock_print, mock_integration_class, mock_load_settings, mock_config, mock_mcp_integration):
        """Test mcp test command with disabled server."""
        mock_load_settings.return_value = mock_config
        
        # Mock disabled server
        disabled_server = MCPServerConfig(
            name="disabled_server",
            url="http://localhost:8002/mcp",
            enabled=False,
            description="Disabled test server"
        )
        
        mock_mcp_integration.mcp_config.servers = {'disabled_server': disabled_server}
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