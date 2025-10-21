"""Example MCP Server implementation.

This module demonstrates the MODERN way to implement an MCP server
using SchemaBasedMCPServer with automatic tool dispatching.

Key Pattern (NO manual call() override needed):
1. Inherit from SchemaBasedMCPServer
2. Define tools in schema.yaml with names like: "{{ name }}_toolname"
3. Implement async methods matching EXACT tool names from schema
4. MCPServer.call() automatically routes to your methods

For a plugin named "example", with tools in schema.yaml:
- "example_calculator" → auto-routes to self.example_calculator(params)
- "example_formatter" → auto-routes to self.example_formatter(params)
- "example_status" → auto-routes to self.example_status(params)
"""

from __future__ import annotations

import logging
from typing import Any, TYPE_CHECKING
from decimal import Decimal, InvalidOperation
from agent_system.mcp.schema_based import SchemaBasedMCPServer

if TYPE_CHECKING:
    from agent_system.config.models import AgentSystemConfig, MCPConfig

logger = logging.getLogger(__name__)


class ExampleServer(SchemaBasedMCPServer):
    """Example MCP server showing the modern way to implement plugins.

    This server demonstrates:
    - Schema-based tool definitions (tools defined in schema.yaml)
    - Generic dispatcher (no manual call() override needed)
    - Tool methods matching tool names (automatic routing)
    - Configuration-driven behavior
    - Clean error handling and input validation
    - Proper logging
    
    All tools are automatically loaded from schema.yaml by SchemaBasedMCPServer.
    Tool calls are automatically routed to methods by MCPServer.call().
    """

    def __init__(self, name: str, system_config: AgentSystemConfig, mcp_config: MCPConfig):
        """Initialize the example server.
        
        Args:
            name: Plugin instance name
            system_config: System-wide configuration
            mcp_config: Plugin-specific configuration
        """
        super().__init__(name, system_config, mcp_config)
        
        # Extract plugin-specific configuration with sensible defaults
        # mcp_config contains the plugin's specific settings
        self.precision = int(getattr(mcp_config, "precision", 2))
        self.max_text_length = int(getattr(mcp_config, "max_text_length", 1000))
        
        logger.info(f"Example server '{name}' initialized with precision={self.precision}, max_length={self.max_text_length}")

    # Tool methods - these are automatically called by SchemaBasedMixin.call() dispatcher
    # Method names should match tool names WITHOUT the "{name}_" prefix
    # Tool "example_calculator" → method "calculator()"
    
    async def calculator(self, params: dict[str, Any]) -> dict[str, Any]:
        """Handle calculator operations with high precision.
        
        This method is automatically called when the "example_calculator" tool is invoked.
        The generic dispatcher in SchemaBasedMixin strips the "example_" prefix
        and routes to this method.
        
        Args:
            params: Parameters containing operation, a, and b
            
        Returns:
            Dict with operation details and result
            
        Raises:
            ValueError: For invalid operations or division by zero
            TypeError: For invalid parameter types
        """
        # Validate required parameters
        if not all(key in params for key in ["operation", "a", "b"]):
            raise ValueError("Missing required parameters: operation, a, b")
        
        operation = params["operation"]
        
        # Validate operation
        valid_operations = ["add", "subtract", "multiply", "divide"]
        if operation not in valid_operations:
            raise ValueError(f"Invalid operation '{operation}'. Valid: {valid_operations}")
        
        # Convert and validate numbers
        try:
            a = Decimal(str(params["a"]))
            b = Decimal(str(params["b"]))
        except (InvalidOperation, TypeError) as e:
            raise TypeError(f"Invalid number format: {e}")
        
        # Perform calculation
        if operation == "add":
            result = a + b
        elif operation == "subtract":
            result = a - b
        elif operation == "multiply":
            result = a * b
        elif operation == "divide":
            if b == 0:
                raise ValueError("Division by zero is not allowed")
            result = a / b
        
        # Format result with configured precision
        result_float = float(result.quantize(Decimal(10) ** -self.precision))
        
        response = {
            "operation": operation,
            "operands": [float(a), float(b)],
            "result": result_float,
            "precision": self.precision
        }
        
        logger.debug(f"Calculator result: {response}")
        return response

    async def formatter(self, params: dict[str, Any]) -> dict[str, Any]:
        """Handle text formatting operations.
        
        This method is automatically called when the "example_formatter" tool is invoked.
        
        Args:
            params: Parameters containing text and format
            
        Returns:
            Dict with original text, format applied, and result
            
        Raises:
            ValueError: For invalid format types or text too long
        """
        # Validate required parameters
        if not all(key in params for key in ["text", "format"]):
            raise ValueError("Missing required parameters: text, format")
        
        text = params["text"]
        format_type = params["format"]
        
        # Validate text type and length
        if not isinstance(text, str):
            raise TypeError("Text parameter must be a string")
        
        if len(text) > self.max_text_length:
            raise ValueError(f"Text length {len(text)} exceeds maximum {self.max_text_length}")
        
        # Validate format type
        valid_formats = ["uppercase", "lowercase", "title", "reverse"]
        if format_type not in valid_formats:
            raise ValueError(f"Invalid format '{format_type}'. Valid: {valid_formats}")
        
        # Apply formatting
        if format_type == "uppercase":
            formatted = text.upper()
        elif format_type == "lowercase":
            formatted = text.lower()
        elif format_type == "title":
            formatted = text.title()
        elif format_type == "reverse":
            formatted = text[::-1]
        
        response = {
            "original": text,
            "format": format_type,
            "formatted": formatted,
            "length": len(formatted)
        }
        
        logger.debug(f"Formatter result: {len(text)} chars -> {len(formatted)} chars")
        return response

    async def status(self, params: dict[str, Any]) -> dict[str, Any]:
        """Handle status information requests.
        
        This method is automatically called when the "example_status" tool is invoked.
        
        Args:
            params: Parameters containing optional verbose flag
            
        Returns:
            Dict with server status and configuration
        """
        verbose = params.get("verbose", False)
        
        tools = self.get_tools()
        status = {
            "server_name": self.name,
            "status": "active", 
            "tools_count": len(tools),
            "version": "1.0.0"
        }
        
        if verbose:
            status.update({
                "config": {
                    "precision": self.precision,
                    "max_text_length": self.max_text_length
                },
                "available_tools": [tool["function"]["name"] for tool in tools]
            })
        
        logger.debug(f"Status request (verbose={verbose}): {len(status)} fields")
        return status