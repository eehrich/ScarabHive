"""Test script_interpreter plugin basic functionality."""

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
async def test_print_function(server, mock_status):
    """Test that print function works and output is captured."""
    code = """
print("Hello, World!")
print("Line 2")
"""
    result = await server.call("execute_python_sandbox", {"code": code, "_status": mock_status})
    
    assert result["result"] is not None
    assert "Hello, World!" in result["result"]
    assert "Line 2" in result["result"]


@pytest.mark.asyncio
async def test_builtin_statistics_functions(server, mock_status):
    """Test built-in statistics functions."""
    code = """
data = [1, 2, 3, 4, 5]
mean_val = mean(data)
median_val = median(data)
min_val = min(data)
max_val = max(data)
print("Mean:", mean_val)
print("Median:", median_val)
"""
    result = await server.call("execute_python_sandbox", {"code": code, "_status": mock_status})
    
    assert "error" not in result
    assert "Mean: 3.0" in result["result"]
    assert "Median: 3" in result["result"]


@pytest.mark.asyncio
async def test_min_max_with_lists(server, mock_status):
    """Test min/max functions work correctly with lists."""
    code = """
temps = [9.5, 9.2, 8.5, 8.2, 7.8]
minv = min(temps)
maxv = max(temps)
print("Min:", minv)
print("Max:", maxv)
"""
    result = await server.call("execute_python_sandbox", {"code": code, "_status": mock_status})
    
    assert "error" not in result
    assert "Min: 7.8" in result["result"]
    assert "Max: 9.5" in result["result"]


@pytest.mark.asyncio
async def test_string_formatting_without_fstrings(server, mock_status):
    """Test string formatting alternatives to f-strings."""
    code = """
name = "Temperature"
value = 23.456
formatted = name + ": " + str(round(value, 1)) + " °C"
print(formatted)
"""
    result = await server.call("execute_python_sandbox", {"code": code, "_status": mock_status})
    
    assert "error" not in result
    assert "Temperature: 23.5 °C" in result["result"]


@pytest.mark.asyncio
async def test_no_none_output_from_print(server, mock_status):
    """Test that print statements don't show None in output."""
    code = """
print("Test message")
x = 42
"""
    result = await server.call("execute_python_sandbox", {"code": code, "_status": mock_status})
    
    # Should not contain "None" from print statement
    assert "Test message" in result["result"]
    assert "\\nNone\\n" not in result["result"]


@pytest.mark.asyncio
async def test_variables_preserved_between_calls(server, mock_status):
    """Test that variables are preserved between calls."""
    # First call
    code1 = "x = 10"
    result1 = await server.call("execute_python_sandbox", {"code": code1, "_status": mock_status})
    assert "error" not in result1
    
    # Second call should see the variable
    code2 = "print('x is:', x)"
    result2 = await server.call("execute_python_sandbox", {"code": code2, "_status": mock_status})
    assert "x is: 10" in result2["result"]


@pytest.mark.asyncio
async def test_reset_sandbox(server, mock_status):
    """Test sandbox reset functionality."""
    # Set a variable
    code1 = "test_var = 123"
    result1 = await server.call("execute_python_sandbox", {"code": code1, "_status": mock_status})
    assert "error" not in result1
    
    # Reset sandbox
    reset_result = await server.call("reset_python_sandbox", {"_status": mock_status})
    assert "error" not in reset_result
    
    # Try to access the variable - should cause an error
    code2 = "print(test_var)"
    result = await server.call("execute_python_sandbox", {"code": code2, "_status": mock_status})
    assert "error" in result  # Variable should not exist after reset