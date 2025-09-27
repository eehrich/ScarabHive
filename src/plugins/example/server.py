"""Example MCP Server implementation.

This module demonstrates the simplest way to implement an MCP server
using SchemaBasedMCPServer. All tools are defined in schema.yaml.
"""

from __future__ import annotations

import logging
from typing import Any
from decimal import Decimal, InvalidOperation
from agent_system.mcp.schema_based import SchemaBasedMCPServer

logger = logging.getLogger(__name__)


class ExampleServer(SchemaBasedMCPServer):
    """Example MCP server showing the modern way to implement plugins.

    This server demonstrates:
    - Schema-based tool definitions (tools defined in schema.yaml)
    - Configuration-driven behavior
    - Clean error handling and input validation
    - Proper logging
    
    All tools are automatically loaded from schema.yaml by SchemaBasedMCPServer.
    """

    def __init__(self, name: str, config: dict[str, Any] | None = None, ssl_verify: bool = True):
        """Initialize the example server."""
        super().__init__(name, config, ssl_verify)
        
        # Extract configuration with sensible defaults
        self.precision = int(self.config.get("precision", 2))
        self.max_text_length = int(self.config.get("max_text_length", 1000))
        
        logger.info(f"Example server '{name}' initialized")

    async def call(self, tool: str, params: dict[str, Any]) -> Any:
        """Route tool calls to appropriate handlers."""
        logger.debug(f"Calling tool '{tool}' with params: {params}")
        
        calculator_name = f"{self.name}_calculator"
        formatter_name = f"{self.name}_formatter"
        status_name = f"{self.name}_status"
        
        if tool == calculator_name:
            return await self._handle_calculator(params)
        elif tool == formatter_name:
            return await self._handle_formatter(params)
        elif tool == status_name:
            return await self._handle_status(params)
        else:
            raise ValueError(f"Unknown tool '{tool}'. Available: {calculator_name}, {formatter_name}, {status_name}")

    async def _handle_calculator(self, params: dict[str, Any]) -> dict[str, Any]:
        """Handle calculator operations with high precision.
        
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

    async def _handle_formatter(self, params: dict[str, Any]) -> dict[str, Any]:
        """Handle text formatting operations.
        
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

    async def _handle_status(self, params: dict[str, Any]) -> dict[str, Any]:
        """Handle status information requests."""
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
                    "max_text_length": self.max_text_length,
                    "ssl_verify": self.ssl_verify
                },
                "available_tools": [tool["function"]["name"] for tool in tools]
            })
        
        logger.debug(f"Status request (verbose={verbose}): {len(status)} fields")
        return status