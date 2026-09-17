"""Auth for an outbound MCP connection: the config model, the headers, the hand-over.

Came from tests/mcp/test_mcp_security.py when build_auth_headers moved into this
plugin. The security manager that lived beside it had no caller and is gone with
it; ``${VAR}`` placeholders are resolved by the config loader, tested there.
"""

import pytest

from agent_system.config.models import MCPAuthConfig
from plugins.mcp_client.auth import build_auth_headers


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


def test_build_auth_headers_variants():
    """build_auth_headers maps every auth type; 'none'/missing -> empty."""
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
