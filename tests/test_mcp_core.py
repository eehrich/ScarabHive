"""
Tests for MCP Core Interfaces
"""

import pytest
from typing import Any, Dict, List

from agent_system.mcp.core import (
    MCPServer, MCPClient, MCPTool,
    MCPMessage, MCPError, MCPCapability, MCPRegistry, MCPTransport
)


class MockTransport(MCPTransport):
    """Mock transport for testing"""
    
    def __init__(self):
        self.messages = []
        self.connected = False
    
    async def send_message(self, message: MCPMessage) -> None:
        self.messages.append(message)
    
    async def receive_message(self) -> MCPMessage:
        if self.messages:
            return self.messages.pop(0)
        raise Exception("No messages available")
    
    async def connect(self) -> None:
        self.connected = True
    
    async def disconnect(self) -> None:
        self.connected = False


class MockMCPServer(MCPServer):
    """Mock MCP server for testing"""
    
    def __init__(self, name: str, description: str = "Test server"):
        super().__init__(name, description)
        self.capabilities = [MCPCapability.TOOLS]
        self.test_tools = [
            MCPTool(
                name="test_tool",
                description="A test tool",
                input_schema={
                    "type": "object",
                    "properties": {
                        "param1": {"type": "string"},
                        "param2": {"type": "integer"}
                    },
                    "required": ["param1"]
                }
            )
        ]
    
    async def list_tools(self) -> List[MCPTool]:
        return self.test_tools
    
    async def call_tool(self, name: str, arguments: Dict[str, Any]) -> Any:
        if name == "test_tool":
            return f"Called test_tool with {arguments}"
        raise Exception(f"Unknown tool: {name}")


class MockMCPClient(MCPClient):
    """Mock MCP client for testing"""
    
    def __init__(self, transport: MCPTransport):
        super().__init__(transport)
        self.connected = False
        self.initialized = False
    
    async def connect(self) -> None:
        await self.transport.connect()
        self.connected = True
    
    async def disconnect(self) -> None:
        await self.transport.disconnect()
        self.connected = False
    
    async def initialize(self) -> Dict[str, Any]:
        self.initialized = True
        return {"capabilities": {"tools": True}}
    
    async def list_tools(self) -> List[MCPTool]:
        return [
            MCPTool(
                name="remote_tool",
                description="A remote tool",
                input_schema={"type": "object"}
            )
        ]
    
    async def call_tool(self, name: str, arguments: Dict[str, Any]) -> Any:
        return f"Remote call: {name}({arguments})"


@pytest.fixture
def mock_transport():
    return MockTransport()


@pytest.fixture
def mock_server():
    return MockMCPServer("test_server")


@pytest.fixture
def mock_client(mock_transport):
    return MockMCPClient(mock_transport)


@pytest.fixture
def registry():
    return MCPRegistry()


class TestMCPMessage:
    """Test MCP message structures"""
    
    def test_create_request_message(self):
        message = MCPMessage(
            jsonrpc="2.0",
            id=1,
            method="test_method",
            params={"param1": "value1"}
        )
        
        assert message.jsonrpc == "2.0"
        assert message.id == 1
        assert message.method == "test_method"
        assert message.params == {"param1": "value1"}
        assert message.result is None
        assert message.error is None
    
    def test_create_response_message(self):
        message = MCPMessage(
            jsonrpc="2.0",
            id=1,
            result={"success": True}
        )
        
        assert message.jsonrpc == "2.0"
        assert message.id == 1
        assert message.result == {"success": True}
        assert message.method is None
        assert message.error is None
    
    def test_create_error_message(self):
        error = MCPError(
            code=-32600,
            message="Invalid request",
            data={"details": "Missing method"}
        )
        
        message = MCPMessage(
            jsonrpc="2.0",
            id=1,
            error=error
        )
        
        assert message.jsonrpc == "2.0"
        assert message.id == 1
        assert message.error.code == -32600
        assert message.error.message == "Invalid request"
        assert message.error.data == {"details": "Missing method"}


class TestMCPTool:
    """Test MCP tool definitions"""
    
    def test_create_tool(self):
        tool = MCPTool(
            name="test_tool",
            description="A test tool",
            input_schema={
                "type": "object",
                "properties": {"param": {"type": "string"}},
                "required": ["param"]
            }
        )
        
        assert tool.name == "test_tool"
        assert tool.description == "A test tool"
        assert tool.input_schema["type"] == "object"
        assert "param" in tool.input_schema["properties"]


class TestMCPServer:
    """Test MCP server functionality"""
    
    @pytest.mark.asyncio
    async def test_server_list_tools(self, mock_server):
        tools = await mock_server.list_tools()
        
        assert len(tools) == 1
        assert tools[0].name == "test_tool"
        assert tools[0].description == "A test tool"
    
    @pytest.mark.asyncio
    async def test_server_call_tool(self, mock_server):
        result = await mock_server.call_tool("test_tool", {"param1": "test"})
        
        assert result == "Called test_tool with {'param1': 'test'}"
    
    @pytest.mark.asyncio
    async def test_server_call_unknown_tool(self, mock_server):
        with pytest.raises(Exception, match="Unknown tool: unknown"):
            await mock_server.call_tool("unknown", {})
    
    @pytest.mark.asyncio
    async def test_server_capabilities(self, mock_server):
        assert MCPCapability.TOOLS in mock_server.capabilities


class TestMCPClient:
    """Test MCP client functionality"""
    
    @pytest.mark.asyncio
    async def test_client_connect(self, mock_client):
        assert not mock_client.connected
        
        await mock_client.connect()
        
        assert mock_client.connected
        assert mock_client.transport.connected
    
    @pytest.mark.asyncio
    async def test_client_initialize(self, mock_client):
        await mock_client.connect()
        
        result = await mock_client.initialize()
        
        assert mock_client.initialized
        assert result["capabilities"]["tools"] is True
    
    @pytest.mark.asyncio
    async def test_client_list_tools(self, mock_client):
        await mock_client.connect()
        
        tools = await mock_client.list_tools()
        
        assert len(tools) == 1
        assert tools[0].name == "remote_tool"
    
    @pytest.mark.asyncio
    async def test_client_call_tool(self, mock_client):
        await mock_client.connect()
        
        result = await mock_client.call_tool("test", {"param": "value"})
        
        assert result == "Remote call: test({'param': 'value'})"


class TestMCPTransport:
    """Test MCP transport functionality"""
    
    @pytest.mark.asyncio
    async def test_transport_connect(self, mock_transport):
        assert not mock_transport.connected
        
        await mock_transport.connect()
        
        assert mock_transport.connected
    
    @pytest.mark.asyncio
    async def test_transport_send_receive(self, mock_transport):
        message = MCPMessage(jsonrpc="2.0", id=1, method="test")
        
        await mock_transport.send_message(message)
        
        assert len(mock_transport.messages) == 1
        
        received = await mock_transport.receive_message()
        
        assert received.jsonrpc == "2.0"
        assert received.id == 1
        assert received.method == "test"


class TestMCPRegistry:
    """Test MCP registry functionality"""
    
    def test_register_server(self, registry, mock_server):
        registry.register_server("test", mock_server)
        
        assert "test" in registry.list_servers()
        assert registry.get_server("test") == mock_server
    
    def test_register_client(self, registry, mock_client):
        registry.register_client("test", mock_client)
        
        assert "test" in registry.list_clients()
        assert registry.get_client("test") == mock_client
    
    def test_get_nonexistent_server(self, registry):
        assert registry.get_server("nonexistent") is None
    
    def test_get_nonexistent_client(self, registry):
        assert registry.get_client("nonexistent") is None