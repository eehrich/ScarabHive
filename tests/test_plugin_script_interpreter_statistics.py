"""Test script_interpreter plugin statistics functions."""

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
async def test_mean_function(server, mock_status):
    """Test mean function with various inputs."""
    code = """
result1 = mean([1, 2, 3, 4, 5])
result2 = mean([10.5, 20.5])
print("Mean of [1,2,3,4,5]:", result1)
print("Mean of [10.5,20.5]:", result2)
"""
    result = await server.call("execute_python", {"code": code, "_status": mock_status})
    
    assert "error" not in result
    assert "Mean of [1,2,3,4,5]: 3.0" in result["result"]
    assert "Mean of [10.5,20.5]: 15.5" in result["result"]


@pytest.mark.asyncio
async def test_median_function(server, mock_status):
    """Test median function with odd and even length lists."""
    code = """
odd_list = [1, 3, 5, 7, 9]
even_list = [2, 4, 6, 8]
median_odd = median(odd_list)
median_even = median(even_list)
print("Median odd:", median_odd)
print("Median even:", median_even)
"""
    result = await server.call("execute_python", {"code": code, "_status": mock_status})
    
    assert "error" not in result
    assert "Median odd: 5" in result["result"]
    assert "Median even: 5.0" in result["result"]


@pytest.mark.asyncio
async def test_mode_function(server, mock_status):
    """Test mode function."""
    code = """
data = [1, 2, 2, 3, 4, 4, 4, 5]
mode_val = mode(data)
print("Mode:", mode_val)
"""
    result = await server.call("execute_python", {"code": code, "_status": mock_status})
    
    assert "error" not in result
    assert "Mode: 4" in result["result"]


@pytest.mark.asyncio
async def test_stdev_function(server, mock_status):
    """Test standard deviation function."""
    code = """
data = [2, 4, 4, 4, 5, 5, 7, 9]
stdev_val = stdev(data)
print("StdDev:", round(stdev_val, 2))
"""
    result = await server.call("execute_python", {"code": code, "_status": mock_status})
    
    assert "error" not in result
    assert "StdDev:" in result["result"]


@pytest.mark.asyncio
async def test_statistics_with_user_scenario(server, mock_status):
    """Test the original user scenario with built-in statistics."""
    code = """
temps = [9.5, 9.2, 8.5, 8.2, 7.8, 7.8, 8.5, 9.4, 11.1, 12.4, 13.4, 14.5, 15.1]
minv = min(temps)
maxv = max(temps)
meanv = mean(temps)
medianv = median(temps)
print("Temperature Analysis:")
print("Min:", round(minv, 1), "°C")
print("Max:", round(maxv, 1), "°C")
print("Mean:", round(meanv, 1), "°C")
print("Median:", round(medianv, 1), "°C")
"""
    result = await server.call("execute_python", {"code": code, "_status": mock_status})
    
    assert "error" not in result
    assert "Min: 7.8 °C" in result["result"]
    assert "Max: 15.1 °C" in result["result"]
    assert "Mean: 10.4 °C" in result["result"] 
    assert "Median: 9.4 °C" in result["result"]


@pytest.mark.asyncio
async def test_empty_list_errors(server, mock_status):
    """Test that statistics functions handle empty lists gracefully."""
    code = "mean([])"
    result = await server.call("execute_python", {"code": code, "_status": mock_status})
    
    assert "error" in result
    # Error can be a dict or string from SafeExecutor
    error_msg = str(result["error"])
    assert "non-empty sequence" in error_msg


@pytest.mark.asyncio
async def test_stdev_insufficient_data(server, mock_status):
    """Test stdev with insufficient data points."""
    code = "stdev([1])"
    result = await server.call("execute_python", {"code": code, "_status": mock_status})
    
    assert "error" in result
    # Error can be a dict or string from SafeExecutor
    error_msg = str(result["error"])
    assert "at least 2 values" in error_msg