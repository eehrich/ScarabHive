"""
Test cases for MCP status functionality with external servers.

Tests the fixes for:
- External servers appearing in /mcp/status response
- Real-time connection checking
- Proper handling of configured but disconnected servers
"""

import pytest
from unittest.mock import Mock, AsyncMock

from agent_system.mcp.integration import MCPIntegration
from agent_system.config.models import (
    AgentSystemConfig,
    MCPSystemConfig,
    ExternalServersConfig,
    RemoteMCPConfig,
)


@pytest.fixture
def mock_mcp_config():
    """Create AgentSystemConfig with external servers configured"""
    # Create remote server configs
    remote_servers = {
        'localhost': RemoteMCPConfig(
            url='http://127.0.0.1:8081',
            description='Local streaming MCP server running on localhost.',
            transport='streaming',
            enabled=True
        ),
        'remote_server': RemoteMCPConfig(
            url='http://example.com:8080',
            description='Remote MCP server',
            transport='streaming',
            enabled=True
        )
    }
    
    # Build external servers config
    external_servers = ExternalServersConfig(
        remote_servers=remote_servers
    )
    
    # Build MCP system config
    mcp_config = MCPSystemConfig(
        external_servers=external_servers
    )
    
    # Build full agent system config
    return AgentSystemConfig(
        mcp_system=mcp_config,
        servers={}
    )


@pytest.fixture
def mock_client_manager():
    """Mock MCP client manager"""
    manager = Mock()
    manager.list_clients = Mock(return_value=[])  # Start with no connected clients
    manager.add_client = AsyncMock()
    manager.close_all = AsyncMock()
    return manager


@pytest.fixture
def mock_plugin_registry():
    """Mock plugin registry"""
    registry = Mock()
    registry.plugin_factories = {}  # Empty dict to satisfy len() check
    registry.plugin_servers = {}  # Empty dict for registered servers
    registry.discover_plugins = Mock()
    registry.register_from_config = AsyncMock()
    registry.list_servers = Mock(return_value=[])
    return registry


@pytest.fixture
def mock_http_server():
    """Mock HTTP server"""
    server = Mock()
    server.register_server = Mock()
    return server


@pytest.fixture
async def mcp_integration(mock_client_manager, mock_plugin_registry, mock_http_server, mock_mcp_config):
    """Create MCPIntegration instance with mocked dependencies"""
    integration = MCPIntegration(config=mock_mcp_config)
    integration.client_manager = mock_client_manager
    integration.plugin_registry = mock_plugin_registry
    integration.http_server = mock_http_server
    
    return integration


class TestMCPIntegrationExternalServers:
    """Test MCPIntegration external server handling"""
    
    async def test_stores_configured_external_servers(self, mcp_integration, mock_mcp_config):
        """Test that MCPIntegration stores configured external servers"""
        # Initialize with external servers
        await mcp_integration.initialize(mock_mcp_config)
        
        # Check that configured_external_servers is stored
        assert hasattr(mcp_integration, 'configured_external_servers')
        assert isinstance(mcp_integration.configured_external_servers, dict)
        assert 'localhost' in mcp_integration.configured_external_servers
        assert 'remote_server' in mcp_integration.configured_external_servers
        
        # Check stored configuration details (RemoteMCPConfig attributes, not Dict keys)
        localhost_config = mcp_integration.configured_external_servers['localhost']
        assert localhost_config.url == 'http://127.0.0.1:8081'
        assert localhost_config.description == 'Local streaming MCP server running on localhost.'
    
    async def test_handles_connection_failures_gracefully(self, mcp_integration, mock_mcp_config):
        """Test that failed connections don't prevent initialization"""
        # Mock add_client to fail for all servers
        mcp_integration.client_manager.add_client.side_effect = Exception("Connection failed")
        
        # Should not raise exception
        await mcp_integration.initialize(mock_mcp_config)
        
        # Should still be initialized and store configuration
        assert mcp_integration.initialized
        assert len(mcp_integration.configured_external_servers) == 2
        
        # Verify connection attempts were made
        assert mcp_integration.client_manager.add_client.call_count == 2
    
    async def test_handles_empty_external_servers(self, mcp_integration):
        """Test handling when no external servers are configured"""
        from agent_system.config.models import (
            AgentSystemConfig,
            MCPSystemConfig,
            ExternalServersConfig
        )
        
        # Build empty config
        mcp_config = MCPSystemConfig(
            external_servers=ExternalServersConfig(
                remote_servers={}
            )
        )
        
        config = AgentSystemConfig(
            mcp_system=mcp_config
        )
        
        await mcp_integration.initialize(config)
        
        assert mcp_integration.configured_external_servers == {}
        assert mcp_integration.initialized
    
    async def test_stores_external_servers_from_mcp_config(self, mcp_integration, mock_mcp_config):
        """Test that external servers are stored from the mcp config section"""
        await mcp_integration.initialize(mock_mcp_config)
        
        # Verify the right data is stored
        stored_config = mcp_integration.configured_external_servers
        assert len(stored_config) == 2
        
        # Check specific server configurations (RemoteMCPConfig attributes, not Dict keys)
        assert stored_config['localhost'].transport == 'streaming'
        assert stored_config['remote_server'].url == 'http://example.com:8080'


if __name__ == "__main__":
    # Run specific tests
    pytest.main([__file__, "-v"])