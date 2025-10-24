"""Integration tests for agent hook overrides with full plugin discovery.

Tests the complete flow:
1. Plugin registers hooks with full name (plugin.hook_name)
2. Agent config has overrides with full name
3. HookIntegrationManager correctly applies overrides
4. Registry executes hooks based on agent-specific configuration

This catches issues like:
- Hook registered without plugin prefix
- Filter not receiving full hook name
- Override matching failing due to name mismatch
"""
from __future__ import annotations

import pytest
from typing import Dict, Any
from unittest.mock import Mock

from agent_system.plugins.discovery import register_plugin_hooks
from agent_system.hooks import (
    PluginHook,
    HookContext,
    HookResult,
    HookType
)
from agent_system.config.models import HooksConfig
from agent_system.servers.agent.components.hook_integration import HookIntegrationManager


class TestHookPlugin(PluginHook):
    """Test plugin with hooks for integration testing."""
    
    def __init__(self, name: str, config: Dict[str, Any] = None):
        super().__init__(name, config or {})
        self.calls = []
    
    async def on_pre_llm_call(self, context: HookContext) -> HookResult:
        self.calls.append('pre_llm_call')
        return HookResult(success=True, modified=False, context=context)
    
    async def on_post_llm_call(self, context: HookContext) -> HookResult:
        self.calls.append('post_llm_call')
        return HookResult(success=True, modified=False, context=context)
    
    async def on_pre_tool_call(self, context: HookContext) -> HookResult:
        return HookResult(success=True, modified=False, context=context)
    
    async def on_post_tool_call(self, context: HookContext) -> HookResult:
        return HookResult(success=True, modified=False, context=context)
    
    async def on_format_output(self, context: HookContext) -> HookResult:
        return HookResult(success=True, modified=False, context=context)
    
    async def on_session_start(self, context: HookContext) -> HookResult:
        return HookResult(success=True, modified=False, context=context)
    
    async def on_session_end(self, context: HookContext) -> HookResult:
        return HookResult(success=True, modified=False, context=context)


@pytest.fixture
def clean_registry():
    """Create a fresh hook registry for each test."""
    from agent_system.hooks.registry import HookRegistry
    registry = HookRegistry(default_timeout=30.0)
    yield registry


@pytest.mark.asyncio
async def test_hook_registered_with_full_name(clean_registry):
    """Test that hooks are registered with plugin.hook_name format."""
    plugin = TestHookPlugin("test_plugin", {})
    metadata = {
        'hooks': [
            {
                'name': 'test_hook',
                'type': 'pre_llm_call',
                'enabled': True
            }
        ]
    }
    
    registered = await register_plugin_hooks("test_plugin", plugin, metadata, clean_registry)
    
    # CRITICAL: Hook should be registered with FULL name including plugin prefix
    assert len(registered) == 1
    assert 'test_plugin.test_hook' in registered
    
    # Verify in registry
    hook_info = clean_registry.get_hook_info('test_plugin.test_hook')
    assert hook_info is not None


@pytest.mark.asyncio
async def test_agent_override_enables_globally_disabled_hook(clean_registry):
    """Test that agent can enable a globally disabled hook via override."""
    plugin = TestHookPlugin("todo_management", {})
    metadata = {
        'hooks': [
            {
                'name': 'inject_todo_tasks',
                'type': 'pre_llm_call',
                'enabled': False  # Globally disabled
            }
        ]
    }
    
    # Register with global disabled state
    registered = await register_plugin_hooks("todo_management", plugin, metadata, clean_registry)
    assert 'todo_management.inject_todo_tasks' in registered
    
    # Create mock agent with override enabling this hook
    agent = Mock()
    agent.name = 'meta_agent'
    agent.agent_config = Mock()
    agent.agent_config.hooks = HooksConfig(
        enabled=True,
        overrides={
            'todo_management.inject_todo_tasks': {'enabled': True}
        }
    )
    
    # Create hook integration manager
    manager = HookIntegrationManager(agent)
    
    # Test with new signature (default_enabled parameter)
    result = manager.is_hook_enabled('todo_management.inject_todo_tasks', default_enabled=False)
    
    # Should be enabled via override despite global disabled state
    assert result is True


@pytest.mark.asyncio
async def test_agent_override_disables_globally_enabled_hook(clean_registry):
    """Test that agent can disable a globally enabled hook via override."""
    plugin = TestHookPlugin("markdown_formatter", {})
    metadata = {
        'hooks': [
            {
                'name': 'format_markdown_output',
                'type': 'format_output',
                'enabled': True  # Globally enabled
            }
        ]
    }
    
    registered = await register_plugin_hooks("markdown_formatter", plugin, metadata, clean_registry)
    assert 'markdown_formatter.format_markdown_output' in registered
    
    # Create mock agent with override disabling this hook
    agent = Mock()
    agent.name = 'simple_agent'
    agent.agent_config = Mock()
    agent.agent_config.hooks = HooksConfig(
        enabled=True,
        overrides={
            'markdown_formatter.format_markdown_output': {'enabled': False}
        }
    )
    
    manager = HookIntegrationManager(agent)
    
    # Test with new signature
    result = manager.is_hook_enabled('markdown_formatter.format_markdown_output', default_enabled=True)
    
    # Should be disabled via override despite global enabled state
    assert result is False


@pytest.mark.asyncio
async def test_agent_without_override_uses_global_state(clean_registry):
    """Test that agent without override uses global enabled state."""
    plugin = TestHookPlugin("test_plugin", {})
    metadata = {
        'hooks': [
            {
                'name': 'some_hook',
                'type': 'pre_llm_call',
                'enabled': False
            }
        ]
    }
    
    registered = await register_plugin_hooks("test_plugin", plugin, metadata, clean_registry)
    assert 'test_plugin.some_hook' in registered
    
    # Agent without override for this hook
    agent = Mock()
    agent.name = 'default_agent'
    agent.agent_config = Mock()
    agent.agent_config.hooks = HooksConfig(
        enabled=True,
        overrides={}  # No overrides
    )
    
    manager = HookIntegrationManager(agent)
    
    # Should use global state (disabled)
    result = manager.is_hook_enabled('test_plugin.some_hook', default_enabled=False)
    assert result is False


@pytest.mark.asyncio
async def test_hook_execution_with_agent_filter(clean_registry):
    """Test that hooks execute correctly with agent-specific filter."""
    plugin = TestHookPlugin("test_plugin", {})
    metadata = {
        'hooks': [
            {
                'name': 'filterable_hook',
                'type': 'pre_llm_call',
                'enabled': False  # Disabled by default
            }
        ]
    }
    
    await register_plugin_hooks("test_plugin", plugin, metadata, clean_registry)
    
    # Agent 1: Enables hook via override
    agent_with_hook = Mock()
    agent_with_hook.name = 'agent_enabled'
    agent_with_hook.agent_config = Mock()
    agent_with_hook.agent_config.hooks = HooksConfig(
        enabled=True,
        overrides={
            'test_plugin.filterable_hook': {'enabled': True}
        }
    )
    
    manager_enabled = HookIntegrationManager(agent_with_hook)
    
    # Execute hooks with filter
    context = HookContext(
        hook_type=HookType.PRE_LLM_CALL,
        request_id="test_req",
        session_id="test_session",
        agent=agent_with_hook,
        messages=[],
        step=1
    )
    
    await clean_registry.execute_hooks(
        HookType.PRE_LLM_CALL,
        context,
        hook_filter=manager_enabled.is_hook_enabled
    )
    
    # Hook should have been called
    assert 'pre_llm_call' in plugin.calls
    
    # Agent 2: Does not enable hook (uses global disabled)
    plugin.calls.clear()
    
    agent_without_hook = Mock()
    agent_without_hook.name = 'agent_disabled'
    agent_without_hook.agent_config = Mock()
    agent_without_hook.agent_config.hooks = HooksConfig(
        enabled=True,
        overrides={}  # No override for this hook
    )
    
    manager_disabled = HookIntegrationManager(agent_without_hook)
    
    context2 = HookContext(
        hook_type=HookType.PRE_LLM_CALL,
        request_id="test_req2",
        session_id="test_session2",
        agent=agent_without_hook,
        messages=[],
        step=1
    )
    
    await clean_registry.execute_hooks(
        HookType.PRE_LLM_CALL,
        context2,
        hook_filter=manager_disabled.is_hook_enabled
    )
    
    # Hook should NOT have been called
    assert 'pre_llm_call' not in plugin.calls


@pytest.mark.asyncio
async def test_multiple_plugins_with_same_hook_name(clean_registry):
    """Test that different plugins can have hooks with same name (namespaced)."""
    plugin_a = TestHookPlugin("plugin_a", {})
    plugin_b = TestHookPlugin("plugin_b", {})
    
    metadata_a = {
        'hooks': [
            {'name': 'my_hook', 'type': 'pre_llm_call', 'enabled': True}
        ]
    }
    metadata_b = {
        'hooks': [
            {'name': 'my_hook', 'type': 'pre_llm_call', 'enabled': True}
        ]
    }
    
    # Both should register successfully with full names
    registered_a = await register_plugin_hooks("plugin_a", plugin_a, metadata_a, clean_registry)
    registered_b = await register_plugin_hooks("plugin_b", plugin_b, metadata_b, clean_registry)
    
    assert 'plugin_a.my_hook' in registered_a
    assert 'plugin_b.my_hook' in registered_b
    
    # Both should exist in registry
    assert clean_registry.get_hook_info('plugin_a.my_hook') is not None
    assert clean_registry.get_hook_info('plugin_b.my_hook') is not None


@pytest.mark.asyncio
async def test_hook_name_format_consistency():
    """Test that hook names follow consistent plugin.hook_name format everywhere."""
    # This test documents the expected format
    
    # 1. In plugin metadata (schema.yaml): just the hook name
    schema_hook_name = 'inject_todo_tasks'
    
    # 2. In registry: full name with plugin prefix
    registry_hook_name = f'todo_management.{schema_hook_name}'
    assert registry_hook_name == 'todo_management.inject_todo_tasks'
    
    # 3. In agent config overrides: full name
    override_key = 'todo_management.inject_todo_tasks'
    
    # 4. In hook filter: receives full name from registry
    filter_receives = registry_hook_name
    
    # All must match for overrides to work
    assert registry_hook_name == override_key == filter_receives
