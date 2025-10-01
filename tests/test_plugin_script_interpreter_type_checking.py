"""Test script_interpreter plugin type checking functions."""

import pytest
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
    """Create script interpreter server for testing."""
    from unittest.mock import Mock
    system_config = Mock(spec=AgentSystemConfig)
    mcp_config = MCPConfig(type="script_interpreter", enabled=True)
    return ScriptInterpreterServer("test", system_config, mcp_config)


@pytest.fixture
def mock_status():
    """Create mock status for testing."""
    return MockStatus()


@pytest.mark.asyncio
async def test_isinstance_basic_types(server, mock_status):
    """Test isinstance with basic built-in types."""
    code = """
result1 = isinstance(42, int)
result2 = isinstance("hello", str)
result3 = isinstance(3.14, float)
result4 = isinstance(True, bool)
result5 = isinstance([1, 2, 3], list)
result6 = isinstance({"a": 1}, dict)
result7 = isinstance((1, 2), tuple)
result8 = isinstance({1, 2, 3}, set)
print(f"int: {result1}, str: {result2}, float: {result3}, bool: {result4}")
print(f"list: {result5}, dict: {result6}, tuple: {result7}, set: {result8}")
"""
    result = await server.call("execute_python_sandbox", {"code": code, "_status": mock_status})
    
    assert "error" not in result
    assert "result" in result
    assert "int: True, str: True, float: True, bool: True" in result["result"]
    assert "list: True, dict: True, tuple: True, set: True" in result["result"]


@pytest.mark.asyncio
async def test_isinstance_multiple_types(server, mock_status):
    """Test isinstance with tuple of types."""
    code = """
result1 = isinstance(42, (int, float))
result2 = isinstance(3.14, (int, float))
result3 = isinstance("hello", (int, float))
result4 = isinstance(True, (int, bool))  # bool is subclass of int
print(f"42 is int or float: {result1}")
print(f"3.14 is int or float: {result2}")
print(f"'hello' is int or float: {result3}")
print(f"True is int or bool: {result4}")
"""
    result = await server.call("execute_python_sandbox", {"code": code, "_status": mock_status})
    
    assert "error" not in result
    assert "result" in result
    assert "42 is int or float: True" in result["result"]
    assert "3.14 is int or float: True" in result["result"]
    assert "'hello' is int or float: False" in result["result"]
    assert "True is int or bool: True" in result["result"]


@pytest.mark.asyncio
async def test_type_function_basic(server, mock_status):
    """Test type() function with basic types."""
    code = """
print(f"type(42): {type(42).__name__}")
print(f"type('hello'): {type('hello').__name__}")
print(f"type(3.14): {type(3.14).__name__}")
print(f"type(True): {type(True).__name__}")
print(f"type([1,2,3]): {type([1,2,3]).__name__}")
print(f"type({{'a': 1}}): {type({'a': 1}).__name__}")
"""
    result = await server.call("execute_python_sandbox", {"code": code, "_status": mock_status})
    
    assert "error" not in result
    assert "result" in result
    assert "type(42): int" in result["result"]
    assert "type('hello'): str" in result["result"]
    assert "type(3.14): float" in result["result"]
    assert "type(True): bool" in result["result"]
    assert "type([1,2,3]): list" in result["result"]
    assert "type({'a': 1}): dict" in result["result"]


@pytest.mark.asyncio
async def test_type_conversion_functions(server, mock_status):
    """Test type conversion functions."""
    code = """
# Test int() conversion
result1 = int("42")
result2 = int(3.14)
result3 = int(True)
print(f"int('42'): {result1}, int(3.14): {result2}, int(True): {result3}")

# Test str() conversion  
result4 = str(42)
result5 = str(3.14)
result6 = str(True)
print(f"str(42): '{result4}', str(3.14): '{result5}', str(True): '{result6}'")

# Test float() conversion
result7 = float("3.14")
result8 = float(42)
result9 = float(True)
print(f"float('3.14'): {result7}, float(42): {result8}, float(True): {result9}")

# Test bool() conversion
result10 = bool(1)
result11 = bool(0)
result12 = bool("hello")
result13 = bool("")
print(f"bool(1): {result10}, bool(0): {result11}, bool('hello'): {result12}, bool(''): {result13}")
"""
    result = await server.call("execute_python_sandbox", {"code": code, "_status": mock_status})
    
    assert "error" not in result
    assert "result" in result
    assert "int('42'): 42, int(3.14): 3, int(True): 1" in result["result"]
    assert "str(42): '42', str(3.14): '3.14', str(True): 'True'" in result["result"]
    assert "float('3.14'): 3.14, float(42): 42.0, float(True): 1.0" in result["result"]
    assert "bool(1): True, bool(0): False, bool('hello'): True, bool(''): False" in result["result"]


@pytest.mark.asyncio
async def test_isinstance_error_cases(server, mock_status):
    """Test isinstance error handling."""
    # Test with wrong number of arguments
    code = "isinstance(42)"
    result = await server.call("execute_python_sandbox", {"code": code, "_status": mock_status})
    
    assert "error" in result
    assert "isinstance() takes exactly 2 arguments" in str(result["error"])


@pytest.mark.asyncio
async def test_type_error_cases(server, mock_status):
    """Test type() error handling."""
    # Test with wrong number of arguments
    code = "type()"
    result = await server.call("execute_python_sandbox", {"code": code, "_status": mock_status})
    
    assert "error" in result
    assert "type() takes exactly 1 argument" in str(result["error"])


@pytest.mark.asyncio
async def test_complex_type_checking_workflow(server, mock_status):
    """Test a complex workflow using type checking functions."""
    code = """
def process_value(value):
    if isinstance(value, str):
        return f"String: '{value}' (length: {len(value)})"
    elif isinstance(value, (int, float)):
        return f"Number: {value} (type: {type(value).__name__})"
    elif isinstance(value, list):
        return f"List: {value} (length: {len(value)})"
    else:
        return f"Other: {value} (type: {type(value).__name__})"

# Test with various inputs
inputs = [42, "hello", 3.14, [1, 2, 3], True, {"key": "value"}]
for item in inputs:
    result = process_value(item)
    print(result)
"""
    result = await server.call("execute_python_sandbox", {"code": code, "_status": mock_status})
    
    assert "error" not in result
    assert "result" in result
    assert "Number: 42 (type: int)" in result["result"]
    assert "String: 'hello' (length: 5)" in result["result"]
    assert "Number: 3.14 (type: float)" in result["result"]
    assert "List: [1, 2, 3] (length: 3)" in result["result"]
    assert "Number: True (type: bool)" in result["result"]  # bool is a subclass of int
    assert "Other: {'key': 'value'} (type: dict)" in result["result"]


@pytest.mark.asyncio
async def test_type_checking_in_conditionals(server, mock_status):
    """Test type checking in conditional statements."""
    code = """
data = [1, "two", 3.0, [4, 5], True]
numbers = []
strings = []
lists = []
others = []

for item in data:
    if isinstance(item, str):
        strings.append(item)
    elif isinstance(item, list):
        lists.append(item)
    elif isinstance(item, bool):
        others.append(item)
    elif isinstance(item, (int, float)):
        numbers.append(item)
    else:
        others.append(item)

print(f"Numbers: {numbers}")
print(f"Strings: {strings}")
print(f"Lists: {lists}")
print(f"Others: {others}")
"""
    result = await server.call("execute_python_sandbox", {"code": code, "_status": mock_status})
    
    assert "error" not in result
    assert "result" in result
    assert "Numbers: [1, 3.0]" in result["result"]
    assert "Strings: ['two']" in result["result"]
    assert "Lists: [[4, 5]]" in result["result"]
    assert "Others: [True]" in result["result"]


@pytest.mark.asyncio
async def test_type_conversion_error_handling(server, mock_status):
    """Test error handling in type conversions."""
    code = """
try:
    result = int("not_a_number")
except ValueError as e:
    print(f"ValueError caught: {e}")

try:
    result = float("not_a_float")
except ValueError as e:
    print(f"ValueError caught: {e}")
"""
    result = await server.call("execute_python_sandbox", {"code": code, "_status": mock_status})
    
    assert "error" not in result
    assert "result" in result
    assert "ValueError caught:" in result["result"]


@pytest.mark.asyncio
async def test_nested_type_checking(server, mock_status):
    """Test nested type checking scenarios."""
    code = """
def analyze_nested_structure(data):
    if isinstance(data, list):
        print(f"List with {len(data)} items:")
        for i, item in enumerate(data):
            if isinstance(item, dict):
                print(f"  Item {i}: dict with keys {list(item.keys())}")
            elif isinstance(item, list):
                print(f"  Item {i}: nested list with {len(item)} items")
            else:
                print(f"  Item {i}: {type(item).__name__} = {item}")
    elif isinstance(data, dict):
        print(f"Dict with keys: {list(data.keys())}")
        for key, value in data.items():
            print(f"  {key}: {type(value).__name__} = {value}")

# Test nested structure
nested_data = [
    {"name": "Alice", "age": 30},
    [1, 2, 3],
    "simple string",
    42
]

analyze_nested_structure(nested_data)
"""
    result = await server.call("execute_python_sandbox", {"code": code, "_status": mock_status})
    
    assert "error" not in result
    assert "result" in result
    assert "List with 4 items:" in result["result"]
    assert "Item 0: dict with keys ['name', 'age']" in result["result"]
    assert "Item 1: nested list with 3 items" in result["result"]
    assert "Item 2: str = simple string" in result["result"]
    assert "Item 3: int = 42" in result["result"]