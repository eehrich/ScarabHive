"""Custom error types for the script interpreter."""

import builtins as _builtins
import re
from typing import Any, Optional


class ScriptInterpreterError(Exception):
    """Base exception for script interpreter errors."""
    pass


class SyntaxError(ScriptInterpreterError):
    """Raised when script has invalid syntax."""
    pass


class RuntimeError(ScriptInterpreterError):
    """Raised when script encounters a runtime error."""

    def __init__(self, message: str, original_error: Optional[Exception] = None):
        super().__init__(message)
        self.original_error = original_error


class UnsupportedFeatureError(ScriptInterpreterError):
    """Raised for a construct the sandbox does not support (import, del, break, ...)."""
    pass


def script_line_from_traceback(tb: Any) -> Optional[int]:
    """The script line an error happened on: the syntax-tree node of the
    DEEPEST interpreter frame. The outermost one, taken before, is the
    top-level statement -- a loop's first line for an error inside it."""
    line = None
    while tb:
        frame = tb.tb_frame
        if frame.f_code.co_name in ("eval_expression", "execute_ast_node"):
            node = frame.f_locals.get("node")
            if getattr(node, "lineno", None):
                line = node.lineno
        tb = tb.tb_next
    return line


_NAME_MAIN_ADVICE = ("The __name__ variable is not allowed. Remove 'if __name__ == \"__main__\":' "
                     "and call functions directly.")


def format_error_for_llm(error: Exception, code: Optional[str] = None) -> dict[str, Any]:
    """Format an error in a way that's helpful for LLMs to understand and potentially fix.

    Args:
        error: The exception that occurred
        code: The code that caused the error (optional)

    Returns:
        Dict with error information formatted for LLM consumption
    """
    error_message = str(error)
    error_info = {
        "type": type(error).__name__,
        "message": error_message,
        "category": "unknown"
    }

    # Syntax errors carry their line: Python's own as an attribute, the
    # interpreter's SyntaxError (this module's class) in its message. Any
    # other message is the script's or a tool's text -- "invalid JSON at
    # line 7" says nothing about the script's line 7.
    if isinstance(error, _builtins.SyntaxError):
        error_info["line_number"] = error.lineno
        if error.offset:
            error_info["column_number"] = error.offset
    elif isinstance(error, SyntaxError):
        line_match = re.search(r'line (\d+)', error_message)
        if line_match:
            error_info["line_number"] = int(line_match.group(1))

    # The script line of a runtime error, from the interpreter's frames. No
    # formatted traceback: nobody read it, and for a runaway recursion it
    # was 188 KB built on every failure.
    if "line_number" not in error_info and code and getattr(error, "__traceback__", None):
        user_line_number = script_line_from_traceback(error.__traceback__)
        if user_line_number:
            error_info["line_number"] = user_line_number

    # Add code context if available and line number is known
    if code and "line_number" in error_info:
        lines = code.split('\n')
        line_num = error_info["line_number"]
        if 1 <= line_num <= len(lines):
            error_info["problematic_line"] = lines[line_num - 1].strip()

            # Add context lines (2 before, 2 after)
            context_start = max(1, line_num - 2)
            context_end = min(len(lines), line_num + 2)
            context_lines = []
            for i in range(context_start, context_end + 1):
                marker = ">>> " if i == line_num else "    "
                context_lines.append(f"{marker}{i}: {lines[i-1]}")
            error_info["code_context"] = "\n".join(context_lines)

    if isinstance(error, UnsupportedFeatureError):
        error_info["category"] = "unsupported_feature"

    elif isinstance(error, SyntaxError):
        error_info["category"] = "syntax"
        error_info["suggestion"] = "Check Python syntax - parentheses, indentation, operators"

    elif isinstance(error, RuntimeError):
        error_info["category"] = "runtime"
        if "__name__" in error_message:
            error_info["suggestion"] = _NAME_MAIN_ADVICE
        else:
            error_info["suggestion"] = "Check for division by zero, undefined variables, or type errors"

    elif isinstance(error, NameError):
        error_info["category"] = "name_error"
        if "__name__" in error_message:
            error_info["suggestion"] = _NAME_MAIN_ADVICE
        else:
            error_info["suggestion"] = "Check variable names for typos and ensure variables are defined before use"

    return error_info
