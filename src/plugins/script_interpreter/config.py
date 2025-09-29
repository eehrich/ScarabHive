"""Configuration for Script Interpreter Plugin"""

from dataclasses import dataclass
from dataclasses import fields as dataclass_fields
from typing import Dict, Any, List, Optional
import logging

logger = logging.getLogger(__name__)


@dataclass
class ScriptInterpreterConfig:
    """Configuration for the script interpreter."""

    # Execution limits
    max_execution_time: float = 5.0  # seconds
    max_memory_mb: int = 50  # MB (where possible to enforce)
    max_output_length: int = 10000  # characters
    max_loop_iterations: int = 100000  # safety cap for loops/range()
    loop_timeout_seconds: float = 2.0  # timeout for individual loops

    # Security settings
    allowed_functions: Optional[List[str]] = None
    allowed_modules: Optional[List[str]] = None
    enable_variables: bool = True
    enable_loops: bool = True  # Enable loops for Task 9063
    enable_functions: bool = True  # Enable function definitions for Task 9063

    def __post_init__(self):
        """Set default allowed functions if not specified."""
        if self.allowed_functions is None:
            self.allowed_functions = [
                # Basic math functions
                "abs", "min", "max", "round", "sum",
                "int", "float", "str", "bool", "type",
                "len", "range", "sorted", "enumerate",
                # Advanced math functions
                "sqrt", "sin", "cos", "tan", "log", "log10", "exp", "floor", "ceil", "pow", 
                "pi", "e", "degrees", "radians", "asin", "acos", "atan", "sinh", "cosh", "tanh",
                # I/O functions
                "print",
                # Statistics functions (built-in)
                "mean", "median", "mode", "stdev",
                # Collection constructors
                "list", "tuple", "dict", "set",
                # String/number formatting functions
                "format", "hex", "bin", "oct", "chr", "ord",
                # Exception constructors
                "ValueError", "RuntimeError", "TypeError",
                # Math operations are handled by operators, not functions
            ]

        if self.allowed_modules is None:
            self.allowed_modules = []  # No modules allowed by default

    @classmethod
    def from_dict(cls, config_dict: Dict[str, Any]) -> "ScriptInterpreterConfig":
        """Create config from dictionary."""
        # Filter unknown keys to maintain forwards-compatibility with larger project configs
        if not isinstance(config_dict, dict):
            return cls()
        allowed = {f.name for f in dataclass_fields(cls)}
        filtered = {k: v for k, v in config_dict.items() if k in allowed}
        return cls(**filtered)

    def to_dict(self) -> Dict[str, Any]:
        """Convert config to dictionary."""
        return {
            "max_execution_time": self.max_execution_time,
            "max_memory_mb": self.max_memory_mb,
            "max_output_length": self.max_output_length,
            "max_loop_iterations": self.max_loop_iterations,
            "loop_timeout_seconds": self.loop_timeout_seconds,
            "allowed_functions": self.allowed_functions,
            "allowed_modules": self.allowed_modules,
            "enable_variables": self.enable_variables,
            "enable_loops": self.enable_loops,
            "enable_functions": self.enable_functions,
        }