"""Tests for hook configuration loading and management."""
from __future__ import annotations

import tempfile
from pathlib import Path

from agent_system.hooks.config import (
    HooksConfig,
    load_hooks_config,
    validate_hook_references,
)


def test_hooks_config_defaults():
    """Test HooksConfig with default values."""
    config = HooksConfig()
    
    assert config.enabled is True
    assert config.default_timeout == 30.0
    assert config.overrides == {}


def test_hooks_config_custom_values():
    """Test HooksConfig with custom values."""
    overrides = {
        "test_hook": {
            "enabled": False,
            "timeout": 10.0,
            "order": {"before": ["end"], "after": ["begin"]}
        }
    }
    
    config = HooksConfig(
        enabled=False,
        default_timeout=15.0,
        overrides=overrides
    )
    
    assert config.enabled is False
    assert config.default_timeout == 15.0
    assert config.overrides == overrides


def test_get_hook_config_defaults():
    """Test getting configuration for a hook with no overrides."""
    config = HooksConfig(enabled=True, default_timeout=20.0)
    
    hook_config = config.get_hook_config("test_hook")
    
    assert hook_config["enabled"] is True
    assert hook_config["timeout"] == 20.0
    assert hook_config["order"] == {"before": [], "after": []}


def test_get_hook_config_with_override():
    """Test getting configuration for a hook with overrides."""
    overrides = {
        "test_hook": {
            "enabled": False,
            "timeout": 5.0,
            "order": {"before": ["other_hook"], "after": ["begin"]}
        }
    }
    
    config = HooksConfig(overrides=overrides)
    hook_config = config.get_hook_config("test_hook")
    
    assert hook_config["enabled"] is False
    assert hook_config["timeout"] == 5.0
    assert hook_config["order"] == {"before": ["other_hook"], "after": ["begin"]}


def test_get_hook_config_partial_override():
    """Test getting configuration with partial overrides."""
    overrides = {
        "test_hook": {
            "enabled": False
            # timeout and order not specified
        }
    }
    
    config = HooksConfig(default_timeout=25.0, overrides=overrides)
    hook_config = config.get_hook_config("test_hook")
    
    assert hook_config["enabled"] is False
    assert hook_config["timeout"] == 25.0  # Default
    assert hook_config["order"] == {"before": [], "after": []}  # Default


def test_apply_to_hook_metadata_no_override():
    """Test applying config to metadata with no overrides."""
    config = HooksConfig(enabled=True, default_timeout=30.0)
    
    metadata = {
        "name": "test_hook",
        "type": "pre_llm_call",
        "enabled": True,
        "timeout": 15.0
    }
    
    result = config.apply_to_hook_metadata("test_hook", metadata)
    
    # Should not modify original metadata when no overrides
    assert result["enabled"] is True
    assert result["timeout"] == 15.0


def test_apply_to_hook_metadata_with_override():
    """Test applying config to metadata with overrides."""
    overrides = {
        "test_hook": {
            "enabled": False,
            "timeout": 5.0,
            "order": {"before": ["end"], "after": ["begin"]}
        }
    }
    
    config = HooksConfig(overrides=overrides)
    
    metadata = {
        "name": "test_hook",
        "type": "pre_llm_call",
        "enabled": True,
        "timeout": 15.0
    }
    
    result = config.apply_to_hook_metadata("test_hook", metadata)
    
    # Should apply overrides
    assert result["enabled"] is False
    assert result["timeout"] == 5.0
    assert result["order"] == {"before": ["end"], "after": ["begin"]}


def test_apply_to_hook_metadata_global_disabled():
    """Test that global enabled=False disables all hooks."""
    config = HooksConfig(enabled=False)
    
    metadata = {
        "name": "test_hook",
        "type": "pre_llm_call",
        "enabled": True,
        "timeout": 15.0
    }
    
    result = config.apply_to_hook_metadata("test_hook", metadata)
    
    # Global disable should override plugin setting
    assert result["enabled"] is False


def test_from_yaml_empty_file():
    """Test loading from an empty YAML file."""
    with tempfile.NamedTemporaryFile(mode='w', suffix='.yaml', delete=False) as f:
        f.write("")
        temp_path = f.name
    
    try:
        config = HooksConfig.from_yaml(temp_path)
        
        # Should use defaults
        assert config.enabled is True
        assert config.default_timeout == 30.0
        assert config.overrides == {}
    finally:
        Path(temp_path).unlink()


def test_from_yaml_with_hooks_section():
    """Test loading from YAML file with hooks section."""
    yaml_content = """
hooks:
  enabled: false
  default_timeout: 15.0
  overrides:
    test_hook:
      enabled: true
      timeout: 5.0
      order:
        before: ["end"]
        after: ["begin"]
"""
    
    with tempfile.NamedTemporaryFile(mode='w', suffix='.yaml', delete=False) as f:
        f.write(yaml_content)
        temp_path = f.name
    
    try:
        config = HooksConfig.from_yaml(temp_path)
        
        assert config.enabled is False
        assert config.default_timeout == 15.0
        assert "test_hook" in config.overrides
        assert config.overrides["test_hook"]["enabled"] is True
        assert config.overrides["test_hook"]["timeout"] == 5.0
    finally:
        Path(temp_path).unlink()


def test_from_yaml_missing_file():
    """Test loading from non-existent file uses defaults."""
    config = HooksConfig.from_yaml("nonexistent/path/config.yaml")
    
    # Should return defaults without crashing
    assert config.enabled is True
    assert config.default_timeout == 30.0


def test_load_hooks_config_default_path():
    """Test load_hooks_config with default path."""
    # Should not crash even if file doesn't exist
    config = load_hooks_config()
    
    assert isinstance(config, HooksConfig)


def test_validate_hook_references_valid():
    """Test validation with valid hook references."""
    overrides = {
        "hook1": {
            "order": {"before": ["hook2"], "after": ["begin"]}
        },
        "hook2": {
            "order": {"before": ["end"], "after": ["hook1"]}
        }
    }
    
    config = HooksConfig(overrides=overrides)
    available_hooks = ["hook1", "hook2"]
    
    errors = validate_hook_references(config, available_hooks)
    
    assert len(errors) == 0


def test_validate_hook_references_unknown_hook():
    """Test validation with unknown hook reference."""
    overrides = {
        "hook1": {
            "order": {"before": ["unknown_hook"], "after": ["begin"]}
        }
    }
    
    config = HooksConfig(overrides=overrides)
    available_hooks = ["hook1"]
    
    errors = validate_hook_references(config, available_hooks)
    
    assert len(errors) == 1
    assert "unknown_hook" in errors[0]
    assert "hook1" in errors[0]


def test_validate_hook_references_self_reference():
    """Test validation detects self-references."""
    overrides = {
        "hook1": {
            "order": {"before": ["hook1"], "after": ["begin"]}
        }
    }
    
    config = HooksConfig(overrides=overrides)
    available_hooks = ["hook1"]
    
    errors = validate_hook_references(config, available_hooks)
    
    assert len(errors) == 1
    assert "itself" in errors[0]


def test_validate_hook_references_multiple_errors():
    """Test validation with multiple errors."""
    overrides = {
        "hook1": {
            "order": {"before": ["unknown1"], "after": ["hook1"]}  # Unknown + self-ref
        },
        "hook2": {
            "order": {"before": ["unknown2"], "after": ["begin"]}  # Unknown
        }
    }
    
    config = HooksConfig(overrides=overrides)
    available_hooks = ["hook1", "hook2"]
    
    errors = validate_hook_references(config, available_hooks)
    
    # Should have 3 errors: 2 unknown refs + 1 self-ref
    assert len(errors) == 3


def test_validate_hook_references_virtual_nodes():
    """Test that virtual nodes (begin/end) are allowed."""
    overrides = {
        "hook1": {
            "order": {"before": ["end"], "after": ["begin"]}
        }
    }
    
    config = HooksConfig(overrides=overrides)
    available_hooks = ["hook1"]
    
    errors = validate_hook_references(config, available_hooks)
    
    # begin and end are virtual nodes, should be valid
    assert len(errors) == 0
