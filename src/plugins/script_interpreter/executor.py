"""Script execution engine with sandboxing and timeout support."""

import logging
import threading
import time
from typing import Any, Dict, Optional
from sandboxed_python import execute_fpy, FPyException
import ast

from .config import ScriptInterpreterConfig
from .security import create_safe_sandbox
from .safe_executor import SafeExecutor
from .errors import (
    ExecutionTimeoutError,
    RuntimeError,
    SecurityViolationError,
    UnsupportedFeatureError,
    format_error_for_llm,
)

logger = logging.getLogger(__name__)


class TimeoutHandler:
    """Handle execution timeouts using threading."""

    def __init__(self, timeout_seconds: float):
        self.timeout_seconds = timeout_seconds
        self.timer = None
        self.timed_out = False

    def _timeout_callback(self):
        """Called when timeout occurs."""
        self.timed_out = True
        logger.warning(f"Script execution timed out after {self.timeout_seconds} seconds")

    def start(self):
        """Start the timeout timer."""
        if self.timeout_seconds > 0:
            self.timer = threading.Timer(self.timeout_seconds, self._timeout_callback)
            self.timer.start()

    def stop(self):
        """Stop the timeout timer."""
        if self.timer:
            self.timer.cancel()
            self.timer = None

    def check_timeout(self):
        """Check if timeout occurred."""
        if self.timed_out:
            raise ExecutionTimeoutError(f"Script execution exceeded {self.timeout_seconds} seconds")


class ScriptExecutor:
    """Secure script executor using sandboxed-python."""

    def __init__(self, config: Optional[ScriptInterpreterConfig] = None):
        self.config = config or ScriptInterpreterConfig()
        self.sandbox = create_safe_sandbox(self.config)
        # Use SafeExecutor when loops/advanced features are enabled
        if self.config.enable_loops or self.config.enable_functions:
            self.safe_executor = SafeExecutor(self.config)
        else:
            self.safe_executor = None

    def execute(self, code: str, reset_sandbox: bool = False) -> Dict[str, Any]:
        """Execute Python code in a secure sandbox.

        Args:
            code: Python code to execute
            reset_sandbox: Whether to reset sandbox state before execution

        Returns:
            Dict containing result, output, and metadata
        """
        # Try SafeExecutor first if available (supports loops and if statements)
        if self.safe_executor:
            if reset_sandbox:
                self.safe_executor.reset()
            try:
                return self.safe_executor.execute(code)
            except UnsupportedFeatureError as e:
                # UnsupportedFeatureError should be returned as an error, not fallback
                # Only fallback for specific features that SafeExecutor intentionally doesn't handle
                error_message = str(e)
                if any(keyword in error_message.lower() for keyword in ['import', 'global', '__name__']):
                    # These are security restrictions, not missing features - return as error
                    return self._format_unsupported_error(e, code)
                else:
                    # Other unsupported features can fallback to sandboxed_python
                    logger.debug(f"SafeExecutor fallback: {e}")
            except Exception as e:
                # Unexpected error - log and fall back
                logger.debug(f"SafeExecutor error, falling back to sandboxed_python: {e}")
        
        # Optionally clear previous state
        if reset_sandbox:
            self.sandbox.reset()
        else:
            # Just clear output buffer but keep variables
            self.sandbox.clear_output()

        # Set up timeout handler
        timeout_handler = TimeoutHandler(self.config.max_execution_time)

        try:
            # Quick AST-based pre-checks to catch obvious infinite loops or large ranges
            try:
                tree = ast.parse(code)
                for node in ast.walk(tree):
                    # Detect 'while True' loops as an immediate rejection
                    if isinstance(node, ast.While):
                        if isinstance(node.test, ast.Constant) and node.test.value is True:
                            raise SecurityViolationError("Infinite 'while True' loops are not allowed")
                    # Detect large range() calls in literals/expressions (best-effort)
                    if isinstance(node, ast.Call) and getattr(node.func, 'id', '') == 'range':
                        # Only handle simple numeric literal ranges here
                        if node.args:
                            arg = node.args[0]
                            if isinstance(arg, ast.Constant) and isinstance(arg.value, int):
                                if arg.value > self.config.max_loop_iterations:
                                    raise SecurityViolationError(f"range() too large: {arg.value} > {self.config.max_loop_iterations}")
            except RuntimeError:
                raise
            except SecurityViolationError:
                raise
            except Exception:
                # If AST parsing fails, fall back to execution and let sandbox handle errors
                pass
            timeout_handler.start()
            start_time = time.time()

            # Execute the code
            logger.debug(f"Executing code: {code[:100]}...")
            execute_fpy(code, sandbox=self.sandbox)

            timeout_handler.check_timeout()
            execution_time = time.time() - start_time

            # Get results
            output = self.sandbox.get_output()
            variables = dict(self.sandbox.variables)

            logger.debug(f"Execution completed in {execution_time:.3f}s")

            return {
                "success": True,
                "output": output,
                "variables": variables,
                "execution_time": execution_time,
                "error": None
            }

        except FPyException as e:
            # Handle sandboxed-python syntax/runtime errors - pass the FPyException directly
            error_info = format_error_for_llm(e, code)
            logger.warning(f"Sandboxed-python error in code: {e}")
            return {
                "success": False,
                "output": "",
                "variables": {},
                "execution_time": 0,
                "error": error_info
            }
        except SecurityViolationError as e:
            error_info = format_error_for_llm(e, code)
            logger.warning(f"Security violation in code: {e}")
            return {
                "success": False,
                "output": "",
                "variables": {},
                "execution_time": 0,
                "error": error_info,
            }

        except ExecutionTimeoutError as e:
            error_info = format_error_for_llm(e, code)
            logger.warning(f"Execution timeout: {e}")
            return {
                "success": False,
                "output": "",
                "variables": {},
                "execution_time": self.config.max_execution_time,
                "error": error_info
            }

        except Exception as e:
            # Handle other errors
            error_info = format_error_for_llm(RuntimeError(str(e), e), code)
            logger.error(f"Unexpected error during execution: {e}")
            return {
                "success": False,
                "output": "",
                "variables": {},
                "execution_time": 0,
                "error": error_info
            }

        finally:
            timeout_handler.stop()

    def get_sandbox_state(self) -> Dict[str, Any]:
        """Get current sandbox state (variables, etc.)."""
        return {
            "variables": dict(self.sandbox.variables),
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
        self.sandbox.reset()
        if self.safe_executor:
            self.safe_executor.reset()
        logger.debug("Sandbox reset")