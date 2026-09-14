"""The web panel and the tool must write the SAME machine store.

They did not. Each carried its own copy of a YAML read-modify-write against
``config/mcp.yaml`` -- four copies in total -- and the add and remove halves
drifted apart: add wrote ``plugins.servers.ssh_control``, remove read
``servers.ssh_control``. Removal silently did nothing and reported success.
Neither endpoint had a test, which is how that survived.
"""
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import yaml

from agent_system.config.models import AgentSystemConfig, MCPConfig
from plugins.ssh_control import machine_store
from plugins.ssh_control.web_endpoints import NewMachine


@pytest.fixture
def panel():
    """The web endpoints of a plugin instance without configured machines."""
    from plugins.ssh_control.plugin import PLUGIN_FACTORY

    config = MCPConfig()
    config.machines = []
    return PLUGIN_FACTORY('ssh_control_test', AgentSystemConfig(), config).web_endpoints


@pytest.fixture
def ssh_ok():
    """A connection whose test command succeeds.

    The panel needs more than a bare AsyncMock: it reads `result.exit_status`
    and awaits `wait_closed()`. With a bare mock, `exit_status != 0` is True
    and the endpoint returns 400 before ever reaching the store -- which is
    how the first version of these tests failed for the wrong reason.
    """
    conn = AsyncMock()
    result = MagicMock()
    result.exit_status = 0
    conn.run = AsyncMock(return_value=result)
    conn.close = MagicMock()
    conn.wait_closed = AsyncMock()
    return conn


def _stored(store_dir, instance='ssh_control_test'):
    path = store_dir / f"machines.{instance}.yaml"
    if not path.exists():
        return []
    return yaml.safe_load(path.read_text(encoding='utf-8'))['machines']


async def test_the_panel_writes_the_store(panel, isolated_machine_store, ssh_ok):
    with patch('plugins.ssh_control.auth.SSHAuthenticator.create_connection',
               AsyncMock(return_value=ssh_ok)):
        result = await panel.add_machine(None, NewMachine(
            name='panel-box', host='10.1.0.1', username='root', persistent=True))

    assert result['persistent'] is True, result
    assert [m['name'] for m in _stored(isolated_machine_store)] == ['panel-box']


async def test_the_panel_removes_what_the_tool_stored(
        panel, isolated_machine_store, mock_system_config, ssh_ok):
    """The crossing case: stored by the TOOL, deleted by the PANEL.

    This is the direction that was broken -- and it can only work while both
    go through the same store under the same instance name.
    """
    from plugins.ssh_control.plugin import PLUGIN_FACTORY

    config = MCPConfig()
    config.machines = []
    tool = PLUGIN_FACTORY('ssh_control_test', mock_system_config, config)

    with patch('plugins.ssh_control.auth.SSHAuthenticator.create_connection',
               AsyncMock(return_value=ssh_ok)):
        add = await tool.mcp_server.add_machine({
            'name': 'crossed', 'host': '10.1.0.2', 'username': 'root',
            'persistent': True})
    assert add['persistent'] is True, add.get('config_error')
    assert [m['name'] for m in _stored(isolated_machine_store)] == ['crossed']

    panel.server.connection_manager.machines = {'crossed': MagicMock()}
    result = await panel.remove_machine(None, name='crossed')

    assert result['removed_from_config'] is True, result
    assert _stored(isolated_machine_store) == []


async def test_the_panel_refuses_a_password_machine(
        panel, isolated_machine_store, ssh_ok):
    """Same refusal as the tool -- the panel must not half-store it either.

    Writing the entry without the password would restore a machine that fails
    every command with "Password required" and blocks its own name.
    """
    with patch('plugins.ssh_control.auth.SSHAuthenticator.create_connection',
               AsyncMock(return_value=ssh_ok)):
        result = await panel.add_machine(None, NewMachine(
            name='pw-box', host='10.1.0.3', username='root',
            auth_method='password', password='hunter2', persistent=True))

    assert result['success'] is True
    assert result['persistent'] is False
    assert 'password' in result['config_error'], result
    assert not (isolated_machine_store / 'machines.ssh_control_test.yaml').exists()


def test_a_broken_store_does_not_take_the_configured_machines_down():
    """A corrupt convenience file must not stop the plugin from starting."""
    machine_store.STORE_DIR.mkdir(parents=True, exist_ok=True)
    machine_store.store_path('ssh_control_test').write_text(
        "machines: [this is: not, valid: yaml\n", encoding='utf-8')

    configured = [{'name': 'hosta', 'host': '192.0.2.2', 'username': 'root'}]
    merged = machine_store.merge_into('ssh_control_test', configured)

    assert merged == configured


def test_entries_without_a_name_are_dropped():
    """`name` is the key everything else looks up; a nameless entry would
    raise a KeyError inside merge_into instead of being ignored."""
    machine_store.STORE_DIR.mkdir(parents=True, exist_ok=True)
    machine_store.store_path('ssh_control_test').write_text(
        yaml.safe_dump({'machines': [
            {'host': '10.0.0.1'},
            {'name': '', 'host': '10.0.0.2'},
            {'name': 'Good', 'host': '10.0.0.3'},
        ]}), encoding='utf-8')

    assert [m['name'] for m in machine_store.load('ssh_control_test')] == ['Good']


def test_a_failed_write_leaves_the_existing_store_intact():
    """The property `os.replace` buys: a write that dies destroys nothing.

    With a plain truncating write, an interrupted save leaves either an
    unparseable file (load then reports NO machines at all) or -- worse,
    because nothing logs it -- valid YAML that lost its tail.
    """
    machine_store.add('ssh_control_test', {
        'name': 'Old', 'host': '10.0.0.1', 'username': 'root',
        'auth_method': 'key', 'key_path': '~/.ssh/id_rsa'})

    with patch('plugins.ssh_control.machine_store.os.replace',
               side_effect=OSError("disk full")):
        with pytest.raises(OSError):
            machine_store.add('ssh_control_test', {
                'name': 'New', 'host': '10.0.0.2', 'username': 'root',
                'auth_method': 'key', 'key_path': '~/.ssh/id_rsa'})

    assert [m['name'] for m in machine_store.load('ssh_control_test')] == ['Old']

