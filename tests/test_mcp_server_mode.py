"""
Test MCP Server Mode - Initialization

Tests the MCP server mode initialization and basic JSON-RPC protocol handling.
"""

import pytest
from fastapi.testclient import TestClient

from agent_system.config.models import AgentSystemConfig, PluginsConfig, MCPServerModeConfig
from agent_system.mcp.base import MCPRegistry
from agent_system.mcp.server_handler import MCPServerHandler, MCPServerSession


class TestMCPServerSession:
    """Test MCPServerSession rate limiting and management."""
    
    def test_session_creation(self):
        """Test creating a new session."""
        client_info = {"name": "test-client", "version": "1.0.0"}
        session = MCPServerSession("test-session-id", client_info)
        
        assert session.session_id == "test-session-id"
        assert session.client_info == client_info
        assert session.request_count_minute == 0
        assert session.request_count_hour == 0
    
    def test_rate_limit_within_limits(self):
        """Test rate limiting when within limits."""
        session = MCPServerSession("test-session", {})
        
        # First request should be allowed
        allowed, reason = session.check_rate_limit(
            requests_per_minute=60,
            requests_per_hour=1000,
            burst_size=10
        )
        
        assert allowed is True
        assert reason is None
        assert session.request_count_minute == 1
        assert session.request_count_hour == 1
    
    def test_rate_limit_burst_exceeded(self):
        """Test rate limiting when burst limit exceeded."""
        session = MCPServerSession("test-session", {})
        
        # Consume all burst tokens
        for _ in range(10):
            allowed, _ = session.check_rate_limit(
                requests_per_minute=60,
                requests_per_hour=1000,
                burst_size=10
            )
            assert allowed is True
        
        # Next request should be denied (burst limit)
        allowed, reason = session.check_rate_limit(
            requests_per_minute=60,
            requests_per_hour=1000,
            burst_size=10
        )
        
        assert allowed is False
        assert "burst limit" in reason.lower()
    
    def test_rate_limit_minute_exceeded(self):
        """Test rate limiting when per-minute limit exceeded."""
        session = MCPServerSession("test-session", {})
        
        # Manually set request count to limit
        session.request_count_minute = 60
        
        allowed, reason = session.check_rate_limit(
            requests_per_minute=60,
            requests_per_hour=1000,
            burst_size=10
        )
        
        assert allowed is False
        assert "60 requests/minute" in reason
    
    def test_session_to_dict(self):
        """Test session serialization."""
        session = MCPServerSession("test-session", {"name": "client"})
        session_dict = session.to_dict()
        
        assert session_dict["session_id"] == "test-session"
        assert session_dict["client_info"] == {"name": "client"}
        assert "created_at" in session_dict
        assert "last_activity" in session_dict
        assert "age_seconds" in session_dict


class TestMCPServerHandler:
    """Test MCPServerHandler request processing."""
    
    @pytest.fixture
    def config(self):
        """Create test configuration."""
        config = AgentSystemConfig()
        config.plugins = PluginsConfig()
        config.server_mode = MCPServerModeConfig(
            enabled=True,
            expose_plugins=["*"],
            authentication={"required": False, "methods": []},
            rate_limit={
                "enabled": False,
                "requests_per_minute": 60,
                "requests_per_hour": 1000,
                "burst_size": 10
            }
        )
        return config
    
    @pytest.fixture
    def registry(self):
        """Create test registry."""
        return MCPRegistry()
    
    @pytest.fixture
    def handler(self, config, registry):
        """Create MCP server handler."""
        return MCPServerHandler(config, registry)
    
    def test_validate_jsonrpc_request_valid(self, handler):
        """Test JSON-RPC request validation with valid request."""
        payload = {
            "jsonrpc": "2.0",
            "method": "initialize",
            "params": {},
            "id": 1
        }
        
        assert handler._validate_jsonrpc_request(payload) is True
    
    def test_validate_jsonrpc_request_invalid_version(self, handler):
        """Test JSON-RPC request validation with wrong version."""
        payload = {
            "jsonrpc": "1.0",
            "method": "initialize",
            "id": 1
        }
        
        assert handler._validate_jsonrpc_request(payload) is False
    
    def test_validate_jsonrpc_request_missing_method(self, handler):
        """Test JSON-RPC request validation without method."""
        payload = {
            "jsonrpc": "2.0",
            "id": 1
        }
        
        assert handler._validate_jsonrpc_request(payload) is False
    
    @pytest.mark.asyncio
    async def test_handle_initialize(self, handler):
        """Test initialize method handler."""
        params = {
            "clientInfo": {
                "name": "test-client",
                "version": "1.0.0"
            }
        }
        
        result = await handler._handle_initialize(params)
        
        assert "session_id" in result
        assert result["protocolVersion"] == "2024-11-05"
        assert "capabilities" in result
        assert "serverInfo" in result
        assert result["capabilities"]["tools"]["listChanged"] is False
    
    @pytest.mark.asyncio
    async def test_handle_tools_list_empty_registry(self, handler):
        """Test tools/list with empty registry."""
        result = await handler._handle_tools_list({}, None)
        
        assert "tools" in result
        assert isinstance(result["tools"], list)
        assert len(result["tools"]) == 0
    
    @pytest.mark.asyncio
    async def test_session_cleanup(self, handler):
        """Test expired session cleanup."""
        # Create a session
        params = {"clientInfo": {"name": "test"}}
        result = await handler._handle_initialize(params)
        session_id = result["session_id"]
        
        # Verify session exists
        assert session_id in handler._sessions
        
        # Manually expire session
        handler._sessions[session_id].last_activity = 0
        
        # Run cleanup
        await handler._cleanup_expired_sessions()
        
        # Session should be removed
        assert session_id not in handler._sessions
    
    def test_get_exposed_plugins_wildcard(self, handler, config):
        """Test getting exposed plugins with wildcard."""
        exposed = handler._get_exposed_plugins()
        
        # With empty registry and wildcard, should return empty list
        assert isinstance(exposed, list)
    
    def test_get_exposed_plugins_specific(self, handler, config):
        """Test getting exposed plugins with specific list."""
        config.plugins.server_mode.expose_plugins = ["weather", "yahoo_finance"]
        
        exposed = handler._get_exposed_plugins()
        
        assert exposed == ["weather", "yahoo_finance"]


@pytest.mark.integration
class TestMCPServerEndpoint:
    """Integration tests for MCP server HTTP endpoint."""
    
    @pytest.fixture
    def app(self):
        """Create minimal FastAPI app with MCP endpoint."""
        from fastapi import FastAPI, Request
        
        app = FastAPI()
        
        # Create config and handler
        config = AgentSystemConfig()
        config.name = "TestServer"
        config.version = "1.0.0"
        config.plugins = PluginsConfig()
        config.server_mode = MCPServerModeConfig(
            enabled=True,
            expose_plugins=["*"],
            authentication={"required": False, "methods": []},
            rate_limit={
                "enabled": False,
                "requests_per_minute": 60,
                "requests_per_hour": 1000,
                "burst_size": 10
            }
        )
        
        registry = MCPRegistry()
        handler = MCPServerHandler(config, registry)
        
        @app.post("/mcp")
        async def mcp_endpoint(request: Request):
            return await handler.handle_request(request)
        
        return app
    
    @pytest.fixture
    def client(self, app):
        """Create test client."""
        return TestClient(app)
    
    def test_initialize_request(self, client):
        """Test initialize request via HTTP."""
        payload = {
            "jsonrpc": "2.0",
            "method": "initialize",
            "params": {
                "clientInfo": {
                    "name": "test-client",
                    "version": "1.0.0"
                }
            },
            "id": 1
        }
        
        response = client.post("/mcp", json=payload)
        
        assert response.status_code == 200
        data = response.json()
        
        assert data["jsonrpc"] == "2.0"
        assert data["id"] == 1
        assert "result" in data
        assert "session_id" in data["result"]
        assert "Mcp-Session-Id" in response.headers
    
    def test_tools_list_request(self, client):
        """Test tools/list request."""
        payload = {
            "jsonrpc": "2.0",
            "method": "tools/list",
            "params": {},
            "id": 2
        }
        
        response = client.post("/mcp", json=payload)
        
        assert response.status_code == 200
        data = response.json()
        
        assert data["jsonrpc"] == "2.0"
        assert data["id"] == 2
        assert "result" in data
        assert "tools" in data["result"]
    
    def test_invalid_jsonrpc_version(self, client):
        """Test request with invalid JSON-RPC version."""
        payload = {
            "jsonrpc": "1.0",
            "method": "initialize",
            "id": 1
        }
        
        response = client.post("/mcp", json=payload)
        
        assert response.status_code == 400
        data = response.json()
        
        assert "error" in data
        assert data["error"]["code"] == -32600
    
    def test_invalid_json(self, client):
        """Test request with invalid JSON."""
        response = client.post(
            "/mcp",
            content="invalid json{",
            headers={"Content-Type": "application/json"}
        )
        
        assert response.status_code == 400
        data = response.json()
        
        assert "error" in data
        assert data["error"]["code"] == -32700


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
