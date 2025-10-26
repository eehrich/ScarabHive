"""
Simple tests for math functions in the script interpreter plugin.
"""

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
    return ScriptInterpreterServer("script_interpreter", system_config, mcp_config)


@pytest.fixture
def mock_status():
    """Create mock status for testing."""
    return MockStatus()


@pytest.mark.asyncio
async def test_math_constants(server, mock_status):
    """Test mathematical constants pi and e."""
    code = """
pi_val = pi()
e_val = e()
print(f"Pi: {pi_val}")
print(f"E: {e_val}")
"""
    
    result = await server.call("script_interpreter_execute", {"code": code, "_status": mock_status})
    
    assert result["result"] is not None
    assert "Pi: 3.141592" in result["result"]
    assert "E: 2.718281" in result["result"]


@pytest.mark.asyncio
async def test_basic_math_functions(server, mock_status):
    """Test basic math functions."""
    code = """
result_sqrt = sqrt(16)
result_abs = abs(-5)
result_floor = floor(4.7)
result_ceil = ceil(4.2)
print(f"sqrt(16) = {result_sqrt}")
print(f"abs(-5) = {result_abs}")
print(f"floor(4.7) = {result_floor}")
print(f"ceil(4.2) = {result_ceil}")
"""
    
    result = await server.call("script_interpreter_execute", {"code": code, "_status": mock_status})
    
    assert result["result"] is not None
    assert "sqrt(16) = 4.0" in result["result"]
    assert "abs(-5) = 5" in result["result"]
    assert "floor(4.7) = 4" in result["result"]
    assert "ceil(4.2) = 5" in result["result"]


@pytest.mark.asyncio
async def test_trigonometric_functions(server, mock_status):
    """Test trigonometric functions."""
    code = """
sin_0 = sin(0)
cos_0 = cos(0)  
tan_0 = tan(0)
print(f"sin(0) = {sin_0}")
print(f"cos(0) = {cos_0}")
print(f"tan(0) = {tan_0}")
"""
    
    result = await server.call("script_interpreter_execute", {"code": code, "_status": mock_status})
    
    assert result["result"] is not None
    assert "sin(0) = 0.0" in result["result"]
    assert "cos(0) = 1.0" in result["result"]
    assert "tan(0) = 0.0" in result["result"]


@pytest.mark.asyncio
async def test_logarithmic_functions(server, mock_status):
    """Test logarithmic functions."""
    code = """
e_val = e()
log_e = log(e_val)
log10_100 = log10(100)
exp_1 = exp(1)
print(f"log(e) = {log_e}")
print(f"log10(100) = {log10_100}")
print(f"exp(1) = {exp_1}")
"""
    
    result = await server.call("script_interpreter_execute", {"code": code, "_status": mock_status})
    
    assert result["result"] is not None
    assert "log(e) = 1.0" in result["result"]
    assert "log10(100) = 2.0" in result["result"]
    assert "exp(1) = 2.718281" in result["result"]


@pytest.mark.asyncio
async def test_power_functions(server, mock_status):
    """Test power functions."""
    code = """
pow_result = pow(2, 3)
print(f"pow(2, 3) = {pow_result}")
"""
    
    result = await server.call("script_interpreter_execute", {"code": code, "_status": mock_status})
    
    assert result["result"] is not None
    assert "pow(2, 3) = 8.0" in result["result"]


@pytest.mark.asyncio
async def test_angle_conversion(server, mock_status):
    """Test angle conversion functions."""
    code = """
pi_val = pi()
degrees_pi = degrees(pi_val)
radians_180 = radians(180)
print(f"degrees(pi) = {degrees_pi}")
print(f"radians(180) = {radians_180}")
"""
    
    result = await server.call("script_interpreter_execute", {"code": code, "_status": mock_status})
    
    assert result["result"] is not None
    assert "degrees(pi) = 180.0" in result["result"]
    assert "radians(180) = 3.141592" in result["result"]


@pytest.mark.asyncio
async def test_complex_math_calculation(server, mock_status):
    """Test complex mathematical calculation."""
    code = """
# Calculate area of circle
radius = 5
area = pi() * pow(radius, 2)
print(f"Area of circle with radius {radius}: {area}")

# Distance formula
x1, y1 = 0, 0
x2, y2 = 3, 4
distance = sqrt(pow(x2 - x1, 2) + pow(y2 - y1, 2))
print(f"Distance from ({x1},{y1}) to ({x2},{y2}): {distance}")
"""
    
    result = await server.call("script_interpreter_execute", {"code": code, "_status": mock_status})
    
    assert result["result"] is not None
    assert "Area of circle" in result["result"]
    assert "78.5398" in result["result"]  # pi * 25
    assert "Distance from" in result["result"]
    assert "5.0" in result["result"]  # sqrt(9 + 16)


@pytest.mark.asyncio
async def test_math_error_handling(server, mock_status):
    """Test error handling for invalid math operations."""
    code = """
try:
    result = sqrt(-1)
    print(f"Should not reach this: {result}")
except ValueError as e:
    print("Correctly caught ValueError")
"""
    
    result = await server.call("script_interpreter_execute", {"code": code, "_status": mock_status})
    
    assert result["result"] is not None
    assert "Correctly caught ValueError" in result["result"]