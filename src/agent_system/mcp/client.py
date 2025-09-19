"""
MCP Client Implementation

Implements an MCP client that can connect to and consume external MCP servers.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from .core import MCPClient, MCPTool, MCPMessage, MCPTransport
from .transport import HTTPTransport
from .streaming_transport import HTTPStreamingTransport
from .status import publish_status, PHASE_START, PHASE_END, PHASE_ERROR

logger = logging.getLogger(__name__)


class StandardMCPClient(MCPClient):
    """Standard MCP client implementation following Anthropic MCP specification"""

    def __init__(self, transport: MCPTransport, name: str = "AgentSystem", initialization_options: Optional[Dict[str, Any]] = None):
        super().__init__(transport)
        self.name = name
        self.initialization_options = initialization_options or {}
        self.server_info: Optional[Dict[str, Any]] = None
        self.server_capabilities: Optional[Dict[str, Any]] = None
        self.available_tools: List[MCPTool] = []

    async def connect(self) -> None:
        """Connect to MCP server"""
        logger.info(f"Connecting MCP client {self.name}")
        try:
            try:
                await publish_status(self.name, "Transport connecting to MCP server", phase=PHASE_START)
                # Also publish under logical server id (e.g., strip AgentSystem- prefix)
                if isinstance(self.name, str) and self.name.startswith("AgentSystem-"):
                    logical = self.name.split("AgentSystem-", 1)[1]
                    await publish_status(logical, "Transport connecting to MCP server", phase=PHASE_START)
            except Exception as e:
                logger.debug("publish_status failed for transport connect start: %s", e)

            await self.transport.connect()

            try:
                await publish_status(self.name, "Transport connected to MCP server", phase=PHASE_END)
                if isinstance(self.name, str) and self.name.startswith("AgentSystem-"):
                    logical = self.name.split("AgentSystem-", 1)[1]
                    await publish_status(logical, "Transport connected to MCP server", phase=PHASE_END)
            except Exception as e:
                logger.debug("publish_status failed for transport connected: %s", e)
        except Exception as e:
            try:
                await publish_status(self.name, f"Transport connection failed: {e}", phase=PHASE_ERROR, meta={"error": str(e)})
                if isinstance(self.name, str) and self.name.startswith("AgentSystem-"):
                    logical = self.name.split("AgentSystem-", 1)[1]
                    await publish_status(logical, f"Transport connection failed: {e}", phase=PHASE_ERROR, meta={"error": str(e)})
            except Exception as pub_e:
                logger.debug("publish_status failed for transport connect error: %s", pub_e)
            raise

    async def disconnect(self) -> None:
        """Disconnect from MCP server"""
        logger.info(f"Disconnecting MCP client {self.name}")
        try:
            try:
                await publish_status(self.name, "Transport disconnecting from MCP server", phase=PHASE_START)
                if isinstance(self.name, str) and self.name.startswith("AgentSystem-"):
                    logical = self.name.split("AgentSystem-", 1)[1]
                    await publish_status(logical, "Transport disconnecting from MCP server", phase=PHASE_START)
            except Exception as e:
                logger.debug("publish_status failed for transport disconnect start: %s", e)

            await self.transport.disconnect()

            try:
                await publish_status(self.name, "Transport disconnected from MCP server", phase=PHASE_END)
                if isinstance(self.name, str) and self.name.startswith("AgentSystem-"):
                    logical = self.name.split("AgentSystem-", 1)[1]
                    await publish_status(logical, "Transport disconnected from MCP server", phase=PHASE_END)
            except Exception as e:
                logger.debug("publish_status failed for transport disconnect end: %s", e)
        except Exception as e:
            try:
                await publish_status(self.name, f"Transport disconnect failed: {e}", phase=PHASE_ERROR, meta={"error": str(e)})
                if isinstance(self.name, str) and self.name.startswith("AgentSystem-"):
                    logical = self.name.split("AgentSystem-", 1)[1]
                    await publish_status(logical, f"Transport disconnect failed: {e}", phase=PHASE_ERROR, meta={"error": str(e)})
            except Exception as pub_e:
                logger.debug("publish_status failed for transport disconnect error: %s", pub_e)
            raise

    async def initialize(self) -> Dict[str, Any]:
        """Initialize connection and get server capabilities"""
        logger.info(f"Initializing MCP connection for {self.name}")
        try:
            await publish_status(self.name, "Initializing MCP connection", phase=PHASE_START)
        except Exception:
            logger.debug("publish_status failed for initialize start")

        # Prepare initialize params
        params = {
            "protocolVersion": "2024-11-05",
            "capabilities": {
                "tools": {}
            },
            "clientInfo": {
                "name": self.name,
                "version": "1.0.0"
            }
        }

        # Include initialization options (may be empty) - some servers expect the key
        params["initializationOptions"] = self.initialization_options or {}

        # Send initialize request
        request = MCPMessage(
            jsonrpc="2.0",
            id=self._next_request_id(),
            method="initialize",
            params=params
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
        try:
            try:
                await publish_status(self.name, "MCP initialization completed", phase=PHASE_END)
                if isinstance(self.name, str) and self.name.startswith("AgentSystem-"):
                    try:
                        logical = self.name.split("AgentSystem-", 1)[1]
                        await publish_status(logical, "MCP initialization completed", phase=PHASE_END)
                    except Exception:
                        pass
            except Exception:
                logger.debug("publish_status failed for initialize end")
        except Exception:
            pass
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

        # Get logical server name for cleaner status messages
        logical_server = self.name
        if isinstance(self.name, str) and self.name.startswith("AgentSystem-"):
            logical_server = self.name.split("AgentSystem-", 1)[1]

        # Publish status for tool call start
        try:
            await publish_status(
                logical_server,
                f"Starting tool call: {name}",
                phase=PHASE_START,
                meta={
                    "tool": name,
                    "server": logical_server,
                    "arguments": {k: str(v)[:50] + "..." if len(str(v)) > 50 else str(v) for k, v in arguments.items()} if arguments else {}
                }
            )
        except Exception:
            logger.debug("publish_status failed for tool call start")

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
            # Publish status for tool call error
            try:
                await publish_status(
                    logical_server,
                    f"Tool call failed: {name}",
                    phase=PHASE_ERROR,
                    meta={
                        "tool": name,
                        "server": logical_server,
                        "error": response.error.message,
                        "error_code": getattr(response.error, 'code', None)
                    }
                )
            except Exception:
                logger.debug("publish_status failed for tool call error")
            raise Exception(f"Tool call failed: {response.error.message}")

        # Extract content from MCP response format
        result = response.result
        if "content" in result:
            content_items = result["content"] or []
            # return the first text item found in order
            for item in content_items:
                if isinstance(item, dict) and item.get("type") == "text":
                    text_result = item.get("text", "")
                    # Publish status for successful tool call
                    try:
                        await publish_status(
                            logical_server,
                            f"Tool call completed: {name}",
                            phase=PHASE_END,
                            meta={
                                "tool": name,
                                "server": logical_server,
                                "result_length": len(text_result),
                                "result_preview": text_result[:100] + "..." if len(text_result) > 100 else text_result
                            }
                        )
                    except Exception:
                        logger.debug("publish_status failed for tool call end")
                    return text_result

        # Publish status for successful tool call
        try:
            await publish_status(
                logical_server,
                f"Tool call completed: {name}",
                phase=PHASE_END,
                meta={
                    "tool": name,
                    "server": logical_server,
                    "result_type": type(result).__name__
                }
            )
        except Exception:
            logger.debug("publish_status failed for tool call end")

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
        ssl_verify: bool = True,
        initialization_options: Optional[Dict[str, Any]] = None
    ) -> StandardMCPClient:
        """Create an HTTP-based MCP client"""
        transport = HTTPTransport(
            base_url=base_url,
            timeout=timeout,
            ssl_verify=ssl_verify
        )

        client = StandardMCPClient(transport, client_name, initialization_options)
        try:
            await client.connect()
            await client.initialize()
            return client
        except Exception:
            try:
                await client.disconnect()
            except Exception:
                pass
            raise

    @staticmethod
    async def create_streaming_client(
        base_url: str,
        client_name: str = "AgentSystem",
        timeout: float = 30.0,
        ssl_verify: bool = True,
        initialization_options: Optional[Dict[str, Any]] = None
    ) -> StandardMCPClient:
        """Create a streaming MCP client"""
        transport = HTTPStreamingTransport(
            base_url=base_url,
            config=initialization_options or {},
            timeout=timeout,
            ssl_verify=ssl_verify
        )

        client = StandardMCPClient(transport, client_name, initialization_options)
        try:
            await client.connect()
            await client.initialize()
            return client
        except Exception:
            try:
                await client.disconnect()
            except Exception:
                pass
            raise

    @staticmethod
    async def create_client_from_config(config: Dict[str, Any]) -> StandardMCPClient:
        """Create an MCP client from configuration"""
        transport_type = config.get("transport", "http")

        # Handle deprecated transport type names
        if transport_type == "smithery":
            transport_type = "streaming"
            logger.warning("Transport type 'smithery' is deprecated. Use 'http' for new configurations.")

        if transport_type == "http":
            # Support both 'url' and 'base_url' for compatibility
            base_url = config.get("base_url") or config.get("url")
            if not base_url:
                raise ValueError("Missing 'url' or 'base_url' in client configuration")

            return await MCPClientFactory.create_http_client(
                base_url=base_url,
                client_name=config.get("client_name", "AgentSystem"),
                timeout=config.get("timeout", 30.0),
                ssl_verify=config.get("ssl_verify", True),
                initialization_options=config.get("initialization_options")
            )
        elif transport_type == "streaming":
            # Support both 'url' and 'base_url' for compatibility
            base_url = config.get("base_url") or config.get("url")
            if not base_url:
                raise ValueError("Missing 'url' or 'base_url' in client configuration")

            return await MCPClientFactory.create_streaming_client(
                base_url=base_url,
                client_name=config.get("client_name", "AgentSystem"),
                timeout=config.get("timeout", 30.0),
                ssl_verify=config.get("ssl_verify", True),
                initialization_options=config.get("initialization_options")
            )
        else:
            raise ValueError(f"Unsupported transport type: {transport_type}")


class MCPClientManager:
    """Manager for multiple MCP clients"""

    def __init__(self):
        self.clients: Dict[str, StandardMCPClient] = {}
        # Tool list caching to reduce external server queries
        self._tools_cache: Optional[Dict[str, List[MCPTool]]] = None
        self._tools_cache_time = 0.0
        self._tools_cache_ttl = 30.0  # Cache for 30 seconds

    async def add_client(self, name: str, config: Dict[str, Any]) -> None:
        """Add an MCP client from configuration"""
        try:
            # Announce connection attempt
            try:
                await publish_status(name, "Connecting to external MCP server", phase=PHASE_START)
            except Exception:
                logger.debug("publish_status failed for start event")
            # If a client with this name already exists, disconnect it first
            if name in self.clients:
                try:
                    await self.clients[name].disconnect()
                except Exception:
                    logger.debug(f"Failed to disconnect existing client {name} before replacing")
                try:
                    del self.clients[name]
                except Exception:
                    pass

            client = await MCPClientFactory.create_client_from_config(config)
            self.clients[name] = client
            logger.info(f"Added MCP client: {name}")
            try:
                await publish_status(name, "Connected to external MCP server", phase=PHASE_END)
            except Exception:
                logger.debug("publish_status failed for connected event")
        except Exception as e:
            logger.debug(f"Failed to add MCP client {name}: {e}")
            try:
                await publish_status(name, f"Failed to connect to external MCP server: {e}", phase=PHASE_ERROR, meta={"error": str(e)})
            except Exception:
                logger.debug("publish_status failed for error event")
            raise

    async def remove_client(self, name: str) -> None:
        """Remove an MCP client"""
        if name in self.clients:
            try:
                await publish_status(name, "Disconnecting external MCP client", phase=PHASE_START)
            except Exception:
                logger.debug("publish_status failed for disconnect start")
            await self.clients[name].disconnect()
            del self.clients[name]
            logger.info(f"Removed MCP client: {name}")
            try:
                await publish_status(name, "Disconnected external MCP client", phase=PHASE_END)
            except Exception:
                logger.debug("publish_status failed for disconnect end")

    def get_client(self, name: str) -> Optional[StandardMCPClient]:
        """Get an MCP client by name"""
        return self.clients.get(name)

    def list_clients(self) -> List[str]:
        """List all client names"""
        return list(self.clients.keys())

    async def list_all_tools(self) -> Dict[str, List[MCPTool]]:
        """List tools from all clients"""
        import time

        # Check cache validity
        now = time.time()
        if (self._tools_cache is not None and
            (now - self._tools_cache_time) < self._tools_cache_ttl):
            logger.debug("Returning cached MCP tools list (age: %.1fs)", now - self._tools_cache_time)
            return self._tools_cache

        logger.debug("Refreshing MCP tools cache...")

        all_tools = {}
        for name, client in self.clients.items():
            try:
                tools = await client.list_tools()
                all_tools[name] = tools
            except Exception as e:
                logger.error(f"Failed to list tools from client {name}: {e}")
                all_tools[name] = []

        # Update cache
        self._tools_cache = all_tools
        self._tools_cache_time = now
        logger.debug("MCP tools cache updated")

        return all_tools

    def invalidate_tools_cache(self) -> None:
        """Invalidate the tools cache"""
        self._tools_cache = None
        self._tools_cache_time = 0.0
        logger.debug("MCP tools cache invalidated")

    def set_cache_ttl(self, ttl_seconds: float) -> None:
        """Set the cache TTL for tool listings"""
        self._tools_cache_ttl = ttl_seconds
        logger.debug(f"MCP client manager cache TTL set to {ttl_seconds}s")

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