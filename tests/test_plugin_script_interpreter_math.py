"""
Comprehensive tests for math functions in the script interpreter plugin.
"""

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
async def test_math_constants(server, mock_status):
    """Test mathematical constants pi and e."""
    code = """
pi_val = pi()
e_val = e()
print(f"Pi: {pi_val}")
print(f"E: {e_val}")
print(f"Pi type: {str(type(pi_val)).split('.')[-1].replace(\"'>\", \"\")}")
"""
    
    result = await server.call("execute_python", {"code": code, "_status": mock_status})
    
    assert result["result"] is not None
    assert "Pi: 3.141592" in result["result"]
    assert "E: 2.718281" in result["result"]


@pytest.mark.asyncio
async def test_basic_math_functions(server, mock_status):
    """Test basic math functions."""
    code = """
# Test basic math functions
sqrt_result = sqrt(16)
abs_result = abs(-5.5)
floor_result = floor(4.7)
ceil_result = ceil(4.2)
round_result = round(3.14159, 2)

print(f"sqrt(16) = {sqrt_result}")
print(f"abs(-5.5) = {abs_result}")
print(f"floor(4.7) = {floor_result}")
print(f"ceil(4.2) = {ceil_result}")
print(f"round(3.14159, 2) = {round_result}")
"""
    
    result = await server.call("execute_python", {"code": code, "_status": mock_status})
    
    assert result["result"] is not None
    assert "sqrt(16) = 4.0" in result["result"]
    assert "abs(-5.5) = 5.5" in result["result"]
    assert "floor(4.7) = 4" in result["result"]
    assert "ceil(4.2) = 5" in result["result"]
    assert "round(3.14159, 2) = 3.14" in result["result"]


@pytest.mark.asyncio
async def test_trigonometric_functions(server, mock_status):
    """Test trigonometric functions."""
    code = """
# Test trigonometric functions
sin_0 = sin(0)
cos_0 = cos(0)
tan_0 = tan(0)
pi_val = pi()
sin_pi_half = sin(pi_val / 2)
cos_pi = cos(pi_val)

print(f"sin(0) = {sin_0}")
print(f"cos(0) = {cos_0}")
print(f"tan(0) = {tan_0}")
print(f"sin(π/2) ≈ {round(sin_pi_half, 6)}")
print(f"cos(π) ≈ {round(cos_pi, 6)}")
"""
    
    result = await server.call("execute_python", {"code": code, "_status": mock_status})
    
    assert result["result"] is not None
    assert "sin(0) = 0.0" in result["result"]
    assert "cos(0) = 1.0" in result["result"]
    assert "tan(0) = 0.0" in result["result"]
    assert "sin(π/2) ≈ 1.0" in result["result"]
    assert "cos(π) ≈ -1.0" in result["result"]


@pytest.mark.asyncio
async def test_inverse_trigonometric_functions(server, mock_status):
    """Test inverse trigonometric functions."""
    code = """
# Test inverse trig functions
asin_1 = asin(1.0)
acos_0 = acos(0.0)  
atan_1 = atan(1.0)
pi_val = pi()

print(f"asin(1) = {asin_1}")
print(f"acos(0) = {acos_0}")
print(f"atan(1) = {atan_1}")
print(f"π/2 = {pi_val / 2}")
print(f"π/4 = {pi_val / 4}")
"""
    
    result = await server.call("execute_python", {"code": code, "_status": mock_status})
    
    assert result["result"] is not None
    assert "asin(1)" in result["result"]
    assert "acos(0)" in result["result"]
    assert "atan(1)" in result["result"]
    assert "π/2" in result["result"]
    assert "π/4" in result["result"]


@pytest.mark.asyncio
async def test_hyperbolic_functions(server, mock_status):
    """Test hyperbolic functions."""
    code = """
# Test hyperbolic functions
sinh_0 = sinh(0)
cosh_0 = cosh(0)
tanh_0 = tanh(0)
sinh_1 = sinh(1)
cosh_1 = cosh(1)

print(f"sinh(0) = {sinh_0}")
print(f"cosh(0) = {cosh_0}")
print(f"tanh(0) = {tanh_0}")
print(f"sinh(1) ≈ {round(sinh_1, 6)}")
print(f"cosh(1) ≈ {round(cosh_1, 6)}")
"""
    
    result = await server.call("execute_python", {"code": code, "_status": mock_status})
    
    assert result["result"] is not None
    assert "sinh(0) = 0.0" in result["result"]
    assert "cosh(0) = 1.0" in result["result"]
    assert "tanh(0) = 0.0" in result["result"]
    assert "sinh(1)" in result["result"]
    assert "cosh(1)" in result["result"]


@pytest.mark.asyncio
async def test_logarithmic_functions(server, mock_status):
    """Test logarithmic and exponential functions."""
    code = """
# Test log and exp functions
e_val = e()
log_e = log(e_val)
log10_100 = log10(100)
log10_1000 = log10(1000)
exp_1 = exp(1)
log_base_2 = log(8, 2)

print(f"log(e) = {log_e}")
print(f"log10(100) = {log10_100}")
print(f"log10(1000) = {log10_1000}")
print(f"exp(1) ≈ {round(exp_1, 6)}")
print(f"log(8, 2) = {log_base_2}")
"""
    
    result = await server.call("execute_python", {"code": code, "_status": mock_status})
    
    assert result["result"] is not None
    assert "log(e) = 1.0" in result["result"]
    assert "log10(100) = 2.0" in result["result"]
    assert "log10(1000) = 3.0" in result["result"]
    assert "exp(1)" in result["result"]
    assert "log(8, 2) = 3.0" in result["result"]


@pytest.mark.asyncio
async def test_power_functions(server, mock_status):
    """Test power functions."""
    code = """
# Test power functions
pow_2_3 = pow(2, 3)
pow_2_8_mod_5 = pow(2, 8, 5)  # (2^8) % 5 = 256 % 5 = 1
pow_negative = pow(2, -2)

print(f"pow(2, 3) = {pow_2_3}")
print(f"pow(2, 8, 5) = {pow_2_8_mod_5}")  
print(f"pow(2, -2) = {pow_negative}")
"""
    
    result = await server.call("execute_python", {"code": code, "_status": mock_status})
    
    assert result["result"] is not None
    assert "pow(2, 3) = 8.0" in result["result"]
    assert "pow(2, 8, 5) = 1" in result["result"]
    assert "pow(2, -2) = 0.25" in result["result"]


@pytest.mark.asyncio
async def test_angle_conversion(server, mock_status):
    """Test angle conversion functions."""
    code = """
# Test angle conversion
pi_val = pi()
degrees_pi = degrees(pi_val)
degrees_pi_half = degrees(pi_val / 2)
radians_180 = radians(180)
radians_90 = radians(90)

print(f"degrees(π) = {degrees_pi}")
print(f"degrees(π/2) = {degrees_pi_half}")
print(f"radians(180) ≈ {round(radians_180, 6)}")
print(f"radians(90) ≈ {round(radians_90, 6)}")
"""
    
    result = await server.call("execute_python", {"code": code, "_status": mock_status})
    
    assert result["result"] is not None
    assert "degrees(π) = 180.0" in result["result"]
    assert "degrees(π/2) = 90.0" in result["result"]
    assert "radians(180)" in result["result"]
    assert "radians(90)" in result["result"]


@pytest.mark.asyncio
async def test_complex_math_calculation(server, mock_status):
    """Test complex mathematical calculation."""
    code = """
# Complex mathematical calculations
radius = 5
pi_val = pi()

# Circle calculations
area = pi_val * pow(radius, 2)
circumference = 2 * pi_val * radius

# Distance formula
x1, y1 = 0, 0
x2, y2 = 3, 4
distance = sqrt(pow(x2 - x1, 2) + pow(y2 - y1, 2))

# Trigonometric calculation
angle_degrees = 45
angle_radians = radians(angle_degrees)
sine_45 = sin(angle_radians)

print(f"Circle area (r={radius}): {round(area, 2)}")
print(f"Circle circumference (r={radius}): {round(circumference, 2)}")
print(f"Distance (0,0) to (3,4): {distance}")
print(f"sin(45°) ≈ {round(sine_45, 6)}")
"""
    
    result = await server.call("execute_python", {"code": code, "_status": mock_status})
    
    assert result["result"] is not None
    assert "Circle area" in result["result"]
    assert "78.54" in result["result"]  # π * 25 ≈ 78.54
    assert "Circle circumference" in result["result"]
    assert "31.42" in result["result"]  # 2π * 5 ≈ 31.42
    assert "Distance (0,0) to (3,4): 5.0" in result["result"]
    assert "sin(45°)" in result["result"]


@pytest.mark.asyncio
async def test_math_error_handling(server, mock_status):
    """Test error handling for invalid math operations."""
    code = """
# Test error handling
try:
    result = sqrt(-1)
    print(f"Should not reach this: {result}")
except ValueError:
    print("ValueError correctly caught for sqrt(-1)")

try:
    result = log(0)
    print(f"Should not reach this: {result}")
except ValueError:
    print("ValueError correctly caught for log(0)")

try:
    result = asin(2)  # asin domain is [-1, 1]
    print(f"Should not reach this: {result}")
except ValueError:
    print("ValueError correctly caught for asin(2)")
"""
    
    result = await server.call("execute_python", {"code": code, "_status": mock_status})
    
    assert result["result"] is not None
    assert "ValueError correctly caught for sqrt(-1)" in result["result"]
    assert "ValueError correctly caught for log(0)" in result["result"]
    assert "ValueError correctly caught for asin(2)" in result["result"]


@pytest.mark.asyncio
async def test_scientific_calculations(server, mock_status):
    """Test scientific calculations using multiple math functions."""
    code = """
# Scientific calculations
# Calculate the period of a pendulum: T = 2π√(L/g)
L = 1.0  # length in meters
g = 9.81  # gravity in m/s²
pi_val = pi()

period = 2 * pi_val * sqrt(L / g)
print(f"Pendulum period (L={L}m): {round(period, 4)} seconds")

# Calculate compound interest: A = P(1 + r/n)^(nt)  
P = 1000  # principal
r = 0.05  # annual interest rate (5%)
n = 12   # compounding frequency (monthly)
t = 10   # time in years

A = P * pow(1 + r/n, n*t)
print(f"Compound interest result: ${round(A, 2)}")

# Calculate normal distribution value: f(x) = (1/σ√(2π)) * e^(-½((x-μ)/σ)²)
x = 1.0
mu = 0.0  # mean
sigma = 1.0  # standard deviation
e_val = e()

coefficient = 1 / (sigma * sqrt(2 * pi_val))
exponent = -0.5 * pow((x - mu) / sigma, 2)
normal_value = coefficient * pow(e_val, exponent)

print(f"Normal distribution f({x}): {round(normal_value, 6)}")
"""
    
    result = await server.call("execute_python", {"code": code, "_status": mock_status})
    
    assert result["result"] is not None
    assert "Pendulum period" in result["result"]
    assert "Compound interest result" in result["result"]
    assert "Normal distribution" in result["result"]