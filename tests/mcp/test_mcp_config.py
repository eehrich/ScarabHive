"""
Tests for MCP configuration management
"""

import pytest
import tempfile
from pathlib import Path
import yaml

from agent_system.config.models import AgentSystemConfig, PluginsConfig, MCPServersConfig, MCPConfig, RemoteMCPConfig


class TestMCPConfigModern:
    def test_mcp_config_defaults(self):
        cfg = MCPConfig(type="basic_agent", enabled=True)
        assert cfg.type == "basic_agent"
        assert cfg.enabled is True

    def test_remote_mcp_config(self):
        r = RemoteMCPConfig(url="http://example.com", enabled=True)
        assert r.url == "http://example.com"
        assert r.enabled is True

    def test_mcp_system_config_serialization(self):
        syscfg = PluginsConfig(plugin_dirs=["plugins"], servers={
            "test": MCPConfig(type="test", enabled=True)
        })
        # Serialize to yaml and reload to ensure structure is preserved
        p = Path(tempfile.gettempdir()) / "test_mcp_system_config.yaml"
        try:
            # Use Pydantic model_dump() for compatibility with pydantic v2
            data = syscfg.model_dump() if hasattr(syscfg, "model_dump") else syscfg.dict()
            Path(p).write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
            loaded = yaml.safe_load(Path(p).read_text(encoding="utf-8"))
            assert "servers" in loaded
            assert "test" in loaded["servers"]
        finally:
            try:
                if p.exists():
                    p.unlink()
            except Exception:
                pass

    def test_validate_server_config(self):
        """Test server configuration validation"""
        # Basic model validation: construct RemoteMCPConfig with expected fields
        cfg = RemoteMCPConfig(url="http://example.com", enabled=True)
        assert cfg.url.startswith("http")
        assert cfg.enabled is True


class TestExampleConfig:
    """Test example configuration generation"""

    def test_create_example_config(self):
        """Test creating example configuration"""
        # Create a small example MCPSystemConfig and ensure expected structure
        example = PluginsConfig(plugin_dirs=["plugins"], servers={
            "example_server": MCPConfig(type="example", enabled=True)
        })

        assert example.plugin_dirs == ["plugins"]
        assert "example_server" in example.servers
        assert example.servers["example_server"].enabled is True


@pytest.mark.asyncio
async def test_config_integration():
    """Test configuration integration with other components"""
    config_data = {
        "mcp": {
            "enabled": True,
            "external_servers": {
                "integration_test": {
                    "url": "http://integration.com",
                    "enabled": True,
                    "features": {
                        "tools": True,
                        "resources": False,
                        "prompts": True
                    }
                }
            }
        }
    }

    # Map the provided dict into our MCPSystemConfig shape for a basic sanity check
    mcp = config_data.get("mcp", {})
    servers = {}
    for name, val in mcp.get("external_servers", {}).items():
        servers[name] = MCPConfig(type=val.get("type", "remote"), enabled=val.get("enabled", False))

    config = PluginsConfig(plugin_dirs=["plugins"], servers=servers)
    assert any(s.enabled for s in config.servers.values())