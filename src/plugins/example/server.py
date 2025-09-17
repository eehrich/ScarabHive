"""Example MCP Server implementation.

This module provides a comprehensive example of how to implement an MCP server
that exposes multiple tools. It demonstrates best practices for error handling,
input validation, configuration management, and extensibility.
"""

from __future__ import annotations

import os
import yaml
import logging
from typing import Any
from decimal import Decimal, InvalidOperation
from agent_system.mcp.base import MCPServer

logger = logging.getLogger(__name__)


class ExampleServer(MCPServer):
    """Example multi-tool MCP server demonstrating best practices.

    This server provides three demonstration tools:
    1. Calculator - Basic arithmetic operations with configurable precision
    2. Text Formatter - Text manipulation in various formats
    3. Status Reporter - Plugin status and configuration information

    The server demonstrates:
    - External schema loading with template variables
    - Comprehensive input validation and error handling
    - Configurable behavior through plugin settings
    - Structured logging and debugging support
    - Type-safe operations with proper error messages
    """

    def __init__(self, name: str, config: dict[str, Any] | None = None, ssl_verify: bool = True):
        """Initialize the example server.
        
        Args:
            name: Plugin instance name
            config: Configuration dictionary
            ssl_verify: SSL verification flag
        """
        super().__init__(name, config, ssl_verify)
        self.logger = logging.getLogger(f"plugins.example.{name}")
        
        # Extract configuration with defaults
        self.precision = int(self.config.get("precision", 2))
        self.max_text_length = int(self.config.get("max_text_length", 1000))
        self.debug_enabled = bool(self.config.get("enable_debug", False))
        
        self.logger.info(f"Initialized example server '{name}' with precision={self.precision}, "
                        f"max_text_length={self.max_text_length}")

    def get_tools(self) -> list[dict[str, Any]]:
        """Return tool definitions, preferring external schema.yaml.
        
        Returns:
            List of tool definitions in MCP function format
        """
        # Try to load external schema first
        schema_path = os.path.join(os.path.dirname(__file__), "schema.yaml")
        if os.path.exists(schema_path):
            try:
                with open(schema_path, "r", encoding="utf-8") as fh:
                    doc = yaml.safe_load(fh)
                tools = doc.get("tools", [])
                
                # Render template variables
                rendered = []
                for tool in tools:
                    rendered_tool = self._render_template_variables(tool)
                    rendered.append(rendered_tool)
                
                self.logger.debug(f"Loaded {len(rendered)} tools from schema.yaml")
                return rendered
                
            except Exception as e:
                self.logger.warning(f"Failed to load schema.yaml: {e}, falling back to inline schema")

        # Fallback to inline definitions
        return self._get_inline_tools()

    def _render_template_variables(self, obj: Any) -> Any:
        """Recursively render template variables in tool definitions.
        
        Args:
            obj: Object to process (dict, list, or primitive)
            
        Returns:
            Object with template variables replaced
        """
        if isinstance(obj, dict):
            return {k: self._render_template_variables(v) for k, v in obj.items()}
        elif isinstance(obj, list):
            return [self._render_template_variables(v) for v in obj]
        elif isinstance(obj, str):
            return obj.replace("{name}", self.name)
        else:
            return obj

    def _get_inline_tools(self) -> list[dict[str, Any]]:
        """Get inline tool definitions as fallback.
        
        Returns:
            List of inline tool definitions
        """
        return [
            {
                "type": "function",
                "function": {
                    "name": f"{self.name}_calculator",
                    "description": f"Perform basic arithmetic operations with {self.precision} decimal precision",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "operation": {
                                "type": "string",
                                "enum": ["add", "subtract", "multiply", "divide"],
                                "description": "The arithmetic operation to perform",
                            },
                            "a": {"type": "number", "description": "First number"},
                            "b": {"type": "number", "description": "Second number"},
                        },
                        "required": ["operation", "a", "b"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": f"{self.name}_formatter",
                    "description": f"Format text in various ways (max length: {self.max_text_length})",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "text": {
                                "type": "string", 
                                "description": f"Text to format (max {self.max_text_length} characters)",
                                "maxLength": self.max_text_length
                            },
                            "format": {
                                "type": "string",
                                "enum": ["uppercase", "lowercase", "title", "reverse"],
                                "description": "Format to apply",
                            },
                        },
                        "required": ["text", "format"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": f"{self.name}_status",
                    "description": "Get server status and configuration information",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "verbose": {
                                "type": "boolean",
                                "default": False,
                                "description": "Include detailed configuration and debug information",
                            }
                        },
                    },
                },
            },
        ]

    def get_schema(self) -> dict[str, Any]:
        """Return single tool schema for backward compatibility.
        
        Returns:
            Schema of the first tool (calculator)
        """
        tools = self.get_tools()
        if not tools:
            raise NotImplementedError("No tools defined")
        return tools[0]

    def get_default_action(self) -> str:
        """Return the default action name.
        
        Returns:
            Name of the first tool (calculator)
        """
        schema = self.get_schema()
        return schema["function"]["name"]

    async def call(self, tool: str, params: dict[str, Any]) -> Any:
        """Route tool calls to appropriate handlers.
        
        Args:
            tool: Tool name to call
            params: Parameters for the tool
            
        Returns:
            Tool execution result
            
        Raises:
            ValueError: If tool is unknown or parameters are invalid
        """
        self.logger.debug(f"Calling tool '{tool}' with params: {params}")
        
        # Extract tool suffix for routing
        expected_prefix = f"{self.name}_"
        if not tool.startswith(expected_prefix):
            raise ValueError(f"Tool '{tool}' does not match plugin prefix '{expected_prefix}'")
        
        tool_suffix = tool[len(expected_prefix):]
        
        try:
            if tool_suffix == "calculator":
                return await self._handle_calculator(params)
            elif tool_suffix == "formatter":
                return await self._handle_formatter(params)
            elif tool_suffix == "status":
                return await self._handle_status(params)
            else:
                available = [f"{self.name}_{t}" for t in ["calculator", "formatter", "status"]]
                raise ValueError(f"Unknown tool '{tool}'. Available tools: {available}")
                
        except Exception as e:
            self.logger.error(f"Error executing tool '{tool}': {e}")
            raise

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
        
        self.logger.debug(f"Calculator result: {response}")
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
        
        self.logger.debug(f"Formatter result: {len(text)} chars -> {len(formatted)} chars")
        return response

    async def _handle_status(self, params: dict[str, Any]) -> dict[str, Any]:
        """Handle status information requests.
        
        Args:
            params: Parameters containing optional verbose flag
            
        Returns:
            Dict with status information
        """
        verbose = params.get("verbose", False)
        
        status = {
            "server_name": self.name,
            "status": "active",
            "tools_count": len(self.get_tools()),
            "version": "1.0.0"
        }
        
        if verbose:
            tools = self.get_tools()
            status.update({
                "config": {
                    "precision": self.precision,
                    "max_text_length": self.max_text_length,
                    "debug_enabled": self.debug_enabled,
                    "ssl_verify": self.ssl_verify
                },
                "available_tools": [tool["function"]["name"] for tool in tools],
                "schema_source": "external" if os.path.exists(
                    os.path.join(os.path.dirname(__file__), "schema.yaml")
                ) else "inline"
            })
        
        self.logger.debug(f"Status request (verbose={verbose}): {len(status)} fields")
        return status


# Backward compatibility alias
MultiToolTestServer = ExampleServer