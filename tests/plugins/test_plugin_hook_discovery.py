"""Tests for plugin hook discovery and registration.

Tests the integration between the plugin system and hook system:
- Parsing hooks from plugin.yaml
- Automatic hook registration during plugin loading
- Hook plugin validation
- Error handling for invalid hook configurations
"""
from __future__ import annotations

import pytest
from typing import Dict, Any

from agent_system.plugins.discovery import register_plugin_hooks
from agent_system.hooks import (
    PluginHook,
    HookContext,
    HookResult,
    HookType
)


class MockHookPlugin(PluginHook):
    """Mock plugin implementing ONLY PluginHook interface (no tools)."""
    
    def __init__(self, name: str, config: Dict[str, Any] = None):
        super().__init__(name, config or {})
        self.pre_llm_calls = 0
        self.post_llm_calls = 0
        # Hook attributes expected by HookRegistry
        self.enabled = True
        self.timeout = 30.0
    
    async def on_pre_llm_call(self, context: HookContext) -> HookResult:
        self.pre_llm_calls += 1
        return HookResult(success=True, modified=False, context=context)
    
    async def on_post_llm_call(self, context: HookContext) -> HookResult:
        self.post_llm_calls += 1
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


class MockNonHookPlugin:
    """Mock plugin NOT implementing PluginHook interface."""
    
    def __init__(self, name: str):
        self.name = name


@pytest.fixture
def clean_registry():
    """Create a fresh hook registry for each test."""
    from agent_system.hooks.registry import HookRegistry
    registry = HookRegistry(default_timeout=30.0)
    yield registry
    # No cleanup needed since each test gets a fresh instance



@pytest.mark.asyncio
async def test_register_single_hook(clean_registry):
    """Test registering a single hook from plugin metadata."""
    plugin = MockHookPlugin("test_plugin", {})
    metadata = {
        'hooks': [
            {
                'name': 'test_hook',
                'type': 'pre_llm_call',
                'enabled': True,
                'description': 'Test hook',
                'order': {'after': ['begin']}
            }
        ]
    }
    
    registered = await register_plugin_hooks("test_plugin", plugin, metadata, clean_registry)
    
    assert len(registered) == 1
    # Hook should be registered with full plugin.hook_name format
    assert 'test_plugin.test_hook' in registered
    
    # Verify hook is registered in registry with full name
    hook_info = clean_registry.get_hook_info('test_plugin.test_hook')
    assert hook_info is not None
    assert hook_info['enabled'] is True


@pytest.mark.asyncio
async def test_register_multiple_hooks(clean_registry):
    """Test registering multiple hooks from same plugin."""
    plugin = MockHookPlugin("test_plugin", {})
    metadata = {
        'hooks': [
            {
                'name': 'hook_pre',
                'type': 'pre_llm_call',
                'enabled': True,
            },
            {
                'name': 'hook_post',
                'type': 'post_llm_call',
                'enabled': True,
                'order': {'after': ['test_plugin.hook_pre']}  # Full name in ordering
            }
        ]
    }
    
    registered = await register_plugin_hooks("test_plugin", plugin, metadata, clean_registry)
    
    assert len(registered) == 2
    assert 'test_plugin.hook_pre' in registered
    assert 'test_plugin.hook_post' in registered


@pytest.mark.asyncio
async def test_plugin_without_plugin_hook_interface(clean_registry):
    """Test error when plugin declares hooks but doesn't implement PluginHook."""
    plugin = MockNonHookPlugin("bad_plugin")
    metadata = {
        'hooks': [
            {
                'name': 'bad_hook',
                'type': 'pre_llm_call'
            }
        ]
    }
    
    with pytest.raises(TypeError, match="does not implement PluginHook interface"):
        await register_plugin_hooks("bad_plugin", plugin, metadata, clean_registry)



@pytest.mark.asyncio
async def test_plugin_without_hooks_metadata(clean_registry):
    """Test plugin without hooks section in metadata."""
    plugin = MockHookPlugin("no_hooks_plugin", {})
    metadata = {}  # No hooks section
    
    registered = await register_plugin_hooks("no_hooks_plugin", plugin, metadata, clean_registry)
    
    assert len(registered) == 0


@pytest.mark.asyncio
async def test_plugin_without_metadata(clean_registry):
    """Test plugin without any metadata."""
    plugin = MockHookPlugin("no_metadata_plugin", {})
    
    registered = await register_plugin_hooks("no_metadata_plugin", plugin, None, clean_registry)
    
    assert len(registered) == 0


@pytest.mark.asyncio
async def test_hook_with_invalid_type(clean_registry):
    """Test hook with invalid type is skipped."""
    plugin = MockHookPlugin("test_plugin", {})
    metadata = {
        'hooks': [
            {
                'name': 'bad_type_hook',
                'type': 'invalid_hook_type',
                'enabled': True
            }
        ]
    }
    
    # Should log warning and skip the hook
    registered = await register_plugin_hooks("test_plugin", plugin, metadata, clean_registry)
    
    assert len(registered) == 0


@pytest.mark.asyncio
async def test_hook_with_missing_name(clean_registry):
    """Test hook with missing name is skipped."""
    plugin = MockHookPlugin("test_plugin", {})
    metadata = {
        'hooks': [
            {
                # Missing 'name'
                'type': 'pre_llm_call',
                'enabled': True
            }
        ]
    }
    
    registered = await register_plugin_hooks("test_plugin", plugin, metadata, clean_registry)
    
    assert len(registered) == 0


@pytest.mark.asyncio
async def test_hook_with_missing_type(clean_registry):
    """Test hook with missing type is skipped."""
    plugin = MockHookPlugin("test_plugin", {})
    metadata = {
        'hooks': [
            {
                'name': 'no_type_hook',
                # Missing 'type'
                'enabled': True
            }
        ]
    }
    
    registered = await register_plugin_hooks("test_plugin", plugin, metadata, clean_registry)
    
    assert len(registered) == 0


@pytest.mark.asyncio
async def test_hook_with_self_reference(clean_registry):
    """Test hook with self-reference in ordering is cleaned up."""
    plugin = MockHookPlugin("test_plugin", {})
    metadata = {
        'hooks': [
            {
                'name': 'self_ref_hook',
                'type': 'pre_llm_call',
                'enabled': True,
                'order': {
                    'before': ['test_plugin.self_ref_hook'],  # Self-reference with full name
                    'after': ['begin']
                }
            }
        ]
    }
    
    # Should remove self-reference and register successfully
    registered = await register_plugin_hooks("test_plugin", plugin, metadata, clean_registry)
    
    assert len(registered) == 1
    assert 'test_plugin.self_ref_hook' in registered


@pytest.mark.asyncio
async def test_hook_with_custom_timeout(clean_registry):
    """Test hook with custom timeout value."""
    plugin = MockHookPlugin("test_plugin", {})
    metadata = {
        'hooks': [
            {
                'name': 'timeout_hook',
                'type': 'pre_llm_call',
                'enabled': True,
                'timeout': 5.0
            }
        ]
    }
    
    registered = await register_plugin_hooks("test_plugin", plugin, metadata, clean_registry)
    
    assert len(registered) == 1
    hook_info = clean_registry.get_hook_info('test_plugin.timeout_hook')
    assert hook_info['timeout'] == 5.0


@pytest.mark.asyncio
async def test_hook_disabled_by_default(clean_registry):
    """Test hook can be disabled in metadata."""
    plugin = MockHookPlugin("test_plugin", {})
    metadata = {
        'hooks': [
            {
                'name': 'disabled_hook',
                'type': 'pre_llm_call',
                'enabled': False
            }
        ]
    }
    
    registered = await register_plugin_hooks("test_plugin", plugin, metadata, clean_registry)
    
    assert len(registered) == 1
    hook_info = clean_registry.get_hook_info('test_plugin.disabled_hook')
    assert hook_info['enabled'] is False


@pytest.mark.asyncio
async def test_registered_hook_execution(clean_registry):
    """Test that registered hooks can be executed."""
    plugin = MockHookPlugin("test_plugin", {})
    metadata = {
        'hooks': [
            {
                'name': 'exec_test_hook',
                'type': 'pre_llm_call',
                'enabled': True
            }
        ]
    }
    
    await register_plugin_hooks("test_plugin", plugin, metadata, clean_registry)
    
    # Create test context
    context = HookContext(
        hook_type=HookType.PRE_LLM_CALL,
        request_id="test_req",
        session_id="test_session",
        agent=None,
        messages=[],
        step=1
    )
    
    # Execute hooks
    modified_context = await clean_registry.execute_hooks(HookType.PRE_LLM_CALL, context)
    
    # Verify hook was called
    assert plugin.pre_llm_calls == 1
    assert modified_context is not None


@pytest.mark.asyncio
async def test_hook_metadata_attached(clean_registry):
    """Test that hook metadata is properly attached."""
    plugin = MockHookPlugin("test_plugin", {})
    metadata = {
        'hooks': [
            {
                'name': 'meta_hook',
                'type': 'pre_llm_call',
                'enabled': True,
                'description': 'Test description'
            }
        ]
    }
    
    await register_plugin_hooks("test_plugin", plugin, metadata, clean_registry)
    
    hook_info = clean_registry.get_hook_info('test_plugin.meta_hook')
    hook_meta = hook_info['metadata']
    assert hook_meta['plugin'] == 'test_plugin'
    assert hook_meta['description'] == 'Test description'
    assert hook_meta['source'] == 'plugin_discovery'


@pytest.mark.asyncio
async def test_hook_with_complex_ordering(clean_registry):
    """Test hook with both before and after ordering."""
    plugin = MockHookPlugin("test_plugin", {})
    metadata = {
        'hooks': [
            {
                'name': 'ordered_hook',
                'type': 'pre_llm_call',
                'enabled': True,
                'order': {
                    'before': ['end', 'other_plugin.other_hook'],
                    'after': ['begin', 'first_plugin.first_hook']
                }
            }
        ]
    }
    
    registered = await register_plugin_hooks("test_plugin", plugin, metadata, clean_registry)
    
    assert len(registered) == 1
    hook_info = clean_registry.get_hook_info('test_plugin.ordered_hook')
    order = hook_info['order']
    assert 'end' in order['before']
    assert 'other_plugin.other_hook' in order['before']
    assert 'begin' in order['after']
    assert 'first_plugin.first_hook' in order['after']


@pytest.mark.asyncio
async def test_multiple_hook_types_same_plugin(clean_registry):
    """Test plugin registering hooks for different lifecycle points."""
    plugin = MockHookPlugin("multi_hook_plugin", {})
    metadata = {
        'hooks': [
            {
                'name': 'pre_hook',
                'type': 'pre_llm_call',
                'enabled': True
            },
            {
                'name': 'post_hook',
                'type': 'post_llm_call',
                'enabled': True
            },
            {
                'name': 'format_hook',
                'type': 'format_output',
                'enabled': True
            }
        ]
    }
    
    registered = await register_plugin_hooks("multi_hook_plugin", plugin, metadata, clean_registry)
    
    assert len(registered) == 3
    assert clean_registry.get_hook_info('multi_hook_plugin.pre_hook') is not None
    assert clean_registry.get_hook_info('multi_hook_plugin.post_hook') is not None
    assert clean_registry.get_hook_info('multi_hook_plugin.format_hook') is not None


@pytest.mark.asyncio
async def test_instance_hook_config_disables_the_registration_default(clean_registry):
    """A server instance's hook_config.enabled=False must win over the schema.

    This is the "one instance on, second instance off" case: the schema says
    enabled, the instance (e.g. writer_context_summarizer) says off-by-default.
    Until 2026-09-02 the instance value was written into configs but read by
    nobody -- every agent without an override ran BOTH summarizer instances.
    """
    plugin = MockHookPlugin("writer_summarizer", {})
    metadata = {'hooks': [{'name': 'summarize', 'type': 'pre_llm_call',
                           'enabled': True}]}

    registered = await register_plugin_hooks(
        "writer_summarizer", plugin, metadata, clean_registry,
        instance_hook_config={'enabled': False})

    assert registered == ['writer_summarizer.summarize']
    assert clean_registry.get_hook_info('writer_summarizer.summarize')['enabled'] is False


@pytest.mark.asyncio
async def test_global_override_still_beats_the_instance_default(clean_registry):
    """Operator config (hooks.overrides) must outrank the instance default."""
    from agent_system.hooks.config import HooksConfig

    plugin = MockHookPlugin("p", {})
    metadata = {'hooks': [{'name': 'h', 'type': 'pre_llm_call', 'enabled': True}]}
    hooks_config = HooksConfig(overrides={'p.h': {'enabled': True}})

    await register_plugin_hooks("p", plugin, metadata, clean_registry,
                                hooks_config=hooks_config,
                                instance_hook_config={'enabled': False})

    assert clean_registry.get_hook_info('p.h')['enabled'] is True


@pytest.mark.asyncio
async def test_instance_enabled_true_does_not_lift_a_schema_off_switch(clean_registry):
    """Lower-only: the shipped sub_agent_manager config says enabled:true
    against a schema that deliberately registers the hook disabled. Honouring
    the True would flip that hook on for every agent -- so True is a no-op."""
    plugin = MockHookPlugin("sam", {})
    metadata = {'hooks': [{'name': 'inject', 'type': 'pre_llm_call',
                           'enabled': False}]}

    await register_plugin_hooks("sam", plugin, metadata, clean_registry,
                                instance_hook_config={'enabled': True})

    assert clean_registry.get_hook_info('sam.inject')['enabled'] is False


@pytest.mark.asyncio
async def test_instance_hook_config_without_enabled_changes_nothing(clean_registry):
    """hook_config may carry other keys; only 'enabled' speaks here."""
    plugin = MockHookPlugin("p", {})
    metadata = {'hooks': [{'name': 'h', 'type': 'pre_llm_call', 'enabled': False}]}

    await register_plugin_hooks("p", plugin, metadata, clean_registry,
                                instance_hook_config={'other_setting': 1})

    assert clean_registry.get_hook_info('p.h')['enabled'] is False

