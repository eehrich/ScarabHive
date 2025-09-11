"""Tests for script_interpreter plugin integration."""

from pathlib import Path
import yaml

def test_plugin_py_exists():
    """Test plugin.py file exists and contains PLUGIN_FACTORY."""
    plugin_path = Path("src/plugins/script_interpreter/plugin.py")
    assert plugin_path.exists()
    
    with open(plugin_path, "r", encoding="utf-8") as f:
        content = f.read()
    
    assert "PLUGIN_FACTORY" in content
    assert "ScriptInterpreterServer" in content


def test_plugin_yaml_exists():
    """Test plugin.yaml file exists and contains required metadata."""
    plugin_yaml_path = Path("src/plugins/script_interpreter/plugin.yaml")
    assert plugin_yaml_path.exists()
    
    with open(plugin_yaml_path, "r", encoding="utf-8") as f:
        config = yaml.safe_load(f)
    
    assert "name" in config
    assert "author" in config
    assert "version" in config
    assert "description" in config
    assert "entrypoint" in config
    
    assert config["name"] == "script_interpreter"
    assert config["entrypoint"] == "plugin:PLUGIN_FACTORY"


def test_plugin_factory_import():
    """Test PLUGIN_FACTORY can be imported successfully."""
    from src.plugins.script_interpreter.plugin import PLUGIN_FACTORY
    assert PLUGIN_FACTORY is not None
    
    # Test that factory creates correct server instance
    server = PLUGIN_FACTORY()
    from src.plugins.script_interpreter.server import ScriptInterpreterServer
    assert isinstance(server, ScriptInterpreterServer)


def test_plugin_factory_creates_working_server():
    """Test plugin factory creates a working server."""
    from src.plugins.script_interpreter.plugin import PLUGIN_FACTORY
    server = PLUGIN_FACTORY()
    
    # Test basic server functionality
    assert hasattr(server, "call")
    assert hasattr(server, "get_schema") 
    assert hasattr(server, "get_default_action")
    
    # Test schema method works
    schema = server.get_schema()
    assert "type" in schema
    assert schema["type"] == "function"