"""
MCP (Model Context Protocol) Core Interfaces

This module defines the core interfaces for MCP compatibility based on Anthropic's MCP standard.
MCP uses JSON-RPC 2.0 for communication between clients and servers.

Key concepts:
- Clients: Applications that consume MCP services
- Servers: Services that expose tools, resources, and prompts
- Tools: Functions that can be called by the AI model
- Resources: Data sources that provide context
- Prompts: Template prompts for specific use cases
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import Enum
from typing import Any, Dict, List, Optional, Union
import logging

logger = logging.getLogger(__name__)


class MCPMessageType(Enum):
    """MCP message types according to JSON-RPC 2.0"""
    REQUEST = "request"
    RESPONSE = "response"
    NOTIFICATION = "notification"
    ERROR = "error"


class MCPCapability(Enum):
    """MCP server capabilities"""
    TOOLS = "tools"
    RESOURCES = "resources"
    PROMPTS = "prompts"


@dataclass
class MCPError:
    """MCP error following JSON-RPC 2.0 standard"""
    code: int
    message: str
    data: Optional[Dict[str, Any]] = None


@dataclass
class MCPTool:
    """MCP Tool definition"""
    name: str
    description: str
    input_schema: Dict[str, Any]  # JSON Schema for input validation


@dataclass
class MCPResource:
    """MCP Resource definition"""
    uri: str
    name: str
    description: Optional[str] = None
    mime_type: Optional[str] = None


@dataclass
class MCPPrompt:
    """MCP Prompt template definition"""
    name: str
    description: str
    arguments: Optional[List[Dict[str, Any]]] = None


@dataclass
class MCPMessage:
    """Base MCP message following JSON-RPC 2.0"""
    jsonrpc: str = "2.0"
    id: Optional[Union[str, int]] = None
    method: Optional[str] = None
    params: Optional[Dict[str, Any]] = None
    result: Optional[Any] = None
    error: Optional[MCPError] = None


class MCPTransport(ABC):
    """Abstract base for MCP transport layers"""

    @abstractmethod
    async def send_message(self, message: MCPMessage) -> None:
        """Send a message to the remote endpoint"""
        pass

    @abstractmethod
    async def receive_message(self) -> MCPMessage:
        """Receive a message from the remote endpoint"""
        pass

    @abstractmethod
    async def connect(self) -> None:
        """Establish connection"""
        pass

    @abstractmethod
    async def disconnect(self) -> None:
        """Close connection"""
        pass


class MCPServer(ABC):
    """Abstract MCP Server interface"""

    def __init__(self, name: str, description: str = ""):
        self.name = name
        self.description = description
        self.capabilities: List[MCPCapability] = []

    @abstractmethod
    async def list_tools(self) -> List[MCPTool]:
        """Return available tools"""
        pass

    @abstractmethod
    async def call_tool(self, name: str, arguments: Dict[str, Any]) -> Any:
        """Execute a tool with given arguments"""
        pass

    async def list_resources(self) -> List[MCPResource]:
        """Return available resources (optional)"""
        return []

    async def read_resource(self, uri: str) -> Any:
        """Read a resource by URI (optional)"""
        raise NotImplementedError("Resources not supported")

    async def list_prompts(self) -> List[MCPPrompt]:
        """Return available prompts (optional)"""
        return []

    async def get_prompt(self, name: str, arguments: Optional[Dict[str, Any]] = None) -> str:
        """Get a prompt by name with optional arguments (optional)"""
        raise NotImplementedError("Prompts not supported")


class MCPClient(ABC):
    """Abstract MCP Client interface"""

    def __init__(self, transport: MCPTransport):
        self.transport = transport
        self._request_id = 0

    def _next_request_id(self) -> int:
        """Generate next request ID"""
        self._request_id += 1
        return self._request_id

    @abstractmethod
    async def connect(self) -> None:
        """Connect to MCP server"""
        pass

    @abstractmethod
    async def disconnect(self) -> None:
        """Disconnect from MCP server"""
        pass

    @abstractmethod
    async def initialize(self) -> Dict[str, Any]:
        """Initialize connection and get server capabilities"""
        pass

    @abstractmethod
    async def list_tools(self) -> List[MCPTool]:
        """List available tools from server"""
        pass

    @abstractmethod
    async def call_tool(self, name: str, arguments: Dict[str, Any]) -> Any:
        """Call a tool on the server"""
        pass