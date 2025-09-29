"""MCP Server for Script Interpreter Plugin."""

import logging
from typing import Any, Optional

import sys
from pathlib import Path

from agent_system.mcp.schema_based import SchemaBasedMCPServer
from .executor import ScriptExecutor
from .config import ScriptInterpreterConfig

# Add the project src directory to the path so we can import our modules when running
# as a script (this is a no-op when package imports are already configured).
src_path = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(src_path))

logger = logging.getLogger(__name__)


class ScriptInterpreterServer(SchemaBasedMCPServer):
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
        status = params.get("_status")

        if tool == "execute_python_sandbox":
            code = params.get("code", "")
            if not code:
                return {"error": "Missing required parameter 'code'"}

            # Check for cancellation before execution
            cancellation_token = params.get("_cancellation_token")
            if cancellation_token and cancellation_token.is_cancelled:
                return {"error": "Python execution cancelled by user", "cancelled": True}

            # Publish start status
            await status.progress("Python execution started")
            await status.progress("Executing code")

            try:
                result = self.executor.execute(code, reset_sandbox=False)

                if not result.get("success", False) or result.get("error"):
                    # Publish error status
                    error_info = result.get("error")
                    
                    if isinstance(error_info, dict):
                        # Structured error from SafeExecutor
                        if 'line_number' in error_info:
                            if error_info.get('category') == 'syntax':
                                error_msg = f"Syntax error on line {error_info['line_number']}: {error_info['message']}"
                            else:
                                error_msg = f"Runtime error on line {error_info['line_number']}: {error_info['message']}"
                        else:
                            error_msg = f"{error_info.get('type', 'Error')}: {error_info['message']}"
                        
                        if 'code_context' in error_info:
                            error_msg += f"\n\nCode context:\n{error_info['code_context']}"
                        
                        if 'stack_trace' in error_info:
                            error_msg += f"\n\nStack trace:\n{error_info['stack_trace']}"
                            
                        await status.error(f"Execution failed: {error_msg}")
                        return {"error": error_info, "error_message": error_msg, "error_details": error_info}
                    else:
                        # Simple string error (legacy format)
                        await status.error(f"Execution failed: {error_info}")
                        return {"error": error_info or "Execution failed", "suggestion": result.get("suggestion", "")}
                else:
                    # Publish end status with execution metadata
                    meta = {
                        "execution_time": result.get("execution_time"),
                        "variables": len(result.get("variables", {})),
                    }
                    await status.end("Execution completed", meta=meta)

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
                await status.error(f"Execution failed: {str(e)}")
                return {"error": f"Execution failed: {str(e)}"}

        elif tool == "reset_python_sandbox":
            await status.progress("Resetting Python sandbox")
            try:
                self.executor.reset_sandbox()
                await status.end("Sandbox reset completed")
                return {"result": "🔄 Python sandbox reset - all variables and state cleared"}
            except Exception as e:
                await status.error("Reset failed")
                return {"error": f"Reset failed: {str(e)}"}
        else:
            return {"error": f"Unknown tool: {tool}. Supported tools: execute_python_sandbox, reset_python_sandbox"}



    def get_default_action(self) -> str:
        """Return the default action for the script interpreter."""
        return "execute_python_sandbox"

