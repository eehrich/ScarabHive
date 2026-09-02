"""
Comprehensive tests for modernized MCPServer base class.

Tests the new modern interface with:
- Constructor: (name, system_config, mcp_config)
- Generic call() dispatcher
- Automatic tool routing by method name
- No legacy code or backwards compatibility
"""
from __future__ import annotations

import pytest
from typing import Any

from agent_system.mcp.base import MCPServer, MCPRegistry
from agent_system.mcp.core import MCPTool
from agent_system.config.models import AgentSystemConfig, MCPConfig, AgentConfig


# ============================================================================
# Mock Server Implementations
# ============================================================================

class SimpleToolServer(MCPServer):
    """Simple server with single tool for basic tests."""
    
    def get_tools(self):
        return [
            {
                'type': 'function',
                'function': {
                    'name': 'greet',
                    'description': 'Greet someone',
                    'parameters': {
                        'type': 'object',
                        'properties': {
                            'name': {'type': 'string', 'description': 'Name to greet'}
                        },
                        'required': ['name']
                    }
                }
            }
        ]
    
    async def greet(self, params: dict[str, Any]) -> str:
        """Greet tool implementation."""
        return f"Hello, {params['name']}!"


class MultiToolServer(MCPServer):
    """Server with multiple tools for comprehensive tests."""
    
    def get_tools(self):
        return [
            {
                'type': 'function',
                'function': {
                    'name': 'add',
                    'description': 'Add two numbers',
                    'parameters': {
                        'type': 'object',
                        'properties': {
                            'x': {'type': 'number'},
                            'y': {'type': 'number'}
                        },
                        'required': ['x', 'y']
                    }
                }
            },
            {
                'type': 'function',
                'function': {
                    'name': 'multiply',
                    'description': 'Multiply two numbers',
                    'parameters': {
                        'type': 'object',
                        'properties': {
                            'x': {'type': 'number'},
                            'y': {'type': 'number'}
                        },
                        'required': ['x', 'y']
                    }
                }
            },
            {
                'type': 'function',
                'function': {
                    'name': 'format_name',
                    'description': 'Format a name',
                    'parameters': {
                        'type': 'object',
                        'properties': {
                            'first': {'type': 'string'},
                            'last': {'type': 'string'}
                        },
                        'required': ['first', 'last']
                    }
                }
            }
        ]
    
    async def add(self, params: dict[str, Any]) -> dict[str, Any]:
        """Add tool implementation."""
        return {'result': params['x'] + params['y'], 'operation': 'add'}
    
    async def multiply(self, params: dict[str, Any]) -> dict[str, Any]:
        """Multiply tool implementation."""
        return {'result': params['x'] * params['y'], 'operation': 'multiply'}
    
    async def format_name(self, params: dict[str, Any]) -> str:
        """Format name tool implementation."""
        return f"{params['first']} {params['last']}"


class SyncMethodServer(MCPServer):
    """Server with synchronous tool methods (should still work)."""
    
    def get_tools(self):
        return [
            {
                'type': 'function',
                'function': {
                    'name': 'upper',
                    'description': 'Convert to uppercase',
                    'parameters': {
                        'type': 'object',
                        'properties': {
                            'text': {'type': 'string'}
                        },
                        'required': ['text']
                    }
                }
            }
        ]
    
    def upper(self, params: dict[str, Any]) -> str:
        """Synchronous tool implementation."""
        return params['text'].upper()


class EmptyToolsServer(MCPServer):
    """Server with no tools defined."""
    
    def get_tools(self):
        return []


class MissingMethodServer(MCPServer):
    """Server that declares tools but doesn't implement methods."""
    
    def get_tools(self):
        return [
            {
                'type': 'function',
                'function': {
                    'name': 'missing_tool',
                    'description': 'This tool method is not implemented',
                    'parameters': {'type': 'object', 'properties': {}}
                }
            }
        ]


# ============================================================================
# Fixtures
# ============================================================================

@pytest.fixture
def system_config():
    """Create test system config."""
    return AgentSystemConfig()


@pytest.fixture
def mcp_config():
    """Create test MCP config."""
    return MCPConfig(
        type='plugin',
        enabled=True,
        agent_config=AgentConfig()
    )


@pytest.fixture
def simple_server(system_config, mcp_config):
    """Create simple test server."""
    return SimpleToolServer('simple', system_config, mcp_config)


@pytest.fixture
def multi_server(system_config, mcp_config):
    """Create multi-tool test server."""
    return MultiToolServer('multi', system_config, mcp_config)


@pytest.fixture
def sync_server(system_config, mcp_config):
    """Create server with sync methods."""
    return SyncMethodServer('sync', system_config, mcp_config)


# ============================================================================
# Test: Constructor and Initialization
# ============================================================================

class TestMCPServerConstructor:
    """Test MCPServer constructor with modern signature."""
    
    def test_constructor_signature(self, system_config, mcp_config):
        """Test that constructor accepts (name, system_config, mcp_config)."""
        server = SimpleToolServer('test', system_config, mcp_config)
        
        assert server.name == 'test'
        assert server.system_config == system_config
        assert server.mcp_config == mcp_config
    
    def test_no_legacy_attributes(self, simple_server):
        """Test that legacy attributes are not present."""
        # No ssl_verify attribute
        assert not hasattr(simple_server, 'ssl_verify')
        
        # No agent_config backwards compatibility
        assert not hasattr(simple_server, 'agent_config')
    
    def test_system_config_access(self, simple_server):
        """Test that system config is accessible."""
        # Can access network settings through system_config
        assert hasattr(simple_server.system_config, 'network')
        assert hasattr(simple_server.system_config.network, 'ssl_verify')
    
    def test_mcp_config_access(self, simple_server):
        """Test that MCP config is accessible."""
        assert simple_server.mcp_config.type == 'plugin'
        assert simple_server.mcp_config.enabled is True
        assert simple_server.mcp_config.agent_config is not None


# ============================================================================
# Test: Generic Call Dispatcher
# ============================================================================

class TestGenericCallDispatcher:
    """Test the generic call() dispatcher that routes to tool methods."""
    
    @pytest.mark.asyncio
    async def test_simple_tool_call(self, simple_server):
        """Test calling a simple tool."""
        result = await simple_server.call('greet', {'name': 'Alice'})
        
        assert result == "Hello, Alice!"
    
    @pytest.mark.asyncio
    async def test_multiple_tool_calls(self, multi_server):
        """Test calling different tools on same server."""
        result1 = await multi_server.call('add', {'x': 5, 'y': 3})
        assert result1 == {'result': 8, 'operation': 'add'}
        
        result2 = await multi_server.call('multiply', {'x': 5, 'y': 3})
        assert result2 == {'result': 15, 'operation': 'multiply'}
        
        result3 = await multi_server.call('format_name', {'first': 'John', 'last': 'Doe'})
        assert result3 == 'John Doe'
    
    @pytest.mark.asyncio
    async def test_sync_method_dispatch(self, sync_server):
        """Test that synchronous tool methods work."""
        result = await sync_server.call('upper', {'text': 'hello'})
        
        assert result == 'HELLO'
    
    @pytest.mark.asyncio
    async def test_nonexistent_tool_error(self, simple_server):
        """Test error when calling non-existent tool."""
        with pytest.raises(ValueError) as exc_info:
            await simple_server.call('nonexistent', {})
        
        error_msg = str(exc_info.value)
        assert 'nonexistent' in error_msg
        assert 'not found' in error_msg
        assert 'simple' in error_msg  # server name
        assert 'greet' in error_msg  # available tool
    
    @pytest.mark.asyncio
    async def test_missing_method_error(self, system_config, mcp_config):
        """Test error when tool declared but method not implemented."""
        server = MissingMethodServer('missing', system_config, mcp_config)
        
        with pytest.raises(ValueError) as exc_info:
            await server.call('missing_tool', {})
        
        error_msg = str(exc_info.value)
        assert 'missing_tool' in error_msg
        assert 'not found' in error_msg
    
    @pytest.mark.asyncio
    async def test_different_param_types(self, multi_server):
        """Test dispatcher handles different parameter types."""
        # Integers
        result1 = await multi_server.call('add', {'x': 10, 'y': 20})
        assert result1['result'] == 30
        
        # Floats
        result2 = await multi_server.call('add', {'x': 1.5, 'y': 2.5})
        assert result2['result'] == 4.0
        
        # Strings
        result3 = await multi_server.call('format_name', {'first': 'Jane', 'last': 'Smith'})
        assert result3 == 'Jane Smith'


# ============================================================================
# Test: list_tools() Method
# ============================================================================

class TestListTools:
    """Test the list_tools() method."""
    
    @pytest.mark.asyncio
    async def test_list_single_tool(self, simple_server):
        """Test listing tools for server with one tool."""
        tools = await simple_server.list_tools()
        
        assert len(tools) == 1
        assert isinstance(tools[0], MCPTool)
        assert tools[0].name == 'greet'
        assert tools[0].description == 'Greet someone'
        assert 'name' in tools[0].input_schema['properties']
    
    @pytest.mark.asyncio
    async def test_list_multiple_tools(self, multi_server):
        """Test listing tools for server with multiple tools."""
        tools = await multi_server.list_tools()
        
        assert len(tools) == 3
        tool_names = [t.name for t in tools]
        assert 'add' in tool_names
        assert 'multiply' in tool_names
        assert 'format_name' in tool_names
        
        # Check they're all MCPTool instances
        assert all(isinstance(t, MCPTool) for t in tools)
    
    @pytest.mark.asyncio
    async def test_list_tools_returns_mcp_tool_objects(self, multi_server):
        """Test that list_tools converts schemas to MCPTool objects."""
        tools = await multi_server.list_tools()
        
        for tool in tools:
            assert hasattr(tool, 'name')
            assert hasattr(tool, 'description')
            assert hasattr(tool, 'input_schema')
            assert isinstance(tool.input_schema, dict)
    
    @pytest.mark.asyncio
    async def test_empty_tools_list(self, system_config, mcp_config):
        """Test server with no tools raises error."""
        server = EmptyToolsServer('empty', system_config, mcp_config)
        
        # Should return empty list
        tools = await server.list_tools()
        assert tools == []
    
    @pytest.mark.asyncio
    async def test_list_tools_error_handling(self, system_config, mcp_config):
        """Test error when server doesn't implement get_tools()."""
        
        class NoToolsServer(MCPServer):
            pass  # Doesn't implement get_tools()
        
        server = NoToolsServer('no_tools', system_config, mcp_config)
        
        with pytest.raises(NotImplementedError) as exc_info:
            await server.list_tools()
        
        error_msg = str(exc_info.value)
        assert 'no_tools' in error_msg
        assert 'must implement' in error_msg


# ============================================================================
# Test: call_with_status() Method
# ============================================================================

class TestCallWithStatus:
    """Test the call_with_status() wrapper method."""
    
    @pytest.mark.asyncio
    async def test_call_with_status_basic(self, simple_server):
        """Test that call_with_status wraps call() correctly."""
        # This injects _status and _request_id into params
        result = await simple_server.call_with_status('greet', {'name': 'Bob'})
        
        assert result == "Hello, Bob!"
    
    @pytest.mark.asyncio
    async def test_call_with_status_injects_params(self, system_config, mcp_config):
        """Test that call_with_status injects status params."""
        
        class InspectParamsServer(MCPServer):
            def get_tools(self):
                return [{
                    'type': 'function',
                    'function': {
                        'name': 'inspect',
                        'description': 'Inspect params',
                        'parameters': {'type': 'object', 'properties': {}}
                    }
                }]
            
            async def inspect(self, params: dict[str, Any]) -> dict[str, Any]:
                return {
                    'has_status': '_status' in params,
                    'has_request_id': '_request_id' in params,
                    'original_params': {k: v for k, v in params.items() if not k.startswith('_')}
                }
        
        server = InspectParamsServer('inspect', system_config, mcp_config)
        result = await server.call_with_status('inspect', {'request_id': 'test-123', 'data': 'test'})
        
        assert result['has_status'] is True
        assert result['has_request_id'] is True
        # request_id is a user-provided parameter, so it should be preserved
        assert result['original_params'] == {'request_id': 'test-123', 'data': 'test'}
    
    @pytest.mark.asyncio
    async def test_call_with_status_supports_both_request_id_formats(self, system_config, mcp_config):
        """Test support for both request_id and requestId (JS convention)."""
        
        class RequestIdServer(MCPServer):
            def get_tools(self):
                return [{
                    'type': 'function',
                    'function': {
                        'name': 'get_request_id',
                        'description': 'Get request ID',
                        'parameters': {'type': 'object', 'properties': {}}
                    }
                }]
            
            async def get_request_id(self, params: dict[str, Any]) -> str:
                return params.get('_request_id', 'no-id')
        
        server = RequestIdServer('reqid', system_config, mcp_config)
        
        # Test snake_case (Python convention)
        result1 = await server.call_with_status('get_request_id', {'request_id': 'python-123'})
        assert result1 == 'python-123'
        
        # Test camelCase (JS convention)
        result2 = await server.call_with_status('get_request_id', {'requestId': 'js-456'})
        assert result2 == 'js-456'


# ============================================================================
# Test: MCPRegistry Integration
# ============================================================================

class TestMCPRegistry:
    """Test MCPRegistry with modernized MCPServer."""
    
    def test_register_server(self, simple_server):
        """Test registering a server."""
        registry = MCPRegistry()
        registry.register('simple', simple_server)
        
        assert 'simple' in registry.list()
        assert registry.get('simple') == simple_server
    
    def test_register_multiple_servers(self, simple_server, multi_server):
        """Test registering multiple servers."""
        registry = MCPRegistry()
        registry.register('simple', simple_server)
        registry.register('multi', multi_server)
        
        assert len(registry.list()) == 2
        assert registry.get('simple') == simple_server
        assert registry.get('multi') == multi_server
    
    @pytest.mark.asyncio
    async def test_call_through_registry(self, simple_server):
        """Test calling tools through registry."""
        registry = MCPRegistry()
        registry.register('simple', simple_server)
        
        server = registry.get('simple')
        result = await server.call('greet', {'name': 'Registry'})
        
        assert result == "Hello, Registry!"


# ============================================================================
# Test: No Legacy Code
# ============================================================================

class TestNoLegacyCode:
    """Verify that all legacy code has been removed."""
    
    def test_no_get_schema_method(self, simple_server):
        """Test that get_schema() legacy method is not supported."""
        # Servers should not have get_schema() method
        # If they do, it shouldn't be used by list_tools()
        assert not hasattr(simple_server, 'get_schema') or \
               hasattr(simple_server.__class__, 'get_schema')
    
    def test_no_get_default_action(self, simple_server):
        """Test that get_default_action() is not used."""
        # Should not have this method
        assert not hasattr(simple_server, 'get_default_action')
    
    def test_no_ssl_verify_attribute(self, simple_server):
        """Test that ssl_verify is not a direct attribute."""
        assert not hasattr(simple_server, 'ssl_verify')
        
        # Should access through system_config instead
        assert hasattr(simple_server.system_config.network, 'ssl_verify')
    
    def test_no_agent_config_backwards_compat(self, simple_server):
        """Test that agent_config backwards compatibility alias is gone."""
        assert not hasattr(simple_server, 'agent_config')
        
        # Should use system_config and mcp_config instead
        assert hasattr(simple_server, 'system_config')
        assert hasattr(simple_server, 'mcp_config')


# ============================================================================
# Test: Error Messages and Debugging
# ============================================================================

class TestErrorMessages:
    """Test that error messages are helpful and informative."""
    
    @pytest.mark.asyncio
    async def test_helpful_tool_not_found_message(self, multi_server):
        """Test that error message lists available tools."""
        with pytest.raises(ValueError) as exc_info:
            await multi_server.call('invalid_tool', {})
        
        error_msg = str(exc_info.value)
        # Should mention the tool name
        assert 'invalid_tool' in error_msg
        # Should mention the server name
        assert 'multi' in error_msg
        # Should list available tools
        assert 'add' in error_msg
        assert 'multiply' in error_msg
        assert 'format_name' in error_msg
    
    @pytest.mark.asyncio
    async def test_not_implemented_error_message(self, system_config, mcp_config):
        """Test error message when plugin doesn't implement required methods."""
        
        class IncompleteServer(MCPServer):
            pass  # No get_tools() implementation
        
        server = IncompleteServer('incomplete', system_config, mcp_config)
        
        with pytest.raises(NotImplementedError) as exc_info:
            await server.list_tools()
        
        error_msg = str(exc_info.value)
        assert 'incomplete' in error_msg
        assert 'must implement' in error_msg


# ============================================================================
# Test: the error safety net in call_with_status()
# ============================================================================

class TestErrorResultsReachTheStatusStream:
    """A handler that RETURNS an error must not end as 'completed'.

    Audited 2026-09-02 across all 45 plugins with tools: 19 had at least one
    error path that returned a failure result without touching ``_status``,
    so the scope closed with its default END and the failure read as a
    success in CLI and WebUI. The net lives here, at the one place every
    tool call routes through, instead of in every plugin.
    """

    @staticmethod
    async def _events(server, action, params=None):
        """Run one tool call and return the status events it published."""
        from agent_system.mcp.status import get_status_bus

        bus = get_status_bus()
        queue = await bus.subscribe(server=f"{server.name}.{action}()")
        try:
            result = await server.call_with_status(action, params or {})
        finally:
            bus.unsubscribe(queue)
        events = []
        while not queue.empty():
            events.append(queue.get_nowait())
        assert events, "no status events arrived — the subscription is vacuous"
        return result, events

    @staticmethod
    def _server(system_config, mcp_config):
        class ResultShapeServer(MCPServer):
            """One tool per result shape the fleet actually returns."""

            def get_tools(self):
                return []

            async def status_error(self, params):
                return {"status": "error", "error": "sandbox denied",
                        "error_type": "PermissionError"}

            async def success_false(self, params):
                return {"success": False, "error": "host unreachable"}

            async def negative_answer(self, params):
                """Did its job; the ANSWER is no. Not a failure."""
                return {"success": False, "message": "was not connected"}

            async def fine(self, params):
                return {"status": "success", "rows": 3}

            async def speaks_for_itself(self, params):
                await params["_status"].error("host unreachable: db-1")
                return {"status": "error", "error": "host unreachable: db-1"}

            async def reports_error_as_data(self, params):
                """Succeeded; the THING it queried is in error state."""
                await params["_status"].end("Checked db-1: unhealthy")
                return {"status": "error", "error": "db-1 is down"}

        return ResultShapeServer('shapes', system_config, mcp_config)

    @pytest.mark.asyncio
    @pytest.mark.parametrize("action,expected", [
        ("status_error", "sandbox denied"),
        ("success_false", "host unreachable"),
    ])
    async def test_a_returned_error_is_published_as_error(
            self, system_config, mcp_config, action, expected):
        from agent_system.mcp.status import StatusPhase

        server = self._server(system_config, mcp_config)
        result, events = await self._events(server, action)

        assert result["error"] == expected, "the result must pass through untouched"
        errors = [e for e in events if e.phase is StatusPhase.ERROR]
        assert len(errors) == 1, [(e.phase, e.message) for e in events]
        assert errors[0].message == expected
        assert not [e for e in events if e.phase is StatusPhase.END], \
            "a failure must not also read as 'completed'"

    @pytest.mark.asyncio
    async def test_error_type_travels_as_meta(self, system_config, mcp_config):
        from agent_system.mcp.status import StatusPhase

        server = self._server(system_config, mcp_config)
        _, events = await self._events(server, "status_error")
        error = next(e for e in events if e.phase is StatusPhase.ERROR)
        assert error.meta == {"error_type": "PermissionError"}

    @pytest.mark.asyncio
    @pytest.mark.parametrize("action", [
        "fine",
        # success: False without an 'error' is a negative ANSWER, not a
        # failure -- mcp_client.disconnect says this for a server that was
        # not connected. Flagging it would cry wolf on a healthy call.
        "negative_answer",
    ])
    async def test_a_successful_call_still_ends(self, system_config, mcp_config,
                                                action):
        """Counter-check: the net must not turn healthy calls into errors."""
        from agent_system.mcp.status import StatusPhase

        server = self._server(system_config, mcp_config)
        _, events = await self._events(server, action)
        assert [e.phase for e in events] == [StatusPhase.START, StatusPhase.END]

    @pytest.mark.asyncio
    @pytest.mark.parametrize("action,phase,message", [
        # The plugin reported it itself — the net must not double-report.
        ("speaks_for_itself", "ERROR", "host unreachable: db-1"),
        # The plugin ENDED deliberately: the call succeeded and 'error' is
        # its payload, not its outcome. The net must keep its hands off.
        ("reports_error_as_data", "END", "Checked db-1: unhealthy"),
    ])
    async def test_the_plugins_own_verdict_wins(self, system_config, mcp_config,
                                                action, phase, message):
        from agent_system.mcp.status import StatusPhase

        server = self._server(system_config, mcp_config)
        _, events = await self._events(server, action)
        terminal = [e for e in events if e.phase is not StatusPhase.START]
        assert len(terminal) == 1, [(e.phase, e.message) for e in terminal]
        assert terminal[0].phase is getattr(StatusPhase, phase)
        assert terminal[0].message == message


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
