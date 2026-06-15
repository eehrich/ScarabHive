"""MCP Server for Script Interpreter Plugin."""

import asyncio
import logging
import time
from typing import Any, TYPE_CHECKING

import sys
from pathlib import Path

from agent_system.mcp.schema_based import SchemaBasedMCPServer
from .executor import ScriptExecutor
from .config import ScriptInterpreterConfig

if TYPE_CHECKING:
    from agent_system.config import AgentSystemConfig, MCPConfig

# Add the project src directory to the path so we can import our modules when running
# as a script (this is a no-op when package imports are already configured).
src_path = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(src_path))

logger = logging.getLogger(__name__)


class ScriptInterpreterServer(SchemaBasedMCPServer):
    """MCP Server for executing scripts in a secure sandbox."""

    def __init__(self, name: str, system_config: "AgentSystemConfig", mcp_config: "MCPConfig") -> None:
        """
        Modern constructor signature.
        
        Args:
            name: Plugin instance name
            system_config: System-wide configuration
            mcp_config: Plugin-specific configuration (script_interpreter settings)
        """
        super().__init__(name, system_config, mcp_config)

        # Extract script-specific config from mcp_config
        script_config_dict = getattr(mcp_config, 'script_interpreter', {})
        if script_config_dict:
            script_config = ScriptInterpreterConfig.from_dict(script_config_dict)
        else:
            script_config = ScriptInterpreterConfig()
        self.script_config = script_config

        # SECURITY / ISOLATION: this server is a process-wide singleton shared
        # by every session and user. A single shared ScriptExecutor would let
        # its SafeExecutor.variables dict persist across sessions, so session B
        # could read variables (e.g. secrets) assigned by session A's code -
        # and execute() returns ALL variables to the caller. Keep one executor
        # PER SESSION instead, with TTL + LRU eviction to bound memory.
        # Documented scope is "persist within session", which this restores.
        self._executors: dict[str, tuple[ScriptExecutor, float]] = {}
        self._session_ttl_seconds: float = float(
            getattr(script_config, "session_ttl_seconds", 3600)
        )
        self._max_tracked_sessions: int = int(
            getattr(script_config, "max_tracked_sessions", 100)
        )

    def _get_executor(self, session_id: str) -> ScriptExecutor:
        """Return the per-session executor, creating it on first use.

        No await points inside, so the dict bookkeeping is atomic under CPython
        and needs no lock. Callers hold the returned reference, so a concurrent
        eviction of the dict entry does not invalidate an in-flight execution.
        """
        now = time.monotonic()
        # Evict expired sessions (never the one being requested)
        if self._session_ttl_seconds > 0:
            expired = [
                sid for sid, (_, last) in self._executors.items()
                if sid != session_id and now - last > self._session_ttl_seconds
            ]
            for sid in expired:
                self._executors.pop(sid, None)
        # LRU-evict if over the cap (oldest last-access first). MUST exclude the
        # session being requested - otherwise, if it is the oldest, it would be
        # evicted right before use and silently lose its sandbox variables.
        if len(self._executors) >= self._max_tracked_sessions:
            candidates = sorted(
                (kv for kv in self._executors.items() if kv[0] != session_id),
                key=lambda kv: kv[1][1],
            )
            for sid, _ in candidates[: len(self._executors) - self._max_tracked_sessions + 1]:
                self._executors.pop(sid, None)
        entry = self._executors.get(session_id)
        executor = entry[0] if entry else ScriptExecutor(self.script_config)
        self._executors[session_id] = (executor, now)
        return executor

    async def execute(self, params: dict[str, Any]) -> dict[str, Any]:
        """
        Execute Python code in secure sandbox.
        
        Tool name: {{ name }}_execute → e.g., 'script_interpreter_execute'
        Method called after dispatcher strips prefix → 'execute'
        """
        status = params["_status"]  # Status is mandatory from framework
        
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

        # Per-session sandbox (see _get_executor). Hold a local reference so a
        # concurrent eviction can't affect this in-flight call.
        session_id = params.get("_session_id") or "default"
        executor = self._get_executor(session_id)

        try:
            # Offload the (potentially CPU-bound) sandbox execution to a worker
            # thread so it never blocks the shared event loop for other sessions.
            # The executor serialises its own concurrent use via an internal lock.
            result = await asyncio.to_thread(executor.execute, code, reset_sandbox=False)

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

    async def reset(self, params: dict[str, Any]) -> dict[str, Any]:
        """
        Reset sandbox environment and clear variables.
        
        Tool name: {{ name }}_reset → e.g., 'script_interpreter_reset'
        Method called after dispatcher strips prefix → 'reset'
        """
        status = params["_status"]  # Status is mandatory from framework

        await status.progress("Resetting Python sandbox")
        # Reset only the calling session's sandbox, not a shared global one.
        session_id = params.get("_session_id") or "default"
        try:
            entry = self._executors.get(session_id)
            if entry:
                entry[0].reset_sandbox()
            await status.end("Sandbox reset completed")
            return {"result": "🔄 Python sandbox reset - all variables and state cleared"}
        except Exception as e:
            await status.error("Reset failed")
            return {"error": f"Reset failed: {str(e)}"}

