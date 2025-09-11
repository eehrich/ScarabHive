"""Script execution engine with sandboxing and timeout support."""

import logging
import threading
import time
from typing import Any, Dict
from sandboxed_python import execute_fpy, FPyException

from .config import ScriptInterpreterConfig
from .security import create_safe_sandbox
from .errors import (
    ExecutionTimeoutError,
    SyntaxError,
    RuntimeError,
    format_error_for_llm
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
    
    def __init__(self, config: ScriptInterpreterConfig | None = None):
        self.config = config or ScriptInterpreterConfig()
        self.sandbox = create_safe_sandbox(self.config)
        
    def execute(self, code: str, reset_sandbox: bool = False) -> Dict[str, Any]:
        """Execute Python code in a secure sandbox.
        
        Args:
            code: Python code to execute
            reset_sandbox: Whether to reset sandbox state before execution
            
        Returns:
            Dict containing result, output, and metadata
        """
        # Optionally clear previous state
        if reset_sandbox:
            self.sandbox.reset()
        else:
            # Just clear output buffer but keep variables
            self.sandbox.clear_output()
        
        # Set up timeout handler
        timeout_handler = TimeoutHandler(self.config.max_execution_time)
        
        try:
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
            # Handle sandboxed-python syntax/runtime errors
            error_info = format_error_for_llm(SyntaxError(str(e)), code)
            logger.warning(f"Syntax error in code: {e}")
            return {
                "success": False,
                "output": "",
                "variables": {},
                "execution_time": 0,
                "error": error_info
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
    
    def validate_syntax(self, code: str) -> Dict[str, Any]:
        """Validate Python syntax without executing.
        
        Args:
            code: Python code to validate
            
        Returns:
            Dict with validation results
        """
        try:
            # Try to compile with sandboxed-python to check syntax
            # This is a basic check - full validation happens during execution
            compile(code, '<string>', 'exec')
            return {
                "valid": True,
                "error": None
            }
        except SyntaxError as e:
            return {
                "valid": False,
                "error": format_error_for_llm(SyntaxError(str(e)), code)
            }
        except Exception as e:
            return {
                "valid": False,
                "error": format_error_for_llm(RuntimeError(str(e)), code)
            }
    
    def get_sandbox_state(self) -> Dict[str, Any]:
        """Get current sandbox state (variables, etc.)."""
        return {
            "variables": dict(self.sandbox.variables),
            "config": self.config.to_dict()
        }
    
    def reset_sandbox(self) -> None:
        """Reset sandbox to clean state."""
        self.sandbox.reset()
        logger.debug("Sandbox reset")