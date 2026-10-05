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

    # Per-session sandboxes held by the server. The server read both with
    # getattr() defaults, but from_dict() drops keys that are not fields --
    # a configured value never arrived.
    session_ttl_seconds: float = 3600.0  # idle sandbox dropped after this
    max_tracked_sessions: int = 100  # least recently used dropped beyond this

    # Security settings
    allowed_functions: Optional[List[str]] = None
    enable_variables: bool = True

    def __post_init__(self):
        """Set default allowed functions if not specified."""
        if self.allowed_functions is None:
            self.allowed_functions = [
                # Basic math functions
                "abs", "min", "max", "round", "sum",
                "int", "float", "str", "bool", "type", "isinstance",
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

    @classmethod
    def from_dict(cls, config_dict: Dict[str, Any]) -> "ScriptInterpreterConfig":
        """Create config from dictionary."""
        # Filter unknown keys to maintain forwards-compatibility with larger project configs
        if not isinstance(config_dict, dict):
            return cls()
        allowed = {f.name for f in dataclass_fields(cls)}
        filtered = {k: v for k, v in config_dict.items() if k in allowed}
        return cls(**filtered)
