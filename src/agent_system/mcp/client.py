"""
MCP Client Implementation

Implements an MCP client that can connect to and consume external MCP servers.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from .core import MCPClient, MCPTool, MCPMessage, MCPTransport
from .transport import HTTPTransport

logger = logging.getLogger(__name__)


class StandardMCPClient(MCPClient):
    """Standard MCP client implementation following Anthropic MCP specification"""

    def __init__(self, transport: MCPTransport, name: str = "AgentSystem"):
        super().__init__(transport)
        self.name = name
        self.server_info: Optional[Dict[str, Any]] = None
        self.server_capabilities: Optional[Dict[str, Any]] = None
        self.available_tools: List[MCPTool] = []

    async def connect(self) -> None:
        """Connect to MCP server"""
        logger.info(f"Connecting MCP client {self.name}")
        await self.transport.connect()

    async def disconnect(self) -> None:
        """Disconnect from MCP server"""
        logger.info(f"Disconnecting MCP client {self.name}")
        await self.transport.disconnect()

    async def initialize(self) -> Dict[str, Any]:
        """Initialize connection and get server capabilities"""
        logger.info(f"Initializing MCP connection for {self.name}")

        # Send initialize request
        request = MCPMessage(
            jsonrpc="2.0",
            id=self._next_request_id(),
            method="initialize",
            params={
                "protocolVersion": "2024-11-05",
                "capabilities": {
                    "tools": {}
                },
                "clientInfo": {
                    "name": self.name,
                    "version": "1.0.0"
                }
            }
        )

        if hasattr(self.transport, 'send_request'):
            response = await self.transport.send_request(request)
        else:
            await self.transport.send_message(request)
            response = await self.transport.receive_message()

        if response.error:
            raise Exception(f"Initialize failed: {response.error.message}")

        result = response.result
        self.server_info = result.get("serverInfo", {})
        self.server_capabilities = result.get("capabilities", {})

        logger.info(f"Connected to MCP server: {self.server_info.get('name', 'Unknown')}")
        return result

    async def list_tools(self) -> List[MCPTool]:
        """List available tools from server"""
        logger.debug("Listing tools from MCP server")

        request = MCPMessage(
            jsonrpc="2.0",
            id=self._next_request_id(),
            method="tools/list",
            params={}
        )

        if hasattr(self.transport, 'send_request'):
            response = await self.transport.send_request(request)
        else:
            await self.transport.send_message(request)
            response = await self.transport.receive_message()

        if response.error:
            raise Exception(f"List tools failed: {response.error.message}")

        tools_data = response.result.get("tools", [])
        tools = []

        for tool_data in tools_data:
            tool = MCPTool(
                name=tool_data["name"],
                description=tool_data["description"],
                input_schema=tool_data.get("inputSchema", {})
            )
            tools.append(tool)

        self.available_tools = tools
        logger.debug(f"Found {len(tools)} tools from MCP server")
        return tools

    async def call_tool(self, name: str, arguments: Dict[str, Any]) -> Any:
        """Call a tool on the server"""
        logger.debug(f"Calling MCP tool: {name}")

        request = MCPMessage(
            jsonrpc="2.0",
            id=self._next_request_id(),
            method="tools/call",
            params={
                "name": name,
                "arguments": arguments
            }
        )

        if hasattr(self.transport, 'send_request'):
            response = await self.transport.send_request(request)
        else:
            await self.transport.send_message(request)
            response = await self.transport.receive_message()

        if response.error:
            raise Exception(f"Tool call failed: {response.error.message}")

        # Extract content from MCP response format
        result = response.result
        if "content" in result:
            content_items = result["content"] or []
            # return the first text item found in order
            for item in content_items:
                if isinstance(item, dict) and item.get("type") == "text":
                    return item.get("text", "")

        return result

    async def list_resources(self) -> List[Dict[str, Any]]:
        """List available resources from server"""
        if not self.server_capabilities or not self.server_capabilities.get("resources"):
            return []

        logger.debug("Listing resources from MCP server")

        request = MCPMessage(
            jsonrpc="2.0",
            id=self._next_request_id(),
            method="resources/list",
            params={}
        )

        if hasattr(self.transport, 'send_request'):
            response = await self.transport.send_request(request)
        else:
            await self.transport.send_message(request)
            response = await self.transport.receive_message()

        if response.error:
            raise Exception(f"List resources failed: {response.error.message}")

        return response.result.get("resources", [])

    async def read_resource(self, uri: str) -> Any:
        """Read a resource by URI"""
        if not self.server_capabilities or not self.server_capabilities.get("resources"):
            raise Exception("Server does not support resources")

        logger.debug(f"Reading MCP resource: {uri}")

        request = MCPMessage(
            jsonrpc="2.0",
            id=self._next_request_id(),
            method="resources/read",
            params={"uri": uri}
        )

        if hasattr(self.transport, 'send_request'):
            response = await self.transport.send_request(request)
        else:
            await self.transport.send_message(request)
            response = await self.transport.receive_message()

        if response.error:
            raise Exception(f"Read resource failed: {response.error.message}")

        contents = response.result.get("contents", [])
        if contents:
            return contents[0].get("text", "")
        return None

    async def list_prompts(self) -> List[Dict[str, Any]]:
        """List available prompts from server"""
        if not self.server_capabilities or not self.server_capabilities.get("prompts"):
            return []

        logger.debug("Listing prompts from MCP server")

        request = MCPMessage(
            jsonrpc="2.0",
            id=self._next_request_id(),
            method="prompts/list",
            params={}
        )

        if hasattr(self.transport, 'send_request'):
            response = await self.transport.send_request(request)
        else:
            await self.transport.send_message(request)
            response = await self.transport.receive_message()

        if response.error:
            raise Exception(f"List prompts failed: {response.error.message}")

        return response.result.get("prompts", [])

    async def get_prompt(self, name: str, arguments: Optional[Dict[str, Any]] = None) -> str:
        """Get a prompt by name with optional arguments"""
        if not self.server_capabilities or not self.server_capabilities.get("prompts"):
            raise Exception("Server does not support prompts")

        logger.debug(f"Getting MCP prompt: {name}")

        params: Dict[str, Any] = {"name": name}
        if arguments:
            params["arguments"] = arguments

        request = MCPMessage(
            jsonrpc="2.0",
            id=self._next_request_id(),
            method="prompts/get",
            params=params
        )

        if hasattr(self.transport, 'send_request'):
            response = await self.transport.send_request(request)
        else:
            await self.transport.send_message(request)
            response = await self.transport.receive_message()

        if response.error:
            raise Exception(f"Get prompt failed: {response.error.message}")

        messages = response.result.get("messages", [])
        if messages:
            content = messages[0].get("content", {})
            if isinstance(content, dict):
                return content.get("text", "")
            return str(content)
        return ""


class MCPClientFactory:
    """Factory for creating MCP clients with different configurations"""

    @staticmethod
    async def create_http_client(
        base_url: str,
        client_name: str = "AgentSystem",
        timeout: float = 30.0,
        ssl_verify: bool = True
    ) -> StandardMCPClient:
        """Create an HTTP-based MCP client"""
        transport = HTTPTransport(
            base_url=base_url,
            timeout=timeout,
            ssl_verify=ssl_verify
        )

        client = StandardMCPClient(transport, client_name)
        await client.connect()
        await client.initialize()

        return client

    @staticmethod
    async def create_client_from_config(config: Dict[str, Any]) -> StandardMCPClient:
        """Create an MCP client from configuration"""
        transport_type = config.get("transport", "http")

        if transport_type == "http":
            # Support both 'url' and 'base_url' for compatibility
            base_url = config.get("base_url") or config.get("url")
            if not base_url:
                raise ValueError("Missing 'url' or 'base_url' in client configuration")
            
            return await MCPClientFactory.create_http_client(
                base_url=base_url,
                client_name=config.get("client_name", "AgentSystem"),
                timeout=config.get("timeout", 30.0),
                ssl_verify=config.get("ssl_verify", True)
            )
        else:
            raise ValueError(f"Unsupported transport type: {transport_type}")


class MCPClientManager:
    """Manager for multiple MCP clients"""

    def __init__(self):
        self.clients: Dict[str, StandardMCPClient] = {}

    async def add_client(self, name: str, config: Dict[str, Any]) -> None:
        """Add an MCP client from configuration"""
        try:
            client = await MCPClientFactory.create_client_from_config(config)
            self.clients[name] = client
            logger.info(f"Added MCP client: {name}")
        except Exception as e:
            logger.error(f"Failed to add MCP client {name}: {e}")
            raise

    async def remove_client(self, name: str) -> None:
        """Remove an MCP client"""
        if name in self.clients:
            await self.clients[name].disconnect()
            del self.clients[name]
            logger.info(f"Removed MCP client: {name}")

    def get_client(self, name: str) -> Optional[StandardMCPClient]:
        """Get an MCP client by name"""
        return self.clients.get(name)

    def list_clients(self) -> List[str]:
        """List all client names"""
        return list(self.clients.keys())

    async def list_all_tools(self) -> Dict[str, List[MCPTool]]:
        """List tools from all clients"""
        all_tools = {}
        for name, client in self.clients.items():
            try:
                tools = await client.list_tools()
                all_tools[name] = tools
            except Exception as e:
                logger.error(f"Failed to list tools from client {name}: {e}")
                all_tools[name] = []
        return all_tools

    async def call_tool(self, client_name: str, tool_name: str, arguments: Dict[str, Any]) -> Any:
        """Call a tool on a specific client"""
        client = self.get_client(client_name)
        if not client:
            raise Exception(f"Unknown MCP client: {client_name}")

        return await client.call_tool(tool_name, arguments)

    async def close_all(self) -> None:
        """Close all client connections"""
        for name in list(self.clients.keys()):
            await self.remove_client(name)