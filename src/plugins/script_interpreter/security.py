"""Security policies and safe environments for the script interpreter."""

import logging
from typing import Any, List, Dict
from sandboxed_python import PySandbox, SourceLocation, Undef
from .config import ScriptInterpreterConfig
from .errors import SecurityViolationError

logger = logging.getLogger(__name__)


class SecureSandbox(PySandbox):
    """Secure sandbox for executing Python code with configurable restrictions."""
    
    def __init__(self, config: ScriptInterpreterConfig):
        self.config = config
        self.variables: Dict[str, Any] = {}
        self.location: SourceLocation | None = None
        self.output_buffer: List[str] = []
        
    def get_location(self) -> SourceLocation | None:
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
                return min(args)
            elif func_name == "max":
                return max(args)
            elif func_name == "round":
                if len(args) == 1:
                    return round(args[0])
                else:
                    return round(args[0], args[1])
            elif func_name == "sum":
                return sum(args)
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
                return list(range(*args))  # Convert to list for security
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