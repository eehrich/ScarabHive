"""
Tests for plugin web adapter and registry
"""

import pytest
from pathlib import Path
from typing import Dict, Any, List, Optional
from fastapi import APIRouter, FastAPI
from fastapi.testclient import TestClient

from agent_system.plugins.web_adapter import (
    PluginWebInterface,
    PluginWebRegistry,
)


class MockWebPlugin(PluginWebInterface):
    """Mock plugin with web capabilities for testing"""
    
    def __init__(self, name: str, has_router: bool = True, has_static: bool = True, has_panels: bool = True):
        self.name = name
        self.has_router = has_router
        self.has_static = has_static
        self.has_panels = has_panels
        
    def get_web_router(self) -> Optional[APIRouter]:
        if not self.has_router:
            return None
            
        router = APIRouter(prefix=f"/plugins/{self.name}")
        
        @router.get("/status")
        def plugin_status():
            return {"status": "ok", "plugin": self.name}
            
        return router
    
    def get_static_assets(self) -> Optional[Path]:
        if not self.has_static:
            return None
        # Return a mock path that exists (use current directory for testing)
        return Path.cwd()
    
    def get_panels(self) -> List[Dict[str, Any]]:
        if not self.has_panels:
            return []
            
        return [{
            "id": f"{self.name}_panel",
            "title": f"{self.name.title()} Panel",
            "url": f"/plugins/{self.name}/panel.html",
            "icon": "chart-bar"
        }]
    
    def get_security_config(self) -> Dict[str, Any]:
        return {
            "require_auth": True,
            "cors_origins": ["*"],
            "rate_limit": "10/minute"
        }


class TestPluginWebInterface:
    """Test the PluginWebInterface base class"""
    
    def test_base_interface_defaults(self):
        """Test that base interface provides reasonable defaults"""
        
        class EmptyPlugin(PluginWebInterface):
            pass
        
        plugin = EmptyPlugin()
        
        assert plugin.get_web_router() is None
        assert plugin.get_static_assets() is None
        assert plugin.get_panels() == []
        
        security_config = plugin.get_security_config()
        assert security_config["require_auth"] is False
        assert security_config["cors_origins"] == []
        assert security_config["rate_limit"] is None


class TestPluginWebRegistry:
    """Test the PluginWebRegistry class"""
    
    @pytest.fixture
    def registry(self):
        """Create a clean registry for each test"""
        return PluginWebRegistry()
    
    @pytest.fixture
    def mock_plugin(self):
        """Create a mock plugin for testing"""
        return MockWebPlugin("test_plugin")
    
    def test_registry_initialization(self, registry):
        """Test registry initializes correctly"""
        assert len(registry.web_plugins) == 0
        assert len(registry.active_routers) == 0
        assert len(registry.static_mounts) == 0
        assert len(registry.security_configs) == 0
    
    def test_register_web_plugin(self, registry, mock_plugin):
        """Test registering a plugin with web capabilities"""
        registry.register_web_plugin("test", mock_plugin)
        
        assert "test" in registry.web_plugins
        assert "test" in registry.active_routers
        assert "test" in registry.static_mounts
        assert "test" in registry.security_configs
        
        assert registry.web_plugins["test"] == mock_plugin
    
    def test_register_plugin_no_router(self, registry):
        """Test registering a plugin without router"""
        plugin = MockWebPlugin("test", has_router=False)
        registry.register_web_plugin("test", plugin)
        
        assert "test" in registry.web_plugins
        assert "test" not in registry.active_routers
    
    def test_register_plugin_no_static(self, registry):
        """Test registering a plugin without static assets"""
        plugin = MockWebPlugin("test", has_static=False)
        registry.register_web_plugin("test", plugin)
        
        assert "test" in registry.web_plugins
        assert "test" not in registry.static_mounts
    
    def test_unregister_web_plugin(self, registry, mock_plugin):
        """Test unregistering a plugin"""
        registry.register_web_plugin("test", mock_plugin)
        assert "test" in registry.web_plugins
        
        registry.unregister_web_plugin("test")
        
        assert "test" not in registry.web_plugins
        assert "test" not in registry.active_routers
        assert "test" not in registry.static_mounts
        assert "test" not in registry.security_configs
    
    def test_get_all_panels(self, registry):
        """Test getting all panels from registered plugins"""
        plugin1 = MockWebPlugin("plugin1")
        plugin2 = MockWebPlugin("plugin2")
        plugin3 = MockWebPlugin("plugin3", has_panels=False)
        
        registry.register_web_plugin("plugin1", plugin1)
        registry.register_web_plugin("plugin2", plugin2)
        registry.register_web_plugin("plugin3", plugin3)
        
        panels = registry.get_all_panels()
        
        assert len(panels) == 2  # Only plugin1 and plugin2 have panels
        
        # Check that plugin names are added to panel configs
        plugin_names = {panel["plugin_name"] for panel in panels}
        assert plugin_names == {"plugin1", "plugin2"}
        
        # Check that defaults are applied
        for panel in panels:
            assert "position" in panel
            assert "width" in panel
            assert "height" in panel
            assert panel["position"] == "right"  # Default value
    
    def test_get_all_panels_missing_required_fields(self, registry):
        """Test handling of panels with missing required fields"""
        
        class BadPlugin(PluginWebInterface):
            def get_panels(self):
                return [
                    {"title": "No ID panel"},  # Missing 'id'
                    {"id": "no_url", "title": "No URL panel"},  # Missing 'url'
                    {"id": "valid", "title": "Valid panel", "url": "/test"}  # Valid
                ]
        
        registry.register_web_plugin("bad", BadPlugin())
        panels = registry.get_all_panels()
        
        # Only the valid panel should be included
        assert len(panels) == 1
        assert panels[0]["id"] == "valid"
    
    def test_get_security_config(self, registry, mock_plugin):
        """Test getting security config for a plugin"""
        registry.register_web_plugin("test", mock_plugin)
        
        config = registry.get_security_config("test")
        
        assert config["require_auth"] is True
        assert config["cors_origins"] == ["*"]
        assert config["rate_limit"] == "10/minute"
        
        # Test non-existent plugin
        empty_config = registry.get_security_config("nonexistent")
        assert empty_config == {}
    
    def test_apply_to_app(self, registry):
        """Test applying web capabilities to FastAPI app"""
        app = FastAPI()
        
        # Register a plugin
        plugin = MockWebPlugin("test")
        registry.register_web_plugin("test", plugin)
        
        # Apply to app
        registry.apply_to_app(app)
        
        # Test that endpoints were added
        client = TestClient(app)
        
        # Test plugin endpoint
        response = client.get("/plugins/test/status")
        assert response.status_code == 200
        assert response.json() == {"status": "ok", "plugin": "test"}
        
        # Test panels endpoint
        response = client.get("/api/plugins/panels")
        assert response.status_code == 200
        data = response.json()
        assert "panels" in data
        assert len(data["panels"]) == 1
        assert data["panels"][0]["plugin_name"] == "test"


class TestPluginWebIntegration:
    """Test integration with existing systems"""
    
    def test_global_registry_instance(self):
        """Test that global registry instance exists"""
        # The global instance should be available
        from agent_system.plugins.web_adapter import plugin_web_registry
        assert plugin_web_registry is not None
        assert isinstance(plugin_web_registry, PluginWebRegistry)