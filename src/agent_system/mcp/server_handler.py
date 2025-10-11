"""
MCP Server Mode Handler

Implements MCP JSON-RPC 2.0 server protocol for exposing AgentSystem plugins
as remote MCP servers. Handles incoming MCP requests and routes them to
appropriate plugins.

This module enables AgentSystem to act as an MCP server, allowing external
MCP clients to discover and use AgentSystem plugins remotely via HTTP.

Supported MCP Methods:
- initialize: Initialize MCP session and return capabilities
- tools/list: List all tools from enabled plugins
- tools/call: Execute tool from plugin
- resources/list: List available resources (future)
- resources/read: Read resource content (future)
- prompts/list: List available prompt templates (future)
- prompts/get: Get prompt template (future)

Protocol Compliance:
- JSON-RPC 2.0 over HTTP POST
- Session management via Mcp-Session-Id header
- Content-Type negotiation (application/json or text/event-stream)
- SSE streaming for long-running operations

Rate Limiting:
- Per-session rate limiting (requests per minute/hour)
- Configurable burst size
- Token bucket algorithm for smooth rate limiting
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from typing import Any, Dict, List, Optional

from fastapi import Request, Response
from fastapi.responses import StreamingResponse, JSONResponse

from ..config.models import AgentSystemConfig
from ..mcp.base import MCPRegistry

logger = logging.getLogger(__name__)


class MCPServerSession:
    """Represents an MCP server session with a remote client."""
    
    def __init__(self, session_id: str, client_info: Dict[str, Any]):
        self.session_id = session_id
        self.client_info = client_info
        self.created_at = time.time()
        self.last_activity = time.time()
        self.capabilities: Dict[str, Any] = {}
        
        # Rate limiting state (token bucket algorithm)
        # Note: Start with burst_size tokens via first check_rate_limit call
        self.rate_limit_tokens = 0.0  # Will be initialized on first check
        self.rate_limit_last_refill = time.time()  # Last refill timestamp
        self.rate_limit_initialized = False  # Track if bucket has been initialized
        self.request_count_minute = 0  # Requests in last minute
        self.request_count_hour = 0  # Requests in last hour
        self.minute_window_start = time.time()
        self.hour_window_start = time.time()
        
    def update_activity(self):
        """Update last activity timestamp."""
        self.last_activity = time.time()
        
    def check_rate_limit(
        self,
        requests_per_minute: int,
        requests_per_hour: int,
        burst_size: int
    ) -> tuple[bool, Optional[str]]:
        """
        Check if request is within rate limits.
        
        Returns:
            (allowed, reason) - allowed is True if within limits, reason explains denial
        """
        current_time = time.time()
        
        # Initialize token bucket on first call with full burst capacity
        if not self.rate_limit_initialized:
            self.rate_limit_tokens = float(burst_size)
            self.rate_limit_initialized = True
        
        # Reset minute window if needed
        if current_time - self.minute_window_start >= 60:
            self.request_count_minute = 0
            self.minute_window_start = current_time
        
        # Reset hour window if needed
        if current_time - self.hour_window_start >= 3600:
            self.request_count_hour = 0
            self.hour_window_start = current_time
        
        # Check minute limit
        if self.request_count_minute >= requests_per_minute:
            return False, f"Rate limit exceeded: {requests_per_minute} requests/minute"
        
        # Check hour limit
        if self.request_count_hour >= requests_per_hour:
            return False, f"Rate limit exceeded: {requests_per_hour} requests/hour"
        
        # Token bucket for burst control
        # Refill tokens at rate of requests_per_minute/60 per second
        time_since_refill = current_time - self.rate_limit_last_refill
        refill_rate = requests_per_minute / 60.0  # tokens per second
        tokens_to_add = time_since_refill * refill_rate
        self.rate_limit_tokens = min(burst_size, self.rate_limit_tokens + tokens_to_add)
        self.rate_limit_last_refill = current_time
        
        # Check if we have tokens available
        if self.rate_limit_tokens < 1.0:
            return False, f"Rate limit exceeded: burst limit {burst_size}"
        
        # Consume one token
        self.rate_limit_tokens -= 1.0
        self.request_count_minute += 1
        self.request_count_hour += 1
        
        return True, None
        
    def to_dict(self) -> Dict[str, Any]:
        """Convert session to dictionary."""
        return {
            "session_id": self.session_id,
            "client_info": self.client_info,
            "created_at": self.created_at,
            "last_activity": self.last_activity,
            "age_seconds": time.time() - self.created_at,
            "request_count_minute": self.request_count_minute,
            "request_count_hour": self.request_count_hour,
        }


class MCPServerHandler:
    """Handles MCP JSON-RPC 2.0 server protocol requests."""
    
    def __init__(self, config: AgentSystemConfig, registry: MCPRegistry):
        self.config = config
        self.registry = registry
        self._sessions: Dict[str, MCPServerSession] = {}
        self._session_ttl = 3600.0  # 1 hour session timeout
        self._request_counter = 0
        
    async def handle_request(self, request: Request) -> Response:
        """
        Handle incoming MCP JSON-RPC 2.0 request.
        
        Args:
            request: FastAPI request object
            
        Returns:
            JSON or SSE StreamingResponse based on Content-Type
        """
        try:
            # Parse JSON-RPC 2.0 request
            payload = await request.json()
            
            # Validate JSON-RPC format
            if not self._validate_jsonrpc_request(payload):
                return self._error_response(
                    -32600,
                    "Invalid Request",
                    "Request must be valid JSON-RPC 2.0"
                )
            
            method = payload.get("method")
            params = payload.get("params", {})
            request_id = payload.get("id")
            
            # Check for session ID in header
            session_id = request.headers.get("Mcp-Session-Id")
            
            # Clean up expired sessions
            await self._cleanup_expired_sessions()
            
            # Check rate limit for existing sessions (not for initialize)
            if session_id and method != "initialize":
                session = self._sessions.get(session_id)
                if session:
                    # Apply rate limiting
                    rate_config = self.config.server_mode.rate_limit
                    if rate_config.enabled:
                        allowed, reason = session.check_rate_limit(
                            rate_config.requests_per_minute,
                            rate_config.requests_per_hour,
                            rate_config.burst_size
                        )
                        if not allowed:
                            logger.warning(f"Rate limit exceeded for session {session_id}: {reason}")
                            return self._error_response(
                                -32000,  # Server error
                                "Rate limit exceeded",
                                reason
                            )
            
            # Dispatch to method handler
            logger.debug(f"MCP request: method={method}, id={request_id}, session={session_id}")
            
            result = await self._dispatch_method(method, params, session_id)
            
            # Create response
            response_data = {
                "jsonrpc": "2.0",
                "id": request_id,
                "result": result
            }
            
            # For initialize, include session ID in header
            headers = {}
            if method == "initialize" and "session_id" in result:
                headers["Mcp-Session-Id"] = result["session_id"]
            
            # Check if client accepts SSE
            accept_header = request.headers.get("Accept", "")
            if "text/event-stream" in accept_header and method == "tools/call":
                # Return SSE stream for long-running tool calls
                return await self._sse_response(response_data, headers)
            
            # Return JSON response
            return JSONResponse(content=response_data, headers=headers)
            
        except json.JSONDecodeError:
            return self._error_response(-32700, "Parse error", "Invalid JSON")
        except Exception as e:
            logger.exception(f"MCP server error: {e}")
            return self._error_response(-32603, "Internal error", str(e))
    
    def _validate_jsonrpc_request(self, payload: Dict[str, Any]) -> bool:
        """Validate JSON-RPC 2.0 request format."""
        if not isinstance(payload, dict):
            return False
        if payload.get("jsonrpc") != "2.0":
            return False
        if "method" not in payload:
            return False
        return True
    
    def _error_response(self, code: int, message: str, data: Any = None) -> JSONResponse:
        """Create JSON-RPC 2.0 error response."""
        error_data = {
            "jsonrpc": "2.0",
            "id": None,
            "error": {
                "code": code,
                "message": message,
            }
        }
        if data is not None:
            error_data["error"]["data"] = data
        return JSONResponse(content=error_data, status_code=400)
    
    async def _dispatch_method(
        self,
        method: str,
        params: Dict[str, Any],
        session_id: Optional[str]
    ) -> Dict[str, Any]:
        """Dispatch MCP method to appropriate handler."""
        if method == "initialize":
            return await self._handle_initialize(params)
        elif method == "tools/list":
            return await self._handle_tools_list(params, session_id)
        elif method == "tools/call":
            return await self._handle_tools_call(params, session_id)
        elif method == "resources/list":
            return await self._handle_resources_list(params, session_id)
        elif method == "resources/read":
            return await self._handle_resources_read(params, session_id)
        elif method == "prompts/list":
            return await self._handle_prompts_list(params, session_id)
        elif method == "prompts/get":
            return await self._handle_prompts_get(params, session_id)
        else:
            raise Exception(f"Method not found: {method}")
    
    async def _handle_initialize(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Handle initialize request - create session and return capabilities."""
        client_info = params.get("clientInfo", {})
        
        # Create new session
        session_id = str(uuid.uuid4())
        session = MCPServerSession(session_id, client_info)
        self._sessions[session_id] = session
        
        logger.info(f"MCP client connected: {client_info.get('name', 'unknown')} (session={session_id})")
        
        # Return server capabilities
        return {
            "session_id": session_id,
            "protocolVersion": "2024-11-05",
            "capabilities": {
                "tools": {"listChanged": False},
                "resources": {"listChanged": False, "subscribe": False},
                "prompts": {"listChanged": False}
            },
            "serverInfo": {
                "name": self.config.name or "AgentSystem MCP Server",
                "version": self.config.version or "1.0.0"
            }
        }
    
    async def _handle_tools_list(
        self,
        params: Dict[str, Any],
        session_id: Optional[str]
    ) -> Dict[str, Any]:
        """Handle tools/list request - list all tools from enabled plugins."""
        if session_id and session_id in self._sessions:
            self._sessions[session_id].update_activity()
        
        all_tools: List[Dict[str, Any]] = []
        
        # Get exposed plugins from configuration
        exposed_plugins = self._get_exposed_plugins()
        
        # Iterate through enabled plugins
        for server_name in exposed_plugins:
            try:
                server = self.registry.get(server_name)
                if not server:
                    continue
                
                # Get tools from server
                if hasattr(server, 'list_tools'):
                    tools = await server.list_tools()
                    for tool in tools:
                        # Add tool with plugin namespace prefix
                        # Use double underscore instead of slash (OpenAI requires ^[a-zA-Z0-9_-]+$)
                        tool_dict = {
                            "name": f"{server_name}__{tool.name}",
                            "description": f"[{server_name}] {tool.description}",
                            "inputSchema": tool.input_schema
                        }
                        all_tools.append(tool_dict)
                        
            except Exception as e:
                logger.error(f"Error listing tools from {server_name}: {e}")
                continue
        
        logger.debug(f"Listed {len(all_tools)} tools from {len(exposed_plugins)} plugins")
        
        return {"tools": all_tools}
    
    async def _handle_tools_call(
        self,
        params: Dict[str, Any],
        session_id: Optional[str]
    ) -> Dict[str, Any]:
        """Handle tools/call request - execute tool from plugin."""
        if session_id and session_id in self._sessions:
            self._sessions[session_id].update_activity()
        
        tool_name = params.get("name")
        arguments = params.get("arguments", {})
        
        if not tool_name:
            raise Exception("Tool name is required")
        
        # Parse plugin name from tool name (format: plugin_name__tool_name)
        # Use double underscore for OpenAI compatibility (requires ^[a-zA-Z0-9_-]+$)
        if "__" not in tool_name:
            # Fallback: try single underscore for backward compatibility
            if "_" in tool_name:
                parts = tool_name.split("_", 1)
                plugin_name = parts[0]
                actual_tool_name = parts[1]
            else:
                raise Exception(
                    f"Invalid tool name format: {tool_name}. "
                    f"Expected: plugin_name__tool_name or plugin_name_tool_name"
                )
        else:
            plugin_name, actual_tool_name = tool_name.split("__", 1)
        
        # Verify plugin is exposed
        exposed_plugins = self._get_exposed_plugins()
        if plugin_name not in exposed_plugins:
            raise Exception(f"Plugin not exposed: {plugin_name}")
        
        # Get server from registry
        server = self.registry.get(plugin_name)
        
        if not server:
            raise Exception(f"Plugin not found: {plugin_name}")
        
        # Call tool
        logger.info(f"Executing tool: {tool_name} with args: {arguments}")
        
        try:
            if hasattr(server, 'call_tool'):
                result = await server.call_tool(actual_tool_name, arguments)
            elif hasattr(server, 'call'):
                result = await server.call(actual_tool_name, arguments)
            else:
                raise Exception(f"Plugin {plugin_name} does not support tool calls")
            
            # Format result
            return {
                "content": [
                    {
                        "type": "text",
                        "text": json.dumps(result, ensure_ascii=False, indent=2) if not isinstance(result, str) else result
                    }
                ]
            }
            
        except Exception as e:
            logger.exception(f"Tool execution failed: {tool_name}")
            raise Exception(f"Tool execution failed: {str(e)}")
    
    async def _handle_resources_list(
        self,
        params: Dict[str, Any],
        session_id: Optional[str]
    ) -> Dict[str, Any]:
        """Handle resources/list request - list available resources."""
        # TODO: Implement resource listing from plugins
        return {"resources": []}
    
    async def _handle_resources_read(
        self,
        params: Dict[str, Any],
        session_id: Optional[str]
    ) -> Dict[str, Any]:
        """Handle resources/read request - read resource content."""
        # TODO: Implement resource reading from plugins
        raise Exception("Resources not yet implemented")
    
    async def _handle_prompts_list(
        self,
        params: Dict[str, Any],
        session_id: Optional[str]
    ) -> Dict[str, Any]:
        """Handle prompts/list request - list available prompts."""
        # TODO: Implement prompt listing from plugins
        return {"prompts": []}
    
    async def _handle_prompts_get(
        self,
        params: Dict[str, Any],
        session_id: Optional[str]
    ) -> Dict[str, Any]:
        """Handle prompts/get request - get prompt template."""
        # TODO: Implement prompt retrieval from plugins
        raise Exception("Prompts not yet implemented")
    
    def _get_exposed_plugins(self) -> List[str]:
        """Get list of plugins to expose as MCP tools."""
        # Check configuration for exposed plugins
        server_config = self.config.server_mode
        
        if not server_config or not getattr(server_config, 'enabled', False):
            return []
        
        exposed = getattr(server_config, 'expose_plugins', ['*'])
        
        # If '*' expose all enabled plugins
        if '*' in exposed:
            return list(self.registry.list())
        
        return exposed
    
    async def _cleanup_expired_sessions(self):
        """Remove expired sessions."""
        now = time.time()
        expired = [
            sid for sid, session in self._sessions.items()
            if now - session.last_activity > self._session_ttl
        ]
        
        for sid in expired:
            logger.debug(f"Removing expired MCP session: {sid}")
            del self._sessions[sid]
    
    async def _sse_response(
        self,
        response_data: Dict[str, Any],
        headers: Dict[str, str]
    ) -> StreamingResponse:
        """Create SSE streaming response."""
        async def event_generator():
            # Send initial response as SSE event
            yield f"data: {json.dumps(response_data, ensure_ascii=False)}\n\n"
        
        return StreamingResponse(
            event_generator(),
            media_type="text/event-stream",
            headers={
                **headers,
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no"
            }
        )
    
    def get_session_stats(self) -> Dict[str, Any]:
        """Get statistics about active sessions."""
        return {
            "active_sessions": len(self._sessions),
            "sessions": [session.to_dict() for session in self._sessions.values()]
        }
