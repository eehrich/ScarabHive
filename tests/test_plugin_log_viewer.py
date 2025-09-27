"""
Tests for log viewer plugin
"""

import pytest
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from plugins.log_viewer.plugin import LogViewerHybridPlugin
from agent_system.plugins.web_adapter import PluginWebRegistry


class TestLogViewerServer:
    """Test the LogViewerServer plugin"""
    
    @pytest.fixture
    def plugin(self):
        """Create a log viewer plugin instance"""
        config = {
            'log_files': ['test.log', 'test2.log'],
            'max_lines': 50,
            'refresh_interval': 0.1  # Faster for testing
        }
        return LogViewerHybridPlugin("log_viewer", config)
    
    def test_plugin_initialization(self, plugin):
        """Test plugin initializes correctly"""
        assert plugin.name == "log_viewer"
        assert plugin.log_files == ['test.log', 'test2.log']
        assert plugin.max_lines == 50
        assert plugin.refresh_interval == 0.1
    
    async def test_plugin_call_interface(self, plugin):
        """Test MCP call interface"""
        result = await plugin.call()
        
        assert result["status"] == "ok"
        assert result["name"] == "log_viewer"
        assert result["log_files"] == ['test.log', 'test2.log']
        assert result["active"] is True
    
    def test_web_router_creation(self, plugin):
        """Test that plugin creates a web router"""
        router = plugin.get_web_router()
        
        assert router is not None
        assert router.prefix == "/plugins/log_viewer"
    
    def test_static_assets_path(self, plugin):
        """Test static assets path"""
        static_path = plugin.get_static_assets()
        
        # Should return the static directory path
        assert static_path is not None
        assert static_path.name == "static"
    
    def test_panels_configuration(self, plugin):
        """Test UI panels configuration"""
        panels = plugin.get_panels()
        
        assert len(panels) == 1
        panel = panels[0]
        
        assert panel["id"] == "log_viewer_panel"
        assert panel["title"] == "System Logs"
        assert panel["url"] == "/plugins/log_viewer/panel.html"
        assert panel["position"] == "bottom"
        assert panel["height"] == "400px"
    
    def test_security_configuration(self, plugin):
        """Test security configuration"""
        config = plugin.get_security_config()
        
        assert config["require_auth"] is False
        assert config["rate_limit"] == "30/minute"


class TestLogViewerWebIntegration:
    """Test log viewer plugin web integration"""
    
    def test_plugin_web_endpoints(self):
        """Test plugin web endpoints work correctly"""
        # Create app with plugin
        app = FastAPI()
        registry = PluginWebRegistry()
        
        # Create plugin and register
        config = {'log_files': ['test.log']}
        plugin = LogViewerHybridPlugin("log_viewer", config)
        registry.register_web_plugin("log_viewer", plugin)
        
        # Apply to app
        registry.apply_to_app(app)
        
        client = TestClient(app)
        
        # Test panels endpoint
        response = client.get("/api/plugins/panels")
        assert response.status_code == 200
        data = response.json()
        assert len(data["panels"]) == 1
        assert data["panels"][0]["plugin_name"] == "log_viewer"
    
    @patch('pathlib.Path.exists')
    def test_list_log_files_endpoint(self, mock_exists):
        """Test the list log files endpoint"""
        # Setup mock
        mock_exists.return_value = True
        
        # Create app with plugin
        app = FastAPI()
        config = {'log_files': ['test.log', 'test2.log']}
        plugin = LogViewerHybridPlugin("log_viewer", config)
        
        router = plugin.get_web_router()
        app.include_router(router)
        
        client = TestClient(app)
        
        # Mock Path.stat() for file stats
        with patch('pathlib.Path.stat') as mock_stat:
            mock_stat.return_value.st_size = 1024
            mock_stat.return_value.st_mtime = 1234567890.0
            
            response = client.get("/plugins/log_viewer/logs/list")
            assert response.status_code == 200
            
            data = response.json()
            assert "logs" in data
            assert len(data["logs"]) == 2
            
            for log in data["logs"]:
                assert log["exists"] is True
                assert "size" in log
                assert "modified" in log
    
    def test_panel_html_endpoint(self):
        """Test the panel HTML endpoint"""
        app = FastAPI()
        config = {'log_files': ['test.log']}
        plugin = LogViewerHybridPlugin("log_viewer", config)
        
        router = plugin.get_web_router()
        app.include_router(router)
        
        client = TestClient(app)
        
        response = client.get("/plugins/log_viewer/panel.html")
        assert response.status_code == 200
        assert "text/html" in response.headers["content-type"]
        
        # Check that HTML contains expected elements
        html_content = response.text
        assert "Log Viewer Panel" in html_content  # Check actual title instead of "System Logs"
        assert "log-container" in html_content
        assert "log_viewer_module.js" in html_content  # Check for external JavaScript file
        assert "log_viewer.css" in html_content  # Check for external CSS file
    
    def test_download_endpoint_security(self):
        """Test download endpoint security (only allows configured files)"""
        app = FastAPI()
        config = {'log_files': ['allowed.log']}
        plugin = LogViewerHybridPlugin("log_viewer", config)
        
        router = plugin.get_web_router()
        app.include_router(router)
        
        client = TestClient(app)
        
        # Try to access non-allowed file
        response = client.get("/plugins/log_viewer/logs/download/notallowed.log")
        assert response.status_code == 200
        data = response.json()
        assert "error" in data
        assert "not allowed" in data["error"].lower()
    
    def test_stream_endpoint_security(self):
        """Test streaming endpoint security"""
        app = FastAPI()  
        config = {'log_files': ['allowed.log']}
        plugin = LogViewerHybridPlugin("log_viewer", config)
        
        router = plugin.get_web_router()
        app.include_router(router)
        
        client = TestClient(app)
        
        # Try to poll non-allowed file (streaming equivalent)
        response = client.get("/plugins/log_viewer/logs/poll/notallowed.log")
        assert response.status_code == 200
        data = response.json()
        assert "error" in data
        assert "not allowed" in data["error"].lower()
    
    def test_static_file_serving(self):
        """Test that static files are served correctly"""
        app = FastAPI()
        config = {'log_files': ['logs/test.log']}
        plugin = LogViewerHybridPlugin("log_viewer", config)
        
        router = plugin.get_web_router()
        app.include_router(router)
        
        client = TestClient(app)
        
        # Test CSS file serving
        response = client.get("/plugins/log_viewer/static/log_viewer.css")
        assert response.status_code == 200
        assert "text/css" in response.headers["content-type"]
        assert ".log-container" in response.text  # Check for CSS content
        
        # Test JavaScript file serving
        response = client.get("/plugins/log_viewer/static/log_viewer.js")
        assert response.status_code == 200
        assert "application/javascript" in response.headers["content-type"]
        assert "EventSource" in response.text  # Check for JS content
        
        # Test security - should not allow directory traversal
        response = client.get("/plugins/log_viewer/static/../endpoints.py")
        assert response.status_code == 404


class TestLogViewerPluginFactory:
    """Test plugin factory and discovery"""
    
    def test_plugin_factory_export(self):
        """Test that PLUGIN_FACTORY is properly exported"""
        from plugins.log_viewer.plugin import PLUGIN_FACTORY
        
        assert PLUGIN_FACTORY is not None
        
        # Test factory can create instances
        plugin = PLUGIN_FACTORY("test", {})
        assert isinstance(plugin, LogViewerHybridPlugin)
        assert plugin.name == "test"