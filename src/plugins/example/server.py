"""Example Tool Server implementation.

This module demonstrates how to implement a tool server with
SchemaBasedToolServer and its automatic tool dispatching.

Key pattern (no call() override needed):
1. Inherit from SchemaBasedToolServer
2. Define tools in schema.yaml with names like: "{{ name }}_toolname"
3. Implement one async method per tool, named after the tool WITHOUT the
   "{name}_" prefix
4. The dispatcher strips the prefix and routes the call to that method

For a server instance named "example", with tools in schema.yaml:
- "example_calculator" -> self.calculator(params)
- "example_formatter" -> self.formatter(params)
- "example_status" -> self.status(params)

The contract every handler keeps (see .claude/skills/plugin-authoring):
- validate its own arguments -- the framework does not check them against
  the schema;
- answer an error as {"status": "error", "error": ...} instead of raising;
- end the status scope with a line that names the result;
- keep its answer bounded.
"""

from __future__ import annotations

import logging
import math
from typing import Any, TYPE_CHECKING
from decimal import Decimal, InvalidOperation
from agent_system.tools.schema_based import SchemaBasedToolServer

if TYPE_CHECKING:
    from agent_system.config.models import AgentSystemConfig, ToolServerConfig

logger = logging.getLogger(__name__)

OPERATIONS = ("add", "subtract", "multiply", "divide")
FORMATS = ("uppercase", "lowercase", "title", "reverse")


def _error(message: str) -> dict[str, Any]:
    """The error answer: the model reads `error` and can correct its call."""
    return {"status": "error", "error": message}


async def _end(params: dict[str, Any], message: str) -> None:
    """End the status scope with a line naming the result.

    `_status` is injected by call_with_status (the path agents use); a direct
    call() has none. An error answer needs no line of its own: the framework
    reports a returned {"status": "error"} as the scope's error.
    """
    status = params.get("_status")
    if status is not None:
        await status.end(message)


class ExampleServer(SchemaBasedToolServer):
    """Example tool server: three tools, two settings, no storage.

    Tools are loaded from schema.yaml by SchemaBasedToolServer and routed to
    the methods below by the inherited call().
    """

    def __init__(self, name: str, system_config: AgentSystemConfig, server_config: ToolServerConfig):
        """Initialize the example server.

        Args:
            name: Plugin instance name
            system_config: System-wide configuration
            server_config: this instance's entry in config/plugins.yaml
        """
        super().__init__(name, system_config, server_config)

        # Settings are flat keys of the server entry; defaults live here, in
        # code -- the config: block of schema.yaml is only documentation.
        self.precision = int(getattr(server_config, "precision", 2))
        self.max_text_length = int(getattr(server_config, "max_text_length", 1000))

        logger.info(f"Example server '{name}' initialized with precision={self.precision}, max_length={self.max_text_length}")

    async def calculator(self, params: dict[str, Any]) -> dict[str, Any]:
        """Tool "<name>_calculator": one arithmetic operation on a and b."""
        operation = params.get("operation")
        if operation not in OPERATIONS:
            return _error(f"Invalid operation '{operation!s:.50}'. Valid: {list(OPERATIONS)}")
        if "a" not in params or "b" not in params:
            return _error("Missing required parameters: a, b")

        try:
            a = Decimal(str(params["a"]))
            b = Decimal(str(params["b"]))
        except InvalidOperation:
            return _error(f"a and b must be numbers, got {params['a']!r:.50} and {params['b']!r:.50}")
        # is_finite() first: float() raises on a signaling NaN ("sNaN").
        if not all(x.is_finite() and math.isfinite(float(x)) for x in (a, b)):
            return _error("a and b must be finite numbers within float range")

        if operation == "add":
            result = a + b
        elif operation == "subtract":
            result = a - b
        elif operation == "multiply":
            result = a * b
        else:
            if b == 0:
                return _error("Division by zero is not allowed")
            result = a / b

        try:
            result = result.quantize(Decimal(10) ** -self.precision)
        except InvalidOperation:
            # More digits than Decimal's 28-digit context holds (1e20 * 1e20,
            # or a large precision): at that size the float carries no
            # decimals anyway, so the unrounded value is the answer.
            pass

        if not math.isfinite(float(result)):
            return _error(f"The result of {operation} is too large for a float")

        response = {
            "operation": operation,
            "operands": [float(a), float(b)],
            "result": float(result),
            "precision": self.precision
        }
        await _end(params, f"{operation} {float(a):g}, {float(b):g} = {float(result):g}")
        return response

    async def formatter(self, params: dict[str, Any]) -> dict[str, Any]:
        """Tool "<name>_formatter": one text transformation."""
        text = params.get("text")
        format_type = params.get("format")

        if not isinstance(text, str):
            return _error("text must be a string")
        # The limit keeps the answer bounded: it echoes the text twice.
        # Error texts cut what they quote (:.50) for the same reason.
        if len(text) > self.max_text_length:
            return _error(f"Text length {len(text)} exceeds maximum {self.max_text_length}")
        if format_type not in FORMATS:
            return _error(f"Invalid format '{format_type!s:.50}'. Valid: {list(FORMATS)}")

        if format_type == "uppercase":
            formatted = text.upper()
        elif format_type == "lowercase":
            formatted = text.lower()
        elif format_type == "title":
            formatted = text.title()
        else:
            formatted = text[::-1]

        response = {
            "original": text,
            "format": format_type,
            "formatted": formatted,
            "length": len(formatted)
        }
        await _end(params, f"{format_type}: {len(text)} -> {len(formatted)} chars")
        return response

    async def status(self, params: dict[str, Any]) -> dict[str, Any]:
        """Tool "<name>_status": the server's name, tool count and settings."""
        verbose = params.get("verbose", False)
        if not isinstance(verbose, bool):
            return _error(f"verbose must be true or false, got {verbose!r:.50}")

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

        await _end(params, f"{self.name}: active, {len(tools)} tools")
        return status
