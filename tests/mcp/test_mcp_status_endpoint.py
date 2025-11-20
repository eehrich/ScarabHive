"""Tests for the MCP status endpoint in interface_api.py"""

import pytest
import httpx
from unittest.mock import Mock, AsyncMock, patch

from agent_system.app import build_app


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
            
            # When MCP service is not initialized, we get an error response
            # This is expected behavior in test environment
            if 'error' in data:
                assert 'MCP service not initialized' in data['error']
            else:
                # If service is initialized, check the structure
                assert 'plugins' in data
                assert 'external_servers' in data
                assert 'total_servers' in data
                assert 'total_tools' in data
                assert isinstance(data['plugins'], dict)
                assert isinstance(data['external_servers'], dict)
                assert isinstance(data['total_servers'], int)
                assert isinstance(data['total_tools'], int)

    @pytest.mark.asyncio
    async def test_mcp_status_with_mock_service(self):
        """Test MCP status endpoint with mocked MCP service."""
        app = build_app()

        # Mock the MCP service
        mock_service = AsyncMock()
        mock_service.get_comprehensive_status.return_value = {
            "plugins": {
                "test_plugin": {
                    "id": "test_plugin",
                    "name": "Test Plugin",
                    "connected": True,
                    "tools": ["test_tool"],
                    "detailed_tools": [{"name": "test_tool", "description": "A test tool", "parameters": {}}],
                    "tool_count": 1
                }
            },
            "external_servers": {
                "test_external": {
                    "id": "test_external",
                    "name": "Test External",
                    "connected": True,
                    "tools": ["external_tool"],
                    "detailed_tools": [{"name": "external_tool", "description": "An external tool", "parameters": {}, "blocked": False}],
                    "tool_count": 1,
                    "url": "http://test.example.com"
                }
            },
            "servers": [],  # For backward compatibility
            "total_servers": 2,
            "total_tools": 2
        }

        # Mock registry
        mock_registry = Mock()
        mock_registry._servers = {}

        with patch('agent_system.app._mcp_service', mock_service), \
             patch('agent_system.app._app_registry', mock_registry):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
                response = await client.get('/mcp/status')
                
                assert response.status_code == 200
                data = response.json()

                # Check plugins
                assert 'test_plugin' in data['plugins']
                plugin_data = data['plugins']['test_plugin']
                assert plugin_data['name'] == 'Test Plugin'
                assert plugin_data['connected'] is True
                assert plugin_data['tool_count'] == 1
                assert len(plugin_data['tools']) == 1
                assert plugin_data['tools'][0] == 'test_tool'
                assert len(plugin_data['detailed_tools']) == 1
                assert plugin_data['detailed_tools'][0]['name'] == 'test_tool'

                # Check external servers
                assert 'test_external' in data['external_servers']
                external_data = data['external_servers']['test_external']
                assert external_data['name'] == 'Test External'
                assert external_data['connected'] is True
                assert external_data['tool_count'] == 1
                assert 'url' in external_data

                # Check totals
                assert data['total_servers'] == 2
                assert data['total_tools'] == 2

    @pytest.mark.asyncio
    async def test_mcp_status_service_not_initialized(self):
        """Test MCP status endpoint when MCP service is not initialized."""
        app = build_app()

        with patch('agent_system.app._mcp_service', None):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
                response = await client.get('/mcp/status')
                
                assert response.status_code == 200
                data = response.json()
                
                assert 'error' in data
                assert 'MCP service not initialized' in data['error']

    @pytest.mark.asyncio
    async def test_mcp_status_registry_not_initialized(self):
        """Test MCP status endpoint when registry is not initialized."""
        app = build_app()

        mock_service = AsyncMock()

        with patch('agent_system.app._mcp_service', mock_service), \
             patch('agent_system.app._app_registry', None):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
                response = await client.get('/mcp/status')
                
                assert response.status_code == 200
                data = response.json()
                
                assert 'error' in data
                assert 'Registry not initialized' in data['error']

    @pytest.mark.asyncio
    async def test_mcp_status_handles_service_exception(self):
        """Test MCP status endpoint handles service exceptions gracefully."""
        app = build_app()

        # Mock service that raises exception
        mock_service = AsyncMock()
        mock_service.get_comprehensive_status.side_effect = Exception("Test service error")

        mock_registry = Mock()
        mock_registry._servers = {}

        with patch('agent_system.app._mcp_service', mock_service), \
             patch('agent_system.app._app_registry', mock_registry):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
                response = await client.get('/mcp/status')
                
                assert response.status_code == 200
                data = response.json()
                
                assert 'error' in data
                assert 'Test service error' in data['error']

    @pytest.mark.asyncio
    async def test_mcp_status_empty_servers(self):
        """Test MCP status endpoint with no servers configured."""
        app = build_app()

        # Mock service returning empty status
        mock_service = AsyncMock()
        mock_service.get_comprehensive_status.return_value = {
            "plugins": {},
            "external_servers": {},
            "servers": [],  # For backward compatibility
            "total_servers": 0,
            "total_tools": 0
        }

        mock_registry = Mock()
        mock_registry._servers = {}

        with patch('agent_system.app._mcp_service', mock_service), \
             patch('agent_system.app._app_registry', mock_registry):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
                response = await client.get('/mcp/status')
                
                assert response.status_code == 200
                data = response.json()

                assert data['plugins'] == {}
                assert data['external_servers'] == {}
                assert data['total_servers'] == 0
                assert data['total_tools'] == 0
