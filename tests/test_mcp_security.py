"""
Tests for MCP security and authentication
"""

import pytest
import os
from unittest.mock import patch

from agent_system.mcp.security import (
    MCPAuthConfig,
    MCPSecurityManager,
    configure_security,
    get_security_manager
)


class TestMCPAuthConfig:
    """Test MCP authentication configuration"""

    def test_default_config(self):
        """Test default authentication configuration"""
        config = MCPAuthConfig()

        assert config.auth_type == "none"
        assert config.api_key is None
        assert config.api_key_header == "Authorization"
        assert config.ssl_verify is True
        assert config.timeout == 30.0

    def test_api_key_config(self):
        """Test API key authentication configuration"""
        config = MCPAuthConfig(
            auth_type="api_key",
            api_key="secret123",
            api_key_header="X-API-Key"
        )

        assert config.auth_type == "api_key"
        assert config.api_key == "secret123"
        assert config.api_key_header == "X-API-Key"

    def test_bearer_config(self):
        """Test bearer token authentication configuration"""
        config = MCPAuthConfig(
            auth_type="bearer",
            bearer_token="token123"
        )

        assert config.auth_type == "bearer"
        assert config.bearer_token == "token123"

    def test_basic_config(self):
        """Test basic authentication configuration"""
        config = MCPAuthConfig(
            auth_type="basic",
            username="user",
            password="pass"
        )

        assert config.auth_type == "basic"
        assert config.username == "user"
        assert config.password == "pass"


class TestMCPSecurityManager:
    """Test MCP security manager"""

    def test_add_auth_config(self):
        """Test adding authentication configuration"""
        manager = MCPSecurityManager()
        config = MCPAuthConfig(auth_type="api_key", api_key="secret")

        manager.add_auth_config("test_server", config)

        retrieved = manager.get_auth_config("test_server")
        assert retrieved is not None
        assert retrieved.auth_type == "api_key"
        assert retrieved.api_key == "secret"

    def test_get_auth_config_not_found(self):
        """Test getting non-existent authentication configuration"""
        manager = MCPSecurityManager()

        config = manager.get_auth_config("nonexistent")
        assert config is None

    def test_get_auth_headers_none(self):
        """Test getting auth headers for no authentication"""
        manager = MCPSecurityManager()

        headers = manager.get_auth_headers("nonexistent")
        assert headers == {}

    def test_get_auth_headers_bearer(self):
        """Test getting auth headers for bearer token"""
        manager = MCPSecurityManager()
        config = MCPAuthConfig(
            auth_type="bearer",
            bearer_token="token123"
        )
        manager.add_auth_config("test_server", config)

        headers = manager.get_auth_headers("test_server")
        assert headers["Authorization"] == "Bearer token123"

    def test_get_auth_headers_api_key(self):
        """Test getting auth headers for API key"""
        manager = MCPSecurityManager()
        config = MCPAuthConfig(
            auth_type="api_key",
            api_key="secret123",
            api_key_header="X-API-Key"
        )
        manager.add_auth_config("test_server", config)

        headers = manager.get_auth_headers("test_server")
        assert headers["X-API-Key"] == "secret123"

    def test_get_auth_headers_basic(self):
        """Test getting auth headers for basic authentication"""
        manager = MCPSecurityManager()
        config = MCPAuthConfig(
            auth_type="basic",
            username="user",
            password="pass"
        )
        manager.add_auth_config("test_server", config)

        headers = manager.get_auth_headers("test_server")
        assert "Authorization" in headers
        assert headers["Authorization"].startswith("Basic ")

        # Decode and verify
        import base64
        encoded = headers["Authorization"][6:]  # Remove "Basic "
        decoded = base64.b64decode(encoded).decode()
        assert decoded == "user:pass"

    def test_resolve_env_vars(self):
        """Test environment variable resolution"""
        with patch.dict(os.environ, {"TEST_VAR": "resolved_value"}):
            result = MCPSecurityManager.resolve_env_vars("${TEST_VAR}")
            assert result == "resolved_value"

        # Test non-env var string
        result = MCPSecurityManager.resolve_env_vars("normal_string")
        assert result == "normal_string"

        # Test missing env var
        result = MCPSecurityManager.resolve_env_vars("${MISSING_VAR}")
        assert result == "${MISSING_VAR}"

    def test_from_config(self):
        """Test creating security manager from configuration"""
        config = {
            "servers": {
                "server1": {
                    "auth": {
                        "type": "api_key",
                        "api_key": "secret123"
                    },
                    "ssl_verify": False,
                    "timeout": 60.0
                },
                "server2": {
                    "auth": {
                        "type": "bearer",
                        "bearer_token": "token456"
                    }
                }
            }
        }

        manager = MCPSecurityManager.from_config(config)

        # Check server1
        auth1 = manager.get_auth_config("server1")
        assert auth1 is not None
        assert auth1.auth_type == "api_key"
        assert auth1.api_key == "secret123"
        assert auth1.ssl_verify is False
        assert auth1.timeout == 60.0

        # Check server2
        auth2 = manager.get_auth_config("server2")
        assert auth2 is not None
        assert auth2.auth_type == "bearer"
        assert auth2.bearer_token == "token456"

    @patch.dict(os.environ, {"API_SECRET": "env_secret"})
    def test_from_config_with_env_vars(self):
        """Test creating security manager with environment variables"""
        config = {
            "servers": {
                "env_server": {
                    "auth": {
                        "type": "api_key",
                        "api_key": "${API_SECRET}"
                    }
                }
            }
        }

        manager = MCPSecurityManager.from_config(config)

        auth = manager.get_auth_config("env_server")
        assert auth is not None
        assert auth.api_key == "env_secret"


class TestGlobalSecurity:
    """Test global security functions"""

    def test_get_security_manager(self):
        """Test getting global security manager"""
        manager = get_security_manager()
        assert isinstance(manager, MCPSecurityManager)

    def test_configure_security(self):
        """Test configuring global security"""
        config = {
            "servers": {
                "global_test": {
                    "auth": {
                        "type": "api_key",
                        "api_key": "global_secret"
                    }
                }
            }
        }

        configure_security(config)

        manager = get_security_manager()
        auth = manager.get_auth_config("global_test")
        assert auth is not None
        assert auth.api_key == "global_secret"


@pytest.mark.asyncio
async def test_security_integration():
    """Test security integration with other components"""
    # This would test integration with actual MCP client/server components
    # when they implement security header support

    manager = MCPSecurityManager()
    config = MCPAuthConfig(
        auth_type="bearer",
        bearer_token="integration_token"
    )
    manager.add_auth_config("integration_server", config)

    headers = manager.get_auth_headers("integration_server")
    assert headers["Authorization"] == "Bearer integration_token"