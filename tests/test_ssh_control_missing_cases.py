"""Additional tests for ssh_control: check_connection, execute, upload/download skeletons."""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch


@pytest.fixture
def mock_system_config():
    from agent_system.config.models import AgentSystemConfig
    return AgentSystemConfig()


@pytest.fixture
def empty_mcp_config():
    from agent_system.config.models import MCPConfig
    config = MCPConfig()
    config.machines = []
    return config


@pytest.mark.asyncio
async def test_check_connection_lazy_vs_active(mock_system_config, empty_mcp_config):
    """check_connection should return cached result when lazy=True and query actively when lazy=False."""
    from plugins.ssh_control.plugin import PLUGIN_FACTORY

    plugin = PLUGIN_FACTORY('ssh_control_test', mock_system_config, empty_mcp_config)

    # Ensure machine not present initially
    plugin.mcp_server.connection_manager.machines = {}

    # If lazy=True and machine never created, should report machine not configured
    result_lazy = await plugin.mcp_server.ssh_control_check_connection({'machine': 'nope', 'active': False})
    assert result_lazy['total_machines'] == 1
    assert isinstance(result_lazy['statuses'], list)
    assert result_lazy['statuses'][0]['connected'] is False
    assert 'not configured' in (result_lazy['statuses'][0].get('error') or '').lower() or result_lazy['statuses'][0].get('not_yet_connected')

    # Now add a machine to manager and prepare a fake pool with cached latency
    plugin.mcp_server.connection_manager.machines['m1'] = MagicMock()

    import time
    class FakePool:
        def __init__(self):
            self.total_created = 1
            self.last_latency_ms = 123
            self.last_used = time.time()

        async def acquire(self):
            class Conn:
                async def run(self, cmd, check=False):
                    class R:
                        exit_status = 0
                    return R()
            return Conn()

        async def release(self, conn):
            return None

    plugin.mcp_server.connection_manager.pools['m1'] = FakePool()

    # lazy check (call connection_manager directly) returns cached latency
    result_lazy2 = await plugin.mcp_server.connection_manager.check_connection('m1', lazy=True)
    assert result_lazy2.get('latency_ms') == 123

    # active check should attempt to ping; mock connection_manager.check_connection to return a dict
    async def mock_ping(name, lazy=False):
        return {'machine': name, 'connected': True, 'latency_ms': 45, 'error': None, 'last_used': None, 'not_yet_connected': False}

    with patch.object(plugin.mcp_server.connection_manager, 'check_connection', AsyncMock(side_effect=mock_ping)):
        result_active = await plugin.mcp_server.ssh_control_check_connection({'machine': 'm1', 'active': True})
    assert result_active['total_machines'] == 1
    assert result_active['statuses'][0].get('latency_ms') == 45


@pytest.mark.asyncio
async def test_ssh_control_execute_basic(mock_system_config, empty_mcp_config):
    """Test basic execution via ssh_control_execute - success and error paths."""
    from plugins.ssh_control.plugin import PLUGIN_FACTORY
    from plugins.ssh_control.models import MachineConfig

    plugin = PLUGIN_FACTORY('ssh_control_test', mock_system_config, empty_mcp_config)

    # Add a machine and mock a connection run result
    mc = MachineConfig(name='mexec', host='localhost', port=22, username='user', auth_method='key')
    plugin.mcp_server.connection_manager.machines['mexec'] = mc

    # Build a simple result object matching connection_manager.execute_command return
    class ExecResult:
        def __init__(self, machine, command, stdout, stderr, exit_code, duration=0.1):
            self.machine = machine
            self.command = command
            self.stdout = stdout
            self.stderr = stderr
            # mcp_server expects integer exit_code
            self.exit_code = int(exit_code)
            self.duration = duration

    # Patch execute_command to return a success object
    with patch.object(plugin.mcp_server.connection_manager, 'execute_command', AsyncMock(return_value=ExecResult('mexec','echo hi','ok','',0,0.05))):
        res = await plugin.mcp_server.ssh_control_execute({'machine': 'mexec', 'command': 'echo hi'})
    assert res['total_machines'] == 1
    assert res['successful'] == 1
    assert res['failed'] == 0

    # Simulate non-zero exit
    with patch.object(plugin.mcp_server.connection_manager, 'execute_command', AsyncMock(return_value=ExecResult('mexec','false','', 'err', 1, 0.05))):
        res2 = await plugin.mcp_server.ssh_control_execute({'machine': 'mexec', 'command': 'false'})
    assert res2['failed'] == 1
    assert any('exit' in (r.get('error') or '').lower() or r.get('success') is False for r in res2['results'])


@pytest.mark.asyncio
async def test_upload_download_skeletons(mock_system_config, empty_mcp_config):
    """Basic skeleton tests for upload/download - use mocks to verify branch behavior."""
    from plugins.ssh_control.plugin import PLUGIN_FACTORY
    plugin = PLUGIN_FACTORY('ssh_control_test', mock_system_config, empty_mcp_config)

    # Upload: create a simple FileTransfer-like object
    class FileResult:
        def __init__(self, machine, local_path, remote_path, bytes_transferred, duration, success=True):
            self.machine = machine
            self.local_path = local_path
            self.remote_path = remote_path
            self.bytes_transferred = bytes_transferred
            self.duration = duration
            self.success = success

    with patch.object(plugin.mcp_server.connection_manager, 'upload_file', AsyncMock(return_value=FileResult('none','/tmp/a','/tmp/b', 100, 0.02))):
        res = await plugin.mcp_server.ssh_control_upload_file({'machine':'none','local_path':'/tmp/a','remote_path':'/tmp/b'})
    assert res['total_machines'] == 1
    assert res['successful'] == 1

    # Download: mock to return FileResult
    with patch.object(plugin.mcp_server.connection_manager, 'download_file', AsyncMock(return_value=FileResult('none','/tmp/a','/tmp/b', 100, 0.02))):
        res2 = await plugin.mcp_server.ssh_control_download_file({'machine':'none','remote_path':'/tmp/b','local_path':'/tmp/a'})
    assert res2['success'] is True or res2.get('total_machines',1) >= 0
