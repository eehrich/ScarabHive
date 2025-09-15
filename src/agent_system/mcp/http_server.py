"""
HTTP Server for MCP using JSON-RPC 2.0

Implements HTTP server to expose our plugins as MCP-compatible services.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, Optional
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

from .core import MCPServer, MCPTool, MCPError, MCPCapability

logger = logging.getLogger(__name__)


class MCPHTTPServer:
    """HTTP server that exposes MCP servers over JSON-RPC 2.0"""

    def __init__(self, app: Optional[FastAPI] = None):
        self.app = app or FastAPI()
        self.servers: Dict[str, MCPServer] = {}
        self._setup_routes()

    def _setup_routes(self) -> None:
        """Setup MCP routes"""
        self.app.post("/mcp")(self._handle_mcp_request)
        self.app.get("/mcp/servers")(self._list_servers)
        self.app.get("/mcp/servers/{server_name}/tools")(self._list_server_tools)

    def register_server(self, name: str, server: MCPServer) -> None:
        """Register an MCP server"""
        self.servers[name] = server
        logger.info(f"Registered MCP server: {name}")

    def unregister_server(self, name: str) -> None:
        """Unregister an MCP server"""
        if name in self.servers:
            del self.servers[name]
            logger.info(f"Unregistered MCP server: {name}")

    async def _list_servers(self) -> JSONResponse:
        """List available MCP servers"""
        servers = []
        for name, server in self.servers.items():
            servers.append({
                "name": name,
                "description": server.description,
                "capabilities": [cap.value for cap in server.capabilities]
            })
        return JSONResponse({"servers": servers})

    async def _list_server_tools(self, server_name: str) -> JSONResponse:
        """List tools for a specific server"""
        if server_name not in self.servers:
            raise HTTPException(status_code=404, detail=f"Server {server_name} not found")

        server = self.servers[server_name]
        tools = await server.list_tools()

        return JSONResponse({
            "tools": [
                {
                    "name": tool.name,
                    "description": tool.description,
                    "input_schema": tool.input_schema
                }
                for tool in tools
            ]
        })

    async def _handle_mcp_request(self, request: Request) -> JSONResponse:
        """Handle MCP JSON-RPC 2.0 requests"""
        try:
            payload = await request.json()
        except json.JSONDecodeError as e:
            return self._error_response(
                None,
                MCPError(code=-32700, message="Parse error", data={"details": str(e)})
            )

        # Validate JSON-RPC 2.0 format
        if payload.get("jsonrpc") != "2.0":
            return self._error_response(
                payload.get("id"),
                MCPError(code=-32600, message="Invalid request: missing jsonrpc 2.0")
            )

        method = payload.get("method")
        if not method:
            return self._error_response(
                payload.get("id"),
                MCPError(code=-32600, message="Invalid request: missing method")
            )

        request_id = payload.get("id")
        params = payload.get("params", {})

        try:
            result = await self._dispatch_method(method, params)
            return JSONResponse({
                "jsonrpc": "2.0",
                "id": request_id,
                "result": result
            })
        except Exception as e:
            logger.error(f"Method {method} failed: {e}")
            return self._error_response(
                request_id,
                MCPError(code=-32603, message="Internal error", data={"details": str(e)})
            )

    async def _dispatch_method(self, method: str, params: Dict[str, Any]) -> Any:
        """Dispatch MCP method to appropriate handler"""
        if method == "initialize":
            return await self._handle_initialize(params)
        elif method == "tools/list":
            return await self._handle_list_tools(params)
        elif method == "tools/call":
            return await self._handle_call_tool(params)
        elif method == "resources/list":
            return await self._handle_list_resources(params)
        elif method == "resources/read":
            return await self._handle_read_resource(params)
        elif method == "prompts/list":
            return await self._handle_list_prompts(params)
        elif method == "prompts/get":
            return await self._handle_get_prompt(params)
        else:
            raise Exception(f"Unknown method: {method}")

    async def _handle_initialize(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Handle initialize request"""
        client_info = params.get("clientInfo", {})
        logger.info(f"MCP client connected: {client_info}")

        # Return server capabilities
        capabilities = set()
        for server in self.servers.values():
            capabilities.update(server.capabilities)

        return {
            "protocolVersion": "2024-11-05",
            "capabilities": {
                "tools": {"listChanged": False},
                "resources": {"listChanged": False, "subscribe": False},
                "prompts": {"listChanged": False}
            },
            "serverInfo": {
                "name": "AgentSystem MCP Server",
                "version": "1.0.0"
            }
        }

    async def _handle_list_tools(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Handle tools/list request"""
        all_tools = []

        for server_name, server in self.servers.items():
            if MCPCapability.TOOLS in server.capabilities:
                tools = await server.list_tools()
                # Prefix tool names with server name to avoid conflicts
                for tool in tools:
                    prefixed_tool = MCPTool(
                        name=f"{server_name}.{tool.name}",
                        description=f"[{server_name}] {tool.description}",
                        input_schema=tool.input_schema
                    )
                    all_tools.append({
                        "name": prefixed_tool.name,
                        "description": prefixed_tool.description,
                        "inputSchema": prefixed_tool.input_schema
                    })

        return {"tools": all_tools}

    async def _handle_call_tool(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Handle tools/call request"""
        tool_name = params.get("name")
        arguments = params.get("arguments", {})

        if not tool_name:
            raise Exception("Missing tool name")

        # Parse server.tool format
        if "." not in tool_name:
            raise Exception(f"Invalid tool name format: {tool_name} (expected server.tool)")

        server_name, actual_tool_name = tool_name.split(".", 1)

        if server_name not in self.servers:
            raise Exception(f"Unknown server: {server_name}")

        server = self.servers[server_name]
        result = await server.call_tool(actual_tool_name, arguments)

        return {
            "content": [
                {
                    "type": "text",
                    "text": str(result)
                }
            ]
        }

    async def _handle_list_resources(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Handle resources/list request"""
        all_resources = []

        for server_name, server in self.servers.items():
            if MCPCapability.RESOURCES in server.capabilities:
                resources = await server.list_resources()
                for resource in resources:
                    all_resources.append({
                        "uri": resource.uri,
                        "name": resource.name,
                        "description": resource.description,
                        "mimeType": resource.mime_type
                    })

        return {"resources": all_resources}

    async def _handle_read_resource(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Handle resources/read request"""
        uri = params.get("uri")
        if not uri:
            raise Exception("Missing resource URI")

        # Find server that owns this resource
        for server in self.servers.values():
            if MCPCapability.RESOURCES in server.capabilities:
                try:
                    content = await server.read_resource(uri)
                    return {
                        "contents": [
                            {
                                "uri": uri,
                                "mimeType": "text/plain",
                                "text": str(content)
                            }
                        ]
                    }
                except NotImplementedError:
                    continue
                except Exception:
                    continue

        raise Exception(f"Resource not found: {uri}")

    async def _handle_list_prompts(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Handle prompts/list request"""
        all_prompts = []

        for server_name, server in self.servers.items():
            if MCPCapability.PROMPTS in server.capabilities:
                prompts = await server.list_prompts()
                for prompt in prompts:
                    all_prompts.append({
                        "name": f"{server_name}.{prompt.name}",
                        "description": prompt.description,
                        "arguments": prompt.arguments or []
                    })

        return {"prompts": all_prompts}

    async def _handle_get_prompt(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Handle prompts/get request"""
        prompt_name = params.get("name")
        arguments = params.get("arguments", {})

        if not prompt_name:
            raise Exception("Missing prompt name")

        # Parse server.prompt format
        if "." not in prompt_name:
            raise Exception(f"Invalid prompt name format: {prompt_name}")

        server_name, actual_prompt_name = prompt_name.split(".", 1)

        if server_name not in self.servers:
            raise Exception(f"Unknown server: {server_name}")

        server = self.servers[server_name]
        prompt_content = await server.get_prompt(actual_prompt_name, arguments)

        return {
            "description": f"Prompt from {server_name}",
            "messages": [
                {
                    "role": "user",
                    "content": {
                        "type": "text",
                        "text": prompt_content
                    }
                }
            ]
        }

    def _error_response(self, request_id: Optional[str], error: MCPError) -> JSONResponse:
        """Create JSON-RPC 2.0 error response"""
        return JSONResponse({
            "jsonrpc": "2.0",
            "id": request_id,
            "error": {
                "code": error.code,
                "message": error.message,
                "data": error.data
            }
        })