"""
Tests for MCP Client Implementation
"""

import pytest

from agent_system.mcp.client import StandardMCPClient, MCPClientFactory, MCPClientManager
from agent_system.mcp.core import MCPMessage


class MockHTTPTransport:
    """Mock HTTP transport for testing"""

    def __init__(self):
        self.connected = False
        self.responses = []
        self.sent_requests = []

    async def connect(self):
        self.connected = True

    async def disconnect(self):
        self.connected = False

    async def send_request(self, message: MCPMessage) -> MCPMessage:
        self.sent_requests.append(message)
        if self.responses:
            return self.responses.pop(0)

        # Default successful responses
        if message.method == "initialize":
            return MCPMessage(
                jsonrpc="2.0",
                id=message.id,
                result={
                    "protocolVersion": "2024-11-05",
                    "capabilities": {
                        "tools": {"listChanged": False},
                        "resources": {"listChanged": False},
                        "prompts": {"listChanged": False}
                    },
                    "serverInfo": {
                        "name": "Test MCP Server",
                        "version": "1.0.0"
                    }
                }
            )
        elif message.method == "tools/list":
            return MCPMessage(
                jsonrpc="2.0",
                id=message.id,
                result={
                    "tools": [
                        {
                            "name": "test_tool",
                            "description": "A test tool",
                            "inputSchema": {
                                "type": "object",
                                "properties": {"param": {"type": "string"}},
                                "required": ["param"]
                            }
                        }
                    ]
                }
            )
        elif message.method == "tools/call":
            return MCPMessage(
                jsonrpc="2.0",
                id=message.id,
                result={
                    "content": [
                        {
                            "type": "text",
                            "text": f"Tool {message.params['name']} called with {message.params['arguments']}"
                        }
                    ]
                }
            )
        else:
            return MCPMessage(
                jsonrpc="2.0",
                id=message.id,
                result={}
            )

    def add_response(self, response: MCPMessage):
        """Add a response to be returned"""
        self.responses.append(response)


@pytest.fixture
def mock_transport():
    return MockHTTPTransport()


@pytest.fixture
def mcp_client(mock_transport):
    return StandardMCPClient(mock_transport, "TestClient")


class TestStandardMCPClient:
    """Test standard MCP client functionality"""

    @pytest.mark.asyncio
    async def test_connect(self, mcp_client, mock_transport):
        await mcp_client.connect()
        assert mock_transport.connected

    @pytest.mark.asyncio
    async def test_disconnect(self, mcp_client, mock_transport):
        await mcp_client.connect()
        await mcp_client.disconnect()
        assert not mock_transport.connected

    @pytest.mark.asyncio
    async def test_initialize(self, mcp_client, mock_transport):
        await mcp_client.connect()
        result = await mcp_client.initialize()

        assert result["protocolVersion"] == "2024-11-05"
        assert "capabilities" in result
        assert "serverInfo" in result
        assert mcp_client.server_info["name"] == "Test MCP Server"
        assert len(mock_transport.sent_requests) == 1

        request = mock_transport.sent_requests[0]
        assert request.method == "initialize"
        assert request.params["clientInfo"]["name"] == "TestClient"

    @pytest.mark.asyncio
    async def test_list_tools(self, mcp_client, mock_transport):
        await mcp_client.connect()
        await mcp_client.initialize()

        tools = await mcp_client.list_tools()

        assert len(tools) == 1
        assert tools[0].name == "test_tool"
        assert tools[0].description == "A test tool"
        assert tools[0].input_schema["type"] == "object"
        assert mcp_client.available_tools == tools

    @pytest.mark.asyncio
    async def test_call_tool(self, mcp_client, mock_transport):
        await mcp_client.connect()
        await mcp_client.initialize()

        result = await mcp_client.call_tool("test_tool", {"param": "value"})

        assert "Tool test_tool called with" in result
        assert "param" in result

        # Check the sent request
        call_request = None
        for req in mock_transport.sent_requests:
            if req.method == "tools/call":
                call_request = req
                break

        assert call_request is not None
        assert call_request.params["name"] == "test_tool"
        assert call_request.params["arguments"] == {"param": "value"}

    @pytest.mark.asyncio
    async def test_call_tool_error(self, mcp_client, mock_transport):
        await mcp_client.connect()
        await mcp_client.initialize()

        # Add error response
        from agent_system.mcp.core import MCPError
        error_response = MCPMessage(
            jsonrpc="2.0",
            id=1,
            error=MCPError(code=-32603, message="Tool not found")
        )
        mock_transport.add_response(error_response)

        with pytest.raises(Exception, match="Tool call failed: Tool not found"):
            await mcp_client.call_tool("unknown_tool", {})

    @pytest.mark.asyncio
    async def test_list_resources_unsupported(self, mcp_client, mock_transport):
        await mcp_client.connect()

        # Initialize without resources capability
        init_response = MCPMessage(
            jsonrpc="2.0",
            id=1,
            result={
                "protocolVersion": "2024-11-05",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "Test Server", "version": "1.0.0"}
            }
        )
        mock_transport.add_response(init_response)

        await mcp_client.initialize()
        resources = await mcp_client.list_resources()

        assert resources == []

    @pytest.mark.asyncio
    async def test_read_resource_unsupported(self, mcp_client, mock_transport):
        await mcp_client.connect()

        # Initialize without resources capability
        init_response = MCPMessage(
            jsonrpc="2.0",
            id=1,
            result={
                "protocolVersion": "2024-11-05",
                "capabilities": {"tools": {}},  # No resources capability
                "serverInfo": {"name": "Test Server", "version": "1.0.0"}
            }
        )
        mock_transport.add_response(init_response)

        await mcp_client.initialize()

        # Server doesn't support resources
        with pytest.raises(Exception, match="Server does not support resources"):
            await mcp_client.read_resource("test://resource")


class TestMCPClientFactory:
    """Test MCP client factory"""

    @pytest.mark.asyncio
    async def test_create_client_from_config_http(self):
        config = {
            "transport": "http",
            "base_url": "http://localhost:8000",
            "client_name": "TestClient",
            "timeout": 30.0,
            "ssl_verify": True
        }

        # Mock the HTTPTransport to avoid actual network calls
        original_create = MCPClientFactory.create_http_client

        async def mock_create_http_client(*args, **kwargs):
            transport = MockHTTPTransport()
            client = StandardMCPClient(transport, kwargs.get("client_name", "AgentSystem"))
            await client.connect()
            await client.initialize()
            return client

        MCPClientFactory.create_http_client = mock_create_http_client

        try:
            client = await MCPClientFactory.create_client_from_config(config)
            assert client.name == "TestClient"
            assert client.transport.connected
        finally:
            MCPClientFactory.create_http_client = original_create

    def test_create_client_from_config_invalid_transport(self):
        config = {
            "transport": "invalid",
            "base_url": "http://localhost:8000"
        }

        with pytest.raises(ValueError, match="Unsupported transport type: invalid"):
            # This will fail synchronously
            import asyncio
            asyncio.run(MCPClientFactory.create_client_from_config(config))


class TestMCPClientManager:
    """Test MCP client manager"""

    @pytest.fixture
    def client_manager(self):
        return MCPClientManager()

    @pytest.mark.asyncio
    async def test_add_client(self, client_manager):
        # Mock the factory method
        async def mock_create_client(config):
            transport = MockHTTPTransport()
            client = StandardMCPClient(transport, "TestClient")
            await client.connect()
            await client.initialize()
            return client

        original_create = MCPClientFactory.create_client_from_config
        MCPClientFactory.create_client_from_config = mock_create_client

        try:
            config = {"base_url": "http://localhost:8000"}
            await client_manager.add_client("test_client", config)

            assert "test_client" in client_manager.list_clients()
            assert client_manager.get_client("test_client") is not None
        finally:
            MCPClientFactory.create_client_from_config = original_create

    @pytest.mark.asyncio
    async def test_remove_client(self, client_manager):
        # Add a mock client
        transport = MockHTTPTransport()
        client = StandardMCPClient(transport, "TestClient")
        await client.connect()
        client_manager.clients["test_client"] = client

        await client_manager.remove_client("test_client")

        assert "test_client" not in client_manager.list_clients()
        assert not transport.connected

    @pytest.mark.asyncio
    async def test_list_all_tools(self, client_manager):
        # Add mock clients
        transport1 = MockHTTPTransport()
        client1 = StandardMCPClient(transport1, "Client1")
        await client1.connect()
        await client1.initialize()

        transport2 = MockHTTPTransport()
        client2 = StandardMCPClient(transport2, "Client2")
        await client2.connect()
        await client2.initialize()

        client_manager.clients["client1"] = client1
        client_manager.clients["client2"] = client2

        all_tools = await client_manager.list_all_tools()

        assert "client1" in all_tools
        assert "client2" in all_tools
        assert len(all_tools["client1"]) == 1  # Default mock tool
        assert len(all_tools["client2"]) == 1  # Default mock tool

    @pytest.mark.asyncio
    async def test_call_tool(self, client_manager):
        # Add mock client
        transport = MockHTTPTransport()
        client = StandardMCPClient(transport, "TestClient")
        await client.connect()
        await client.initialize()
        client_manager.clients["test_client"] = client

        result = await client_manager.call_tool("test_client", "test_tool", {"param": "value"})

        assert "Tool test_tool called with" in result

    @pytest.mark.asyncio
    async def test_call_tool_unknown_client(self, client_manager):
        with pytest.raises(Exception, match="Unknown MCP client: unknown"):
            await client_manager.call_tool("unknown", "test_tool", {})

    @pytest.mark.asyncio
    async def test_close_all(self, client_manager):
        # Add mock clients
        transport1 = MockHTTPTransport()
        client1 = StandardMCPClient(transport1, "Client1")
        await client1.connect()

        transport2 = MockHTTPTransport()
        client2 = StandardMCPClient(transport2, "Client2")
        await client2.connect()

        client_manager.clients["client1"] = client1
        client_manager.clients["client2"] = client2

        await client_manager.close_all()

        assert len(client_manager.list_clients()) == 0
        assert not transport1.connected
        assert not transport2.connected