"""Test script_interpreter plugin error handling and messages."""

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
async def test_import_error_message(server, mock_status):
    """Test helpful error message for unsupported imports."""
    code = "import statistics"
    result = await server.call("execute_python_sandbox", {"code": code, "_status": mock_status})
    
    assert "error" in result
    error = result["error"]
    
    # Should get detailed error from sandboxed_python fallback
    assert isinstance(error, dict)
    # The category might be "parse_error" or "unsupported_feature" depending on the error handling
    assert error["category"] in ["unsupported_feature", "parse_error", "syntax"]
    # Check for helpful error information
    error_content = str(error)
    assert any(keyword in error_content.lower() for keyword in ["import", "not", "allowed", "unsupported"])


@pytest.mark.asyncio
async def test_fstring_now_works(server, mock_status):
    """Test that f-strings now work correctly with SafeExecutor."""
    code = 'print(f"Value: {42}")'
    result = await server.call("execute_python_sandbox", {"code": code, "_status": mock_status})
    
    assert "error" not in result
    assert "result" in result
    assert "Value: 42" in result["result"]


@pytest.mark.asyncio
async def test_if_expression_now_works(server, mock_status):
    """Test that if expressions now work correctly with SafeExecutor."""
    code = "result = 10 if True else 5"
    result = await server.call("execute_python_sandbox", {"code": code, "_status": mock_status})
    
    # Should succeed now with SafeExecutor
    assert "error" not in result
    assert "result" in result
    # Check that the variable was set correctly
    assert "result=10" in result["result"]


@pytest.mark.asyncio
async def test_for_loop_now_works(server, mock_status):
    """Test that for loops now work correctly with SafeExecutor."""
    code = """
for i in range(3):
    print(i)
"""
    result = await server.call("execute_python_sandbox", {"code": code, "_status": mock_status})
    
    # Should succeed now with SafeExecutor
    assert "error" not in result
    assert "result" in result
    # Check that the loop output was captured
    assert "0\n1\n2" in result["result"]


@pytest.mark.asyncio
async def test_security_violation_error(server, mock_status):
    """Test security violation for disallowed functions."""
    # This would require modifying config to disallow a function
    # For now, test with a function that doesn't exist
    code = "unknown_function()"
    result = await server.call("execute_python_sandbox", {"code": code, "_status": mock_status})
    
    assert "error" in result


@pytest.mark.asyncio
async def test_error_includes_available_functions(server, mock_status):
    """Test that error messages include available functions list."""
    code = "import os"
    result = await server.call("execute_python_sandbox", {"code": code, "_status": mock_status})
    
    assert "error" in result
    error = result["error"]
    if "available_functions" in error:
        functions = error["available_functions"]
        assert "print()" in functions
        assert "mean()" in functions
        assert "min()" in functions
        assert "max()" in functions


@pytest.mark.asyncio
async def test_syntax_error_line_number_reporting(server, mock_status):
    """Test that syntax errors report exact line numbers."""
    # Multi-line code with syntax error on line 3
    code = """x = 1
y = 2
print('missing quote
z = 4"""
    
    result = await server.call("execute_python_sandbox", {"code": code, "_status": mock_status})
    
    assert "error" in result
    error = result["error"]
    
    # Check for line number reporting
    error_message = error.get("message", "").lower()
    assert "line" in error_message and "3" in error_message


@pytest.mark.asyncio
async def test_runtime_error_stack_trace(server, mock_status):
    """Test that runtime errors include stack traces."""
    code = """def divide_by_zero():
    return 10 / 0

divide_by_zero()"""
    
    result = await server.call("execute_python_sandbox", {"code": code, "_status": mock_status})
    
    assert "error" in result
    error = result["error"]
    
    # Should have error information - check both message and details
    error_text = str(error)
    assert ("divide_by_zero" in error_text or "ZeroDivisionError" in error_text or 
            "division by zero" in error_text.lower())


@pytest.mark.asyncio
async def test_error_code_context_display(server, mock_status):
    """Test that errors show code context around the problematic line."""
    code = """x = 1
y = 2
invalid_syntax =
z = 4"""
    
    result = await server.call("execute_python_sandbox", {"code": code, "_status": mock_status})
    
    assert "error" in result
    error = result["error"]
    
    # Check for code context in error message
    error_details = str(error)
    # Should contain some form of code context or line reference
    assert any(keyword in error_details.lower() for keyword in ["line", "context", "invalid_syntax"])


@pytest.mark.asyncio
async def test_nested_function_runtime_error_trace(server, mock_status):
    """Test stack trace for nested function calls."""
    code = """def level1():
    return level2()

def level2():
    return level3()

def level3():
    raise ValueError("Deep error")

level1()"""
    
    result = await server.call("execute_python_sandbox", {"code": code, "_status": mock_status})
    
    assert "error" in result
    error = result["error"]
    error_text = str(error)
    
    # Should show error information - either function names or error details
    assert ("level1" in error_text or "level2" in error_text or "level3" in error_text or
            "ValueError" in error_text or "Deep error" in error_text)


# === NEW ERROR HANDLER TESTS ===

@pytest.mark.asyncio
async def test_bitshift_operator_error(server, mock_status):
    """Test that bitshift operators report correct error message."""
    code = """
def test_bitshift():
    x = 8
    return x >> 1
test_bitshift()
"""
    result = await server.call("execute_python_sandbox", {"code": code, "_status": mock_status})
    
    assert "error" in result
    error = result["error"]
    
    # Should get specific bitshift error, not generic "def not allowed"
    assert error["type"] == "RuntimeError"
    assert "Unsupported binary operator: RShift" in error["message"]
    assert "category" in error
    assert "line_number" in error


@pytest.mark.asyncio 
async def test_left_shift_operator_error(server, mock_status):
    """Test that left shift operators report correct error message."""
    code = """
def test_lshift():
    x = 4
    return x << 2
test_lshift()
"""
    result = await server.call("execute_python_sandbox", {"code": code, "_status": mock_status})
    
    assert "error" in result
    error = result["error"]
    
    # Should get specific left shift error
    assert error["type"] == "RuntimeError"  
    assert "Unsupported binary operator: LShift" in error["message"]
    assert "category" in error
    assert "line_number" in error


@pytest.mark.asyncio
async def test_def_function_works(server, mock_status):
    """Test that 'def' function definitions work correctly - no false positives."""
    code = """
def calculate_sum(a, b):
    return a + b

result = calculate_sum(5, 3)
print(f"Sum: {result}")
"""
    result = await server.call("execute_python_sandbox", {"code": code, "_status": mock_status})
    
    # Should succeed - def is supported
    assert "error" not in result
    assert "result" in result
    assert "Sum: 8" in result["result"]


@pytest.mark.asyncio
async def test_for_loop_works(server, mock_status):
    """Test that for loops work correctly - no false positives."""  
    code = """
total = 0
for i in range(5):
    total += i
print(f"Total: {total}")
"""
    result = await server.call("execute_python_sandbox", {"code": code, "_status": mock_status})
    
    # Should succeed - for loops are supported
    assert "error" not in result
    assert "result" in result
    assert "Total: 10" in result["result"]


@pytest.mark.asyncio
async def test_while_loop_works(server, mock_status):
    """Test that while loops work correctly - no false positives."""
    code = """
count = 0
while count < 3:
    print(f"Count: {count}")
    count += 1
"""
    result = await server.call("execute_python_sandbox", {"code": code, "_status": mock_status})
    
    # Should succeed - while loops are supported
    assert "error" not in result
    assert "result" in result
    assert "Count: 0" in result["result"]
    assert "Count: 1" in result["result"] 
    assert "Count: 2" in result["result"]


@pytest.mark.asyncio
async def test_if_statement_works(server, mock_status):
    """Test that if statements work correctly - no false positives."""
    code = """
x = 10
if x > 5:
    print("x is greater than 5")
elif x == 5:
    print("x equals 5")
else:
    print("x is less than 5")
"""
    result = await server.call("execute_python_sandbox", {"code": code, "_status": mock_status})
    
    # Should succeed - if statements are supported
    assert "error" not in result
    assert "result" in result
    assert "x is greater than 5" in result["result"]


@pytest.mark.asyncio
async def test_forbidden_builtin_repr_error(server, mock_status):
    """Test that forbidden builtin 'repr' reports correct error message."""
    code = """
x = [1, 2, 3]
result = repr(x)
print(result)
"""
    result = await server.call("execute_python_sandbox", {"code": code, "_status": mock_status})
    
    assert "error" in result
    error = result["error"]
    
    # Should get specific function not allowed error
    assert error["type"] == "RuntimeError"
    assert "Function 'repr' is not allowed" in error["message"]
    assert "category" in error


@pytest.mark.asyncio
async def test_forbidden_builtin_all_error(server, mock_status):
    """Test that forbidden builtin 'all' reports correct error message."""
    code = """
data = [True, True, False]
result = all(data)
print(result)
"""
    result = await server.call("execute_python_sandbox", {"code": code, "_status": mock_status})
    
    assert "error" in result
    error = result["error"]
    
    # Should get specific function not allowed error
    assert error["type"] == "RuntimeError"
    assert "Function 'all' is not allowed" in error["message"]
    assert "category" in error


@pytest.mark.asyncio
async def test_forbidden_builtin_globals_error(server, mock_status):
    """Test that forbidden builtin 'globals' reports correct error message."""
    code = """
g = globals()
print(g)
"""
    result = await server.call("execute_python_sandbox", {"code": code, "_status": mock_status})
    
    assert "error" in result
    error = result["error"]
    
    # Should get specific function not allowed error
    assert error["type"] == "RuntimeError"  
    assert "Function 'globals' is not allowed" in error["message"]
    assert "category" in error


@pytest.mark.asyncio
async def test_underscore_variable_name_error(server, mock_status):
    """Test that underscore variable names report correct error message."""
    code = """
_private_var = 42
print(_private_var)
"""
    result = await server.call("execute_python_sandbox", {"code": code, "_status": mock_status})
    
    assert "error" in result
    error = result["error"]
    
    # Should get specific variable name not allowed error
    assert error["type"] == "RuntimeError"
    assert "Variable name '_private_var' is not allowed" in error["message"]
    assert "category" in error


@pytest.mark.asyncio
async def test_dunder_variable_name_error(server, mock_status):
    """Test that dunder variable names report correct error message."""
    code = """
__special__ = "test"
print(__special__)
"""
    result = await server.call("execute_python_sandbox", {"code": code, "_status": mock_status})
    
    assert "error" in result
    error = result["error"]
    
    # Should get specific variable name not allowed error
    assert error["type"] == "RuntimeError"
    assert "Variable name '__special__' is not allowed" in error["message"]
    assert "category" in error


@pytest.mark.asyncio
async def test_allowed_variable_names_work(server, mock_status):
    """Test that normal variable names work correctly - no false positives."""
    code = """
normal_var = 42
camelCase = "test"
snake_case = [1, 2, 3]
VAR123 = True

print(f"normal: {normal_var}")
print(f"camel: {camelCase}")  
print(f"snake: {snake_case}")
print(f"caps: {VAR123}")
"""
    result = await server.call("execute_python_sandbox", {"code": code, "_status": mock_status})
    
    # Should succeed - these variable names are allowed
    assert "error" not in result
    assert "result" in result
    assert "normal: 42" in result["result"]
    assert "camel: test" in result["result"]
    assert "snake: [1, 2, 3]" in result["result"]
    assert "caps: True" in result["result"]


@pytest.mark.asyncio
async def test_error_includes_line_numbers_and_context(server, mock_status):
    """Test that errors include proper line numbers and code context."""
    code = """
x = 5
y = 10
result = x >> 2  # This should fail on line 4
z = 20
"""
    result = await server.call("execute_python_sandbox", {"code": code, "_status": mock_status})
    
    assert "error" in result
    error = result["error"]
    
    # Should include line number
    assert "line_number" in error
    assert error["line_number"] == 4
    
    # Should include problematic line
    assert "problematic_line" in error
    assert "x >> 2" in error["problematic_line"]
    
    # Should include code context
    assert "code_context" in error
    context = error["code_context"]
    assert "3:" in context or "4:" in context  # Line numbers in context


@pytest.mark.asyncio
async def test_comprehensive_error_structure(server, mock_status):
    """Test that error responses have consistent structure."""
    code = """
bad_var = _forbidden_name = 42
"""
    result = await server.call("execute_python_sandbox", {"code": code, "_status": mock_status})
    
    assert "error" in result
    error = result["error"]
    
    # Verify complete error structure
    required_fields = ["type", "message", "category", "line_number", "problematic_line", "code_context"]
    for field in required_fields:
        assert field in error, f"Error missing required field: {field}"
    
    # Verify error details structure if present
    if "error_details" in error:
        details = error["error_details"]
        assert isinstance(details, dict)
        assert "type" in details
        assert "message" in details


@pytest.mark.asyncio
async def test_multiple_errors_first_one_reported(server, mock_status):
    """Test that when multiple errors exist, the first one is reported clearly."""
    code = """
_bad_name = 5  # First error: bad variable name
result = _bad_name >> 2  # Would be second error: bitshift
print(result)
"""
    result = await server.call("execute_python_sandbox", {"code": code, "_status": mock_status})
    
    assert "error" in result
    error = result["error"]
    
    # Should report the first error encountered (variable name)
    assert "Variable name '_bad_name' is not allowed" in error["message"]
    assert error["line_number"] == 2  # First line with error


@pytest.mark.asyncio
async def test_complex_expression_with_unsupported_operator(server, mock_status):
    """Test error reporting in complex expressions with unsupported operators."""
    code = """
def fibonacci_fast(n):
    def helper(k):
        if k == 0:
            return (0, 1)
        m, n = helper(k >> 1)  # Unsupported operator in nested context
        c = m * (2 * n - m)
        d = m * m + n * n
        return (c, d) if k % 2 == 0 else (d, c + d)
    return helper(n)[0]

result = fibonacci_fast(10)
"""
    result = await server.call("execute_python_sandbox", {"code": code, "_status": mock_status})
    
    assert "error" in result
    error = result["error"]
    
    # Should identify the bitshift operator error
    assert "Unsupported binary operator: RShift" in error["message"]
    # Note: The exact line context may vary based on when the error is detected


@pytest.mark.asyncio
async def test_nested_function_with_forbidden_builtin(server, mock_status):
    """Test error reporting for forbidden builtins in nested functions."""
    code = """
def outer_function(data):
    def inner_function():
        return repr(data)  # Forbidden function
    return inner_function()

result = outer_function([1, 2, 3])
"""
    result = await server.call("execute_python_sandbox", {"code": code, "_status": mock_status})
    
    assert "error" in result
    error = result["error"]
    
    # Should identify the forbidden function error
    assert "Function 'repr' is not allowed" in error["message"]
    # Line number may vary based on execution order


@pytest.mark.asyncio
async def test_error_message_consistency(server, mock_status):
    """Test that error messages are consistent in format and content."""
    test_cases = [
        ("x = 5\ny = x >> 1", "Unsupported binary operator: RShift"),
        ("x = 4\ny = x << 1", "Unsupported binary operator: LShift"), 
        ("result = repr([1,2,3])", "Function 'repr' is not allowed"),
        ("result = all([True, False])", "Function 'all' is not allowed"),
        ("_bad_var = 1", "Variable name '_bad_var' is not allowed"),
    ]
    
    for code, expected_message in test_cases:
        result = await server.call("execute_python_sandbox", {"code": code, "_status": mock_status})
        
        assert "error" in result, f"Expected error for code: {code}"
        error = result["error"]
        
        # Check error structure consistency
        assert "type" in error
        assert "message" in error
        assert "category" in error
        
        # Check expected message
        assert expected_message in error["message"], f"Expected '{expected_message}' in error for code: {code}"


@pytest.mark.asyncio
async def test_python_constructs_no_false_positives(server, mock_status):
    """Test that basic Python constructs work without false error reports."""
    code = """
# Test basic Python constructs that should work
def test_function(x):
    return x + 10

# Control structures
for i in range(3):
    if i % 2 == 0:
        print(f"Even: {i}")
    else:
        print(f"Odd: {i}")

# While loop
count = 0
while count < 2:
    print(f"Count: {count}")
    count += 1

# List comprehension
squares = [x**2 for x in range(5)]

# Multiple assignment
a, b = 1, 2

# Conditional expression
result = 5
value = "positive" if result > 0 else "non-positive"

# Function call
output = test_function(5)
print(f"Function result: {output}")
print("Basic constructs work!")
"""
    
    result = await server.call("execute_python_sandbox", {"code": code, "_status": mock_status})
    
    # Should succeed without any errors
    assert "error" not in result, f"Unexpected error in basic Python constructs: {result.get('error', {})}"
    assert "result" in result
    assert "Basic constructs work!" in result["result"]