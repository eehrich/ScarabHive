"""
Tests for MCP configuration management
"""

import pytest
import tempfile
from pathlib import Path
import yaml

from agent_system.mcp.config import (
    MCPConfig, 
    MCPServerConfig, 
    MCPConfigManager,
    create_example_config
)


class TestMCPServerConfig:
    """Test MCP server configuration"""
    
    def test_default_config(self):
        """Test default server configuration"""
        config = MCPServerConfig(name="test", url="http://example.com")
        
        assert config.name == "test"
        assert config.url == "http://example.com"
        assert config.enabled is True
        assert config.auth_type == "none"
        assert config.timeout == 30.0
        assert config.tools is True
        assert config.resources is True
        assert config.prompts is True
    
    def test_auth_config(self):
        """Test authentication configuration"""
        config = MCPServerConfig(
            name="test",
            url="http://example.com",
            auth_type="api_key",
            api_key="secret123",
            api_key_header="X-API-Key"
        )
        
        assert config.auth_type == "api_key"
        assert config.api_key == "secret123"
        assert config.api_key_header == "X-API-Key"


class TestMCPConfig:
    """Test main MCP configuration"""
    
    def test_default_config(self):
        """Test default configuration"""
        config = MCPConfig()
        
        assert config.enabled is True
        assert config.expose_local_server is True
        assert config.local_server_port == 8000
        assert config.default_timeout == 30.0
        assert len(config.servers) == 0
    
    def test_config_with_servers(self):
        """Test configuration with external servers"""
        server_config = MCPServerConfig(name="test", url="http://example.com")
        config = MCPConfig(servers={"test": server_config})
        
        assert len(config.servers) == 1
        assert "test" in config.servers
        assert config.servers["test"].url == "http://example.com"


class TestMCPConfigManager:
    """Test configuration manager"""
    
    def test_load_empty_config(self):
        """Test loading empty configuration"""
        manager = MCPConfigManager()
        config = manager.load_config({})
        
        assert isinstance(config, MCPConfig)
        assert config.enabled is True
        assert len(config.servers) == 0
    
    def test_load_config_with_data(self):
        """Test loading configuration from data"""
        config_data = {
            "mcp": {
                "enabled": True,
                "local_server_port": 9000,
                "external_servers": {
                    "test_server": {
                        "url": "http://test.com",
                        "enabled": True,
                        "auth": {
                            "type": "api_key",
                            "api_key": "test123"
                        }
                    }
                }
            }
        }
        
        manager = MCPConfigManager()
        config = manager.load_config(config_data)
        
        assert config.enabled is True
        assert config.local_server_port == 9000
        assert len(config.servers) == 1
        assert "test_server" in config.servers
        
        server = config.servers["test_server"]
        assert server.url == "http://test.com"
        assert server.auth_type == "api_key"
        assert server.api_key == "test123"
    
    def test_load_config_from_file(self):
        """Test loading configuration from file"""
        config_data = {
            "mcp": {
                "enabled": True,
                "external_servers": {
                    "file_server": {
                        "url": "http://file.com",
                        "enabled": True
                    }
                }
            }
        }
        
        with tempfile.NamedTemporaryFile(mode='w', suffix='.yaml', delete=False) as f:
            yaml.dump(config_data, f)
            temp_path = f.name
        
        try:
            manager = MCPConfigManager(temp_path)
            config = manager.load_config()
            
            assert config.enabled is True
            assert len(config.servers) == 1
            assert "file_server" in config.servers
        finally:
            Path(temp_path).unlink()
    
    def test_save_config(self):
        """Test saving configuration to file"""
        config = MCPConfig(
            enabled=True,
            local_server_port=9000
        )
        
        server_config = MCPServerConfig(
            name="save_test",
            url="http://save.com",
            auth_type="bearer",
            bearer_token="token123"
        )
        config.servers["save_test"] = server_config
        
        with tempfile.NamedTemporaryFile(mode='w', suffix='.yaml', delete=False) as f:
            temp_path = f.name
        
        try:
            manager = MCPConfigManager(temp_path)
            manager.save_config(config)
            
            # Verify file was created and can be loaded
            assert Path(temp_path).exists()
            
            with open(temp_path, 'r') as f:
                saved_data = yaml.safe_load(f)
            
            assert saved_data["mcp"]["enabled"] is True
            assert saved_data["mcp"]["local_server_port"] == 9000
            assert "save_test" in saved_data["mcp"]["external_servers"]
            
            server_data = saved_data["mcp"]["external_servers"]["save_test"]
            assert server_data["url"] == "http://save.com"
            assert server_data["auth"]["type"] == "bearer"
            assert server_data["auth"]["bearer_token"] == "token123"
        finally:
            Path(temp_path).unlink()
    
    def test_validate_config(self):
        """Test configuration validation"""
        manager = MCPConfigManager()
        
        # Valid configuration
        valid_config = MCPConfig()
        issues = manager.validate_config(valid_config)
        assert len(issues) == 0
        
        # Invalid port
        invalid_config = MCPConfig(local_server_port=99999)
        issues = manager.validate_config(invalid_config)
        assert len(issues) > 0
        assert any("Invalid local_server_port" in issue for issue in issues)
        
        # Invalid timeout
        invalid_config = MCPConfig(default_timeout=-1)
        issues = manager.validate_config(invalid_config)
        assert len(issues) > 0
        assert any("Invalid default_timeout" in issue for issue in issues)
    
    def test_validate_server_config(self):
        """Test server configuration validation"""
        manager = MCPConfigManager()
        
        # Invalid URL
        config = MCPServerConfig(name="test", url="invalid-url")
        issues = manager._validate_server_config("test", config)
        assert len(issues) > 0
        assert any("URL must start with http" in issue for issue in issues)
        
        # Missing auth details
        config = MCPServerConfig(
            name="test",
            url="http://example.com",
            auth_type="api_key"
        )
        issues = manager._validate_server_config("test", config)
        assert len(issues) > 0
        assert any("api_key required" in issue for issue in issues)


class TestExampleConfig:
    """Test example configuration generation"""
    
    def test_create_example_config(self):
        """Test creating example configuration"""
        config = create_example_config()
        
        assert "mcp" in config
        mcp_config = config["mcp"]
        
        assert mcp_config["enabled"] is True
        assert "external_servers" in mcp_config
        assert len(mcp_config["external_servers"]) > 0
        
        # Check example server
        example_server = mcp_config["external_servers"]["example_server"]
        assert example_server["url"] == "https://api.example.com/mcp"
        assert example_server["auth"]["type"] == "api_key"


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
    
    manager = MCPConfigManager()
    config = manager.load_config(config_data)
    
    # Verify loaded configuration
    assert config.enabled is True
    assert len(config.servers) == 1
    
    server = config.servers["integration_test"]
    assert server.tools is True
    assert server.resources is False
    assert server.prompts is True