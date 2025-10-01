"""Security tests for script interpreter - ensure dangerous operations are blocked."""

import pytest
from unittest.mock import AsyncMock, Mock

from agent_system.config.models import AgentSystemConfig, MCPConfig
from src.plugins.script_interpreter.server import ScriptInterpreterServer


class MockStatus:
    """Mock status object for testing."""
    def __init__(self):
        self.progress = AsyncMock()
        self.update = AsyncMock()
        self.set_error = AsyncMock()
        self.set_success = AsyncMock()
        self.error = AsyncMock()
        self.end = AsyncMock()


@pytest.fixture
async def server():
    """Create a script interpreter server for testing."""
    system_config = Mock(spec=AgentSystemConfig)
    mcp_config = MCPConfig(type="script_interpreter", enabled=True)
    server = ScriptInterpreterServer("test", system_config, mcp_config)
    yield server


@pytest.fixture
def mock_status():
    """Create mock status for testing."""
    return MockStatus()


@pytest.mark.asyncio
async def test_os_module_blocked(server, mock_status):
    """Test that os module import is blocked."""
    code = "import os"
    result = await server.call("execute_python_sandbox", {"code": code, "_status": mock_status})
    
    assert "error" in result
    error = result["error"]
    assert isinstance(error, dict)
    assert error["category"] == "unsupported_feature"
    assert "import" in error["message"].lower() or "import" in error["suggestion"].lower()


@pytest.mark.asyncio
async def test_subprocess_module_blocked(server, mock_status):
    """Test that subprocess module import is blocked."""
    code = "import subprocess"
    result = await server.call("execute_python_sandbox", {"code": code, "_status": mock_status})
    
    assert "error" in result
    error = result["error"]
    assert isinstance(error, dict)
    assert error["category"] == "unsupported_feature"


@pytest.mark.asyncio
async def test_sys_module_blocked(server, mock_status):
    """Test that sys module import is blocked."""
    code = "import sys"
    result = await server.call("execute_python_sandbox", {"code": code, "_status": mock_status})
    
    assert "error" in result
    error = result["error"]
    assert isinstance(error, dict)
    assert error["category"] == "unsupported_feature"


@pytest.mark.asyncio
async def test_file_operations_blocked_open(server, mock_status):
    """Test that file operations using open() are blocked."""
    code = "open('test.txt', 'w')"
    result = await server.call("execute_python_sandbox", {"code": code, "_status": mock_status})
    
    assert "error" in result
    # Should fail because open() is not in allowed functions


@pytest.mark.asyncio
async def test_file_operations_blocked_with(server, mock_status):
    """Test that file operations using with statement are blocked."""
    code = """
with open('test.txt', 'w') as f:
    f.write('test')
"""
    result = await server.call("execute_python_sandbox", {"code": code, "_status": mock_status})
    
    assert "error" in result
    # Should fail because open() is not allowed


@pytest.mark.asyncio
async def test_exec_function_blocked(server, mock_status):
    """Test that exec() function is blocked."""
    code = "exec('print(\"dangerous\")')"
    result = await server.call("execute_python_sandbox", {"code": code, "_status": mock_status})
    
    assert "error" in result
    # exec is not in allowed functions


@pytest.mark.asyncio
async def test_eval_function_blocked(server, mock_status):
    """Test that eval() function is blocked."""
    code = "eval('1+1')"
    result = await server.call("execute_python_sandbox", {"code": code, "_status": mock_status})
    
    assert "error" in result
    # eval is not in allowed functions


@pytest.mark.asyncio
async def test_compile_function_blocked(server, mock_status):
    """Test that compile() function is blocked."""
    code = "compile('print(1)', '<string>', 'exec')"
    result = await server.call("execute_python_sandbox", {"code": code, "_status": mock_status})
    
    assert "error" in result
    # compile is not in allowed functions


@pytest.mark.asyncio
async def test_globals_function_blocked(server, mock_status):
    """Test that globals() function is blocked."""
    code = "globals()"
    result = await server.call("execute_python_sandbox", {"code": code, "_status": mock_status})
    
    assert "error" in result
    # globals is not in allowed functions


@pytest.mark.asyncio
async def test_locals_function_blocked(server, mock_status):
    """Test that locals() function is blocked."""
    code = "locals()"
    result = await server.call("execute_python_sandbox", {"code": code, "_status": mock_status})
    
    assert "error" in result
    # locals is not in allowed functions


@pytest.mark.asyncio
async def test_vars_function_blocked(server, mock_status):
    """Test that vars() function is blocked."""
    code = "vars()"
    result = await server.call("execute_python_sandbox", {"code": code, "_status": mock_status})
    
    assert "error" in result
    # vars is not in allowed functions


@pytest.mark.asyncio
async def test_dir_function_blocked(server, mock_status):
    """Test that dir() function is blocked."""
    code = "dir()"
    result = await server.call("execute_python_sandbox", {"code": code, "_status": mock_status})
    
    assert "error" in result
    # dir is not in allowed functions


@pytest.mark.asyncio
async def test_getattr_blocked(server, mock_status):
    """Test that getattr() function is blocked."""
    code = "getattr(int, '__name__')"
    result = await server.call("execute_python_sandbox", {"code": code, "_status": mock_status})
    
    assert "error" in result
    # getattr is not in allowed functions


@pytest.mark.asyncio
async def test_setattr_blocked(server, mock_status):
    """Test that setattr() function is blocked."""
    code = "setattr(object(), 'test', 'value')"
    result = await server.call("execute_python_sandbox", {"code": code, "_status": mock_status})
    
    assert "error" in result
    # setattr is not in allowed functions


@pytest.mark.asyncio
async def test_delattr_blocked(server, mock_status):
    """Test that delattr() function is blocked."""
    code = "delattr(object(), 'test')"
    result = await server.call("execute_python_sandbox", {"code": code, "_status": mock_status})
    
    assert "error" in result
    # delattr is not in allowed functions


@pytest.mark.asyncio
async def test_hasattr_blocked(server, mock_status):
    """Test that hasattr() function is blocked."""
    code = "hasattr(int, '__name__')"
    result = await server.call("execute_python_sandbox", {"code": code, "_status": mock_status})
    
    assert "error" in result
    # hasattr is not in allowed functions


@pytest.mark.asyncio
async def test_import_inside_string_blocked(server, mock_status):
    """Test that imports hidden in strings are still blocked."""
    code = "__import__('os')"
    result = await server.call("execute_python_sandbox", {"code": code, "_status": mock_status})
    
    assert "error" in result
    # __import__ is not in allowed functions


@pytest.mark.asyncio
async def test_class_definition_with_dangerous_methods(server, mock_status):
    """Test that class definitions with dangerous methods are handled safely."""
    code = """
class Test:
    def __init__(self):
        import os
        self.os = os
"""
    result = await server.call("execute_python_sandbox", {"code": code, "_status": mock_status})
    
    assert "error" in result
    # Should fail due to import inside class


@pytest.mark.asyncio
async def test_function_definition_with_dangerous_operations(server, mock_status):
    """Test that function definitions with dangerous operations are handled."""
    code = """
def dangerous():
    import subprocess
    return subprocess.run(['ls'])

# Call the function to trigger the security violation
dangerous()
"""
    result = await server.call("execute_python_sandbox", {"code": code, "_status": mock_status})
    
    assert "error" in result
    # Should fail due to import inside function when called


@pytest.mark.asyncio
async def test_lambda_with_dangerous_operations(server, mock_status):
    """Test that lambda functions can't bypass security."""
    code = "f = lambda: __import__('os')"
    result = await server.call("execute_python_sandbox", {"code": code, "_status": mock_status})
    
    assert "error" in result
    # Should fail due to __import__ call


@pytest.mark.asyncio
async def test_list_comprehension_security(server, mock_status):
    """Test that list comprehensions can't bypass security."""
    code = "[__import__('os') for i in range(1)]"
    result = await server.call("execute_python_sandbox", {"code": code, "_status": mock_status})
    
    assert "error" in result
    # Should fail due to __import__ in list comprehension


@pytest.mark.asyncio
async def test_generator_expression_security(server, mock_status):
    """Test that generator expressions can't bypass security."""
    code = "list(__import__('os') for i in range(1))"
    result = await server.call("execute_python_sandbox", {"code": code, "_status": mock_status})
    
    assert "error" in result
    # Should fail due to __import__ in generator


@pytest.mark.asyncio
async def test_nested_function_calls_security(server, mock_status):
    """Test that nested dangerous function calls are blocked."""
    code = "print(exec('import os'))"
    result = await server.call("execute_python_sandbox", {"code": code, "_status": mock_status})
    
    assert "error" in result
    # Should fail due to exec call


@pytest.mark.asyncio
async def test_safe_operations_still_work(server, mock_status):
    """Test that safe operations continue to work despite security restrictions."""
    code = """
# Safe arithmetic and data structures
numbers = [1, 2, 3, 4, 5]
result = sum(numbers)
average = result / len(numbers)
# Use loops instead of list comprehension (which SafeExecutor supports)
squared = []
for x in numbers:
    if x > 2:
        squared.append(x * x)
print("Average: " + str(average) + ", Squared: " + str(squared))
"""
    result = await server.call("execute_python_sandbox", {"code": code, "_status": mock_status})
    
    # This should work fine
    assert "error" not in result
    assert "result" in result
    assert "Average: 3.0" in result["result"]


@pytest.mark.asyncio
async def test_string_operations_safe(server, mock_status):
    """Test that string operations work safely."""
    code = """
text = "Hello World"
upper_text = text.upper()
words = text.split()
joined = "-".join(words)
# Use string concatenation instead of f-strings
print("Upper: " + upper_text + ", Joined: " + joined)
"""
    result = await server.call("execute_python_sandbox", {"code": code, "_status": mock_status})
    
    # This should work fine
    assert "error" not in result
    assert "result" in result
    assert "HELLO WORLD" in result["result"]


@pytest.mark.asyncio
async def test_allowed_builtin_functions_work(server, mock_status):
    """Test that explicitly allowed builtin functions work correctly."""
    code = """
# Test allowed functions
numbers = [3, 1, 4, 1, 5, 9, 2, 6]
print("Original:", numbers)
print("Min:", min(numbers))
print("Max:", max(numbers))
print("Sum:", sum(numbers))
print("Len:", len(numbers))
print("Sorted:", sorted(numbers))
# Create range manually without list() function
range_items = []
for i in range(5):
    range_items.append(i)
print("Range:", range_items)
print("Round:", round(3.14159, 2))
print("Abs:", abs(-42))
print("Int:", int("123"))
print("Float:", float("3.14"))
print("Str:", str(123))
print("Bool:", bool(1))
"""
    result = await server.call("execute_python_sandbox", {"code": code, "_status": mock_status})
    
    # This should work fine
    assert "error" not in result
    assert "result" in result
    assert "Min: 1" in result["result"]
    assert "Max: 9" in result["result"]


@pytest.mark.asyncio
async def test_statistics_functions_work_safely(server, mock_status):
    """Test that built-in statistics functions work safely."""
    code = """
data = [1, 2, 3, 4, 5, 6, 7, 8, 9, 10]
print("Mean:", mean(data))
print("Median:", median(data))
print("Mode:", mode([1, 1, 2, 2, 2, 3]))
print("Stdev:", round(stdev(data), 2))
"""
    result = await server.call("execute_python_sandbox", {"code": code, "_status": mock_status})
    
    # This should work fine
    assert "error" not in result
    assert "result" in result
    assert "Mean: 5.5" in result["result"]
    assert "Median: 5.5" in result["result"]


@pytest.mark.asyncio
async def test_io_module_blocked(server, mock_status):
    """Test that io module is blocked."""
    code = "import io"
    result = await server.call("execute_python_sandbox", {"code": code, "_status": mock_status})
    
    assert "error" in result
    error = result["error"]
    assert isinstance(error, dict)
    assert error["category"] == "unsupported_feature"


@pytest.mark.asyncio
async def test_pathlib_module_blocked(server, mock_status):
    """Test that pathlib module is blocked."""
    code = "import pathlib"
    result = await server.call("execute_python_sandbox", {"code": code, "_status": mock_status})
    
    assert "error" in result
    error = result["error"]
    assert isinstance(error, dict)
    assert error["category"] == "unsupported_feature"


@pytest.mark.asyncio
async def test_shutil_module_blocked(server, mock_status):
    """Test that shutil module is blocked."""
    code = "import shutil"
    result = await server.call("execute_python_sandbox", {"code": code, "_status": mock_status})
    
    assert "error" in result
    error = result["error"]
    assert isinstance(error, dict)
    assert error["category"] == "unsupported_feature"


@pytest.mark.asyncio
async def test_socket_module_blocked(server, mock_status):
    """Test that socket module is blocked."""
    code = "import socket"
    result = await server.call("execute_python_sandbox", {"code": code, "_status": mock_status})
    
    assert "error" in result
    error = result["error"]
    assert isinstance(error, dict)
    assert error["category"] == "unsupported_feature"


@pytest.mark.asyncio
async def test_urllib_module_blocked(server, mock_status):
    """Test that urllib module is blocked."""
    code = "import urllib"
    result = await server.call("execute_python_sandbox", {"code": code, "_status": mock_status})
    
    assert "error" in result
    error = result["error"]
    assert isinstance(error, dict)
    assert error["category"] == "unsupported_feature"


@pytest.mark.asyncio
async def test_requests_module_blocked(server, mock_status):
    """Test that requests module (if available) is blocked."""
    code = "import requests"
    result = await server.call("execute_python_sandbox", {"code": code, "_status": mock_status})
    
    assert "error" in result
    error = result["error"]
    assert isinstance(error, dict)
    assert error["category"] == "unsupported_feature"


@pytest.mark.asyncio
async def test_threading_module_blocked(server, mock_status):
    """Test that threading module is blocked."""
    code = "import threading"
    result = await server.call("execute_python_sandbox", {"code": code, "_status": mock_status})
    
    assert "error" in result
    error = result["error"]
    assert isinstance(error, dict)
    assert error["category"] == "unsupported_feature"


@pytest.mark.asyncio
async def test_multiprocessing_module_blocked(server, mock_status):
    """Test that multiprocessing module is blocked."""
    code = "import multiprocessing"
    result = await server.call("execute_python_sandbox", {"code": code, "_status": mock_status})
    
    assert "error" in result
    error = result["error"]
    assert isinstance(error, dict)
    assert error["category"] == "unsupported_feature"


@pytest.mark.asyncio
async def test_pickle_module_blocked(server, mock_status):
    """Test that pickle module is blocked."""
    code = "import pickle"
    result = await server.call("execute_python_sandbox", {"code": code, "_status": mock_status})
    
    assert "error" in result
    error = result["error"]
    assert isinstance(error, dict)
    assert error["category"] == "unsupported_feature"


@pytest.mark.asyncio
async def test_dunder_import_blocked(server, mock_status):
    """Test that __import__ builtin is blocked."""
    code = "__import__('sys')"
    result = await server.call("execute_python_sandbox", {"code": code, "_status": mock_status})
    
    assert "error" in result
    # Should fail because __import__ is not in allowed functions


@pytest.mark.asyncio
async def test_builtins_access_blocked(server, mock_status):
    """Test that accessing builtins module is blocked."""
    code = "import builtins"
    result = await server.call("execute_python_sandbox", {"code": code, "_status": mock_status})
    
    assert "error" in result
    error = result["error"]
    assert isinstance(error, dict)
    assert error["category"] == "unsupported_feature"


@pytest.mark.asyncio
async def test_importlib_blocked(server, mock_status):
    """Test that importlib module is blocked."""
    code = "import importlib"
    result = await server.call("execute_python_sandbox", {"code": code, "_status": mock_status})
    
    assert "error" in result
    error = result["error"]
    assert isinstance(error, dict)
    assert error["category"] == "unsupported_feature"


@pytest.mark.asyncio
async def test_code_injection_via_string_blocked(server, mock_status):
    """Test that code injection via string manipulation is blocked."""
    code = """
dangerous_code = "import os"
exec(dangerous_code)
"""
    result = await server.call("execute_python_sandbox", {"code": code, "_status": mock_status})
    
    assert "error" in result
    # Should fail because exec() is not allowed


@pytest.mark.asyncio
async def test_attribute_access_on_types_safe(server, mock_status):
    """Test that attribute access on safe types works but dangerous access is blocked."""
    code = """
# Safe attribute access
text = "hello"
print("Length method exists:", hasattr(text, 'upper'))
"""
    result = await server.call("execute_python_sandbox", {"code": code, "_status": mock_status})
    
    assert "error" in result
    # Should fail because hasattr is not allowed


@pytest.mark.asyncio
async def test_complex_calculation_works(server, mock_status):
    """Test that complex but safe calculations work."""
    code = """
# Complex mathematical operations
data = [1, 2, 3, 4, 5, 6, 7, 8, 9, 10]
total = sum(data)
count = len(data)
average = total / count
variance = 0
for x in data:
    variance = variance + (x - average) * (x - average)
variance = variance / count
std_dev = variance ** 0.5
print("Data analysis complete")
print("Total:", total)
print("Average:", round(average, 2))
print("Std Dev:", round(std_dev, 2))
"""
    result = await server.call("execute_python_sandbox", {"code": code, "_status": mock_status})
    
    # This should work fine
    assert "error" not in result
    assert "result" in result
    assert "Total: 55" in result["result"]
    assert "Average: 5.5" in result["result"]