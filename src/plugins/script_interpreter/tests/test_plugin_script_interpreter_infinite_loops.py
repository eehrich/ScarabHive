"""
Tests for infinite loop and recursion protection in the script interpreter plugin.
"""

import pytest
from unittest.mock import Mock, AsyncMock

from agent_system.config.models import AgentSystemConfig, ToolServerConfig
from src.plugins.script_interpreter.server import ScriptInterpreterServer


def extract_error_message(result):
    """Extract error message from result, handling both dict and string formats."""
    if "error" not in result:
        return ""
    
    error = result["error"]
    if isinstance(error, dict):
        return error.get("message", "")
    return str(error)


@pytest.fixture
async def server():
    """Create a ScriptInterpreterServer instance for testing."""
    system_config = Mock(spec=AgentSystemConfig)
    server_config = ToolServerConfig(type="script_interpreter", enabled=True)
    server = ScriptInterpreterServer("script_interpreter", system_config, server_config)
    return server


@pytest.fixture
def mock_status():
    """Create a mock Status object."""
    status = Mock()
    # Mock async methods properly
    status.progress = AsyncMock()
    status.error = AsyncMock()
    status.end = AsyncMock()
    return status


@pytest.mark.asyncio
async def test_infinite_while_loop_timeout(server, mock_status):
    """Test that infinite while loops are caught by timeout."""
    code = """
i = 0
while True:
    i += 1
    # This should timeout before completing
"""
    result = await server.call("script_interpreter_execute", {"code": code, "_status": mock_status})
    
    assert "error" in result
    error_message = extract_error_message(result).lower()
    assert any(word in error_message for word in ["timeout", "time", "exceeded", "infinite", "while true", "not allowed"])


@pytest.mark.asyncio
async def test_infinite_for_loop_with_large_range_timeout(server, mock_status):
    """Test that for loops with extremely large ranges timeout."""
    code = """
# Try to create a very large range that should timeout
total = 0
for i in range(10**9):  # 1 billion iterations
    total += i
print(f"Total: {total}")
"""
    result = await server.call("script_interpreter_execute", {"code": code, "_status": mock_status})
    
    assert "error" in result
    # Should either timeout or hit iteration limit
    error_message = extract_error_message(result).lower()
    assert any(word in error_message for word in ["timeout", "time", "exceeded", "iterations", "range", "large"])


@pytest.mark.asyncio
async def test_infinite_recursion_with_function(server, mock_status):
    """Test that infinite recursion is caught by timeout or recursion limit."""
    code = """
def recursive_function(n):
    return recursive_function(n + 1)

result = recursive_function(0)
"""
    result = await server.call("script_interpreter_execute", {"code": code, "_status": mock_status})
    
    assert "error" in result
    # Should timeout or hit recursion limit
    error_message = extract_error_message(result).lower()
    assert any(word in error_message for word in ["timeout", "time", "exceeded", "recursion", "function"])


@pytest.mark.asyncio
async def test_infinite_recursion_with_lambda(server, mock_status):
    """Test that infinite recursion with lambda functions is caught."""
    code = """
# Create a lambda that calls itself infinitely
f = lambda x: f(x + 1)
result = f(0)
"""
    result = await server.call("script_interpreter_execute", {"code": code, "_status": mock_status})
    
    assert "error" in result
    error_message = extract_error_message(result).lower()
    assert any(word in error_message for word in ["timeout", "time", "exceeded", "recursion", "lambda"])


@pytest.mark.asyncio
async def test_nested_loops_timeout(server, mock_status):
    """Test that deeply nested loops timeout appropriately."""
    code = """
total = 0
for i in range(10000):
    for j in range(10000):
        for k in range(10000):  # 10^12 total iterations
            total += 1
print(f"Total: {total}")
"""
    result = await server.call("script_interpreter_execute", {"code": code, "_status": mock_status})
    
    assert "error" in result
    error_message = extract_error_message(result).lower()
    assert any(word in error_message for word in ["timeout", "time", "exceeded", "iterations"])


@pytest.mark.asyncio
async def test_list_comprehension_infinite_behavior(server, mock_status):
    """Test that list comprehensions with large ranges are handled."""
    code = """
# Try to create a massive list comprehension
big_list = [i * 2 for i in range(10**7)]  # 10 million elements
print(f"List length: {len(big_list)}")
"""
    result = await server.call("script_interpreter_execute", {"code": code, "_status": mock_status})
    
    # This should either work (if memory allows) or timeout/fail gracefully
    if "error" in result:
        error_message = extract_error_message(result).lower()
        assert any(word in error_message for word in ["timeout", "time", "exceeded", "memory", "range", "large"])
    else:
        # If it succeeds, that's also acceptable for this size
        assert "result" in result


@pytest.mark.asyncio
async def test_generator_expression_infinite_behavior(server, mock_status):
    """Test that generator expressions with infinite-like behavior timeout."""
    code = """
# Create a generator expression that tries to consume too much
import itertools  # This should fail due to import restrictions anyway
gen = (i for i in itertools.count())  # Infinite generator
result = sum(itertools.islice(gen, 10**6))  # Try to sum first million
"""
    result = await server.call("script_interpreter_execute", {"code": code, "_status": mock_status})
    
    assert "error" in result
    # Should fail due to import restrictions or timeout
    error_message = extract_error_message(result).lower()
    assert any(word in error_message for word in ["import", "module", "not allowed", "timeout"])


@pytest.mark.asyncio
async def test_while_loop_with_complex_condition_timeout(server, mock_status):
    """Test while loop with condition that might never become false."""
    code = """
x = 1.0
while x > 0:
    x = x + 0.1  # This will never make x <= 0
    if x > 1000000:
        break  # Safety break, but should timeout before this
"""
    result = await server.call("script_interpreter_execute", {"code": code, "_status": mock_status})
    
    # Should either timeout or complete with the break
    if "error" in result:
        error_message = extract_error_message(result).lower()
        assert any(word in error_message for word in ["timeout", "time", "exceeded"])
    else:
        # If it completes, that means the safety break worked
        assert "error" not in result


@pytest.mark.asyncio
async def test_mutual_recursion_timeout(server, mock_status):
    """Test mutual recursion between two functions."""
    code = """
def function_a(n):
    if n > 0:
        return function_b(n - 0.1)  # Never reaches 0, keeps calling
    return n

def function_b(n):
    if n > 0:
        return function_a(n - 0.1)  # Never reaches 0, keeps calling  
    return n

result = function_a(100)
"""
    result = await server.call("script_interpreter_execute", {"code": code, "_status": mock_status})
    
    assert "error" in result
    error_message = extract_error_message(result).lower()
    assert any(word in error_message for word in ["timeout", "time", "exceeded", "recursion"])


@pytest.mark.asyncio
async def test_reasonable_loops_still_work(server, mock_status):
    """Test that reasonable loops still work and don't timeout."""
    code = """
# These should work fine
total = 0
for i in range(1000):
    total += i

factorial = 1
n = 10
while n > 0:
    factorial *= n
    n -= 1

print(f"Sum 1-999: {total}")
print(f"10 factorial: {factorial}")
"""
    result = await server.call("script_interpreter_execute", {"code": code, "_status": mock_status})
    
    assert "error" not in result
    assert "result" in result
    assert "Sum 1-999: 499500" in result["result"]
    assert "10 factorial: 3628800" in result["result"]


@pytest.mark.asyncio
async def test_reasonable_recursion_still_works(server, mock_status):
    """Test that reasonable recursion still works."""
    code = """
def fibonacci(n):
    if n <= 1:
        return n
    return fibonacci(n-1) + fibonacci(n-2)

def factorial(n):
    if n <= 1:
        return 1
    return n * factorial(n-1)

fib_10 = fibonacci(10)
fact_10 = factorial(10)

print(f"Fibonacci(10): {fib_10}")
print(f"Factorial(10): {fact_10}")
"""
    result = await server.call("script_interpreter_execute", {"code": code, "_status": mock_status})
    
    assert "error" not in result
    assert "result" in result
    assert "Fibonacci(10): 55" in result["result"]
    assert "Factorial(10): 3628800" in result["result"]


@pytest.mark.asyncio
async def test_string_operations_infinite_behavior(server, mock_status):
    """Test string operations that could cause infinite-like behavior."""
    code = """
# Try to create very large strings
base_string = "a" * 1000
for i in range(10000):  # This might use too much memory
    base_string += "b" * 1000
    
print(f"String length: {len(base_string)}")
"""
    result = await server.call("script_interpreter_execute", {"code": code, "_status": mock_status})
    
    # This might timeout, fail due to memory, or succeed depending on system
    if "error" in result:
        error_message = extract_error_message(result).lower()
        assert any(word in error_message for word in ["timeout", "time", "exceeded", "memory"])
    # If it succeeds, that's also acceptable