"""Script Interpreter MCP Plugin

A secure sandboxed Python interpreter for LLM tool calls.
Allows LLMs to execute mathematical expressions and basic Python code safely.
"""

__version__ = "0.1.0"

# Import classes when needed to avoid circular imports
def get_executor():
    from .executor import ScriptExecutor
    return ScriptExecutor

def get_server():
    from .server import ScriptInterpreterServer
    return ScriptInterpreterServer