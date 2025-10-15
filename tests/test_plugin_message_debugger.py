"""Tests for message_debugger plugin.

Tests the message capture, storage, API endpoints, and web UI functionality.
"""
from __future__ import annotations

import pytest
from pathlib import Path
from unittest.mock import Mock

from agent_system.hooks import HookContext
from agent_system.llm.models import ChatMessage
from agent_system.config.models import AgentSystemConfig, MCPConfig


@pytest.fixture
def plugin_dir():
    """Get path to message_debugger plugin directory."""
    return Path(__file__).parent.parent / "src" / "plugins" / "message_debugger"


@pytest.fixture
def message_history():
    """Create empty message history list."""
    return []


@pytest.fixture
def hooks_plugin(plugin_dir, message_history):
    """Create MessageDebuggerPlugin instance."""
    from plugins.message_debugger.hooks import MessageDebuggerPlugin
    return MessageDebuggerPlugin(plugin_dir, message_history=message_history)


@pytest.fixture
def web_factory(message_history):
    """Create MessageDebuggerWebFactory instance."""
    from plugins.message_debugger.web_endpoints import MessageDebuggerWebFactory
    return MessageDebuggerWebFactory(message_history)


@pytest.fixture
def hybrid_plugin(plugin_dir):
    """Create MessageDebuggerHybridPlugin instance."""
    from plugins.message_debugger.plugin import MessageDebuggerHybridPlugin
    
    system_config = Mock(spec=AgentSystemConfig)
    mcp_config = Mock(spec=MCPConfig)
    
    return MessageDebuggerHybridPlugin("message_debugger", system_config, mcp_config)


@pytest.fixture
def sample_messages():
    """Create sample chat messages for testing."""
    return [
        ChatMessage(role="system", content="You are a helpful assistant."),
        ChatMessage(role="user", content="What is the capital of France?"),
        ChatMessage(role="assistant", content="The capital of France is Paris."),
    ]


@pytest.fixture
def sample_messages_with_tools():
    """Create sample messages including tool calls."""
    return [
        ChatMessage(role="user", content="What's the weather in Berlin?"),
        ChatMessage(
            role="assistant",
            content=None,
            tool_calls=[{
                "id": "call_123",
                "type": "function",
                "function": {
                    "name": "get_weather",
                    "arguments": '{"city": "Berlin", "units": "celsius"}'
                }
            }]
        ),
        ChatMessage(
            role="tool",
            content='{"temperature": 22, "condition": "sunny"}',
            tool_call_id="call_123"
        ),
        ChatMessage(role="assistant", content="The weather in Berlin is sunny with 22°C."),
    ]


# ============================================================================
# Hook Plugin Tests
# ============================================================================

class TestMessageDebuggerHooks:
    """Test message capture hook functionality."""
    
    @pytest.mark.asyncio
    async def test_capture_pre_llm_basic(self, hooks_plugin, sample_messages, message_history):
        """Test basic pre-LLM message capture."""
        context = HookContext(
            hook_type="pre_llm_call",
            agent_name="test_agent",
            request_id="req_123",
            session_id="sess_456",
            messages=sample_messages
        )
        
        result = await hooks_plugin.debugger_capture_pre_llm(context)
        
        assert result.success is True
        assert result.modified is False
        assert len(message_history) == 1
        
        snapshot = message_history[0]
        assert snapshot['snapshot_type'] == 'pre_llm'
        assert snapshot['agent_name'] == "test_agent"
        assert snapshot['request_id'] == "req_123"
        assert snapshot['session_id'] == "sess_456"
        assert snapshot['message_count'] == 3
        assert len(snapshot['messages']) == 3
    
    @pytest.mark.asyncio
    async def test_capture_post_llm_basic(self, hooks_plugin, sample_messages, message_history):
        """Test basic post-LLM message capture."""
        context = HookContext(
            hook_type="post_llm_call",
            agent_name="test_agent",
            request_id="req_123",
            session_id="sess_456",
            messages=sample_messages,
            llm_response={'model': 'gpt-4', 'usage': {'total_tokens': 50}}
        )
        
        result = await hooks_plugin.debugger_capture_post_llm(context)
        
        assert result.success is True
        assert result.modified is False
        assert len(message_history) == 1
        
        snapshot = message_history[0]
        assert snapshot['snapshot_type'] == 'post_llm'
        assert snapshot.get('llm_response') is not None
        assert snapshot['llm_response']['model'] == 'gpt-4'
    
    @pytest.mark.asyncio
    async def test_capture_disabled(self, hooks_plugin, sample_messages, message_history):
        """Test that capture can be disabled."""
        hooks_plugin.capture_pre_llm = False
        
        context = HookContext(
            hook_type="pre_llm_call",
            request_id="req_123",
            session_id="sess_456",
            agent_name="test_agent",
            messages=sample_messages
        )
        
        await hooks_plugin.debugger_capture_pre_llm(context)
        
        assert len(message_history) == 0
    
    @pytest.mark.asyncio
    async def test_capture_empty_messages(self, hooks_plugin, message_history):
        """Test handling of empty message list."""
        context = HookContext(
            hook_type="pre_llm_call",
            request_id="req_123",
            session_id="sess_456",
            agent_name="test_agent",
            messages=[]
        )
        
        result = await hooks_plugin.debugger_capture_pre_llm(context)
        
        assert result.success is True
        assert result.modified is False
        assert len(message_history) == 0
        assert result.metadata['reason'] == 'no_messages'
    
    @pytest.mark.asyncio
    async def test_capture_with_tool_calls(self, hooks_plugin, sample_messages_with_tools, message_history):
        """Test capture of messages with tool calls."""
        context = HookContext(
            hook_type="pre_llm_call",
            request_id="req_123",
            session_id="sess_456",
            agent_name="test_agent",
            messages=sample_messages_with_tools
        )
        
        result = await hooks_plugin.debugger_capture_pre_llm(context)
        
        assert result.success is True
        snapshot = message_history[0]
        
        # Find assistant message with tool call
        tool_call_msg = next(m for m in snapshot['messages'] if m.get('tool_calls'))
        assert tool_call_msg['tool_call_count'] == 1
        assert tool_call_msg['tool_calls'][0]['function']['name'] == "get_weather"
        
        # Find tool result message
        tool_result_msg = next(m for m in snapshot['messages'] if m.get('is_tool_result'))
        assert tool_result_msg['is_tool_result'] is True
        assert tool_result_msg['tool_call_id'] == "call_123"
    
    @pytest.mark.asyncio
    async def test_token_estimation(self, hooks_plugin, sample_messages, message_history):
        """Test token estimation for captured messages."""
        context = HookContext(
            hook_type="pre_llm_call",
            request_id="req_123",
            session_id="sess_456",
            agent_name="test_agent",
            messages=sample_messages
        )
        
        await hooks_plugin.debugger_capture_pre_llm(context)
        
        snapshot = message_history[0]
        assert snapshot['total_estimated_tokens'] > 0
        
        for msg in snapshot['messages']:
            assert msg['estimated_tokens'] is not None
            assert msg['estimated_tokens'] > 0
    
    @pytest.mark.asyncio
    async def test_auto_cleanup(self, hooks_plugin, sample_messages, message_history):
        """Test automatic cleanup when history exceeds threshold."""
        hooks_plugin.max_history = 5
        hooks_plugin.auto_cleanup_threshold = 7
        
        # Add 10 snapshots
        for i in range(10):
            context = HookContext(
                hook_type="pre_llm_call",
                request_id=f"req_{i}",
                session_id=f"sess_{i}",
                agent_name=f"agent_{i}",
                messages=sample_messages
            )
            await hooks_plugin.debugger_capture_pre_llm(context)
        
        # Cleanup happens when threshold (7) is exceeded
        # After 10 items, should have been cleaned up multiple times
        # Final size should be close to max_history (5)
        assert len(message_history) <= hooks_plugin.auto_cleanup_threshold
        assert len(message_history) >= hooks_plugin.max_history
    
    @pytest.mark.asyncio
    async def test_capture_preserves_context(self, hooks_plugin, sample_messages):
        """Test that capture hook doesn't modify context."""
        context = HookContext(
            hook_type="pre_llm_call",
            request_id="req_123",
            session_id="sess_456",
            agent_name="test_agent",
            messages=sample_messages
        )
        
        original_messages = context.messages.copy()
        result = await hooks_plugin.debugger_capture_pre_llm(context)
        
        assert result.modified is False
        assert context.messages == original_messages


# ============================================================================
# Web Endpoints Tests
# ============================================================================

class TestMessageDebuggerWebEndpoints:
    """Test REST API endpoints."""
    
    @pytest.mark.asyncio
    async def test_list_snapshots_empty(self, web_factory):
        """Test listing snapshots when history is empty."""
        router = web_factory.get_web_router()
        
        # Find the list_snapshots endpoint
        list_endpoint = next(r for r in router.routes if r.path == "/api/plugins/message-debugger/snapshots")
        
        # Mock request with default params
        result = await list_endpoint.endpoint(agent_name=None, session_id=None, limit=50)
        
        assert result['total'] == 0
        assert result['filtered'] == 0
        assert result['snapshots'] == []
    
    @pytest.mark.asyncio
    async def test_filter_by_agent(self, web_factory, message_history):
        """Test filtering snapshots by agent name."""
        # Add snapshots for different agents
        message_history.extend([
            {'agent_name': 'agent_1', 'session_id': 'sess_1', 'message_count': 3, 'messages': []},
            {'agent_name': 'agent_2', 'session_id': 'sess_2', 'message_count': 3, 'messages': []},
            {'agent_name': 'agent_1', 'session_id': 'sess_3', 'message_count': 3, 'messages': []},
        ])
        
        router = web_factory.get_web_router()
        list_endpoint = next(r for r in router.routes if r.path == "/api/plugins/message-debugger/snapshots")
        
        result = await list_endpoint.endpoint(agent_name='agent_1', session_id=None, limit=50)
        
        assert result['filtered'] == 2
        assert all(s['agent_name'] == 'agent_1' for s in result['snapshots'])
    
    @pytest.mark.asyncio
    async def test_get_stats(self, web_factory, message_history):
        """Test getting statistics."""
        # Add test data
        message_history.extend([
            {'agent_name': 'agent_1', 'session_id': 'sess_1', 'message_count': 5, 'total_estimated_tokens': 100, 'messages': []},
            {'agent_name': 'agent_2', 'session_id': 'sess_2', 'message_count': 3, 'total_estimated_tokens': 50, 'messages': []},
        ])
        
        router = web_factory.get_web_router()
        stats_endpoint = next(r for r in router.routes if r.path == "/api/plugins/message-debugger/stats")
        
        result = await stats_endpoint.endpoint()
        
        assert result['total_snapshots'] == 2
        assert len(result['unique_agents']) == 2
        assert result['total_messages'] == 8
        assert result['total_tokens'] == 150
    
    @pytest.mark.asyncio
    async def test_clear_snapshots(self, web_factory, message_history):
        """Test clearing all snapshots."""
        # Add test data
        message_history.extend([
            {'agent_name': 'agent_1', 'messages': []},
            {'agent_name': 'agent_2', 'messages': []},
        ])
        
        router = web_factory.get_web_router()
        clear_endpoint = next(r for r in router.routes if r.path == "/api/plugins/message-debugger/snapshots" 
                             and r.methods == {'DELETE'})
        
        result = await clear_endpoint.endpoint()
        
        assert result['status'] == 'cleared'
        assert result['removed_count'] == 2
        assert len(message_history) == 0


# ============================================================================
# Hybrid Plugin Tests
# ============================================================================

class TestMessageDebuggerHybridPlugin:
    """Test hybrid plugin integration."""
    
    def test_plugin_initialization(self, hybrid_plugin):
        """Test that hybrid plugin initializes correctly."""
        assert hybrid_plugin.name == "message_debugger"
        assert hasattr(hybrid_plugin, 'hooks_plugin')
        assert hasattr(hybrid_plugin, 'web_factory')
        assert hasattr(hybrid_plugin, '_message_history')
    
    def test_get_hooks(self, hybrid_plugin):
        """Test that hooks are properly exposed."""
        hooks = hybrid_plugin.get_hooks()
        assert isinstance(hooks, list)
        assert len(hooks) == 2  # pre_llm and post_llm
        
        hook_names = [h['name'] for h in hooks]
        assert 'debugger_capture_pre_llm' in hook_names
        assert 'debugger_capture_post_llm' in hook_names
    
    def test_get_web_router(self, hybrid_plugin):
        """Test that web router is properly exposed."""
        router = hybrid_plugin.get_web_router()
        assert router is not None
        assert router.prefix == "/api/plugins/message-debugger"
    
    def test_get_panels(self, hybrid_plugin):
        """Test that UI panels are properly defined."""
        panels = hybrid_plugin.get_panels()
        assert len(panels) == 1
        assert panels[0]['id'] == 'message-debugger'
        assert panels[0]['title'] == 'Message Debugger'
        assert panels[0]['icon'] == 'bug'
    
    def test_shared_history(self, hybrid_plugin):
        """Test that hooks and web factory share the same history."""
        assert hybrid_plugin.hooks_plugin.message_history is hybrid_plugin._message_history
        assert hybrid_plugin.web_factory.message_history is hybrid_plugin._message_history


# ============================================================================
# Integration Tests
# ============================================================================

class TestMessageDebuggerIntegration:
    """Test end-to-end integration scenarios."""
    
    @pytest.mark.asyncio
    async def test_pre_and_post_capture_workflow(self, hybrid_plugin, sample_messages):
        """Test full workflow: capture pre -> capture post."""
        # 1. Capture pre-LLM
        pre_context = HookContext(
            hook_type="pre_llm_call",
            agent_name="test_agent",
            request_id="req_123",
            session_id="sess_456",
            messages=sample_messages
        )
        
        await hybrid_plugin.hooks_plugin.debugger_capture_pre_llm(pre_context)
        
        # 2. Capture post-LLM (with response)
        post_context = HookContext(
            hook_type="post_llm_call",
            agent_name="test_agent",
            request_id="req_123",
            session_id="sess_456",
            messages=sample_messages + [ChatMessage(role="assistant", content="Response")],
            llm_response={'model': 'gpt-4', 'usage': {'total_tokens': 60}}
        )
        
        await hybrid_plugin.hooks_plugin.debugger_capture_post_llm(post_context)
        
        # 3. Verify both snapshots captured
        assert len(hybrid_plugin._message_history) == 2
        assert hybrid_plugin._message_history[0]['snapshot_type'] == 'pre_llm'
        assert hybrid_plugin._message_history[1]['snapshot_type'] == 'post_llm'
        
        # 4. Verify via API
        router = hybrid_plugin.get_web_router()
        list_endpoint = next(r for r in router.routes if r.path == "/api/plugins/message-debugger/snapshots")
        list_result = await list_endpoint.endpoint(agent_name=None, session_id=None, limit=50)
        
        assert list_result['total'] == 2
