"""Tests for SSH Control web SSE endpoints.

Note: These are basic structural tests. For full functional testing,
use the web UI or run manual test script.
"""

import pytest
from unittest.mock import AsyncMock
from collections import deque

from agent_system.config.models import AgentSystemConfig, MCPConfig
from plugins.ssh_control.web_endpoints import SSHControlWebEndpoints
from plugins.ssh_control.models import MachineConfig


@pytest.fixture
def mock_connection_manager():
    """Create mock connection manager."""
    manager = AsyncMock()
    manager.machines = {
        'test-machine': MachineConfig(
            name='test-machine',
            host='localhost',
            port=22,
            username='testuser',
            auth_method='key',
            key_path='/fake/key',
            tags=['test']
        )
    }
    return manager


@pytest.fixture
def web_endpoints(mock_connection_manager):
    """Create web endpoints instance."""
    system_config = AgentSystemConfig()
    mcp_config = MCPConfig()
    command_history = deque(maxlen=100)
    
    return SSHControlWebEndpoints(
        name='ssh_control',
        system_config=system_config,
        mcp_config=mcp_config,
        connection_manager=mock_connection_manager,
        command_history=command_history
    )


def test_web_endpoints_has_sse_route(web_endpoints):
    """Test that web endpoints router includes SSE streaming route."""
    router = web_endpoints.get_web_router()
    assert router is not None
    
    # Check router has routes
    assert len(router.routes) > 0
    
    # Check for streaming endpoint
    route_paths = [route.path for route in router.routes]
    assert any('/api/execute/stream' in path for path in route_paths)


def test_web_endpoints_connection_manager_required(mock_connection_manager):
    """Test that endpoints work with connection manager."""
    system_config = AgentSystemConfig()
    mcp_config = MCPConfig()
    
    endpoints = SSHControlWebEndpoints(
        name='ssh_control',
        system_config=system_config,
        mcp_config=mcp_config,
        connection_manager=mock_connection_manager,
        command_history=deque(maxlen=100)
    )
    
    assert endpoints.connection_manager is not None
    assert endpoints.connection_manager == mock_connection_manager


def test_web_endpoints_without_connection_manager():
    """Test that endpoints can be created without connection manager."""
    system_config = AgentSystemConfig()
    mcp_config = MCPConfig()
    
    endpoints = SSHControlWebEndpoints(
        name='ssh_control',
        system_config=system_config,
        mcp_config=mcp_config,
        connection_manager=None,
        command_history=deque(maxlen=100)
    )
    
    assert endpoints.connection_manager is None
    router = endpoints.get_web_router()
    assert router is not None
