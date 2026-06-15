"""Script execution engine with sandboxing and timeout support."""

import logging
import threading
from typing import Any, Dict, Optional

from .config import ScriptInterpreterConfig
from .safe_executor import SafeExecutor
from .errors import (
    RuntimeError,
    UnsupportedFeatureError,
    format_error_for_llm,
)

logger = logging.getLogger(__name__)


class ScriptExecutor:
    """Secure script executor using SafeExecutor (AST-based)."""

    def __init__(self, config: Optional[ScriptInterpreterConfig] = None):
        self.config = config or ScriptInterpreterConfig()
        # Always use SafeExecutor - supports all features securely
        self.safe_executor = SafeExecutor(self.config)
        # Serialises execution of THIS executor. The server runs execute() in a
        # worker thread (asyncio.to_thread); two concurrent calls for the same
        # session share one executor and its mutable SafeExecutor state
        # (variables / output_buffer / start_time), so they must not run at once.
        # Held only in worker threads -> never blocks the event loop.
        self._lock = threading.Lock()

    def execute(self, code: str, reset_sandbox: bool = False) -> Dict[str, Any]:
        """Execute Python code in a secure sandbox.

        Args:
            code: Python code to execute
            reset_sandbox: Whether to reset sandbox state before execution

        Returns:
            Dict containing result, output, and metadata
        """
        with self._lock:
            return self._execute_locked(code, reset_sandbox)

    def _execute_locked(self, code: str, reset_sandbox: bool) -> Dict[str, Any]:
        # Use SafeExecutor (supports loops, if statements, functions, etc.)
        if self.safe_executor:
            if reset_sandbox:
                self.safe_executor.reset()
            try:
                return self.safe_executor.execute(code)
            except UnsupportedFeatureError as e:
                # UnsupportedFeatureError should be returned as an error
                return self._format_unsupported_error(e, code)
            except Exception as e:
                # Log unexpected errors
                logger.error(f"SafeExecutor error: {e}")
                error_info = format_error_for_llm(RuntimeError(str(e), e), code)
                return {
                    "success": False,
                    "output": "",
                    "variables": {},
                    "execution_time": 0,
                    "error": error_info
                }
        
        # Fallback: No SafeExecutor available
        error_info = format_error_for_llm(
            RuntimeError("Script execution not available"),
            code
        )
        return {
            "success": False,
            "output": "",
            "variables": {},
            "execution_time": 0,
            "error": error_info
        }

    def get_sandbox_state(self) -> Dict[str, Any]:
        """Get current sandbox state (variables, etc.)."""
        return {
            "variables": dict(self.safe_executor.variables),
            "config": self.config.to_dict()
        }

    def _format_unsupported_error(self, error: Exception, code: str) -> Dict[str, Any]:
        """Format UnsupportedFeatureError as proper error result."""
        return {
            "success": False,
            "output": "",
            "variables": {},
            "execution_time": 0,
            "error": {
                "type": type(error).__name__,
                "message": str(error),
                "category": "unsupported_feature",
                "line_number": 1,  # Could be improved to find actual line
                "stack_trace": "",
                "code": code
            }
        }

    def reset_sandbox(self) -> None:
        """Reset sandbox to clean state."""
        self.safe_executor.reset()
        logger.debug("Sandbox reset")