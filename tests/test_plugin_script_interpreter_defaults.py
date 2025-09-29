"""Test script_interpreter plugin default argument functionality."""

import pytest
from src.plugins.script_interpreter.server import ScriptInterpreterServer


class MockStatus:
    """Mock status for testing."""
    def __init__(self):
        self.messages = []
    
    async def progress(self, msg):
        self.messages.append(("progress", msg))
    
    async def error(self, msg):
        self.messages.append(("error", msg))
    
    async def end(self, msg, meta=None):
        self.messages.append(("end", msg, meta))


@pytest.fixture
def server():
    """Create script interpreter server for testing."""
    return ScriptInterpreterServer("test", {})


@pytest.fixture
def mock_status():
    """Create mock status for testing."""
    return MockStatus()


@pytest.mark.asyncio
async def test_function_with_default_arguments(server, mock_status):
    """Test function definitions with default arguments work correctly."""
    code = """
def greet(name, greeting="Hello"):
    return f"{greeting}, {name}!"

# Test with and without default argument
result1 = greet("Alice")
result2 = greet("Bob", "Hi")
print(f"Default: {result1}")
print(f"Custom: {result2}")
"""
    result = await server.call("execute_python_sandbox", {"code": code, "_status": mock_status})
    
    assert "error" not in result
    assert "result" in result
    assert "Default: Hello, Alice!" in result["result"]
    assert "Custom: Hi, Bob!" in result["result"]


@pytest.mark.asyncio
async def test_function_with_multiple_defaults(server, mock_status):
    """Test function with multiple default arguments."""
    code = """
def calculate(x, y=10, z=5):
    return x + y + z

# Test with different numbers of arguments
result1 = calculate(1)        # Uses both defaults: 1 + 10 + 5 = 16
result2 = calculate(1, 20)    # Uses one default: 1 + 20 + 5 = 26
result3 = calculate(1, 20, 30) # No defaults: 1 + 20 + 30 = 51

print(f"All defaults: {result1}")
print(f"One default: {result2}")
print(f"No defaults: {result3}")
"""
    result = await server.call("execute_python_sandbox", {"code": code, "_status": mock_status})
    
    assert "error" not in result
    assert "result" in result
    assert "All defaults: 16" in result["result"]
    assert "One default: 26" in result["result"]
    assert "No defaults: 51" in result["result"]


@pytest.mark.asyncio
async def test_function_with_none_default(server, mock_status):
    """Test function with None as default argument (like the Fibonacci memo)."""
    code = """
def fibonacci_memo(n, memo=None):
    if memo is None:
        memo = {}
    
    if n in memo:
        return memo[n]
    
    if n <= 1:
        memo[n] = n
        return n
    
    memo[n] = fibonacci_memo(n-1, memo) + fibonacci_memo(n-2, memo)
    return memo[n]

# Test the function
result = fibonacci_memo(6)
print(f"F(6) = {result}")
"""
    result = await server.call("execute_python_sandbox", {"code": code, "_status": mock_status})
    
    assert "error" not in result
    assert "result" in result
    assert "F(6) = 8" in result["result"]


@pytest.mark.asyncio
async def test_function_default_argument_error_handling(server, mock_status):
    """Test error handling with default arguments."""
    code = """
def test_func(a, b=10):
    return a + b

try:
    # This should work
    result1 = test_func(5)
    print(f"Success: {result1}")
    
    # This should fail - not enough arguments
    result2 = test_func()
except Exception as e:
    print(f"Error as expected: {type(e).__name__}")
"""
    result = await server.call("execute_python_sandbox", {"code": code, "_status": mock_status})
    
    assert "error" not in result
    assert "result" in result
    assert "Success: 15" in result["result"]
    assert "Error as expected: str" in result["result"]


@pytest.mark.asyncio
async def test_underscore_variable_allowed(server, mock_status):
    """Test that single underscore _ is allowed as throwaway variable."""
    code = """
# Test underscore in loop
total = 0
for _ in range(5):
    total += 1

print(f"Total iterations: {total}")

# Test underscore in list comprehension
squares = [x*x for x in range(4)]
print(f"Squares: {squares}")
"""
    result = await server.call("execute_python_sandbox", {"code": code, "_status": mock_status})
    
    assert "error" not in result
    assert "result" in result
    assert "Total iterations: 5" in result["result"]
    assert "Squares: [0, 1, 4, 9]" in result["result"]


@pytest.mark.asyncio
async def test_isinstance_with_function_arguments(server, mock_status):
    """Test isinstance checks within function arguments work correctly."""
    code = """
def type_checker(value):
    if isinstance(value, int):
        return f"Integer: {value}"
    elif isinstance(value, str):
        return f"String: '{value}'"
    elif isinstance(value, list):
        return f"List with {len(value)} items"
    else:
        return f"Other type: {type(value).__name__}"

# Test with different types
test_values = [42, "hello", [1, 2, 3], 3.14]
for val in test_values:
    result = type_checker(val)
    print(result)
"""
    result = await server.call("execute_python_sandbox", {"code": code, "_status": mock_status})
    
    assert "error" not in result
    assert "result" in result
    assert "Integer: 42" in result["result"]
    assert "String: 'hello'" in result["result"]
    assert "List with 3 items" in result["result"]
    assert "Other type: float" in result["result"]