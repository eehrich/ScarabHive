"""Tests for YAML parsing error logging in configuration files."""
from __future__ import annotations

import pytest
import yaml
import logging
from pathlib import Path

from agent_system.plugins.schema_loader import load_schema_from_dir


class TestYAMLErrorLogging:
    """Test that YAML parsing errors are properly logged."""
    
    def test_invalid_yaml_syntax_in_schema_raises_error(self, tmp_path: Path, caplog):
        """Test that invalid YAML syntax in schema.yaml raises error with logging."""
        # Create plugin directory with invalid schema
        plugin_dir = tmp_path / "test_plugin"
        plugin_dir.mkdir()
        
        schema_file = plugin_dir / "schema.yaml"
        # Invalid YAML: unclosed bracket
        schema_file.write_text("""
tools:
  - type: function
    function:
      name: [invalid bracket here
      description: Test
""", encoding="utf-8")
        
        with caplog.at_level(logging.ERROR):
            with pytest.raises(RuntimeError, match="Invalid YAML syntax|Failed to parse schema"):
                load_schema_from_dir(plugin_dir)
        
        # Check that error was logged
        assert any("YAML syntax error" in record.message or "Failed to parse" in record.message 
                   for record in caplog.records)
    
    def test_jinja2_template_error_in_schema_raises_error(self, tmp_path: Path, caplog):
        """Test that Jinja2 template errors in schema.yaml are logged."""
        plugin_dir = tmp_path / "test_plugin"
        plugin_dir.mkdir()
        
        schema_file = plugin_dir / "schema.yaml"
        # Invalid Jinja2: unclosed template variable
        schema_file.write_text("""
tools:
  - type: function
    function:
      name: {{ name
      description: Test
""", encoding="utf-8")
        
        with caplog.at_level(logging.ERROR):
            with pytest.raises(RuntimeError, match="Failed to render schema template"):
                load_schema_from_dir(plugin_dir)
        
        # Check that error was logged
        assert any("Jinja2 template rendering failed" in record.message for record in caplog.records)
    
    def test_valid_schema_loads_without_errors(self, tmp_path: Path, caplog):
        """Test that valid schema loads without errors."""
        plugin_dir = tmp_path / "test_plugin"
        plugin_dir.mkdir()
        
        schema_file = plugin_dir / "schema.yaml"
        schema_file.write_text("""
tools:
  - type: function
    function:
      name: test_tool
      description: Test tool
      parameters:
        type: object
        properties:
          param1:
            type: string
            description: Test parameter
        required:
          - param1
""", encoding="utf-8")
        
        with caplog.at_level(logging.ERROR):
            result = load_schema_from_dir(plugin_dir)
        
        # Should load successfully
        assert result is not None
        assert "tools" in result
        
        # No errors should be logged
        assert not any(record.levelname == "ERROR" for record in caplog.records)
    
    def test_non_dict_yaml_returns_none_with_logging(self, tmp_path: Path, caplog):
        """Test that non-dict YAML content logs error and returns None."""
        plugin_dir = tmp_path / "test_plugin"
        plugin_dir.mkdir()
        
        schema_file = plugin_dir / "schema.yaml"
        # Valid YAML but not a dict
        schema_file.write_text("- item1\n- item2\n- item3", encoding="utf-8")
        
        with caplog.at_level(logging.ERROR):
            result = load_schema_from_dir(plugin_dir)
        
        # Should return None
        assert result is None
        
        # Should log error about wrong type
        assert any("did not parse to a dictionary" in record.message for record in caplog.records)


class TestConfigYAMLErrorLogging:
    """Test YAML error logging in main configuration files."""
    
    def test_invalid_yaml_in_main_config_raises_with_logging(self, tmp_path: Path, caplog):
        """Test that invalid YAML in main config raises error with logging."""
        from agent_system.config.settings import load_settings
        
        # Create invalid config file with clear YAML syntax error
        config_file = tmp_path / "config.yaml"
        config_file.write_text("""
plugins:
  servers:
    test_server:
      type: test
      enabled: true
  - invalid list here [
    should not work
""", encoding="utf-8")
        
        with caplog.at_level(logging.ERROR):
            with pytest.raises((ValueError, yaml.YAMLError, RuntimeError)):
                load_settings(str(config_file))
        
        # Should log error
        assert any("YAML syntax error" in record.message or "Failed to" in record.message 
                   for record in caplog.records)
    
    def test_invalid_yaml_in_included_config_logs_warning(self, tmp_path: Path, caplog):
        """Test that invalid YAML in included config logs warning but continues."""
        from agent_system.config.settings import load_settings
        
        # Create main config
        config_file = tmp_path / "config.yaml"
        config_file.write_text("""
includes:
  - included.yaml

plugins:
  servers:
    test_server:
      type: test
      enabled: true
""", encoding="utf-8")
        
        # Create invalid included file
        included_file = tmp_path / "included.yaml"
        included_file.write_text("""
agents:
  bad_agent:
    type: basic_agent
    # Invalid syntax here
    [broken
""", encoding="utf-8")
        
        with caplog.at_level(logging.ERROR):
            # Should load successfully (continues despite included file error)
            config = load_settings(str(config_file))
        
        # Config should be loaded (without the broken included file)
        assert config is not None
        
        # Should log error about included file
        assert any("YAML syntax error" in record.message and "included.yaml" in record.message 
                   for record in caplog.records)
        # Check for warning about skipping the file
        assert any(("Skipping" in record.message or "YAML syntax error" in record.message) 
                   and record.levelname in ("WARNING", "ERROR")
                   for record in caplog.records)
