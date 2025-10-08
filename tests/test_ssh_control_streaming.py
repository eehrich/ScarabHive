"""Tests for SSH Control streaming functionality.

Note: These are basic structural tests. For full functional testing,
run the manual test script: python -m scripts.test_ssh_streaming
"""

import pytest
from plugins.ssh_control.connection_manager import SSHConnectionManager
from plugins.ssh_control.models import MachineConfig


@pytest.fixture
def machine_config():
    """Create test machine configuration."""
    return MachineConfig(
        name="test-machine",
        host="localhost",
        port=22,
        username="testuser",
        auth_method="key",
        key_path="/fake/key",
        tags=["test"]
    )


@pytest.fixture
def connection_manager(machine_config):
    """Create connection manager with test config."""
    config = {
        'machines': [machine_config.model_dump()],
        'defaults': {},
        'known_hosts_file': None,
        'strict_host_key_checking': False,
        'audit_log_enabled': False
    }
    return SSHConnectionManager(config)


def test_connection_manager_has_streaming_method(connection_manager):
    """Test that connection manager has execute_command_stream method."""
    assert hasattr(connection_manager, 'execute_command_stream')
    assert callable(connection_manager.execute_command_stream)


@pytest.mark.asyncio
async def test_execute_command_stream_unknown_machine(connection_manager):
    """Test streaming with unknown machine."""
    with pytest.raises(ValueError, match="Unknown machine"):
        async for _ in connection_manager.execute_command_stream(
            "unknown-machine",
            "echo test"
        ):
            pass


def test_streaming_method_signature(connection_manager):
    """Test streaming method has correct signature."""
    import inspect
    sig = inspect.signature(connection_manager.execute_command_stream)
    params = list(sig.parameters.keys())
    
    assert 'machine_name' in params
    assert 'command' in params
    assert 'timeout' in params
