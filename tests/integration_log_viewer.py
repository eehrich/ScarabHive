"""
Integration test for log viewer plugin with full server
"""

import tempfile
from pathlib import Path
from fastapi import FastAPI
from fastapi.testclient import TestClient

from agent_system.plugins.web_adapter import PluginWebRegistry
from plugins.log_viewer.plugin import LogViewerHybridPlugin


def test_full_server_integration():
    """Test log viewer plugin in full server context"""
    # Create temporary log file
    with tempfile.NamedTemporaryFile(mode='w', suffix='.log', delete=False) as f:
        f.write("2024-01-01 10:00:00 INFO Application started\n")
        f.write("2024-01-01 10:00:01 WARNING Something happened\n") 
        f.write("2024-01-01 10:00:02 ERROR An error occurred\n")
        log_file_path = f.name
    
    try:
        # Create app with plugin
        app = FastAPI()
        registry = PluginWebRegistry()
        
        # Create and register plugin
        config = {
            'log_files': [log_file_path],
            'max_lines': 10
        }
        plugin = LogViewerHybridPlugin("log_viewer", config)
        registry.register_web_plugin("log_viewer", plugin)
        
        # Apply to app
        registry.apply_to_app(app)
        
        client = TestClient(app)
        
        # Test plugin panels endpoint
        response = client.get("/api/plugins/panels")
        assert response.status_code == 200
        data = response.json()
        assert len(data["panels"]) == 1
        panel = data["panels"][0]
        assert panel["plugin_name"] == "log_viewer"
        assert panel["title"] == "System Logs"
        
        # Test plugin endpoints
        response = client.get("/plugins/log_viewer/logs/list")
        assert response.status_code == 200
        data = response.json()
        assert len(data["logs"]) == 1
        assert data["logs"][0]["exists"] is True
        
        # Test panel HTML
        response = client.get("/plugins/log_viewer/panel.html")
        assert response.status_code == 200
        assert "System Logs" in response.text
        
        # Test log download
        response = client.get(f"/plugins/log_viewer/logs/download/{log_file_path}")
        assert response.status_code == 200
        content = response.text
        assert "Application started" in content
        assert "WARNING Something happened" in content
        assert "ERROR An error occurred" in content
        
        print("✓ All integration tests passed!")
        
    finally:
        # Clean up
        Path(log_file_path).unlink(missing_ok=True)


if __name__ == "__main__":
    test_full_server_integration()