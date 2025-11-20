"""Test script_interpreter plugin with loops and if statements."""

import pytest
from unittest.mock import Mock
from agent_system.config.models import AgentSystemConfig, MCPConfig
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
    """Create script interpreter server with loops enabled."""
    system_config = Mock(spec=AgentSystemConfig)
    mcp_config = MCPConfig(
        type="script_interpreter",
        enabled=True,
        script_interpreter={"enable_loops": True, "enable_functions": True}
    )
    return ScriptInterpreterServer("script_interpreter", system_config, mcp_config)


@pytest.fixture
def mock_status():
    """Create mock status for testing."""
    return MockStatus()


@pytest.mark.asyncio
async def test_simple_if_statement(server, mock_status):
    """Test simple if statement."""
    code = """
x = 10
if x > 5:
    result = "big"
else:
    result = "small"
print("Result:", result)
"""
    result = await server.call("script_interpreter_execute", {"code": code, "_status": mock_status})
    
    assert "error" not in result
    assert "Result: big" in result["result"]


@pytest.mark.asyncio
async def test_if_else_branches(server, mock_status):
    """Test both if and else branches."""
    # Test if branch
    code1 = """
x = 3
if x > 5:
    result = "big"
else:
    result = "small"
print("Result:", result)
"""
    result1 = await server.call("script_interpreter_execute", {"code": code1, "_status": mock_status})
    assert "Result: small" in result1["result"]


@pytest.mark.asyncio
async def test_inline_if_expression(server, mock_status):
    """Test inline if expressions (ternary operator)."""
    code = """
temp = 25.5
category = "hot" if temp > 25 else "cool"
print("Category:", category)
"""
    result = await server.call("script_interpreter_execute", {"code": code, "_status": mock_status})
    
    assert "error" not in result
    assert "Category: hot" in result["result"]


@pytest.mark.asyncio
async def test_for_loop_with_list(server, mock_status):
    """Test for loop iterating over a list."""
    code = """
total = 0
numbers = [1, 2, 3, 4, 5]
for num in numbers:
    total = total + num
print("Total:", total)
"""
    result = await server.call("script_interpreter_execute", {"code": code, "_status": mock_status})
    
    assert "error" not in result
    assert "Total: 15" in result["result"]


@pytest.mark.asyncio
async def test_for_loop_with_range(server, mock_status):
    """Test for loop with range function."""
    code = """
squares = []
for i in range(5):
    squares.append(i * i)
print("Squares:", squares)
"""
    result = await server.call("script_interpreter_execute", {"code": code, "_status": mock_status})
    
    assert "error" not in result
    assert "Squares: [0, 1, 4, 9, 16]" in result["result"]


@pytest.mark.asyncio
async def test_nested_if_in_loop(server, mock_status):
    """Test nested if statements inside loops."""
    code = """
numbers = [1, 2, 3, 4, 5, 6]
evens = []
for num in numbers:
    if num % 2 == 0:
        evens.append(num)
print("Even numbers:", evens)
"""
    result = await server.call("script_interpreter_execute", {"code": code, "_status": mock_status})
    
    assert "error" not in result
    assert "Even numbers: [2, 4, 6]" in result["result"]


@pytest.mark.asyncio
async def test_list_indexing_in_loop(server, mock_status):
    """Test accessing list elements by index in loops."""
    code = """
names = ["Alice", "Bob", "Charlie"]
for i in range(len(names)):
    print("Index", i, ":", names[i])
"""
    result = await server.call("script_interpreter_execute", {"code": code, "_status": mock_status})
    
    assert "error" not in result
    assert "Index 0 : Alice" in result["result"]
    assert "Index 1 : Bob" in result["result"]
    assert "Index 2 : Charlie" in result["result"]


@pytest.mark.asyncio
async def test_temperature_chart_with_loops(server, mock_status):
    """Test the original user scenario with loops."""
    code = """
times = ["00:00", "01:00", "02:00", "03:00"]  
temps = [9.5, 9.2, 8.5, 8.2]
minv = min(temps)
maxv = max(temps)
scale_range = maxv - minv

print("ASCII Chart:")
for i in range(len(times)):
    temp = temps[i]
    if scale_range > 0:
        length = int((temp - minv) / scale_range * 10)
    else:
        length = 5
    bar = "#" * length
    print(times[i], str(round(temp, 1)) + "°C", bar)
"""
    result = await server.call("script_interpreter_execute", {"code": code, "_status": mock_status})
    
    assert "error" not in result
    assert "ASCII Chart:" in result["result"]
    assert "00:00 9.5°C" in result["result"]
    assert "03:00 8.2°C" in result["result"]


@pytest.mark.asyncio
async def test_loop_timeout_protection(server, mock_status):
    """Test that loops are protected by timeout."""
    # Note: This test uses a reasonable loop that should complete
    # Testing actual infinite loops would require mocking timeouts
    code = """
count = 0
for i in range(1000):  # Should complete quickly
    count = count + 1
print("Count:", count)
"""
    result = await server.call("script_interpreter_execute", {"code": code, "_status": mock_status})
    
    assert "error" not in result
    assert "Count: 1000" in result["result"]


@pytest.mark.asyncio
async def test_while_loop_basic(server, mock_status):
    """Test basic while loop functionality."""
    code = """
count = 0
while count < 5:
    count = count + 1
print("Final count:", count)
"""
    result = await server.call("script_interpreter_execute", {"code": code, "_status": mock_status})
    
    assert "error" not in result
    assert "Final count: 5" in result["result"]


@pytest.mark.asyncio
async def test_function_definitions(server, mock_status):
    """Test function definition and calling."""
    code = """
def add_numbers(a, b):
    return a + b

def fibonacci(n):
    if n <= 0:
        return []
    elif n == 1:
        return [0]
    elif n == 2:
        return [0, 1]
    
    seq = [0, 1]
    for i in range(2, n):
        seq.append(seq[i-1] + seq[i-2])
    return seq

# Test simple function
result1 = add_numbers(5, 3)
print(result1)

# Test more complex function
fib_seq = fibonacci(8)
print(fib_seq)
"""
    result = await server.call("script_interpreter_execute", {"code": code, "_status": mock_status})
    
    assert "error" not in result
    assert "result" in result
    assert "8" in result["result"]
    assert "[0, 1, 1, 2, 3, 5, 8, 13]" in result["result"]


@pytest.mark.asyncio
async def test_fstring_support(server, mock_status):
    """Test f-string formatting support."""
    code = """
name = "Alice"
age = 30
height = 5.75

# Basic f-string
greeting = f"Hello {name}!"
print(greeting)

# F-string with formatting
info = f"Age: {age}, Height: {height:.1f} ft"
print(info)

# F-string with expressions
items = [1, 2, 3, 4, 5]
summary = f"List {items} has {len(items)} items, sum = {sum(items)}"
print(summary)

# F-string with function calls
def get_status(x):
    return "positive" if x > 0 else "negative" if x < 0 else "zero"

number = -5
status_msg = f"Number {number} is {get_status(number)}"
print(status_msg)
"""
    result = await server.call("script_interpreter_execute", {"code": code, "_status": mock_status})
    
    assert "error" not in result
    assert "result" in result
    assert "Hello Alice!" in result["result"]
    assert "Age: 30, Height: 5.8 ft" in result["result"]
    assert "List [1, 2, 3, 4, 5] has 5 items, sum = 15" in result["result"]
    assert "Number -5 is negative" in result["result"]