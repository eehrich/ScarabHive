from __future__ import annotations

from typing import Any
from agent_system.mcp.base import MCPServer


class MultiToolTestServer(MCPServer):
    """Example multi-tool server (moved from test_multi_tool).

    Provides three example tools: <name>_calculator, <name>_formatter and
    <name>_status. This mirrors the previous test plugin behavior so tests can
    exercise a real multi-tool plugin under `plugins.example`.
    """

    def get_tools(self) -> list[dict[str, Any]]:
        """Return multiple tools for testing."""
        return [
            {
                "type": "function",
                "function": {
                    "name": f"{self.name}_calculator",
                    "description": "Perform basic arithmetic operations",
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
                    "description": "Format text in various ways",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "text": {"type": "string", "description": "Text to format"},
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
                    "description": "Get server status information",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "verbose": {
                                "type": "boolean",
                                "default": False,
                                "description": "Include detailed status information",
                            }
                        },
                    },
                },
            },
        ]

    async def call(self, tool: str, params: dict[str, Any]) -> Any:
        """Handle calls to any of the multiple tools."""
        # Extract tool name without plugin prefix for routing
        tool_suffix = tool.replace(f"{self.name}_", "")

        if tool_suffix == "calculator":
            return await self._handle_calculator(params)
        elif tool_suffix == "formatter":
            return await self._handle_formatter(params)
        elif tool_suffix == "status":
            return await self._handle_status(params)
        else:
            raise ValueError(f"Unknown tool: {tool}")

    async def _handle_calculator(self, params: dict[str, Any]) -> dict[str, Any]:
        """Handle calculator operations."""
        operation = params["operation"]
        a = float(params["a"])
        b = float(params["b"])

        if operation == "add":
            result = a + b
        elif operation == "subtract":
            result = a - b
        elif operation == "multiply":
            result = a * b
        elif operation == "divide":
            if b == 0:
                raise ValueError("Division by zero")
            result = a / b
        else:
            raise ValueError(f"Unknown operation: {operation}")

        return {"operation": operation, "operands": [a, b], "result": result}

    async def _handle_formatter(self, params: dict[str, Any]) -> dict[str, Any]:
        """Handle text formatting operations."""
        text = params["text"]
        format_type = params["format"]

        if format_type == "uppercase":
            formatted = text.upper()
        elif format_type == "lowercase":
            formatted = text.lower()
        elif format_type == "title":
            formatted = text.title()
        elif format_type == "reverse":
            formatted = text[::-1]
        else:
            raise ValueError(f"Unknown format: {format_type}")

        return {"original": text, "format": format_type, "formatted": formatted}

    async def _handle_status(self, params: dict[str, Any]) -> dict[str, Any]:
        """Handle status requests."""
        verbose = params.get("verbose", False)

        status = {"server_name": self.name, "status": "active", "tools_count": len(self.get_tools())}

        if verbose:
            status.update(
                {
                    "config": self.config,
                    "ssl_verify": self.ssl_verify,
                    "available_tools": [tool["function"]["name"] for tool in self.get_tools()],
                }
            )

        return status
