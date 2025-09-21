"""MCP Server for Script Interpreter Plugin."""

import asyncio
import logging
from typing import Any, Dict, Optional
from pathlib import Path
import sys
import json

# Add the src directory to the path so we can import our modules
sys.path.insert(0, "/".join(__file__.split("/")[:-4]))

from agent_system.mcp.base import MCPServer
from agent_system.mcp.improved_status import (
    publish_status_improved,
    StatusPhase,
)
from .executor import ScriptExecutor
from .config import ScriptInterpreterConfig

logger = logging.getLogger(__name__)


class ScriptInterpreterServer(MCPServer):
    """MCP Server for executing scripts in a secure sandbox."""

    def __init__(self, name: str = "script_interpreter", config: Optional[dict] = None, ssl_verify: bool = True):
        super().__init__(name, config, ssl_verify)
        # Convert dict config to ScriptInterpreterConfig if needed
        if isinstance(config, dict):
            script_config = ScriptInterpreterConfig.from_dict(config)
        else:
            script_config = config or ScriptInterpreterConfig()
        self.script_config = script_config
        self.executor = ScriptExecutor(script_config)

    async def call(self, tool: str, params: dict[str, Any]) -> Any:
        """Handle MCP tool calls."""
        if tool == "eval":
            code = params.get("code", "")
            if not code:
                return {"error": "Missing required parameter 'code'"}
            request_id = params.get("request_id")
            reset_sandbox = params.get("reset_sandbox", False)

            # Publish start status
            await publish_status_improved(self.name, "Execution started", request_id=request_id, phase=StatusPhase.START)
            try:
                # Publish progress
                await publish_status_improved(self.name, "Executing code", request_id=request_id, phase=StatusPhase.PROGRESS)

                result = self.executor.execute(code, reset_sandbox=reset_sandbox)

                if not result.get("success", False) or result.get("error"):
                    # Publish error status
                    await publish_status_improved(
                        self.name,
                        f"Execution failed: {result.get('error')}",
                        request_id=request_id,
                        level="error",
                        phase=StatusPhase.ERROR,
                    )
                    return {"error": result["error"] or "Execution failed", "suggestion": result.get("suggestion", "")}
                else:
                    # Publish end status with execution metadata
                    meta = {
                        "execution_time": result.get("execution_time"),
                        "variables": len(result.get("variables", {})),
                    }
                    await publish_status_improved(
                        self.name,
                        "Execution completed",
                        request_id=request_id,
                        phase=StatusPhase.END,
                        meta=meta,
                    )

                    output_parts = []
                    if result.get("output"):
                        output_parts.append(f"Output: {result['output']}")
                    if result.get("variables"):
                        var_str = ", ".join(f"{k}={v}" for k, v in result["variables"].items())
                        output_parts.append(f"Variables: {var_str}")
                    if result.get("execution_time") is not None:
                        output_parts.append(f"Execution time: {result['execution_time']:.3f}s")

                    return {"result": "\n".join(output_parts)}
            except Exception as e:
                await publish_status_improved(self.name, f"Execution failed: {str(e)}", request_id=request_id, level="error", phase=StatusPhase.ERROR)
                return {"error": f"Execution failed: {str(e)}"}

        elif tool == "validate":
            code = params.get("code", "")
            if not code:
                return {"error": "Missing required parameter 'code'"}

            try:
                validation_result = self.executor.validate_syntax(code)
                if validation_result["valid"]:
                    return {"result": "✅ Syntax is valid"}
                else:
                    return {"error": f"❌ {validation_result['error']}"}
            except Exception as e:
                return {"error": f"❌ Syntax error: {str(e)}", "suggestion": "Check Python syntax - parentheses, indentation, operators"}

        elif tool == "reset":
            try:
                self.executor.reset_sandbox()
                return {"result": "🔄 Sandbox reset - all variables and state cleared"}
            except Exception as e:
                return {"error": f"Reset failed: {str(e)}"}
        else:
            return {"error": f"Unknown tool: {tool}"}

    def get_schema(self) -> dict[str, Any]:
        """Return the OpenAI function schema for the script interpreter."""
        # Load MCP schema and convert to OpenAI function format
        from agent_system.plugins.schema_loader import load_schema_from_dir

        # Try to load MCP schema first
        schema_data = load_schema_from_dir(Path(__file__).parent, template_vars={"name": "script_interpreter"})

        if schema_data and "tools" in schema_data:
            # Convert MCP multi-tool schema to OpenAI function format
            # For now, default to the primary "eval" tool
            eval_tool = next((tool for tool in schema_data["tools"] if tool["name"] == "eval"), None)
            if eval_tool:
                return {
                    "type": "function",
                    "function": {
                        "name": "script_interpreter",
                        "description": eval_tool["description"],
                        "parameters": eval_tool["inputSchema"]
                    }
                }

        # Fallback to hardcoded schema
        return {
            "type": "function",
            "function": {
                "name": "script_interpreter",
                "description": "Execute Python code in a secure sandbox. Supports mathematical expressions, basic operations, and simple programming constructs.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "action": {
                            "type": "string",
                            "enum": ["eval", "validate", "reset"],
                            "default": "eval"
                        },
                        "code": {
                            "type": "string",
                            "description": "Python code to execute. Examples: '(2+3)*4', 'x = 42; y = x * 2', 'abs(-15)'"
                        }
                    },
                    "required": ["code"],
                    "additionalProperties": True
                }
            }
        }

    def get_default_action(self) -> str:
        """Return the default action for the script interpreter."""
        return "eval"

    # Legacy MCP JSON-RPC interface (optional, for direct MCP clients)
    async def handle_request(self, request: Dict[str, Any]) -> Dict[str, Any]:
        """Handle incoming MCP requests."""
        method = request.get("method")
        params = request.get("params", {})

        if method == "tools/list":
            return await self._list_tools()
        elif method == "tools/call":
            return await self._call_tool(params)
        else:
            return {
                "error": {
                    "code": -32601,
                    "message": f"Method not found: {method}"
                }
            }

    async def _list_tools(self) -> Dict[str, Any]:
        """List available tools."""
        return {
            "tools": [
                {
                    "name": "eval",
                    "description": "Execute Python code in a secure sandbox. Supports mathematical expressions, basic operations, and simple programming constructs.",
                    "inputSchema": {
                        "type": "object",
                        "properties": {
                            "code": {
                                "type": "string",
                                "description": "Python code to execute. Examples: '(2+3)*4', 'x = 42; y = x * 2', 'abs(-15)'"
                            }
                        },
                        "required": ["code"]
                    }
                },
                {
                    "name": "validate",
                    "description": "Validate Python syntax without executing the code.",
                    "inputSchema": {
                        "type": "object",
                        "properties": {
                            "code": {
                                "type": "string",
                                "description": "Python code to validate"
                            }
                        },
                        "required": ["code"]
                    }
                },
                {
                    "name": "reset",
                    "description": "Reset the sandbox environment, clearing all variables and state.",
                    "inputSchema": {
                        "type": "object",
                        "properties": {},
                        "required": []
                    }
                }
            ]
        }

    async def _call_tool(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Execute a tool call."""
        tool_name = params.get("name")
        arguments = params.get("arguments", {})

        try:
            if tool_name == "eval":
                return await self._eval_code(arguments)
            elif tool_name == "validate":
                return await self._validate_code(arguments)
            elif tool_name == "reset":
                return await self._reset_sandbox(arguments)
            else:
                return {
                    "error": {
                        "code": -32602,
                        "message": f"Unknown tool: {tool_name}"
                    }
                }
        except Exception as e:
            logger.error(f"Error executing tool {tool_name}: {e}")
            return {
                "error": {
                    "code": -32603,
                    "message": f"Internal error: {str(e)}"
                }
            }

    async def _eval_code(self, arguments: Dict[str, Any]) -> Dict[str, Any]:
        """Execute Python code."""
        code = arguments.get("code")
        if not code:
            return {
                "error": {
                    "code": -32602,
                    "message": "Missing required argument: code"
                }
            }
        request_id = arguments.get("request_id")

        # Publish start status
        await publish_status_improved(self.name, "Execution started", request_id=request_id, phase=StatusPhase.START)

        # Execute code in a separate thread to avoid blocking
        loop = asyncio.get_event_loop()
        await publish_status_improved(self.name, "Executing code", request_id=request_id, phase=StatusPhase.PROGRESS)
        result = await loop.run_in_executor(None, self.executor.execute, code)

        if result["success"]:
            # Publish end status with execution metadata
            meta = {"execution_time": result.get("execution_time"), "variables": len(result.get("variables", {}))}
            await publish_status_improved(self.name, "Execution completed", request_id=request_id, phase=StatusPhase.END, meta=meta)

            output_parts = []
            if result["output"]:
                output_parts.append(f"Output: {result['output']}")

            if result["variables"]:
                var_summary = ", ".join([f"{k}={v}" for k, v in result["variables"].items()])
                output_parts.append(f"Variables: {var_summary}")

            output_parts.append(f"Execution time: {result['execution_time']:.3f}s")

            return {
                "content": [
                    {
                        "type": "text",
                        "text": "\\n".join(output_parts) if output_parts else "Code executed successfully (no output)"
                    }
                ]
            }
        else:
            error_info = result["error"]
            await publish_status_improved(self.name, f"Execution failed: {error_info}", request_id=request_id, level="error", phase=StatusPhase.ERROR, meta={"error": error_info})
            error_text = f"Error ({error_info['category']}): {error_info['message']}"
            if "suggestion" in error_info:
                error_text += f"\\nSuggestion: {error_info['suggestion']}"

            return {
                "content": [
                    {
                        "type": "text",
                        "text": error_text
                    }
                ]
            }

    async def _validate_code(self, arguments: Dict[str, Any]) -> Dict[str, Any]:
        """Validate Python syntax."""
        code = arguments.get("code")
        if not code:
            return {
                "error": {
                    "code": -32602,
                    "message": "Missing required argument: code"
                }
            }

        result = self.executor.validate_syntax(code)

        if result["valid"]:
            return {
                "content": [
                    {
                        "type": "text",
                        "text": "✅ Syntax is valid"
                    }
                ]
            }
        else:
            error_info = result["error"]
            error_text = f"❌ Syntax error: {error_info['message']}"
            if "suggestion" in error_info:
                error_text += f"\\nSuggestion: {error_info['suggestion']}"

            return {
                "content": [
                    {
                        "type": "text",
                        "text": error_text
                    }
                ]
            }

    async def _reset_sandbox(self, arguments: Dict[str, Any]) -> Dict[str, Any]:
        """Reset the sandbox environment."""
        self.executor.reset_sandbox()
        return {
            "content": [
                {
                    "type": "text",
                    "text": "🔄 Sandbox reset - all variables and state cleared"
                }
            ]
        }


async def main():
    """Main entry point for the MCP server."""
    # Basic MCP server setup
    server = ScriptInterpreterServer()

    # Read JSON-RPC requests from stdin
    while True:
        try:
            line = await asyncio.get_event_loop().run_in_executor(None, sys.stdin.readline)
            if not line:
                break

            request = json.loads(line.strip())
            response = await server.handle_request(request)

            # Add request ID if present
            if "id" in request:
                response["id"] = request["id"]

            print(json.dumps(response, ensure_ascii=False))

        except json.JSONDecodeError as e:
            logger.error(f"Invalid JSON request: {e}")
            error_response = {
                "error": {
                    "code": -32700,
                    "message": "Parse error"
                }
            }
            if "id" in request:
                error_response["id"] = request["id"]
            print(json.dumps(error_response))

        except Exception as e:
            logger.error(f"Unexpected error: {e}")
            error_response = {
                "error": {
                    "code": -32603,
                    "message": f"Internal error: {str(e)}"
                }
            }
            print(json.dumps(error_response))


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(main())