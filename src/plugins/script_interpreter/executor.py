"""Script execution engine with sandboxing and timeout support."""

import logging
import threading
from typing import Any, Dict, Optional

from .config import ScriptInterpreterConfig
from .safe_executor import SafeExecutor
from .errors import RuntimeError, format_error_for_llm

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
        if reset_sandbox:
            self.safe_executor.reset()
        try:
            return self.safe_executor.execute(code)
        except Exception as e:
            # SafeExecutor answers the script's errors itself; this is a bug
            # in the interpreter.
            logger.error(f"SafeExecutor error: {e}")
            return {
                "success": False,
                "output": "",
                "variables": {},
                "execution_time": 0,
                "error": format_error_for_llm(RuntimeError(str(e), e), code),
            }

    def reset_sandbox(self) -> None:
        """Reset sandbox to clean state."""
        self.safe_executor.reset()
        logger.debug("Sandbox reset")