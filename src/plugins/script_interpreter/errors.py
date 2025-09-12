"""Custom error types for the script interpreter."""

from typing import Any, Optional


class ScriptInterpreterError(Exception):
    """Base exception for script interpreter errors."""
    pass


class SecurityViolationError(ScriptInterpreterError):
    """Raised when code attempts to perform a prohibited operation."""
    pass


class ExecutionTimeoutError(ScriptInterpreterError):
    """Raised when script execution exceeds the time limit."""
    pass


class SyntaxError(ScriptInterpreterError):
    """Raised when script has invalid syntax."""
    pass


class RuntimeError(ScriptInterpreterError):
    """Raised when script encounters a runtime error."""

    def __init__(self, message: str, original_error: Optional[Exception] = None):
        super().__init__(message)
        self.original_error = original_error


class MemoryLimitError(ScriptInterpreterError):
    """Raised when script exceeds memory limits."""
    pass


class OutputTooLargeError(ScriptInterpreterError):
    """Raised when script output exceeds size limits."""
    pass


def format_error_for_llm(error: Exception, code: Optional[str] = None) -> dict[str, Any]:
    """Format an error in a way that's helpful for LLMs to understand and potentially fix.

    Args:
        error: The exception that occurred
        code: The code that caused the error (optional)

    Returns:
        Dict with error information formatted for LLM consumption
    """
    error_info = {
        "type": type(error).__name__,
        "message": str(error),
        "category": "unknown"
    }

    # Categorize errors for better LLM understanding
    if isinstance(error, SecurityViolationError):
        error_info["category"] = "security"
        error_info["suggestion"] = "Use only allowed functions and operations"
    elif isinstance(error, ExecutionTimeoutError):
        error_info["category"] = "timeout"
        error_info["suggestion"] = "Simplify the computation or reduce iterations"
    elif isinstance(error, SyntaxError):
        error_info["category"] = "syntax"
        error_info["suggestion"] = "Check Python syntax - parentheses, indentation, operators"
    elif isinstance(error, RuntimeError):
        error_info["category"] = "runtime"
        error_info["suggestion"] = "Check for division by zero, undefined variables, or type errors"
    elif isinstance(error, MemoryLimitError):
        error_info["category"] = "memory"
        error_info["suggestion"] = "Reduce data size or complexity"
    elif isinstance(error, OutputTooLargeError):
        error_info["category"] = "output"
        error_info["suggestion"] = "Reduce output size or use summarization"

    if code:
        error_info["code"] = code

    return error_info