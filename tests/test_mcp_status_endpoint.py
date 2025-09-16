"""Tests for the MCP status endpoint in interface_api.py"""

import pytest
import httpx
from unittest.mock import Mock, AsyncMock, patch
from agent_system.agent.interface_api import build_app


class TestMCPStatusEndpoint:
    """Test the /mcp/status endpoint functionality."""

    @pytest.mark.asyncio
    async def test_mcp_status_endpoint_basic_structure(self):
        """Test that the MCP status endpoint returns the expected structure."""
        app = build_app()
        
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            response = await client.get('/mcp/status')
            
            assert response.status_code == 200
            data = response.json()
            
            # Check basic structure
            assert 'plugins' in data
            assert 'external_servers' in data
            assert isinstance(data['plugins'], dict)
            assert isinstance(data['external_servers'], dict)

    @pytest.mark.asyncio 
    async def test_mcp_status_with_mock_integration(self):
        """Test MCP status endpoint with mocked MCP integration."""
        app = build_app()
        
        # Mock the MCP integration
        mock_integration = Mock()
        mock_integration.initialized = True
        mock_integration.configured_external_servers = {"test_external": Mock()}
        mock_integration.get_server_info.return_value = {
            "plugins": {
                "registered": ["test_plugin"],
                "available": ["test_plugin", "other_plugin"]
            },
            "external_servers": ["test_external"]
        }
        
        # Mock list_all_tools as async
        mock_integration.list_all_tools = AsyncMock(return_value={
            "plugins": {
                "test_plugin": [
                    {
                        "name": "test_tool",
                        "description": "A test tool",
                        "input_schema": {"type": "object"}
                    }
                ]
            },
            "external_servers": {
                "test_external": [
                    {
                        "name": "external_tool",
                        "description": "An external tool",
                        "input_schema": {"type": "string"}
                    }
                ]
            }
        })
        
        # Mock plugin registry
        mock_plugin_server = Mock()
        mock_plugin_server.description = "Test plugin server"
        mock_integration.plugin_registry.get_server.return_value = mock_plugin_server
        
        # Mock config for external servers
        mock_server_config = Mock()
        mock_server_config.description = "Test external server"
        mock_server_config.url = "http://test.example.com"
        mock_server_config.enabled = True
        mock_server_config.transport_type = "http"
        mock_integration.mcp_config.servers = {"test_external": mock_server_config}
        
        # Mock client manager
        mock_integration.client_manager.get_client.return_value = Mock()  # Connected
        mock_integration.client_manager.list_clients.return_value = ["test_external"]
        
        with patch('agent_system.agent.interface_api._mcp_integration', mock_integration), \
             patch('agent_system.agent.interface_api._app_registry') as mock_registry:
            # Set up mock registry with test plugin
            mock_server = Mock()
            mock_server.get_schema.return_value = {
                'function': {
                    'name': 'test_tool',
                    'description': 'A test tool',
                    'parameters': {'type': 'object'}
                }
            }
            mock_registry._servers = {'test_plugin': mock_server}
            
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
                response = await client.get('/mcp/status')
                
                assert response.status_code == 200
                data = response.json()
                
                # Check plugins
                assert 'test_plugin' in data['plugins']
                plugin_data = data['plugins']['test_plugin']
                assert plugin_data['name'] == 'Test Plugin'  # Formatted from server_id.replace('_', ' ').title()
                assert plugin_data['connected'] is True
                assert plugin_data['tool_count'] == 1
                assert len(plugin_data['tools']) == 1
                assert plugin_data['tools'][0] == 'test_tool'  # tools is a list of tool names (strings)
                assert len(plugin_data['detailed_tools']) == 1
                assert plugin_data['detailed_tools'][0]['name'] == 'test_tool'
                
                # Check external servers
                assert 'test_external' in data['external_servers']
                external_data = data['external_servers']['test_external']
                assert external_data['name'] == 'Test External'  # Also formatted
                assert external_data['connected'] is True
                assert external_data['tool_count'] == 1  # Fix field name

    @pytest.mark.asyncio
    async def test_mcp_status_handles_empty_servers(self):
        """Test MCP status endpoint when no servers are configured."""
        app = build_app()
        
        mock_integration = Mock()
        mock_integration.get_server_info.return_value = {
            "plugins": {"registered": [], "available": []},
            "external_servers": []
        }
        mock_integration.list_all_tools = AsyncMock(return_value={
            "plugins": {},
            "external_servers": {}
        })
        
        with patch('agent_system.agent.interface_api.get_mcp_integration', return_value=mock_integration):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
                response = await client.get('/mcp/status')
                
                assert response.status_code == 200
                data = response.json()
                
                assert data['plugins'] == {}
                assert data['external_servers'] == {}

    @pytest.mark.asyncio
    async def test_mcp_status_handles_errors_gracefully(self):
        """Test MCP status endpoint handles errors gracefully."""
        app = build_app()
        
        # Mock integration that raises an exception
        mock_integration = Mock()
        mock_integration.get_server_info.side_effect = Exception("Test error")
        
        with patch('agent_system.agent.interface_api.get_mcp_integration', return_value=mock_integration):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
                response = await client.get('/mcp/status')
                
                assert response.status_code == 200
                data = response.json()
                
                # Should return error response with empty server lists
                assert 'error' in data
                assert 'Test error' in data['error']
                assert data['plugins'] == {}
                assert data['external_servers'] == {}

    @pytest.mark.asyncio
    async def test_mcp_status_disconnected_external_server(self):
        """Test MCP status with disconnected external server."""
        app = build_app()
        
        mock_integration = Mock()
        mock_integration.get_server_info.return_value = {
            "plugins": {"registered": [], "available": []},
            "external_servers": ["disconnected_server"]
        }
        mock_integration.list_all_tools = AsyncMock(return_value={
            "plugins": {},
            "external_servers": {"disconnected_server": []}
        })
        
        # Mock disconnected server
        mock_server_config = Mock()
        mock_server_config.description = "Disconnected server"
        mock_server_config.url = "http://offline.example.com"
        mock_server_config.enabled = True
        mock_server_config.transport_type = "http"
        mock_integration.mcp_config.servers = {"disconnected_server": mock_server_config}
        
        # Mock client manager returns None (disconnected)
        mock_integration.client_manager.get_client.return_value = None
        
        with patch('agent_system.agent.interface_api.get_mcp_integration', return_value=mock_integration):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
                response = await client.get('/mcp/status')
                
                assert response.status_code == 200
                data = response.json()
                
                assert 'disconnected_server' in data['external_servers']
                server_data = data['external_servers']['disconnected_server']
                assert server_data['connected'] is False
                assert server_data['enabled'] is True