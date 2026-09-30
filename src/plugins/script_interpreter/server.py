"""Tool server for Script Interpreter Plugin."""

import asyncio
import logging
import time
from typing import Any, TYPE_CHECKING

import sys
from pathlib import Path

from agent_system.tools.schema_based import SchemaBasedToolServer
from .executor import ScriptExecutor

from .safe_executor import SEEDED_TYPE_NAMES, estimate_text_size
from .config import ScriptInterpreterConfig

#: Characters of one variable's value in the tool's answer.
_VARIABLE_TEXT_LIMIT = 200


def _variable_text(value: Any) -> str:
    """A variable's value for the answer: as text when short, else only its
    type. str() of every variable ran whole on the event loop -- a 6 MB text
    went back in full, and ``10 ** 5000`` failed the whole call with the
    4300-digit error."""
    if callable(value) and not isinstance(value, type):
        # A lambda's str() is the interpreter's own class path and a memory
        # address ("<plugins.script_interpreter.safe_executor...LambdaFunction
        # object at 0x...>") -- nothing the model can use.
        return "<function>"
    # The estimate only rules out what is far too long; the text's own
    # length decides the rest (it counted eight floats as over 200 chars).
    if estimate_text_size(value, 4 * _VARIABLE_TEXT_LIMIT) > 4 * _VARIABLE_TEXT_LIMIT:
        return f"<{type(value).__name__}, over {_VARIABLE_TEXT_LIMIT} chars — omitted>"
    try:
        text = str(value)
    except ValueError:
        return f"<{type(value).__name__} — not shown>"
    if len(text) > _VARIABLE_TEXT_LIMIT:
        return f"<{type(value).__name__}, {len(text)} chars — omitted>"
    return text

if TYPE_CHECKING:
    from agent_system.config import AgentSystemConfig, ToolServerConfig

# Add the project src directory to the path so we can import our modules when running
# as a script (this is a no-op when package imports are already configured).
src_path = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(src_path))

logger = logging.getLogger(__name__)


class ScriptInterpreterServer(SchemaBasedToolServer):
    """Tool server for executing scripts in a secure sandbox."""

    def __init__(self, name: str, system_config: "AgentSystemConfig", server_config: "ToolServerConfig") -> None:
        """
        Modern constructor signature.
        
        Args:
            name: Plugin instance name
            system_config: System-wide configuration
            server_config: Plugin-specific configuration (script_interpreter settings)
        """
        super().__init__(name, system_config, server_config)

        # Extract script-specific config from server_config
        script_config_dict = getattr(server_config, 'script_interpreter', {})
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
                    # Structured error from SafeExecutor (format_error_for_llm).
                    # Given once: the same dict also went back as
                    # `error_details`, with the interpreter's Python traceback
                    # -- about 600 KB for an unbounded recursion.
                    if 'line_number' in error_info:
                        if error_info.get('category') == 'syntax':
                            error_msg = f"Syntax error on line {error_info['line_number']}: {error_info['message']}"
                        else:
                            error_msg = f"Runtime error on line {error_info['line_number']}: {error_info['message']}"
                    else:
                        error_msg = f"{error_info.get('type', 'Error')}: {error_info['message']}"
                    
                    if 'code_context' in error_info:
                        error_msg += f"\n\nCode context:\n{error_info['code_context']}"
                    
                    # Headline only. `error_msg` accumulates the code
                    # context, and the status message is one row --
                    # the full text goes back in `error_message`, where the
                    # caller already reads it. Unbounded it also landed in the
                    # SSE payload and in the persisted sub-agent activity.
                    await status.error(
                        f"Execution failed: {error_msg.splitlines()[0][:120]}")
                    answer = {"error": error_info, "error_message": error_msg}
                    # What the script printed before it failed -- the model's
                    # only way to debug a run; the answer dropped it.
                    if result.get("output"):
                        answer["output"] = result["output"]
                    return answer
                else:
                    # Simple string error (legacy format)
                    await status.error(f"Execution failed: {str(error_info)[:120]}")
                    return {"error": error_info or "Execution failed", "suggestion": result.get("suggestion", "")}
            else:
                # Publish end status with execution metadata
                # The sandbox seeds its variable table with eight built-in
                # types so isinstance() works; they are not the script's.
                # Counting them reported "9 variable(s)" for a script that
                # assigned one -- and listed `int=<class 'int'>` back to the
                # caller. Subtracted once, for both readers.
                user_vars = {k: v for k, v in (result.get("variables") or {}).items()
                             if k not in SEEDED_TYPE_NAMES}
                meta = {
                    "execution_time": result.get("execution_time"),
                    "variables": len(user_vars),
                }
                # The numbers only lived in meta, which the WebUI does not
                # render -- the line that stays said "Execution completed".
                bits = []
                if result.get("output"):
                    bits.append(f"{len(result['output'])} chars output")
                if meta["variables"]:
                    bits.append(f"{meta['variables']} variable(s)")
                if meta["execution_time"] is not None:
                    bits.append(f"{meta['execution_time']:.3f}s")
                await status.end(
                    f"Executed: {', '.join(bits)}" if bits else "Executed (no output)",
                    meta=meta)

                output_parts = []
                if result.get("output"):
                    output_parts.append(f"Output: {result['output']}")
                if user_vars:
                    var_str = ", ".join(f"{k}={_variable_text(v)}" for k, v in user_vars.items())
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
            await status.end(f"Sandbox reset for session {session_id}")
            return {"result": "🔄 Python sandbox reset - all variables and state cleared"}
        except Exception as e:
            # The reason was in the return value but not in the status line,
            # which is the one the person sees.
            await status.error(f"Reset failed: {str(e)}")
            return {"error": f"Reset failed: {str(e)}"}

