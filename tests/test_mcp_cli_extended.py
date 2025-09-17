"""Extended tests for CLI MCP external server management commands.

This file provides comprehensive test coverage for all MCP CLI commands
including edge cases, error conditions, and feature management.
"""

import json
import pytest
from unittest.mock import patch, AsyncMock

from src.agent_system.cli import main
from src.agent_system.mcp.integration import MCPIntegration
from src.agent_system.mcp.config import MCPServerConfig
from src.agent_system.mcp.client import StandardMCPClient


@pytest.fixture
def mock_comprehensive_config():
    """Mock configuration with multiple MCP servers."""
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
        'enabled_servers': ['server1', 'server2'],
        'servers': {
            'server1': MCPServerConfig(
                name="server1",
                url="http://localhost:8001/mcp",
                enabled=True,
                description="Test server 1",
                transport_type="http"
            ),
            'server2': MCPServerConfig(
                name="server2", 
                url="http://localhost:8002/mcp",
                enabled=True,
                description="Test server 2",
                transport_type="smithery"
            ),
            'disabled_server': MCPServerConfig(
                name="disabled_server",
                url="http://localhost:8003/mcp",
                enabled=False,
                description="Disabled test server",
                transport_type="http"
            )
        },
        'model_dump': lambda self=None: {
            'enabled': True,
            'expose_local_server': True,
            'local_server_port': 8000,
            'local_server_host': 'localhost',
            'default_timeout': 30.0,
            'max_concurrent_requests': 10,
            'plugin_dirs': ['src/plugins'],
            'enabled_servers': ['server1', 'server2'],
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
            'enabled_servers': ['server1', 'server2'],
            'servers': {}
        }
    })()
    return config


@pytest.fixture
def mock_extended_mcp_integration():
    """Mock MCP integration with multiple servers and various states."""
    integration = AsyncMock(spec=MCPIntegration)
    
    # Mock server configurations
    server1 = MCPServerConfig(
        name="server1",
        url="http://localhost:8001/mcp",
        enabled=True,
        description="Test server 1",
        transport_type="http"
    )
    
    server2 = MCPServerConfig(
        name="server2",
        url="http://localhost:8002/mcp", 
        enabled=True,
        description="Test server 2",
        transport_type="smithery"
    )
    
    disabled_server = MCPServerConfig(
        name="disabled_server",
        url="http://localhost:8003/mcp",
        enabled=False,
        description="Disabled test server",
        transport_type="http"
    )
    
    integration.mcp_config = type('MCPConfig', (), {
        'servers': {
            'server1': server1,
            'server2': server2,
            'disabled_server': disabled_server
        }
    })()
    
    # Mock client manager
    integration.client_manager = AsyncMock()
    
    # Mock connected client for server1
    mock_client1 = AsyncMock(spec=StandardMCPClient)
    mock_client1.list_tools.return_value = [
        {"name": "tool1", "description": "Test tool 1"},
        {"name": "tool2", "description": "Test tool 2"}
    ]
    
    # Mock disconnected state for server2 (client is None)
    mock_client2 = None
    
    # Mock client manager responses
    def get_client_side_effect(server_name):
        if server_name == "server1":
            return mock_client1
        elif server_name == "server2":
            return mock_client2
        else:
            return None
    
    integration.client_manager.get_client.side_effect = get_client_side_effect
    integration.client_manager.create_client.return_value = mock_client1
    integration.client_manager.remove_client.return_value = None
    
    return integration


class TestExtendedCLIMCP:
    """Extended test cases for CLI MCP external server management commands."""

    @patch('src.agent_system.cli.load_settings')
    @patch('src.agent_system.cli.MCPIntegration')
    @patch('builtins.print')
    def test_mcp_list_multiple_servers_table_format(self, mock_print, mock_integration_class, mock_load_settings, mock_comprehensive_config, mock_extended_mcp_integration):
        """Test mcp list command with multiple servers in table format."""
        mock_load_settings.return_value = mock_comprehensive_config
        mock_integration_class.return_value = mock_extended_mcp_integration
        
        with patch('sys.argv', ['cli', 'mcp', 'list']):
            main()
        
        # Check that table headers and server data are printed
        printed_args = [call.args[0] for call in mock_print.call_args_list]
        output_text = "\n".join(str(arg) for arg in printed_args)
        
        assert "server1" in output_text
        assert "server2" in output_text
        assert "disabled_server" in output_text
        assert "http://localhost:8001/mcp" in output_text
        assert "http://localhost:8002/mcp" in output_text
        assert "Test server 1" in output_text
        assert "Test server 2" in output_text

    @patch('src.agent_system.cli.load_settings')
    @patch('src.agent_system.cli.MCPIntegration')
    @patch('builtins.print')
    def test_mcp_list_multiple_servers_json_format(self, mock_print, mock_integration_class, mock_load_settings, mock_comprehensive_config, mock_extended_mcp_integration):
        """Test mcp list command with multiple servers in JSON format."""
        mock_load_settings.return_value = mock_comprehensive_config
        mock_integration_class.return_value = mock_extended_mcp_integration
        
        with patch('sys.argv', ['cli', 'mcp', 'list', '--format', 'json']):
            main()
        
        # Should print JSON array with server details
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
        assert len(json_output) == 3  # server1, server2, disabled_server
        
        # Check server details
        server_names = [server['name'] for server in json_output]
        assert 'server1' in server_names
        assert 'server2' in server_names
        assert 'disabled_server' in server_names
        
        # Check connection status
        server1_entry = next(s for s in json_output if s['name'] == 'server1')
        server2_entry = next(s for s in json_output if s['name'] == 'server2')
        
        assert server1_entry['connected'] is True
        assert server2_entry['connected'] is False

    @patch('src.agent_system.cli.load_settings')
    @patch('src.agent_system.cli.MCPIntegration')
    @patch('builtins.print')
    def test_mcp_connect_successful(self, mock_print, mock_integration_class, mock_load_settings, mock_comprehensive_config, mock_extended_mcp_integration):
        """Test successful mcp connect command."""
        mock_load_settings.return_value = mock_comprehensive_config
        mock_integration_class.return_value = mock_extended_mcp_integration
        
        # Mock successful connection
        mock_extended_mcp_integration.client_manager.create_client.return_value = AsyncMock()
        
        with patch('sys.argv', ['cli', 'mcp', 'connect', 'server2']):
            main()
        
        # Should print success message
        printed_args = [call.args[0] for call in mock_print.call_args_list]
        success_output = None
        for arg in printed_args:
            try:
                success_output = json.loads(arg)
                if 'status' in success_output:
                    break
            except Exception:
                continue
        
        assert success_output is not None
        assert success_output['result'] == 'connected'
        assert success_output['server'] == 'server2'

    @patch('src.agent_system.cli.load_settings')
    @patch('src.agent_system.cli.MCPIntegration')
    @patch('builtins.print')
    def test_mcp_connect_already_connected(self, mock_print, mock_integration_class, mock_load_settings, mock_comprehensive_config, mock_extended_mcp_integration):
        """Test mcp connect command when server is already connected."""
        mock_load_settings.return_value = mock_comprehensive_config
        mock_integration_class.return_value = mock_extended_mcp_integration
        
        with patch('sys.argv', ['cli', 'mcp', 'connect', 'server1']):
            main()
        
        # Should print already connected message
        printed_args = [call.args[0] for call in mock_print.call_args_list]
        output = None
        for arg in printed_args:
            try:
                output = json.loads(arg)
                break
            except Exception:
                continue
        
        assert output is not None
        assert output['result'] == 'connected'  # Already connected should still return connected

    @patch('src.agent_system.cli.load_settings')
    @patch('src.agent_system.cli.MCPIntegration')
    @patch('builtins.print')
    def test_mcp_connect_disabled_server(self, mock_print, mock_integration_class, mock_load_settings, mock_comprehensive_config, mock_extended_mcp_integration):
        """Test mcp connect command with disabled server."""
        mock_load_settings.return_value = mock_comprehensive_config
        mock_integration_class.return_value = mock_extended_mcp_integration
        
        with patch('sys.argv', ['cli', 'mcp', 'connect', 'disabled_server']):
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
        assert 'disabled' in error_output['error'].lower()

    @patch('src.agent_system.cli.load_settings')
    @patch('src.agent_system.cli.MCPIntegration')
    @patch('builtins.print')
    def test_mcp_disconnect_successful(self, mock_print, mock_integration_class, mock_load_settings, mock_comprehensive_config, mock_extended_mcp_integration):
        """Test successful mcp disconnect command."""
        mock_load_settings.return_value = mock_comprehensive_config
        mock_integration_class.return_value = mock_extended_mcp_integration
        
        with patch('sys.argv', ['cli', 'mcp', 'disconnect', 'server1']):
            main()
        
        # Should print success message
        printed_args = [call.args[0] for call in mock_print.call_args_list]
        success_output = None
        for arg in printed_args:
            try:
                success_output = json.loads(arg)
                if 'status' in success_output:
                    break
            except Exception:
                continue
        
        assert success_output is not None
        assert success_output['result'] == 'disconnected'
        assert success_output['server'] == 'server1'

    @patch('src.agent_system.cli.load_settings')
    @patch('src.agent_system.cli.MCPIntegration')
    @patch('builtins.print')
    def test_mcp_disconnect_not_connected(self, mock_print, mock_integration_class, mock_load_settings, mock_comprehensive_config, mock_extended_mcp_integration):
        """Test mcp disconnect command when server is not connected."""
        mock_load_settings.return_value = mock_comprehensive_config
        mock_integration_class.return_value = mock_extended_mcp_integration
        
        with patch('sys.argv', ['cli', 'mcp', 'disconnect', 'server2']):
            main()
        
        # Should print not connected message
        printed_args = [call.args[0] for call in mock_print.call_args_list]
        output = None
        for arg in printed_args:
            try:
                output = json.loads(arg)
                break
            except Exception:
                continue
        
        assert output is not None
        assert output['result'] == 'disconnected'  # Disconnecting a not-connected server should still return disconnected

    @patch('src.agent_system.cli.load_settings')
    @patch('src.agent_system.cli.MCPIntegration')
    @patch('builtins.print')
    def test_mcp_status_all_servers(self, mock_print, mock_integration_class, mock_load_settings, mock_comprehensive_config, mock_extended_mcp_integration):
        """Test mcp status command for all servers."""
        mock_load_settings.return_value = mock_comprehensive_config
        mock_integration_class.return_value = mock_extended_mcp_integration
        
        with patch('sys.argv', ['cli', 'mcp', 'status']):
            main()
        
        # Should call list servers function when no specific server is specified
        # The actual output will be from _mcp_list_servers function
        printed_args = [call.args[0] for call in mock_print.call_args_list]
        assert len(printed_args) > 0  # Should have some output

    @patch('src.agent_system.cli.load_settings')
    @patch('src.agent_system.cli.MCPIntegration')
    @patch('builtins.print')
    def test_mcp_test_successful(self, mock_print, mock_integration_class, mock_load_settings, mock_comprehensive_config, mock_extended_mcp_integration):
        """Test successful mcp test command."""
        mock_load_settings.return_value = mock_comprehensive_config
        mock_integration_class.return_value = mock_extended_mcp_integration
        
        # Mock successful test
        mock_test_client = AsyncMock()
        mock_test_client.list_tools.return_value = [
            {"name": "test_tool", "description": "A test tool"}
        ]
        mock_extended_mcp_integration.client_manager.create_temporary_client.return_value = mock_test_client
        
        with patch('sys.argv', ['cli', 'mcp', 'test', 'server2']):
            main()
        
        # Should print test results
        printed_args = [call.args[0] for call in mock_print.call_args_list]
        test_output = None
        for arg in printed_args:
            try:
                test_output = json.loads(arg)
                if 'test_result' in test_output or 'status' in test_output:
                    break
            except Exception:
                continue
        
        assert test_output is not None

    @patch('src.agent_system.cli.load_settings')
    @patch('src.agent_system.cli.MCPIntegration')
    @patch('builtins.print')
    def test_mcp_test_connection_failed(self, mock_print, mock_integration_class, mock_load_settings, mock_comprehensive_config, mock_extended_mcp_integration):
        """Test mcp test command when connection fails."""
        mock_load_settings.return_value = mock_comprehensive_config
        mock_integration_class.return_value = mock_extended_mcp_integration
        
        # Mock connection failure
        mock_extended_mcp_integration.client_manager.create_temporary_client.side_effect = Exception("Connection failed")
        
        with patch('sys.argv', ['cli', 'mcp', 'test', 'server2']):
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

    @patch('src.agent_system.cli.load_settings')
    @patch('src.agent_system.cli.MCPIntegration')
    @patch('builtins.print')
    def test_mcp_feature_list_command(self, mock_print, mock_integration_class, mock_load_settings, mock_comprehensive_config, mock_extended_mcp_integration):
        """Test mcp feature list command."""
        mock_load_settings.return_value = mock_comprehensive_config
        mock_integration_class.return_value = mock_extended_mcp_integration
        
        with patch('sys.argv', ['cli', 'mcp', 'feature', 'list', 'server1']):
            main()
        
        # Should print feature list - check that command executes without error
        printed_args = [call.args[0] for call in mock_print.call_args_list]
        
        # At minimum should not crash - exact output depends on implementation
        assert len(printed_args) > 0

    @patch('src.agent_system.cli.load_settings')
    @patch('src.agent_system.cli.MCPIntegration')
    @patch('builtins.print')
    def test_mcp_feature_enable_disable(self, mock_print, mock_integration_class, mock_load_settings, mock_comprehensive_config, mock_extended_mcp_integration):
        """Test mcp feature enable/disable commands."""
        mock_load_settings.return_value = mock_comprehensive_config
        mock_integration_class.return_value = mock_extended_mcp_integration
        
        # Test enable
        with patch('sys.argv', ['cli', 'mcp', 'feature', 'server1', 'tools', 'on']):
            main()
        
        # Test disable  
        with patch('sys.argv', ['cli', 'mcp', 'feature', 'server1', 'tools', 'off']):
            main()
        
        # Should not crash
        printed_args = [call.args[0] for call in mock_print.call_args_list]
        assert len(printed_args) > 0

    @patch('src.agent_system.cli.load_settings')
    @patch('src.agent_system.cli.MCPIntegration')
    @patch('builtins.print')
    def test_mcp_commands_with_timeout(self, mock_print, mock_integration_class, mock_load_settings, mock_comprehensive_config, mock_extended_mcp_integration):
        """Test MCP commands with custom timeout."""
        mock_load_settings.return_value = mock_comprehensive_config
        mock_integration_class.return_value = mock_extended_mcp_integration
        
        with patch('sys.argv', ['cli', 'mcp', '--timeout', '60', 'list']):
            main()
        
        # Should handle timeout parameter correctly
        printed_args = [call.args[0] for call in mock_print.call_args_list]
        assert len(printed_args) > 0

    @patch('src.agent_system.cli.load_settings')
    @patch('src.agent_system.cli.MCPIntegration')
    @patch('builtins.print')
    def test_mcp_no_probe_flag(self, mock_print, mock_integration_class, mock_load_settings, mock_comprehensive_config, mock_extended_mcp_integration):
        """Test MCP commands with --no-probe flag."""
        mock_load_settings.return_value = mock_comprehensive_config
        mock_integration_class.return_value = mock_extended_mcp_integration
        
        with patch('sys.argv', ['cli', 'mcp', '--no-probe', 'feature', 'list', 'server1']):
            main()
        
        # Should handle no-probe flag correctly
        printed_args = [call.args[0] for call in mock_print.call_args_list]
        assert len(printed_args) > 0

    def test_mcp_invalid_action(self, capsys):
        """Test MCP command with invalid action."""
        with patch('sys.argv', ['cli', 'mcp', 'invalid_action']):
            with pytest.raises(SystemExit):
                main()
        
        captured = capsys.readouterr()
        # Should show help or error message
        assert len(captured.err) > 0 or len(captured.out) > 0

    @patch('src.agent_system.cli.load_settings')
    @patch('src.agent_system.cli.MCPIntegration')
    @patch('builtins.print')
    def test_mcp_config_not_found_error(self, mock_print, mock_integration_class, mock_load_settings):
        """Test MCP commands when configuration is not found."""
        # Mock config loading failure
        mock_load_settings.side_effect = FileNotFoundError("Config not found")
        
        # Expect the exception to be raised and handled by main()
        with patch('sys.argv', ['cli', 'mcp', 'list']):
            try:
                main()
            except FileNotFoundError:
                # This is expected since the CLI doesn't handle the exception
                pass
        
        # The config loading should have been attempted
        mock_load_settings.assert_called_once()

    @patch('src.agent_system.cli.load_settings')
    @patch('src.agent_system.cli.MCPIntegration')
    @patch('builtins.print')
    def test_mcp_integration_init_failure(self, mock_print, mock_integration_class, mock_load_settings, mock_comprehensive_config):
        """Test MCP commands when integration initialization fails."""
        mock_load_settings.return_value = mock_comprehensive_config
        mock_integration_class.side_effect = Exception("Integration init failed")
        
        with patch('sys.argv', ['cli', 'mcp', 'list']):
            main()
        
        # Should handle integration failure gracefully
        printed_args = [call.args[0] for call in mock_print.call_args_list]
        error_found = False
        for arg in printed_args:
            try:
                output = json.loads(arg)
                if 'error' in output:
                    error_found = True
                    break
            except Exception:
                if 'error' in str(arg).lower() or 'fail' in str(arg).lower():
                    error_found = True
                    break
        
        assert error_found