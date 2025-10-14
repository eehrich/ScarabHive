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
    get_hook_registry,
    PluginHook,
    HookContext,
    HookResult,
    HookType
)
from agent_system.mcp.base import MCPServer


class MockHookPlugin(MCPServer, PluginHook):
    """Mock plugin implementing PluginHook interface."""
    
    def __init__(self, name: str, system_config=None, mcp_config=None):
        # Create mock configs if not provided
        if system_config is None:
            from agent_system.config.models import AgentSystemConfig
            system_config = AgentSystemConfig()
        if mcp_config is None:
            from agent_system.config.models import MCPConfig
            mcp_config = MCPConfig()
        
        super().__init__(name, system_config, mcp_config)
        self.pre_llm_calls = 0
        self.post_llm_calls = 0
    
    async def list_tools(self):
        return []
    
    async def call_tool(self, tool_name: str, arguments: Dict[str, Any] | None = None):
        raise ValueError(f"Tool '{tool_name}' not found")
    
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


class MockNonHookPlugin(MCPServer):
    """Mock plugin NOT implementing PluginHook interface."""
    
    def __init__(self, name: str, system_config=None, mcp_config=None):
        # Create mock configs if not provided
        if system_config is None:
            from agent_system.config.models import AgentSystemConfig
            system_config = AgentSystemConfig()
        if mcp_config is None:
            from agent_system.config.models import MCPConfig
            mcp_config = MCPConfig()
        
        super().__init__(name, system_config, mcp_config)
    
    async def list_tools(self):
        return []
    
    async def call_tool(self, tool_name: str, arguments: Dict[str, Any] | None = None):
        raise ValueError(f"Tool '{tool_name}' not found")


@pytest.fixture
def clean_registry():
    """Create a fresh hook registry for each test."""
    from agent_system.hooks.registry import HookRegistry
    registry = HookRegistry(default_timeout=30.0)
    yield registry
    # No cleanup needed since each test gets a fresh instance



def test_register_single_hook(clean_registry):
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
    
    registered = register_plugin_hooks("test_plugin", plugin, metadata, clean_registry)
    
    assert len(registered) == 1
    assert 'test_hook' in registered
    
    # Verify hook is registered in registry
    hook_info = clean_registry.get_hook_info('test_hook')
    assert hook_info is not None
    assert hook_info['enabled'] is True


def test_register_multiple_hooks(clean_registry):
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
                'order': {'after': ['hook_pre']}
            }
        ]
    }
    
    registered = register_plugin_hooks("test_plugin", plugin, metadata, clean_registry)
    
    assert len(registered) == 2
    assert 'hook_pre' in registered
    assert 'hook_post' in registered


def test_plugin_without_plugin_hook_interface(clean_registry):
    """Test error when plugin declares hooks but doesn't implement PluginHook."""
    plugin = MockNonHookPlugin("bad_plugin", {})
    metadata = {
        'hooks': [
            {
                'name': 'bad_hook',
                'type': 'pre_llm_call'
            }
        ]
    }
    
    with pytest.raises(TypeError, match="does not implement PluginHook interface"):
        register_plugin_hooks("bad_plugin", plugin, metadata, clean_registry)


def test_plugin_without_hooks_metadata(clean_registry):
    """Test plugin without hooks section in metadata."""
    plugin = MockHookPlugin("no_hooks_plugin", {})
    metadata = {}  # No hooks section
    
    registered = register_plugin_hooks("no_hooks_plugin", plugin, metadata, clean_registry)
    
    assert len(registered) == 0


def test_plugin_without_metadata(clean_registry):
    """Test plugin without any metadata."""
    plugin = MockHookPlugin("no_metadata_plugin", {})
    
    registered = register_plugin_hooks("no_metadata_plugin", plugin, None, clean_registry)
    
    assert len(registered) == 0


def test_hook_with_invalid_type(clean_registry):
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
    registered = register_plugin_hooks("test_plugin", plugin, metadata, clean_registry)
    
    assert len(registered) == 0


def test_hook_with_missing_name(clean_registry):
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
    
    registered = register_plugin_hooks("test_plugin", plugin, metadata, clean_registry)
    
    assert len(registered) == 0


def test_hook_with_missing_type(clean_registry):
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
    
    registered = register_plugin_hooks("test_plugin", plugin, metadata, clean_registry)
    
    assert len(registered) == 0


def test_hook_with_self_reference(clean_registry):
    """Test hook with self-reference in ordering is cleaned up."""
    plugin = MockHookPlugin("test_plugin", {})
    metadata = {
        'hooks': [
            {
                'name': 'self_ref_hook',
                'type': 'pre_llm_call',
                'enabled': True,
                'order': {
                    'before': ['self_ref_hook'],  # Self-reference
                    'after': ['begin']
                }
            }
        ]
    }
    
    # Should remove self-reference and register successfully
    registered = register_plugin_hooks("test_plugin", plugin, metadata, clean_registry)
    
    assert len(registered) == 1
    assert 'self_ref_hook' in registered


def test_hook_with_custom_timeout(clean_registry):
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
    
    registered = register_plugin_hooks("test_plugin", plugin, metadata, clean_registry)
    
    assert len(registered) == 1
    hook_info = clean_registry.get_hook_info('timeout_hook')
    assert hook_info['timeout'] == 5.0


def test_hook_disabled_by_default(clean_registry):
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
    
    registered = register_plugin_hooks("test_plugin", plugin, metadata, clean_registry)
    
    assert len(registered) == 1
    hook_info = clean_registry.get_hook_info('disabled_hook')
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
    
    register_plugin_hooks("test_plugin", plugin, metadata, clean_registry)
    
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


def test_hook_metadata_attached(clean_registry):
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
    
    register_plugin_hooks("test_plugin", plugin, metadata, clean_registry)
    
    hook_info = clean_registry.get_hook_info('meta_hook')
    hook_meta = hook_info['metadata']
    assert hook_meta['plugin'] == 'test_plugin'
    assert hook_meta['description'] == 'Test description'
    assert hook_meta['source'] == 'plugin_discovery'


def test_hook_with_complex_ordering(clean_registry):
    """Test hook with both before and after ordering."""
    plugin = MockHookPlugin("test_plugin", {})
    metadata = {
        'hooks': [
            {
                'name': 'ordered_hook',
                'type': 'pre_llm_call',
                'enabled': True,
                'order': {
                    'before': ['end', 'other_hook'],
                    'after': ['begin', 'first_hook']
                }
            }
        ]
    }
    
    registered = register_plugin_hooks("test_plugin", plugin, metadata, clean_registry)
    
    assert len(registered) == 1
    hook_info = clean_registry.get_hook_info('ordered_hook')
    order = hook_info['order']
    assert 'end' in order['before']
    assert 'other_hook' in order['before']
    assert 'begin' in order['after']
    assert 'first_hook' in order['after']


def test_multiple_hook_types_same_plugin(clean_registry):
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
    
    registered = register_plugin_hooks("multi_hook_plugin", plugin, metadata, clean_registry)
    
    assert len(registered) == 3
    assert clean_registry.get_hook_info('pre_hook') is not None
    assert clean_registry.get_hook_info('post_hook') is not None
    assert clean_registry.get_hook_info('format_hook') is not None
