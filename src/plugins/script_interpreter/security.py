"""Security policies and safe environments for the script interpreter."""

import logging
from typing import Any, List, Dict, Optional
from sandboxed_python import PySandbox, SourceLocation, Undef
from .config import ScriptInterpreterConfig
from .errors import SecurityViolationError

logger = logging.getLogger(__name__)


class SecureSandbox(PySandbox):
    """Secure sandbox for executing Python code with configurable restrictions."""

    def __init__(self, config: ScriptInterpreterConfig):
        self.config = config
        self.variables: Dict[str, Any] = {}
        self.location: Optional[SourceLocation] = None
        self.output_buffer: List[str] = []

    def get_location(self) -> Optional[SourceLocation]:
        return self.location

    def set_location(self, location: SourceLocation) -> None:
        self.location = location

    def func_call(self, func_name: str, args: List[Any], location: SourceLocation) -> Any:
        """Handle function calls with security restrictions."""
        # Check if function is allowed
        allowed_functions = self.config.allowed_functions or []
        if func_name not in allowed_functions:
            raise SecurityViolationError(f"Function '{func_name}' is not allowed")

        # Execute allowed functions
        try:
            if func_name == "abs":
                return abs(args[0])
            elif func_name == "min":
                if len(args) == 1:
                    # min([1,2,3]) -> min of list
                    return min(args[0])
                else:
                    # min(1,2,3) -> min of arguments
                    return min(args)
            elif func_name == "max":
                if len(args) == 1:
                    # max([1,2,3]) -> max of list
                    return max(args[0])
                else:
                    # max(1,2,3) -> max of arguments
                    return max(args)
            elif func_name == "round":
                if len(args) == 1:
                    return round(args[0])
                else:
                    return round(args[0], args[1])
            elif func_name == "sum":
                if len(args) == 1:
                    return sum(args[0])
                elif len(args) == 2:
                    return sum(args[0], args[1])
                else:
                    return sum(args[0])
            elif func_name == "int":
                return int(args[0])
            elif func_name == "float":
                return float(args[0])
            elif func_name == "str":
                return str(args[0])
            elif func_name == "bool":
                return bool(args[0])
            elif func_name == "len":
                return len(args[0])
            elif func_name == "range":
                # Prevent creating huge ranges/lists
                try:
                    # Compute an approximate length for common signatures
                    if len(args) == 1 and isinstance(args[0], int):
                        length = args[0]
                    elif len(args) == 2 and all(isinstance(a, int) for a in args):
                        length = max(0, args[1] - args[0])
                    elif len(args) == 3 and all(isinstance(a, int) for a in args):
                        start, stop, step = args
                        if step == 0:
                            raise SecurityViolationError("range() step argument cannot be zero")
                        length = max(0, (stop - start + (step - 1)) // step)
                    else:
                        # Unknown dynamic args - disallow for safety
                        raise SecurityViolationError("Dynamic range() arguments are not allowed")

                    if length > self.config.max_loop_iterations:
                        raise SecurityViolationError(f"range() size {length} exceeds allowed maximum of {self.config.max_loop_iterations}")

                    return list(range(*args))  # Convert to list for security
                except SecurityViolationError:
                    raise
                except Exception as e:
                    raise SecurityViolationError(f"Error evaluating range(): {e}")
            elif func_name == "print":
                # Handle print function - capture output to buffer
                if not args:
                    output = ""
                else:
                    # Convert all args to strings and join with spaces (like Python print)
                    str_args = [str(arg) for arg in args]
                    output = " ".join(str_args)
                
                self.output_buffer.append(output)
                
                # Check output size limit
                total_output = "\n".join(self.output_buffer)
                if len(total_output) > self.config.max_output_length:
                    from .errors import OutputTooLargeError
                    raise OutputTooLargeError("Output exceeds maximum allowed length")
                
                return None  # print() returns None
            elif func_name == "sorted":
                if len(args) == 1:
                    return sorted(args[0])
                else:
                    # sorted() with key or reverse - simplified for safety
                    raise SecurityViolationError("sorted() with additional arguments not supported")
            elif func_name == "mean":
                # Built-in statistics function
                if len(args) != 1:
                    raise SecurityViolationError("mean() takes exactly one argument")
                values = args[0]
                if not values:
                    raise SecurityViolationError("mean() requires non-empty sequence")
                return sum(values) / len(values)
            elif func_name == "median":
                # Built-in statistics function
                if len(args) != 1:
                    raise SecurityViolationError("median() takes exactly one argument")
                values = sorted(args[0])
                n = len(values)
                if n == 0:
                    raise SecurityViolationError("median() requires non-empty sequence")
                if n % 2 == 1:
                    return values[n // 2]
                else:
                    return (values[n // 2 - 1] + values[n // 2]) / 2
            elif func_name == "mode":
                # Built-in statistics function - most frequent value
                if len(args) != 1:
                    raise SecurityViolationError("mode() takes exactly one argument")
                values = args[0]
                if not values:
                    raise SecurityViolationError("mode() requires non-empty sequence")
                # Count frequencies
                counts = {}
                for value in values:
                    counts[value] = counts.get(value, 0) + 1
                # Find most frequent
                max_count = max(counts.values())
                modes = [k for k, v in counts.items() if v == max_count]
                return modes[0]  # Return first mode found
            elif func_name == "stdev":
                # Built-in standard deviation function
                if len(args) != 1:
                    raise SecurityViolationError("stdev() takes exactly one argument")
                values = args[0]
                if len(values) < 2:
                    raise SecurityViolationError("stdev() requires at least 2 values")
                mean_val = sum(values) / len(values)
                variance = sum((x - mean_val) ** 2 for x in values) / (len(values) - 1)
                return variance ** 0.5
            else:
                raise SecurityViolationError(f"Function '{func_name}' is not implemented")

        except Exception as e:
            raise SecurityViolationError(f"Error executing function '{func_name}': {e}")

    def method_call(self, subject: Any, method_name: str, args: List[Any], location: SourceLocation) -> Any:
        """Handle method calls with security restrictions."""
        # Only allow safe string methods for now
        if isinstance(subject, str) and method_name in ["upper", "lower", "strip", "replace"]:
            try:
                return getattr(subject, method_name)(*args)
            except Exception as e:
                raise SecurityViolationError(f"Error executing method '{method_name}': {e}")

        # Only allow safe list methods
        if isinstance(subject, list) and method_name in ["append", "extend", "count", "index"]:
            try:
                return getattr(subject, method_name)(*args)
            except Exception as e:
                raise SecurityViolationError(f"Error executing method '{method_name}': {e}")

        raise SecurityViolationError(f"Method '{method_name}' is not allowed on {type(subject).__name__}")

    def get_var(self, name: str) -> Any:
        """Get variable value."""
        if not self.config.enable_variables and name not in ["__builtins__"]:
            raise SecurityViolationError("Variable access is disabled")

        return self.variables.get(name, Undef.undef)

    def set_var(self, name: str, value: Any) -> None:
        """Set variable value."""
        if not self.config.enable_variables:
            raise SecurityViolationError("Variable assignment is disabled")

        # Check for dangerous variable names
        if name.startswith("_") or name in ["__builtins__", "__import__", "eval", "exec"]:
            raise SecurityViolationError(f"Variable name '{name}' is not allowed")

        if isinstance(value, Undef):
            self.variables.pop(name, None)
        else:
            self.variables[name] = value

    def list_vars(self) -> List[str]:
        """List available variables."""
        return list(self.variables.keys())

    def display(self, value: Any) -> None:
        """Handle display output (e.g., from expressions at the end of lines)."""
        # Don't display None values (e.g., from print() function calls)
        if value is not None:
            output = str(value)
            self.output_buffer.append(output)

            # Check output size limit
            total_output = "\\n".join(self.output_buffer)
            if len(total_output) > self.config.max_output_length:
                from .errors import OutputTooLargeError
                raise OutputTooLargeError("Output exceeds maximum allowed length")

    def exception_to_message(self, exception: Exception) -> str:
        """Convert exception to user-friendly message."""
        return str(exception)

    def get_output(self) -> str:
        """Get all output generated by the script."""
        return "\\n".join(self.output_buffer)

    def clear_output(self) -> None:
        """Clear the output buffer."""
        self.output_buffer.clear()

    def reset(self) -> None:
        """Reset the sandbox state."""
        self.variables.clear()
        self.output_buffer.clear()
        self.location = None


def create_safe_sandbox(config: ScriptInterpreterConfig) -> SecureSandbox:
    """Create a secure sandbox with the given configuration."""
    return SecureSandbox(config)