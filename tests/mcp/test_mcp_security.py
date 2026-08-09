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

        assert config.type == "none"
        assert config.api_key is None
        assert config.api_key_header == "Authorization"
        # Note: ssl_verify and timeout are now in centralized config, not MCPAuthConfig
        assert config.max_retries == 3
        assert config.retry_delay == 1.0

    def test_api_key_config(self):
        """Test API key authentication configuration"""
        config = MCPAuthConfig(
            type="api_key",
            api_key="secret123",
            api_key_header="X-API-Key"
        )

        assert config.type == "api_key"
        assert config.api_key == "secret123"
        assert config.api_key_header == "X-API-Key"

    def test_bearer_config(self):
        """Test bearer token authentication configuration"""
        config = MCPAuthConfig(
            type="bearer",
            bearer_token="token123"
        )

        assert config.type == "bearer"
        assert config.bearer_token == "token123"

    def test_basic_config(self):
        """Test basic authentication configuration"""
        config = MCPAuthConfig(
            type="basic",
            username="user",
            password="pass"
        )

        assert config.type == "basic"
        assert config.username == "user"
        assert config.password == "pass"


class TestMCPSecurityManager:
    """Test MCP security manager"""

    def test_add_auth_config(self):
        """Test adding authentication configuration"""
        manager = MCPSecurityManager()
        config = MCPAuthConfig(type="api_key", api_key="secret")

        manager.add_auth_config("test_server", config)

        retrieved = manager.get_auth_config("test_server")
        assert retrieved is not None
        assert retrieved.type == "api_key"
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
            type="bearer",
            bearer_token="token123"
        )
        manager.add_auth_config("test_server", config)

        headers = manager.get_auth_headers("test_server")
        assert headers["Authorization"] == "Bearer token123"

    def test_get_auth_headers_api_key(self):
        """Test getting auth headers for API key"""
        manager = MCPSecurityManager()
        config = MCPAuthConfig(
            type="api_key",
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
            type="basic",
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
        """Test creating security manager from AgentSystemConfig"""
        from agent_system.config.models import (
            AgentSystemConfig,
            MCPServersConfig,
            RemoteMCPConfig,
            MCPAuthConfig  # Changed from AuthConfig
        )
        
        # Build config using Pydantic models
        remote_servers = {
            "server1": RemoteMCPConfig(
                url="http://server1.com",
                transport="streaming",
                enabled=True,
                auth=MCPAuthConfig(  # Changed from AuthConfig
                    type="api_key",
                    api_key="secret123"
                ),
                ssl_verify=False,
                timeout=60.0
            ),
            "server2": RemoteMCPConfig(
                url="http://server2.com",
                transport="streaming",
                enabled=True,
                auth=MCPAuthConfig(  # Changed from AuthConfig
                    type="bearer",
                    bearer_token="token456"
                )
            )
        }
        
        external_servers = MCPServersConfig(
            remote_servers=remote_servers
        )
        
        config = AgentSystemConfig(
            external_servers=external_servers
        )

        manager = MCPSecurityManager.from_config(config)

        # Check server1
        auth1 = manager.get_auth_config("server1")
        assert auth1 is not None
        assert auth1.type == "api_key"
        assert auth1.api_key == "secret123"
        # Note: ssl_verify and timeout are now in centralized config, not MCPAuthConfig
        assert auth1.max_retries == 3
        assert auth1.retry_delay == 1.0

        # Check server2
        auth2 = manager.get_auth_config("server2")
        assert auth2 is not None
        assert auth2.type == "bearer"
        assert auth2.bearer_token == "token456"

    @patch.dict(os.environ, {"API_SECRET": "env_secret"})
    def test_from_config_with_env_vars(self):
        """Test creating security manager with environment variables"""
        from agent_system.config.models import (
            AgentSystemConfig,
            MCPServersConfig,
            RemoteMCPConfig,
            MCPAuthConfig  # Changed from AuthConfig
        )
        
        remote_servers = {
            "env_server": RemoteMCPConfig(
                url="http://env-server.com",
                transport="streaming",
                enabled=True,
                auth=MCPAuthConfig(  # Changed from AuthConfig
                    type="api_key",
                    api_key="${API_SECRET}"
                )
            )
        }
        
        external_servers = MCPServersConfig(
            remote_servers=remote_servers
        )
        
        config = AgentSystemConfig(
            external_servers=external_servers
        )

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
        from agent_system.config.models import (
            AgentSystemConfig,
            MCPServersConfig,
            RemoteMCPConfig,
            MCPAuthConfig  # Changed from AuthConfig
        )
        
        remote_servers = {
            "global_test": RemoteMCPConfig(
                url="http://global-test.com",
                transport="streaming",
                enabled=True,
                auth=MCPAuthConfig(  # Changed from AuthConfig
                    type="api_key",
                    api_key="global_secret"
                )
            )
        }
        
        external_servers = MCPServersConfig(
            remote_servers=remote_servers
        )
        
        config = AgentSystemConfig(
            external_servers=external_servers
        )

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
        type="bearer",
        bearer_token="integration_token"
    )
    manager.add_auth_config("integration_server", config)

    headers = manager.get_auth_headers("integration_server")
    assert headers["Authorization"] == "Bearer integration_token"


def test_build_auth_headers_variants():
    """build_auth_headers maps every auth type; 'none'/missing -> empty."""
    from agent_system.mcp.security import build_auth_headers

    assert build_auth_headers(None) == {}
    assert build_auth_headers(MCPAuthConfig(type="none")) == {}
    assert build_auth_headers(MCPAuthConfig(type="bearer", bearer_token="t"))["Authorization"] == "Bearer t"
    api = build_auth_headers(MCPAuthConfig(type="api_key", api_key="k", api_key_header="X-API-Key"))
    assert api["X-API-Key"] == "k"
    basic = build_auth_headers(MCPAuthConfig(type="basic", username="u", password="p"))
    assert basic["Authorization"].startswith("Basic ")


@pytest.mark.asyncio
async def test_configured_auth_reaches_the_transport():
    """Credentials must arrive at the actual request headers.

    Regression this guards: the headers used to be built and then never
    applied, so every outbound MCP connection went out unauthenticated while
    the config looked perfectly fine. The transport is the SDK's now, so the
    check is that the headers are handed to it -- asserting on our own helper
    alone would pass again even if the hand-over were dropped.
    """
    import types
    from contextlib import asynccontextmanager
    from unittest.mock import patch

    from agent_system.config.models import MCPAuthConfig
    from plugins.mcp_client.connection import ServerConnection

    config = types.SimpleNamespace(
        url="http://example.com/mcp", transport="streaming",
        initialization_options=None,
        auth=MCPAuthConfig(type="bearer", bearer_token="secret-token"),
    )
    connection = ServerConnection("secured", config, timeout=5.0)

    assert connection._auth_headers()["Authorization"] == "Bearer secret-token"

    seen = {}

    @asynccontextmanager
    async def fake_streamable(url, headers=None, **kwargs):
        seen["url"] = url
        seen["headers"] = headers
        raise RuntimeError("stop here -- the handover is all we need to see")
        yield  # pragma: no cover

    with patch("mcp.client.streamable_http.streamablehttp_client", fake_streamable):
        with pytest.raises(RuntimeError, match="stop here"):
            async with connection._open_streams():
                pass

    assert seen["headers"]["Authorization"] == "Bearer secret-token"
    assert seen["url"] == "http://example.com/mcp"