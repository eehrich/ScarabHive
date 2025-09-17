from __future__ import annotations

from typing import Any
import os
import yaml
from agent_system.mcp.base import MCPServer


class MultiToolTestServer(MCPServer):
    """Example multi-tool server (moved from test_multi_tool).

    Provides three example tools: <name>_calculator, <name>_formatter and
    <name>_status. This mirrors the previous test plugin behavior so tests can
    exercise a real multi-tool plugin under `plugins.example`.
    """

    def get_tools(self) -> list[dict[str, Any]]:
        """Return multiple tools for testing."""
        # Prefer loading tool definitions from an external `schema.yaml` if present.
        schema_path = os.path.join(os.path.dirname(__file__), "schema.yaml")
        if os.path.exists(schema_path):
            try:
                with open(schema_path, "r", encoding="utf-8") as fh:
                    doc = yaml.safe_load(fh)
                tools = doc.get("tools", [])
                # Render `{name}` placeholders using the plugin name
                rendered = []
                for t in tools:
                    rt = yaml.safe_load(yaml.safe_dump(t))
                    # Walk and replace {name} occurrences in nested strings
                    def replace_placeholders(obj):
                        if isinstance(obj, dict):
                            return {k: replace_placeholders(v) for k, v in obj.items()}
                        if isinstance(obj, list):
                            return [replace_placeholders(v) for v in obj]
                        if isinstance(obj, str):
                            return obj.replace("{name}", self.name)
                        return obj

                    rendered.append(replace_placeholders(rt))

                return rendered
            except Exception:
                # Fall back to inline definition below on any error
                pass

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

    def get_schema(self) -> dict[str, Any]:
        """Return a schema compatible with the single-tool legacy interface.

        For backward compatibility the MCPServer-derived API expects
        `get_schema()` to provide a single function schema. We return the
        first tool definition from `get_tools()` so older consumers still
        work.
        """
        tools = self.get_tools()
        if not tools:
            raise NotImplementedError("MCPServer subclasses must implement either get_tools() or get_schema()")
        # Return the first tool's wrapped function schema if present
        first = tools[0]
        if isinstance(first, dict) and "function" in first:
            return first
        return {"type": "function", "function": first}

    def get_default_action(self) -> str:
        """Return the default action name derived from the first tool."""
        schema = self.get_schema()
        return schema["function"]["name"]

    async def call(self, tool: str | None = None, params: dict[str, Any] | None = None) -> Any:
        """Handle calls to any of the multiple tools.

        Backwards compatible: if `tool` is omitted, call the server's
        default action (derived from `get_default_action()`).
        """
        if tool is None:
            tool = self.get_default_action()
        params = params or {}

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
