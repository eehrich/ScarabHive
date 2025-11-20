"""Tests for SSH Control dynamic machine provisioning (add/remove)."""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch, mock_open


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
    config.machines = []
    return config


@pytest.fixture
def mock_connection():
    """Create mock SSH connection."""
    conn = AsyncMock()
    # Mock successful connection test
    result = MagicMock()
    result.exit_status = 0
    result.stdout = "Connection test\n"
    conn.run = AsyncMock(return_value=result)
    conn.close = MagicMock()
    return conn


@pytest.mark.asyncio
async def test_add_machine_success(mock_system_config, empty_mcp_config, mock_connection):
    """Test successful addition of a new SSH machine."""
    from plugins.ssh_control.plugin import PLUGIN_FACTORY
    
    plugin = PLUGIN_FACTORY('ssh_control_test', mock_system_config, empty_mcp_config)
    
    # Mock SSH connection
    with patch('plugins.ssh_control.auth.SSHAuthenticator.create_connection', 
               AsyncMock(return_value=mock_connection)):
        
        result = await plugin.mcp_server.add_machine({
            'name': 'test-machine',
            'host': '192.168.1.100',
            'username': 'testuser',
            'port': 22,
            'auth_method': 'key',
            'key_path': '~/.ssh/id_rsa',
            'tags': ['test', 'dev'],
            'persistent': False,
            'max_connections': 3
        })
    
    assert result['success'] is True
    assert 'test-machine' in plugin.mcp_server.connection_manager.machines
    assert plugin.mcp_server.connection_manager.machines['test-machine'].host == '192.168.1.100'
    assert plugin.mcp_server.connection_manager.machines['test-machine'].username == 'testuser'


@pytest.mark.asyncio
async def test_add_machine_missing_required_params(mock_system_config, empty_mcp_config):
    """Test add_machine with missing required parameters."""
    from plugins.ssh_control.plugin import PLUGIN_FACTORY
    
    plugin = PLUGIN_FACTORY('ssh_control_test', mock_system_config, empty_mcp_config)
    
    # Missing 'name'
    result = await plugin.mcp_server.add_machine({
        'host': '192.168.1.100',
        'username': 'testuser'
    })
    assert result['success'] is False
    assert 'required' in result['error'].lower()
    
    # Missing 'host'
    result = await plugin.mcp_server.add_machine({
        'name': 'test-machine',
        'username': 'testuser'
    })
    assert result['success'] is False
    assert 'required' in result['error'].lower()
    
    # Missing 'username'
    result = await plugin.mcp_server.add_machine({
        'name': 'test-machine',
        'host': '192.168.1.100'
    })
    assert result['success'] is False
    assert 'required' in result['error'].lower()


@pytest.mark.asyncio
async def test_add_machine_duplicate_name(mock_system_config, empty_mcp_config, mock_connection):
    """Test add_machine with duplicate machine name."""
    from plugins.ssh_control.plugin import PLUGIN_FACTORY
    
    plugin = PLUGIN_FACTORY('ssh_control_test', mock_system_config, empty_mcp_config)
    
    # Add first machine
    with patch('plugins.ssh_control.auth.SSHAuthenticator.create_connection', 
               AsyncMock(return_value=mock_connection)):
        result = await plugin.mcp_server.add_machine({
            'name': 'test-machine',
            'host': '192.168.1.100',
            'username': 'testuser'
        })
        assert result['success'] is True
    
    # Try to add duplicate
    result = await plugin.mcp_server.add_machine({
        'name': 'test-machine',
        'host': '192.168.1.101',
        'username': 'testuser2'
    })
    
    assert result['success'] is False
    assert 'already exists' in result['error'].lower()


@pytest.mark.asyncio
async def test_add_machine_connection_test_failure(mock_system_config, empty_mcp_config):
    """Test add_machine when connection test fails."""
    from plugins.ssh_control.plugin import PLUGIN_FACTORY
    import asyncssh
    
    plugin = PLUGIN_FACTORY('ssh_control_test', mock_system_config, empty_mcp_config)
    
    # Mock failed connection - asyncssh.Error requires code and reason
    with patch('plugins.ssh_control.auth.SSHAuthenticator.create_connection',
               side_effect=asyncssh.PermissionDenied('Authentication failed', 'PERMISSION_DENIED')):
        
        result = await plugin.mcp_server.add_machine({
            'name': 'test-machine',
            'host': '192.168.1.100',
            'username': 'testuser'
        })
    
    assert result['success'] is False
    assert 'failed' in result['error'].lower()
    assert 'test-machine' not in plugin.mcp_server.connection_manager.machines


@pytest.mark.asyncio
async def test_add_machine_connection_timeout(mock_system_config, empty_mcp_config):
    """Test add_machine when connection times out."""
    from plugins.ssh_control.plugin import PLUGIN_FACTORY
    import asyncio
    
    plugin = PLUGIN_FACTORY('ssh_control_test', mock_system_config, empty_mcp_config)
    
    # Mock connection timeout
    async def timeout_connection(*args, **kwargs):
        await asyncio.sleep(15)  # Longer than timeout
    
    with patch('plugins.ssh_control.auth.SSHAuthenticator.create_connection',
               side_effect=timeout_connection):
        
        result = await plugin.mcp_server.add_machine({
            'name': 'test-machine',
            'host': '192.168.1.100',
            'username': 'testuser'
        })
    
    assert result['success'] is False
    assert 'timeout' in result['error'].lower()
    assert 'test-machine' not in plugin.mcp_server.connection_manager.machines


@pytest.mark.asyncio
async def test_add_machine_persistent_flag(mock_system_config, empty_mcp_config, mock_connection):
    """Test add_machine with persistent=True saves to config."""
    from plugins.ssh_control.plugin import PLUGIN_FACTORY
    
    plugin = PLUGIN_FACTORY('ssh_control_test', mock_system_config, empty_mcp_config)
    
    mock_config = {'servers': {'ssh_control': {'machines': []}}}
    
    with patch('plugins.ssh_control.auth.SSHAuthenticator.create_connection', 
               AsyncMock(return_value=mock_connection)), \
         patch('pathlib.Path.exists', return_value=True), \
         patch('builtins.open', mock_open(read_data='servers:\n  ssh_control:\n    machines: []\n')), \
         patch('yaml.safe_load', return_value=mock_config), \
         patch('yaml.safe_dump') as mock_dump:
        
        result = await plugin.mcp_server.add_machine({
            'name': 'test-machine',
            'host': '192.168.1.100',
            'username': 'testuser',
            'persistent': True
        })
    
    assert result['success'] is True
    # Verify yaml.safe_dump was called (config was written)
    assert mock_dump.called


@pytest.mark.asyncio
async def test_remove_machine_success(mock_system_config, empty_mcp_config, mock_connection):
    """Test successful removal of an SSH machine."""
    from plugins.ssh_control.plugin import PLUGIN_FACTORY
    
    plugin = PLUGIN_FACTORY('ssh_control_test', mock_system_config, empty_mcp_config)
    
    # First add a machine
    with patch('plugins.ssh_control.auth.SSHAuthenticator.create_connection', 
               AsyncMock(return_value=mock_connection)):
        await plugin.mcp_server.add_machine({
            'name': 'test-machine',
            'host': '192.168.1.100',
            'username': 'testuser'
        })
    
    assert 'test-machine' in plugin.mcp_server.connection_manager.machines
    
    # Now remove it
    result = await plugin.mcp_server.remove_machine({
        'name': 'test-machine'
    })
    
    assert result['success'] is True
    assert 'test-machine' not in plugin.mcp_server.connection_manager.machines


@pytest.mark.asyncio
async def test_remove_machine_not_found(mock_system_config, empty_mcp_config):
    """Test remove_machine for non-existent machine."""
    from plugins.ssh_control.plugin import PLUGIN_FACTORY
    
    plugin = PLUGIN_FACTORY('ssh_control_test', mock_system_config, empty_mcp_config)
    
    result = await plugin.mcp_server.remove_machine({
        'name': 'nonexistent-machine'
    })
    
    assert result['success'] is False
    assert 'not found' in result['error'].lower()


@pytest.mark.asyncio
async def test_remove_machine_missing_name(mock_system_config, empty_mcp_config):
    """Test remove_machine with missing name parameter."""
    from plugins.ssh_control.plugin import PLUGIN_FACTORY
    
    plugin = PLUGIN_FACTORY('ssh_control_test', mock_system_config, empty_mcp_config)
    
    result = await plugin.mcp_server.remove_machine({})
    
    assert result['success'] is False
    assert 'required' in result['error'].lower()


@pytest.mark.asyncio
async def test_remove_machine_cleanup_connections(mock_system_config, empty_mcp_config, mock_connection):
    """Test that remove_machine properly closes active connections."""
    from plugins.ssh_control.plugin import PLUGIN_FACTORY
    
    plugin = PLUGIN_FACTORY('ssh_control_test', mock_system_config, empty_mcp_config)
    
    # Add machine
    with patch('plugins.ssh_control.auth.SSHAuthenticator.create_connection', 
               AsyncMock(return_value=mock_connection)):
        await plugin.mcp_server.add_machine({
            'name': 'test-machine',
            'host': '192.168.1.100',
            'username': 'testuser'
        })
    
    # Create a mock connection pool
    mock_pool = AsyncMock()
    mock_pool.close_all = AsyncMock()
    plugin.mcp_server.connection_manager.pools['test-machine'] = mock_pool
    
    # Remove machine
    result = await plugin.mcp_server.remove_machine({
        'name': 'test-machine'
    })
    
    assert result['success'] is True
    # Verify pool was closed
    mock_pool.close_all.assert_called_once()
    assert 'test-machine' not in plugin.mcp_server.connection_manager.pools


@pytest.mark.asyncio
async def test_remove_machine_from_config(mock_system_config, empty_mcp_config, mock_connection):
    """Test remove_machine with remove_from_config=True."""
    from plugins.ssh_control.plugin import PLUGIN_FACTORY
    
    plugin = PLUGIN_FACTORY('ssh_control_test', mock_system_config, empty_mcp_config)
    
    # Add machine
    with patch('plugins.ssh_control.auth.SSHAuthenticator.create_connection', 
               AsyncMock(return_value=mock_connection)):
        await plugin.mcp_server.add_machine({
            'name': 'test-machine',
            'host': '192.168.1.100',
            'username': 'testuser'
        })
    
    mock_config = {
        'servers': {
            'ssh_control': {
                'machines': [
                    {'name': 'test-machine', 'host': '192.168.1.100', 'username': 'testuser'}
                ]
            }
        }
    }
    
    with patch('pathlib.Path.exists', return_value=True), \
         patch('builtins.open', mock_open(read_data='dummy')), \
         patch('yaml.safe_load', return_value=mock_config), \
         patch('yaml.safe_dump') as mock_dump:
        
        result = await plugin.mcp_server.remove_machine({
            'name': 'test-machine',
            'remove_from_config': True
        })
    
    assert result['success'] is True
    assert result['removed_from_config'] is True
    # Verify yaml.safe_dump was called
    assert mock_dump.called


@pytest.mark.asyncio
async def test_add_remove_machine_integration(mock_system_config, empty_mcp_config, mock_connection):
    """Integration test: add then remove a machine."""
    from plugins.ssh_control.plugin import PLUGIN_FACTORY
    
    plugin = PLUGIN_FACTORY('ssh_control_test', mock_system_config, empty_mcp_config)
    
    # Add machine
    with patch('plugins.ssh_control.auth.SSHAuthenticator.create_connection', 
               AsyncMock(return_value=mock_connection)):
        add_result = await plugin.mcp_server.add_machine({
            'name': 'integration-test',
            'host': '192.168.1.200',
            'username': 'admin',
            'tags': ['test', 'integration']
        })
    
    assert add_result['success'] is True
    assert 'integration-test' in plugin.mcp_server.connection_manager.machines
    
    # Verify machine config
    machine = plugin.mcp_server.connection_manager.machines['integration-test']
    assert machine.host == '192.168.1.200'
    assert machine.username == 'admin'
    assert 'test' in machine.tags
    
    # Remove machine
    remove_result = await plugin.mcp_server.remove_machine({
        'name': 'integration-test'
    })
    
    assert remove_result['success'] is True
    assert 'integration-test' not in plugin.mcp_server.connection_manager.machines
