"""Custom error types for the script interpreter."""

import builtins as _builtins
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


class UnsupportedFeatureError(ScriptInterpreterError):
    """Raised when SafeExecutor encounters unsupported AST nodes that should fall back to sandboxed_python."""
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


def format_error_for_llm(error: Exception, code: Optional[str] = None) -> dict[str, Any]:
    """Format an error in a way that's helpful for LLMs to understand and potentially fix.

    Args:
        error: The exception that occurred
        code: The code that caused the error (optional)

    Returns:
        Dict with error information formatted for LLM consumption
    """
    import traceback
    import re
    
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

    # Add stack trace for runtime errors
    if hasattr(error, '__traceback__') and error.__traceback__:
        tb_lines = traceback.format_exception(type(error), error, error.__traceback__)
        error_info["stack_trace"] = ''.join(tb_lines)
        if "line_number" not in error_info and code:
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

    # Intelligent error analysis for FPyException with root cause detection
    if hasattr(error, 'error') and hasattr(error.error, 'message'):
        try:
            from sandboxed_python import FPyParseError, FPyExecError
            import ast
            import re
            
            fpy_error = error.error
            message = fpy_error.message
            location = getattr(fpy_error, 'location', None)
            
            # Extract location information
            if location:
                error_info["line_number"] = location.line
                error_info["column_number"] = location.column
                if hasattr(location, 'source') and location.source:
                    error_info["failing_code"] = location.source
            
            # Case 1: FPyExecError - Runtime errors (missing functions, security violations)
            if isinstance(fpy_error, FPyExecError):
                error_info["category"] = "runtime_error"
                
                # Extract function name from "Function 'X' is not allowed" messages
                func_match = re.search(r"Function '([^']+)' is not allowed", message)
                var_match = re.search(r"Variable '([^']+)' is not allowed", message)
                access_match = re.search(r"Access to '([^']+)' is not allowed", message)
                
                if func_match:
                    missing_func = func_match.group(1)
                    error_info["missing_function"] = missing_func
                    error_info["construct_type"] = "missing_function_call"
                    error_info["suggestion"] = f"Function '{missing_func}' is not available in the sandbox."
                    
                    # Suggest alternatives for common missing functions
                    alternatives = {
                        'factorial': 'Use math operations: e.g., 5*4*3*2*1 for factorial(5)',
                        'eval': 'Dynamic code evaluation is not allowed for security',
                        'exec': 'Dynamic code execution is not allowed for security', 
                        'open': 'File operations are not allowed for security',
                        'input': 'User input functions are not available in sandbox',
                        'random': 'Use deterministic calculations instead of random numbers',
                        'datetime': 'Date/time functions are not available',
                        'json': 'JSON parsing is not available, use simple data structures',
                        'hashlib': 'Cryptographic functions are not available',
                        'os': 'Operating system functions are not available for security',
                        'sys': 'System functions are not available for security',
                    }
                    
                    if missing_func in alternatives:
                        error_info["alternative"] = alternatives[missing_func]
                    else:
                        error_info["alternative"] = "Use built-in mathematical and string functions instead"
                
                elif var_match or access_match:
                    blocked_var = var_match.group(1) if var_match else access_match.group(1)
                    error_info["blocked_variable"] = blocked_var
                    error_info["construct_type"] = "blocked_variable_access"
                    
                    if blocked_var == '__name__':
                        error_info["suggestion"] = "The __name__ variable is not allowed in the sandbox. Instead of using 'if __name__ == \"__main__\":', call your functions directly at the module level."
                        error_info["alternative"] = "Remove the 'if __name__ == \"__main__\":' guard and call your functions directly. Example: Replace 'if __name__ == \"__main__\": main()' with just 'main()'"
                        error_info["example"] = """# Instead of:
if __name__ == "__main__":
    result = my_function()
    print(result)

# Use:
result = my_function()
print(result)"""
                    elif blocked_var in ['__builtins__', '__import__', '__file__']:
                        error_info["suggestion"] = f"The {blocked_var} variable is not allowed for security reasons."
                        error_info["alternative"] = "Use only standard variables and functions provided by the sandbox"
                    else:
                        error_info["suggestion"] = f"Access to '{blocked_var}' is not allowed in the sandbox."
                        error_info["alternative"] = "Use standard Python variables and functions instead"
                
                else:
                    error_info["suggestion"] = "Runtime error in function execution. Check function arguments and availability."
            
            # Case 2: FPyParseError - Parser rejections (could be construct or hidden function issue)  
            elif isinstance(fpy_error, FPyParseError):
                error_info["category"] = "parse_error"
                
                if message.startswith("Unsupported statement:"):
                    # This might be a construct rejection due to unsupported function calls inside
                    statement_code = location.source if location and location.source else message[22:].strip()
                    
                    # Parse the code to find function calls
                    try:
                        # Get current allowed functions from config
                        from .config import ScriptInterpreterConfig
                        config = ScriptInterpreterConfig()
                        allowed_functions = set(config.allowed_functions or [])
                        
                        # Parse AST to find function calls
                        tree = ast.parse(statement_code)
                        function_calls = []
                        
                        for node in ast.walk(tree):
                            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                                function_calls.append(node.func.id)
                        
                        # Check if any function calls are not allowed
                        disallowed_calls = [func for func in function_calls if func not in allowed_functions]
                        
                        if disallowed_calls:
                            # The real issue is missing function(s), not the construct itself
                            error_info["construct_type"] = "missing_function_in_construct"
                            error_info["missing_functions"] = disallowed_calls
                            error_info["suggestion"] = f"The statement contains unsupported function(s): {', '.join(disallowed_calls)}. The construct itself (def, for, etc.) is supported, but these function calls are not allowed."
                            error_info["example"] = f"Replace {disallowed_calls[0]}() with supported functions like: print(), min(), max(), sum(), len(), etc."
                            
                        else:
                            # No disallowed functions found - the construct itself might be unsupported
                            source_first_word = statement_code.strip().split()[0] if statement_code.strip() else ""
                            
                            # Only certain constructs are truly unsupported in sandboxed_python
                            truly_unsupported = ["class", "async", "with", "try", "except", "finally", "import", "from"]
                            
                            if source_first_word in truly_unsupported:
                                error_info["construct_type"] = "unsupported_construct"
                                error_info["suggestion"] = f"The '{source_first_word}' statement is not supported in sandboxed_python mode."
                                
                                construct_alternatives = {
                                    'class': 'Use dict() and simple data structures instead of classes',
                                    'async': 'Asynchronous operations are not supported',
                                    'with': 'Context managers are not supported, use direct operations',
                                    'try': 'Exception handling blocks are not supported',
                                    'import': 'Imports are not allowed for security reasons',
                                    'from': 'Imports are not allowed for security reasons',
                                }
                                error_info["alternative"] = construct_alternatives.get(source_first_word, "Use simpler expressions")
                            elif source_first_word in ["def", "for", "while", "if"]:
                                # These constructs ARE supported, so this is likely a parsing issue or other problem
                                error_info["construct_type"] = "supported_construct_parsing_issue"
                                error_info["suggestion"] = f"The '{source_first_word}' statement is supported, but there may be a syntax error or unsupported syntax within it. Check for proper indentation and syntax."
                                error_info["alternative"] = "Verify the syntax is correct and all function calls within the construct are allowed"
                            else:
                                error_info["construct_type"] = "unknown_statement_issue"
                                error_info["suggestion"] = "This statement structure may not be supported in sandboxed_python mode."
                                
                    except Exception as parse_err:
                        error_info["construct_type"] = "parse_analysis_failed"
                        error_info["suggestion"] = "Could not analyze the statement structure. Use simple expressions and built-in functions."
                        error_info["analysis_error"] = str(parse_err)
                
                elif message.startswith("Unsupported expression:"):
                    # Expression-level rejections (Lambda, JoinedStr, etc.)
                    expr_type = message[23:].strip()
                    error_info["construct_type"] = "unsupported_expression"
                    error_info["unsupported_expression"] = expr_type
                    
                    expression_alternatives = {
                        'Lambda': 'Lambda functions are not supported. Use simple expressions or separate function definitions.',
                        'JoinedStr': 'F-strings are not supported. Use string concatenation: "Value: " + str(x)',
                        'IfExp': 'Ternary operators (x if condition else y) are not supported. Use regular if statements.',
                        'ListComp': 'List comprehensions are not supported. Use simple loops or built-in functions.',
                        'DictComp': 'Dictionary comprehensions are not supported. Build dictionaries step by step.',
                        'SetComp': 'Set comprehensions are not supported. Use set() constructor with lists.',
                    }
                    
                    error_info["suggestion"] = expression_alternatives.get(expr_type, f"Expression type '{expr_type}' is not supported.")
                
                elif message.startswith("Invalid Python syntax:"):
                    # Syntax errors
                    error_info["construct_type"] = "syntax_error"
                    error_info["suggestion"] = "There is a Python syntax error in your code. Check for missing parentheses, colons, proper indentation, etc."
                    error_info["syntax_details"] = message[23:].strip()  # Extract the specific syntax issue
                
                # Add note about SafeExecutor vs sandboxed_python
                error_info["note"] = "This error comes from sandboxed_python fallback. The SafeExecutor supports more constructs but may have failed due to missing functions."
        
        except Exception as analysis_error:
            # If analysis fails, provide basic guidance
            error_info["category"] = "analysis_failed"
            error_info["suggestion"] = "Could not analyze the error structure. Use simple expressions and built-in functions."
            error_info["analysis_error"] = str(analysis_error)
    
    # Legacy fallback handlers - only used if intelligent analysis failed
    # Note: These should rarely trigger now that we have intelligent FPyException analysis above
        
    elif isinstance(error, SecurityViolationError):
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
        # Check if this is a __name__ error
        if "__name__" in error_message:
            error_info["suggestion"] = "The __name__ variable is not allowed. Remove 'if __name__ == \"__main__\":' and call functions directly."
            error_info["alternative"] = "Instead of using if __name__ == '__main__': guard, call your functions directly at module level."
            error_info["example"] = """# Don't use:
if __name__ == "__main__":
    result = my_function()

# Use instead:
result = my_function()"""
        else:
            error_info["suggestion"] = "Check for division by zero, undefined variables, or type errors"
    
    elif isinstance(error, NameError):
        error_info["category"] = "name_error"
        # Check if this is trying to access __name__
        if "__name__" in error_message:
            error_info["suggestion"] = "The __name__ variable is not available in the sandbox. Remove 'if __name__ == \"__main__\":' guard and call functions directly."
            error_info["alternative"] = "Call your functions directly instead of using if __name__ == '__main__': pattern."
            error_info["example"] = """# Don't use:
if __name__ == "__main__":
    main()

# Use instead:  
main()"""
        else:
            error_info["suggestion"] = "Check variable names for typos and ensure variables are defined before use"
        
    elif isinstance(error, MemoryLimitError):
        error_info["category"] = "memory"
        error_info["suggestion"] = "Reduce data size or complexity"
        
    elif isinstance(error, OutputTooLargeError):
        error_info["category"] = "output"
        error_info["suggestion"] = "Reduce output size or use summarization"

    # Add available functions list for unsupported features
    if error_info["category"] == "unsupported_feature":
        error_info["available_functions"] = [
            "print()", "min()", "max()", "sum()", "len()", "range()", "sorted()",
            "mean()", "median()", "mode()", "stdev()", "round()", "abs()",
            "int()", "float()", "str()", "bool()"
        ]

    if code:
        error_info["code"] = code

    return error_info