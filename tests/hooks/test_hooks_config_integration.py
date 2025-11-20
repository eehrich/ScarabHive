"""Tests for global hooks configuration integration with plugin discovery."""
from __future__ import annotations

import pytest
import tempfile
from pathlib import Path

from agent_system.hooks import HooksConfig
from agent_system.hooks.registry import HookRegistry
from agent_system.plugins.discovery import register_plugin_hooks
from tests.plugins.test_plugin_hook_discovery import MockHookPlugin


@pytest.fixture
def clean_registry():
    """Create a fresh hook registry for each test."""
    registry = HookRegistry(default_timeout=30.0)
    yield registry


@pytest.mark.asyncio
async def test_global_config_disables_hook(clean_registry):
    """Test that global hooks configuration can disable a plugin hook."""
    # Create hooks config with global disable
    hooks_config = HooksConfig(enabled=False)
    
    plugin = MockHookPlugin("test_plugin")
    metadata = {
        'hooks': [
            {
                'name': 'test_hook',
                'type': 'pre_llm_call',
                'enabled': True  # Plugin enables it, but global config disables
            }
        ]
    }
    
    await register_plugin_hooks("test_plugin", plugin, metadata, clean_registry, hooks_config)
    
    hook_info = clean_registry.get_hook_info('test_plugin.test_hook')  # Use full hook name
    assert hook_info is not None
    assert hook_info['enabled'] is False  # Global config overrides


@pytest.mark.asyncio
async def test_global_config_override_timeout(clean_registry):
    """Test that global hooks configuration can override timeout."""
    # Create hooks config with timeout override for test_plugin.test_hook
    hooks_config = HooksConfig(
        overrides={
            "test_plugin.test_hook": {
                "timeout": 5.0
            }
        }
    )
    
    plugin = MockHookPlugin("test_plugin")
    metadata = {
        'hooks': [
            {
                'name': 'test_hook',
                'type': 'pre_llm_call',
                'enabled': True,
                'timeout': 30.0  # Plugin default
            }
        ]
    }
    
    await register_plugin_hooks("test_plugin", plugin, metadata, clean_registry, hooks_config)
    
    hook_info = clean_registry.get_hook_info('test_plugin.test_hook')
    assert hook_info is not None
    assert hook_info['timeout'] == 5.0  # Global override applied


@pytest.mark.asyncio
async def test_global_config_override_order(clean_registry):
    """Test that global hooks configuration can override hook ordering."""
    # Create hooks config with order override
    hooks_config = HooksConfig(
        overrides={
            "test_plugin.test_hook": {
                "order": {
                    "before": ["end"],
                    "after": ["begin"]
                }
            }
        }
    )
    
    plugin = MockHookPlugin("test_plugin")
    metadata = {
        'hooks': [
            {
                'name': 'test_hook',
                'type': 'pre_llm_call',
                'enabled': True,
                'order': {
                    'before': [],
                    'after': []
                }
            }
        ]
    }
    
    await register_plugin_hooks("test_plugin", plugin, metadata, clean_registry, hooks_config)
    
    hook_info = clean_registry.get_hook_info('test_plugin.test_hook')
    assert hook_info is not None
    assert hook_info['order']['before'] == ['end']
    assert hook_info['order']['after'] == ['begin']


@pytest.mark.asyncio
async def test_global_config_selective_override(clean_registry):
    """Test that global config only overrides specified fields."""
    # Create hooks config with only enabled override
    hooks_config = HooksConfig(
        overrides={
            "test_plugin.test_hook": {
                "enabled": False
                # timeout and order not specified, should use plugin values
            }
        }
    )
    
    plugin = MockHookPlugin("test_plugin")
    metadata = {
        'hooks': [
            {
                'name': 'test_hook',
                'type': 'pre_llm_call',
                'enabled': True,
                'timeout': 15.0,
                'order': {
                    'before': ['end'],
                    'after': ['begin']
                }
            }
        ]
    }
    
    await register_plugin_hooks("test_plugin", plugin, metadata, clean_registry, hooks_config)
    
    hook_info = clean_registry.get_hook_info('test_plugin.test_hook')
    assert hook_info is not None
    assert hook_info['enabled'] is False  # Overridden
    assert hook_info['timeout'] == 15.0   # From plugin
    assert hook_info['order']['before'] == ['end']  # From plugin
    assert hook_info['order']['after'] == ['begin']  # From plugin


@pytest.mark.asyncio
async def test_global_config_no_override_uses_plugin_defaults(clean_registry):
    """Test that without global config override, plugin defaults are used."""
    # Create hooks config with no overrides for this plugin
    hooks_config = HooksConfig(
        overrides={
            "other_plugin.other_hook": {
                "enabled": False
            }
        }
    )
    
    plugin = MockHookPlugin("test_plugin")
    metadata = {
        'hooks': [
            {
                'name': 'test_hook',
                'type': 'pre_llm_call',
                'enabled': True,
                'timeout': 20.0
            }
        ]
    }
    
    await register_plugin_hooks("test_plugin", plugin, metadata, clean_registry, hooks_config)
    
    hook_info = clean_registry.get_hook_info('test_plugin.test_hook')
    assert hook_info is not None
    assert hook_info['enabled'] is True   # Plugin default
    assert hook_info['timeout'] == 20.0   # Plugin default


@pytest.mark.asyncio
async def test_global_config_from_yaml_integration(clean_registry):
    """Test loading hooks config from YAML and applying to plugins."""
    yaml_content = """
hooks:
  enabled: true
  default_timeout: 25.0
  overrides:
    test_plugin.test_hook:
      enabled: false
      timeout: 10.0
      order:
        before: ["end"]
        after: ["begin"]
"""
    
    with tempfile.NamedTemporaryFile(mode='w', suffix='.yaml', delete=False) as f:
        f.write(yaml_content)
        temp_path = f.name
    
    try:
        hooks_config = HooksConfig.from_yaml(temp_path)
        
        plugin = MockHookPlugin("test_plugin")
        metadata = {
            'hooks': [
                {
                    'name': 'test_hook',
                    'type': 'pre_llm_call',
                    'enabled': True,
                    'timeout': 30.0
                }
            ]
        }
        
        await register_plugin_hooks("test_plugin", plugin, metadata, clean_registry, hooks_config)
        
        hook_info = clean_registry.get_hook_info('test_plugin.test_hook')
        assert hook_info is not None
        assert hook_info['enabled'] is False  # From YAML override
        assert hook_info['timeout'] == 10.0   # From YAML override
        assert hook_info['order']['before'] == ['end']
        assert hook_info['order']['after'] == ['begin']
        
    finally:
        Path(temp_path).unlink()


@pytest.mark.asyncio
async def test_multiple_hooks_different_overrides(clean_registry):
    """Test that different hooks can have different global overrides."""
    hooks_config = HooksConfig(
        overrides={
            "test_plugin.hook1": {
                "enabled": False
            },
            "test_plugin.hook2": {
                "timeout": 5.0
            }
        }
    )
    
    plugin = MockHookPlugin("test_plugin")
    metadata = {
        'hooks': [
            {
                'name': 'hook1',
                'type': 'pre_llm_call',
                'enabled': True,
                'timeout': 30.0
            },
            {
                'name': 'hook2',
                'type': 'post_llm_call',
                'enabled': True,
                'timeout': 30.0
            }
        ]
    }
    
    await register_plugin_hooks("test_plugin", plugin, metadata, clean_registry, hooks_config)
    
    hook1_info = clean_registry.get_hook_info('test_plugin.hook1')  # Use full hook name
    hook2_info = clean_registry.get_hook_info('test_plugin.hook2')  # Use full hook name
    
    assert hook1_info['enabled'] is False  # Disabled by global config
    assert hook1_info['timeout'] == 30.0   # Plugin default
    
    assert hook2_info['enabled'] is True   # Plugin default
    assert hook2_info['timeout'] == 5.0    # Overridden by global config
