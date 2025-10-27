"""Tests for SSH Control plugin."""

import pytest

from plugins.ssh_control.plugin import PLUGIN_FACTORY as ssh_control_factory


@pytest.fixture
def mock_system_config():
    """Create mock system config for testing."""
    from agent_system.config.models import AgentSystemConfig
    return AgentSystemConfig()


@pytest.fixture
def empty_mcp_config():
    """Create empty MCP config for testing."""
    from agent_system.config.models import MCPConfig
    config = MCPConfig()
    # Add empty ssh_control config
    config.machines = []
    return config


@pytest.fixture
def populated_mcp_config():
    """Create populated MCP config with test machines."""
    from agent_system.config.models import MCPConfig
    config = MCPConfig()
    # Add ssh_control config with machines
    config.machines = [
        {
            'name': 'test-server1',
            'host': '192.168.1.100',
            'port': 22,
            'username': 'deploy',
            'auth_method': 'key',
            'key_path': '~/.ssh/id_rsa',
            'tags': ['production', 'web']
        },
        {
            'name': 'test-server2',
            'host': '192.168.1.101',
            'port': 2222,
            'username': 'admin',
            'auth_method': 'password',
            'password': 'test123',
            'tags': ['staging']
        }
    ]
    return config


def test_ssh_control_plugin_factory_exists():
    """Test that PLUGIN_FACTORY exists and is callable."""
    assert ssh_control_factory is not None
    assert callable(ssh_control_factory)


def test_ssh_control_plugin_instantiation(mock_system_config, empty_mcp_config):
    """Test that ssh_control plugin can be instantiated."""
    plugin_instance = ssh_control_factory('ssh_control_test', mock_system_config, empty_mcp_config)
    
    # Verify it's a hybrid plugin
    assert hasattr(plugin_instance, 'mcp_server')
    assert hasattr(plugin_instance, 'command_history')
    assert hasattr(plugin_instance, 'get_tools')


def test_ssh_control_get_tools(mock_system_config, empty_mcp_config):
    """Test that ssh_control plugin exposes correct tools."""
    plugin_instance = ssh_control_factory('ssh_control_test', mock_system_config, empty_mcp_config)
    tools = plugin_instance.get_tools()
    
    # Verify expected tools are present
    tool_names = []
    for tool in tools:
        if 'name' in tool:
            tool_names.append(tool['name'])
        elif 'function' in tool and 'name' in tool['function']:
            tool_names.append(tool['function']['name'])
    
    # Tools are prefixed with plugin name in tests
    assert 'ssh_control_test_list_machines' in tool_names
    assert 'ssh_control_test_execute' in tool_names
    assert 'ssh_control_test_upload_file' in tool_names
    assert 'ssh_control_test_download_file' in tool_names
    assert 'ssh_control_test_check_connection' in tool_names
    assert 'ssh_control_test_add_machine' in tool_names
    assert 'ssh_control_test_remove_machine' in tool_names
    
    # Verify we have exactly 7 tools (simplified schema)
    assert len(tools) == 7


@pytest.mark.asyncio
async def test_ssh_control_list_machines_empty(mock_system_config, empty_mcp_config):
    """Test listing machines with no machines configured."""
    plugin_instance = ssh_control_factory('ssh_control_test', mock_system_config, empty_mcp_config)
    
    # Call list_machines tool
    result = await plugin_instance.mcp_server.list_machines({})
    
    assert 'machines' in result
    assert 'count' in result
    assert result['count'] == 0
    assert len(result['machines']) == 0


@pytest.mark.asyncio
async def test_ssh_control_connection_manager_config(mock_system_config, populated_mcp_config):
    """Test that connection manager loads machine configs correctly."""
    plugin_instance = ssh_control_factory('ssh_control_test', mock_system_config, populated_mcp_config)
    
    # Verify machines loaded
    assert len(plugin_instance.mcp_server.connection_manager.machines) == 2
    assert 'test-server1' in plugin_instance.mcp_server.connection_manager.machines
    assert 'test-server2' in plugin_instance.mcp_server.connection_manager.machines
    
    # Verify machine configs
    machine1 = plugin_instance.mcp_server.connection_manager.machines['test-server1']
    assert machine1.host == '192.168.1.100'
    assert machine1.port == 22
    assert machine1.auth_method == 'key'
    assert 'production' in machine1.tags
    
    machine2 = plugin_instance.mcp_server.connection_manager.machines['test-server2']
    assert machine2.host == '192.168.1.101'
    assert machine2.port == 2222
    assert machine2.auth_method == 'password'
    assert 'staging' in machine2.tags


@pytest.mark.asyncio
async def test_ssh_control_list_machines_with_tags(mock_system_config, populated_mcp_config):
    """Test listing machines with tag filtering."""
    plugin_instance = ssh_control_factory('ssh_control_test', mock_system_config, populated_mcp_config)
    
    # List all machines
    result = await plugin_instance.mcp_server.list_machines({})
    assert result['count'] == 2
    
    # Filter by production tag
    result = await plugin_instance.mcp_server.list_machines({'tags': ['production']})
    assert result['count'] == 1
    assert result['machines'][0]['name'] == 'test-server1'
    
    # Filter by staging tag
    result = await plugin_instance.mcp_server.list_machines({'tags': ['staging']})
    assert result['count'] == 1
    assert result['machines'][0]['name'] == 'test-server2'
