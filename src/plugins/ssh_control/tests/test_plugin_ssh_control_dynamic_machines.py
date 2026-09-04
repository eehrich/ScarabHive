"""Tests for SSH Control dynamic machine provisioning (add/remove)."""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch


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
async def test_add_machine_persistent_writes_the_store(
        mock_system_config, empty_mcp_config, mock_connection, isolated_machine_store):
    """persistent=True must land in data/ssh_control/machines.<instance>.yaml.

    A real file, not mocked yaml: the mocks were what let this pass while the
    plugin wrote a path nothing reads.
    """
    import yaml

    from plugins.ssh_control.plugin import PLUGIN_FACTORY

    plugin = PLUGIN_FACTORY('ssh_control_test', mock_system_config, empty_mcp_config)

    with patch('plugins.ssh_control.auth.SSHAuthenticator.create_connection',
               AsyncMock(return_value=mock_connection)):
        result = await plugin.mcp_server.add_machine({
            'name': 'test-machine',
            'host': '192.168.1.100',
            'username': 'testuser',
            'persistent': True,
        })

    assert result['success'] is True
    assert result['persistent'] is True, result.get('config_error')

    store_file = isolated_machine_store / 'machines.ssh_control_test.yaml'
    assert store_file.exists(), f"nothing written to {store_file}"
    stored = yaml.safe_load(store_file.read_text(encoding='utf-8'))['machines']
    assert [m['name'] for m in stored] == ['test-machine']
    assert stored[0]['host'] == '192.168.1.100'


@pytest.mark.asyncio
async def test_a_persisted_machine_survives_a_restart(
        mock_system_config, empty_mcp_config, mock_connection):
    """The point of `persistent`, and the bug that hid here for as long as it existed.

    add_machine wrote config/mcp.yaml, which config/config.yaml does not
    include -- so nothing ever read it back and every "saved" machine was gone
    at the next start. The tool reported success either way. This drives the
    only thing that proves persistence: build a SECOND server with the same
    instance name and look for the machine.
    """
    from plugins.ssh_control.plugin import PLUGIN_FACTORY

    first = PLUGIN_FACTORY('ssh_control_test', mock_system_config, empty_mcp_config)
    with patch('plugins.ssh_control.auth.SSHAuthenticator.create_connection',
               AsyncMock(return_value=mock_connection)):
        await first.mcp_server.add_machine({
            'name': 'survivor',
            'host': '192.168.1.101',
            'username': 'testuser',
            'persistent': True,
        })

    # A machine added WITHOUT the flag must not come back -- otherwise this
    # test would pass on a store that simply keeps everything.
    with patch('plugins.ssh_control.auth.SSHAuthenticator.create_connection',
               AsyncMock(return_value=mock_connection)):
        await first.mcp_server.add_machine({
            'name': 'ephemeral',
            'host': '192.168.1.102',
            'username': 'testuser',
            'persistent': False,
        })

    restarted = PLUGIN_FACTORY('ssh_control_test', mock_system_config, empty_mcp_config)

    assert 'survivor' in restarted.mcp_server.connection_manager.machines
    assert 'ephemeral' not in restarted.mcp_server.connection_manager.machines
    assert restarted.mcp_server.connection_manager.machines['survivor'].host == '192.168.1.101'


@pytest.mark.asyncio
async def test_a_password_machine_is_refused_not_half_stored(
        mock_system_config, empty_mcp_config, mock_connection, isolated_machine_store):
    """Storing it without the password would restore a machine that cannot connect.

    The secret must not go into a plain file -- but writing the entry WITHOUT
    it is worse than not writing it: it comes back at the next start, fails
    every command with "Password required" from auth.py, and occupies the name
    so add_machine refuses to repair it. So the tool says no, now, in the
    result the operator reads.
    """
    from plugins.ssh_control.plugin import PLUGIN_FACTORY

    plugin = PLUGIN_FACTORY('ssh_control_test', mock_system_config, empty_mcp_config)

    with patch('plugins.ssh_control.auth.SSHAuthenticator.create_connection',
               AsyncMock(return_value=mock_connection)):
        result = await plugin.mcp_server.add_machine({
            'name': 'pw-machine',
            'host': '192.168.1.103',
            'username': 'testuser',
            'auth_method': 'password',
            'password': 'hunter2',
            'persistent': True,
        })

    # Added to the running session, but NOT stored, and it says why.
    assert result['success'] is True
    assert 'pw-machine' in plugin.mcp_server.connection_manager.machines
    assert result['persistent'] is False
    assert 'password' in result['config_error'], result
    assert not (isolated_machine_store / 'machines.ssh_control_test.yaml').exists()


@pytest.mark.asyncio
async def test_a_restored_machine_can_actually_connect(
        mock_system_config, empty_mcp_config, mock_connection):
    """Surviving the restart is worthless if the entry cannot be used.

    add_machine defaults key_path to '~/.ssh/id_rsa', and the first version of
    the store skipped the field when it held that default. The machine came
    back looking fine in list_machines and died on first use in auth.py with
    "Key path required" -- while blocking its own name, because add_machine
    rejects a duplicate. So this asserts the restored machine reaches the
    authenticator, not merely that it exists.
    """
    from plugins.ssh_control.plugin import PLUGIN_FACTORY

    first = PLUGIN_FACTORY('ssh_control_test', mock_system_config, empty_mcp_config)
    with patch('plugins.ssh_control.auth.SSHAuthenticator.create_connection',
               AsyncMock(return_value=mock_connection)):
        await first.mcp_server.add_machine({
            'name': 'usable',
            'host': '192.168.1.104',
            'username': 'testuser',
            'persistent': True,
        })

    restored = PLUGIN_FACTORY(
        'ssh_control_test', mock_system_config, empty_mcp_config
    ).mcp_server.connection_manager.machines['usable']

    # The real check: the authenticator's own precondition, not a field test.
    assert restored.key_path, "restored without a key path -- auth.py would raise"
    with patch('asyncssh.connect', AsyncMock(return_value=mock_connection)),          patch('os.path.exists', return_value=True):
        from plugins.ssh_control.auth import SSHAuthenticator
        await SSHAuthenticator.create_connection(restored, None, False)


def test_configured_machines_win_over_the_store(mock_system_config, isolated_machine_store):
    """A stored entry must not silently shadow what an admin wrote in config."""
    from agent_system.config.models import MCPConfig
    from plugins.ssh_control import machine_store
    from plugins.ssh_control.plugin import PLUGIN_FACTORY

    machine_store.add('ssh_control_test', {
        'name': 'hosta', 'host': '10.0.0.99', 'username': 'stale',
        'auth_method': 'key', 'key_path': '~/.ssh/id_rsa'})

    config = MCPConfig()
    config.machines = [
        {'name': 'hosta', 'host': '192.0.2.2', 'username': 'root', 'auth_method': 'key'}]

    plugin = PLUGIN_FACTORY('ssh_control_test', mock_system_config, config)

    machines = plugin.mcp_server.connection_manager.machines
    assert machines['hosta'].host == '192.0.2.2',         "the stored entry overrode the configured one"
    assert machines['hosta'].username == 'root'
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
async def test_remove_machine_from_the_store(
        mock_system_config, empty_mcp_config, mock_connection, isolated_machine_store):
    """A real round trip: remove must find what add WROTE.

    A hand-built dict is what let the two halves drift apart -- add wrote
    plugins.servers.ssh_control while remove read servers.ssh_control, so
    removal silently did nothing and still reported success. Both now go
    through machine_store, and this drives the file on disk.
    """
    import yaml

    from plugins.ssh_control.plugin import PLUGIN_FACTORY

    plugin = PLUGIN_FACTORY('ssh_control_test', mock_system_config, empty_mcp_config)
    store_file = isolated_machine_store / 'machines.ssh_control_test.yaml'

    with patch('plugins.ssh_control.auth.SSHAuthenticator.create_connection',
               AsyncMock(return_value=mock_connection)):
        add_result = await plugin.mcp_server.add_machine({
            'name': 'persisted-machine',
            'host': '192.168.1.101',
            'username': 'testuser',
            'persistent': True,
        })

    assert add_result['persistent'] is True, add_result.get('config_error')
    assert 'persisted-machine' in store_file.read_text(encoding='utf-8'),         "nothing was stored -- the round trip would be vacuous"

    result = await plugin.mcp_server.remove_machine({
        'name': 'persisted-machine',
        'remove_from_config': True,
    })

    assert result['success'] is True
    assert result['removed_from_config'] is True, result.get('config_error')
    remaining = yaml.safe_load(store_file.read_text(encoding='utf-8'))['machines']
    assert [m['name'] for m in remaining] == []


@pytest.mark.asyncio
async def test_removing_a_configured_machine_says_it_was_not_stored(
        mock_system_config, mock_connection):
    """Honest reporting: machines from the YAML config are not in the store.

    The old code answered "removed from config" for these, because it wrote a
    file it had just created and then reported the requested flag.
    """
    from agent_system.config.models import MCPConfig
    from plugins.ssh_control.plugin import PLUGIN_FACTORY

    config = MCPConfig()
    config.machines = [
        {'name': 'hosta', 'host': '192.0.2.2', 'username': 'root', 'auth_method': 'key'}]
    plugin = PLUGIN_FACTORY('ssh_control_test', mock_system_config, config)

    result = await plugin.mcp_server.remove_machine({
        'name': 'hosta',
        'remove_from_config': True,
    })

    assert result['success'] is True
    assert result['removed_from_config'] is False
    assert 'not in' in result['config_error'], result
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


def test_the_old_never_read_file_is_reported_not_migrated(
        mock_system_config, empty_mcp_config, caplog):
    """Machines stranded in config/mcp.yaml get a warning, not a resurrection.

    Those entries have been inert since they were written (nothing included
    the file), so silently bringing hosts back at some later restart would be
    a surprise. Naming the file and the count tells the operator where they
    are.
    """
    import logging

    import yaml

    from plugins.ssh_control import machine_store
    from plugins.ssh_control.plugin import PLUGIN_FACTORY

    machine_store.LEGACY_PATH.write_text(yaml.safe_dump({
        'plugins': {'servers': {'ssh_control': {'machines': [
            {'name': 'Stranded1', 'host': '10.0.0.1', 'username': 'root'},
            {'name': 'Stranded2', 'host': '10.0.0.2', 'username': 'root'},
        ]}}}
    }), encoding='utf-8')

    with caplog.at_level(logging.WARNING, logger='plugins.ssh_control.mcp_server'):
        plugin = PLUGIN_FACTORY('ssh_control_test', mock_system_config, empty_mcp_config)

    assert '2 machine(s)' in caplog.text, caplog.text
    assert str(machine_store.LEGACY_PATH) in caplog.text
    # Reported, NOT loaded.
    assert plugin.mcp_server.connection_manager.machines == {}
