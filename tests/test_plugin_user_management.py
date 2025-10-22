"""
Tests for User Management Plugin

Comprehensive tests for user management plugin web interface,
endpoints, and integration with authentication system.
"""

import pytest
from unittest.mock import Mock, patch
from pathlib import Path
from datetime import datetime

from fastapi import FastAPI
from fastapi.testclient import TestClient

from agent_system.config.models import AgentSystemConfig, MCPConfig, AgentConfig
from agent_system.auth.models import UserRole
from plugins.user_management.plugin import UserManagementPlugin
from plugins.user_management.endpoints import UserManagementWebEndpoints


class TestUserManagementPlugin:
    """Test UserManagementPlugin core functionality"""
    
    @pytest.fixture
    def mock_system_config(self):
        """Create mock system config with auth enabled"""
        config = Mock(spec=AgentSystemConfig)
        config.auth = Mock()
        config.auth.enabled = True
        return config
    
    @pytest.fixture
    def mock_system_config_no_auth(self):
        """Create mock system config with auth disabled"""
        config = Mock(spec=AgentSystemConfig)
        config.auth = Mock()
        config.auth.enabled = False
        return config
    
    @pytest.fixture
    def mcp_config(self):
        """Create MCP config for user management"""
        return MCPConfig(
            type="user_management",
            enabled=True,
            agent_config=AgentConfig(),
            items_per_page=20,
            allow_self_delete=False,
            show_api_keys=True
        )
    
    @pytest.fixture
    def plugin(self, mock_system_config, mcp_config):
        """Create user management plugin instance"""
        return UserManagementPlugin("user_management", mock_system_config, mcp_config)
    
    @pytest.fixture
    def plugin_no_auth(self, mock_system_config_no_auth, mcp_config):
        """Create plugin with auth disabled"""
        return UserManagementPlugin("user_management", mock_system_config_no_auth, mcp_config)
    
    def test_plugin_initialization(self, plugin):
        """Test plugin initializes correctly"""
        assert plugin.name == "user_management"
        assert plugin.auth_enabled is True
        assert plugin.items_per_page == 20
        assert plugin.allow_self_delete is False
        assert plugin.show_api_keys is True
    
    def test_plugin_initialization_no_auth(self, plugin_no_auth):
        """Test plugin initialization when auth disabled"""
        assert plugin_no_auth.name == "user_management"
        assert plugin_no_auth.auth_enabled is False
    
    async def test_plugin_call_interface(self, plugin):
        """Test MCP call interface returns status"""
        result = await plugin.call()
        
        assert result["status"] == "ok"
        assert result["name"] == "user_management"
        assert result["type"] == "web_ui_only"
        assert result["auth_enabled"] is True
        assert result["active"] is True
    
    async def test_plugin_call_interface_no_auth(self, plugin_no_auth):
        """Test call interface when auth disabled"""
        result = await plugin_no_auth.call()
        
        assert result["status"] == "ok"
        assert result["active"] is False
    
    def test_web_router_creation(self, plugin):
        """Test that plugin creates a web router"""
        router = plugin.get_web_router()
        
        assert router is not None
        assert router.prefix == "/plugins/user_management"
    
    def test_static_assets(self, plugin):
        """Test static assets retrieval"""
        # Should delegate to web endpoints
        assets = plugin.get_static_assets()
        assert assets is not None
    
    def test_panels_configuration(self, plugin):
        """Test UI panels configuration"""
        panels = plugin.get_panels()
        
        assert len(panels) >= 1
        # Check that user management panel exists
        user_panel = next((p for p in panels if p["id"] == "user_management"), None)
        assert user_panel is not None
        assert user_panel["title"] == "User Management"
        assert user_panel["requires_admin"] is True
        assert user_panel["category"] == "admin"
    
    def test_panels_when_auth_disabled(self, plugin_no_auth):
        """Test panels when auth is disabled"""
        panels = plugin_no_auth.get_panels()
        
        assert len(panels) >= 1
        disabled_panel = panels[0]
        assert "Disabled" in disabled_panel["title"] or disabled_panel.get("enabled") is False
    
    def test_security_configuration(self, plugin):
        """Test security configuration"""
        config = plugin.get_security_config()
        
        assert config["requires_auth"] is True
        assert config["requires_admin"] is True
        assert "rate_limit" in config
        assert config["allowed_methods"] == ["GET", "POST", "DELETE"]


class TestUserManagementWebEndpoints:
    """Test web endpoints functionality"""
    
    @pytest.fixture
    def mock_system_config(self):
        """Create mock system config"""
        config = Mock(spec=AgentSystemConfig)
        config.auth = Mock()
        config.auth.enabled = True
        return config
    
    @pytest.fixture
    def mcp_config(self):
        """Create MCP config"""
        return MCPConfig(
            type="user_management",
            enabled=True,
            agent_config=AgentConfig(),
            items_per_page=10
        )
    
    @pytest.fixture
    def endpoints(self, mock_system_config, mcp_config):
        """Create endpoints instance"""
        return UserManagementWebEndpoints("user_management", mock_system_config, mcp_config)
    
    def test_endpoints_initialization(self, endpoints):
        """Test endpoints initialize correctly"""
        assert endpoints.name == "user_management"
        assert endpoints.auth_enabled is True
        assert endpoints.items_per_page == 10
    
    def test_get_user_database_when_auth_disabled(self):
        """Test database access when auth disabled"""
        config = Mock(spec=AgentSystemConfig)
        config.auth = Mock()
        config.auth.enabled = False
        
        mcp_cfg = MCPConfig(type="user_management", enabled=True, agent_config=AgentConfig())
        endpoints = UserManagementWebEndpoints("user_management", config, mcp_cfg)
        
        from fastapi import HTTPException
        with pytest.raises(HTTPException) as exc_info:
            endpoints._get_user_database()
        
        assert exc_info.value.status_code == 503
        assert "not enabled" in exc_info.value.detail.lower()
    
    def test_check_admin_permission(self, endpoints):
        """Test admin permission check"""
        # Should succeed when auth enabled
        from fastapi import Request
        mock_request = Mock(spec=Request)
        
        result = endpoints._check_admin_permission(mock_request)
        assert result is True
    
    def test_check_admin_permission_no_auth(self):
        """Test admin permission check when auth disabled"""
        config = Mock(spec=AgentSystemConfig)
        config.auth = Mock()
        config.auth.enabled = False
        
        mcp_cfg = MCPConfig(type="user_management", enabled=True, agent_config=AgentConfig())
        endpoints = UserManagementWebEndpoints("user_management", config, mcp_cfg)
        
        from fastapi import HTTPException, Request
        mock_request = Mock(spec=Request)
        
        with pytest.raises(HTTPException) as exc_info:
            endpoints._check_admin_permission(mock_request)
        
        assert exc_info.value.status_code == 403


class TestUserManagementEndpoints:
    """Test user management API endpoints"""
    
    @pytest.fixture
    def app_with_plugin(self):
        """Create FastAPI app with user management plugin"""
        from agent_system.config.models import AgentSystemConfig, MCPConfig, AgentConfig
        
        app = FastAPI()
        
        # Create plugin with auth enabled
        system_config = Mock(spec=AgentSystemConfig)
        system_config.auth = Mock()
        system_config.auth.enabled = True
        
        mcp_config = MCPConfig(
            type="user_management",
            enabled=True,
            agent_config=AgentConfig(),
            items_per_page=10
        )
        
        plugin = UserManagementPlugin("user_management", system_config, mcp_config)
        router = plugin.get_web_router()
        app.include_router(router)
        
        return app
    
    @patch('agent_system.auth.database.get_db')
    def test_user_management_home_endpoint(self, mock_get_db, app_with_plugin):
        """Test home/dashboard endpoint"""
        # Mock database
        mock_db = Mock()
        mock_user = Mock()
        mock_user.id = 1
        mock_user.username = "testuser"
        mock_user.email = "test@example.com"
        mock_user.full_name = "Test User"
        mock_user.is_active = True
        mock_user.role = UserRole.USER
        mock_user.created_at = datetime.now()
        mock_user.last_login = None
        mock_user.api_key = None
        
        mock_db.list_users.return_value = [mock_user]
        mock_get_db.return_value = mock_db
        
        client = TestClient(app_with_plugin)
        response = client.get("/plugins/user_management/")
        
        assert response.status_code == 200
        assert "text/html" in response.headers["content-type"]
    
    @patch('agent_system.auth.database.get_db')
    def test_list_users_endpoint(self, mock_get_db, app_with_plugin):
        """Test list users API endpoint"""
        # Mock database
        mock_db = Mock()
        mock_user = Mock()
        mock_user.id = 1
        mock_user.username = "testuser"
        mock_user.email = "test@example.com"
        mock_user.full_name = "Test User"
        mock_user.is_active = True
        mock_user.role = UserRole.USER
        mock_user.created_at = datetime.now()
        mock_user.last_login = None
        mock_user.api_key = None
        
        mock_db.list_users.return_value = [mock_user]
        mock_get_db.return_value = mock_db
        
        client = TestClient(app_with_plugin)
        response = client.get("/plugins/user_management/users/list")
        
        assert response.status_code == 200
        data = response.json()
        assert "users" in data
        assert len(data["users"]) == 1
        assert data["users"][0]["username"] == "testuser"
    
    @patch('agent_system.auth.database.get_db')
    def test_get_user_endpoint(self, mock_get_db, app_with_plugin):
        """Test get single user endpoint"""
        # Mock database
        mock_db = Mock()
        mock_user = Mock()
        mock_user.id = 1
        mock_user.username = "testuser"
        mock_user.email = "test@example.com"
        mock_user.full_name = "Test User"
        mock_user.is_active = True
        mock_user.role = UserRole.USER
        mock_user.created_at = datetime.now()
        mock_user.updated_at = None
        mock_user.last_login = None
        mock_user.api_key = None
        
        mock_db.get_user_by_id.return_value = mock_user
        mock_get_db.return_value = mock_db
        
        client = TestClient(app_with_plugin)
        response = client.get("/plugins/user_management/users/1")
        
        assert response.status_code == 200
        data = response.json()
        assert data["username"] == "testuser"
        assert data["id"] == 1
    
    @patch('agent_system.auth.database.get_db')
    def test_get_user_not_found(self, mock_get_db, app_with_plugin):
        """Test get user when user doesn't exist"""
        mock_db = Mock()
        mock_db.get_user_by_id.return_value = None
        mock_get_db.return_value = mock_db
        
        client = TestClient(app_with_plugin)
        response = client.get("/plugins/user_management/users/999")
        
        assert response.status_code == 404
    
    @patch('agent_system.auth.database.get_db')
    def test_get_user_stats_endpoint(self, mock_get_db, app_with_plugin):
        """Test user statistics endpoint"""
        # Mock database with various users
        mock_db = Mock()
        
        admin_user = Mock()
        admin_user.is_active = True
        admin_user.role = "ADMIN"
        admin_user.api_key = "key123"
        
        regular_user = Mock()
        regular_user.is_active = True
        regular_user.role = "USER"
        regular_user.api_key = None
        
        inactive_user = Mock()
        inactive_user.is_active = False
        inactive_user.role = "USER"
        inactive_user.api_key = None
        
        mock_db.list_users.return_value = [admin_user, regular_user, inactive_user]
        mock_get_db.return_value = mock_db
        
        client = TestClient(app_with_plugin)
        response = client.get("/plugins/user_management/stats")
        
        assert response.status_code == 200
        stats = response.json()
        
        assert stats["total_users"] == 3
        assert stats["active_users"] == 2
        assert stats["inactive_users"] == 1
        assert stats["admins"] == 1
        assert stats["users_with_api_keys"] == 1


class TestUserManagementIntegration:
    """Integration tests with full plugin system"""
    
    @patch('agent_system.auth.database.get_db')
    def test_plugin_with_web_registry(self, mock_get_db):
        """Test plugin integration with web registry"""
        from agent_system.plugins.web_adapter import PluginWebRegistry
        
        app = FastAPI()
        registry = PluginWebRegistry()
        
        # Create plugin
        system_config = Mock(spec=AgentSystemConfig)
        system_config.auth = Mock()
        system_config.auth.enabled = True
        
        mcp_config = MCPConfig(
            type="user_management",
            enabled=True,
            agent_config=AgentConfig()
        )
        
        plugin = UserManagementPlugin("user_management", system_config, mcp_config)
        registry.register_web_plugin("user_management", plugin)
        registry.apply_to_app(app)
        
        # Mock database
        mock_db = Mock()
        mock_db.list_users.return_value = []
        mock_get_db.return_value = mock_db
        
        client = TestClient(app)
        
        # Test panels endpoint
        response = client.get("/api/plugins/panels")
        assert response.status_code == 200
        data = response.json()
        
        # Check that user management panel is registered
        user_panels = [p for p in data.get("panels", []) if p.get("plugin_name") == "user_management"]
        assert len(user_panels) > 0
    
    def test_plugin_templates_directory_exists(self):
        """Test that templates directory exists"""
        plugin_dir = Path(__file__).parent.parent / "src" / "plugins" / "user_management"
        templates_dir = plugin_dir / "templates"
        
        # Templates should exist for web UI
        assert templates_dir.exists(), "Templates directory should exist for user management plugin"
    
    def test_plugin_configuration_from_schema(self):
        """Test that plugin can be configured from schema.yaml"""
        plugin_dir = Path(__file__).parent.parent / "src" / "plugins" / "user_management"
        schema_file = plugin_dir / "schema.yaml"
        
        assert schema_file.exists(), "schema.yaml should exist for user management plugin"


class TestUserManagementEdgeCases:
    """Test edge cases and error handling"""
    
    def test_plugin_with_missing_auth_config(self):
        """Test plugin when auth config is missing"""
        config = Mock(spec=AgentSystemConfig)
        # No auth attribute at all
        delattr(config, 'auth')
        
        mcp_config = MCPConfig(
            type="user_management",
            enabled=True,
            agent_config=AgentConfig()
        )
        
        # Should handle gracefully
        plugin = UserManagementPlugin("user_management", config, mcp_config)
        assert plugin.auth_enabled is False
    
    def test_endpoints_with_none_mcp_config_values(self):
        """Test endpoints with missing config values"""
        config = Mock(spec=AgentSystemConfig)
        config.auth = Mock()
        config.auth.enabled = True
        
        # Minimal MCP config
        mcp_config = MCPConfig(
            type="user_management",
            enabled=True,
            agent_config=AgentConfig()
        )
        
        endpoints = UserManagementWebEndpoints("user_management", config, mcp_config)
        
        # Should use defaults
        assert endpoints.items_per_page == 20  # Default
        assert endpoints.allow_self_delete is False
        assert endpoints.show_api_keys is True
    
    @patch('agent_system.auth.database.get_db')
    def test_static_file_serving_security(self, mock_get_db):
        """Test that static file serving prevents path traversal"""
        from agent_system.config.models import AgentSystemConfig, MCPConfig, AgentConfig
        
        app = FastAPI()
        
        system_config = Mock(spec=AgentSystemConfig)
        system_config.auth = Mock()
        system_config.auth.enabled = True
        
        mcp_config = MCPConfig(
            type="user_management",
            enabled=True,
            agent_config=AgentConfig()
        )
        
        plugin = UserManagementPlugin("user_management", system_config, mcp_config)
        router = plugin.get_web_router()
        app.include_router(router)
        
        client = TestClient(app)
        
        # Attempt path traversal
        response = client.get("/plugins/user_management/static/../../../../../../etc/passwd")
        
        # Should be blocked
        assert response.status_code in [403, 404]
