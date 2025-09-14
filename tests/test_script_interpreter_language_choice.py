"""Test cases for Task 9059: Script language choice and basic functionality."""
import pytest
from sandboxed_python import execute_fpy, PySandbox, FPyException, SourceLocation, Undef


class BaseSandbox(PySandbox):
    """Base sandbox with all required abstract methods implemented."""

    def __init__(self):
        self.variables = {}
        self.location = None
        self.output = []

    def get_location(self):
        return self.location

    def set_location(self, location):
        self.location = location

    def display(self, value):
        """Display method called by execute_fpy for expression results."""
        self.output.append(value)
        return value

    def method_call(self, subject, method_name, args, location):
        # Allow basic string methods for future use
        if isinstance(subject, str) and method_name in ["upper", "lower", "strip"]:
            return getattr(subject, method_name)(*args)
        raise Exception(f"Method {method_name} not allowed on {type(subject)}")

    def get_var(self, name):
        return self.variables.get(name, Undef.undef)

    def set_var(self, name, value):
        if isinstance(value, Undef):
            self.variables.pop(name, None)
        else:
            self.variables[name] = value

    def list_vars(self):
        return self.variables.keys()

    def exception_to_message(self, exception):
        return str(exception)


class TestScriptLanguageChoice:
    """Test that sandboxed-python works for our use cases."""

    def test_basic_math_expressions(self):
        """Test that basic math expressions work correctly."""
        # Test simple arithmetic
        result = []

        class TestSandbox(BaseSandbox):
            def __init__(self):
                super().__init__()
                self.output = []

            def func_call(self, func_name, args, location):
                if func_name == "capture_result":
                    result.append(args[0])
                    return args[0]
                raise Exception(f"Function {func_name} not allowed")

        sandbox = TestSandbox()

        # Test basic arithmetic that LLMs commonly use
        execute_fpy("capture_result((2+3)*4)", sandbox=sandbox)
        assert result[0] == 20

        execute_fpy("capture_result(15**0.5)", sandbox=sandbox)
        assert abs(result[1] - 3.872983346207417) < 1e-10

    def test_security_restrictions(self):
        """Test that dangerous operations are blocked."""
        # These should fail in a secure sandbox
        dangerous_codes = [
            "import os",
            "open('/etc/passwd')",
            "__import__('subprocess')",
            "exec('print(1)')",
            "eval('1+1')",
        ]

        class TestSandbox(BaseSandbox):
            def func_call(self, func_name, args, location):
                raise Exception(f"Function {func_name} not allowed")

        sandbox = TestSandbox()
        for dangerous_code in dangerous_codes:
            with pytest.raises(Exception):  # Should raise some kind of exception
                execute_fpy(dangerous_code, sandbox=sandbox)

    def test_syntax_error_handling(self):
        """Test that syntax errors are handled gracefully."""
        class TestSandbox(BaseSandbox):
            def func_call(self, func_name, args, location):
                raise Exception(f"Function {func_name} not allowed")

        sandbox = TestSandbox()
        with pytest.raises(FPyException):
            execute_fpy("invalid syntax here )(", sandbox=sandbox)

    def test_mathematical_functions(self):
        """Test that basic mathematical operations work."""
        result = []

        class MathSandbox(BaseSandbox):
            def func_call(self, func_name, args, location):
                if func_name == "abs":
                    return abs(args[0])
                elif func_name == "min":
                    return min(args)
                elif func_name == "max":
                    return max(args)
                elif func_name == "capture":
                    result.append(args[0])
                    return args[0]
                raise Exception(f"Function {func_name} not allowed")

        sandbox = MathSandbox()

        # Test mathematical functions
        execute_fpy("capture(abs(-42))", sandbox=sandbox)
        assert result[0] == 42

        execute_fpy("capture(min(5, 3, 8, 1))", sandbox=sandbox)
        assert result[1] == 1

        execute_fpy("capture(max(5, 3, 8, 1))", sandbox=sandbox)
        assert result[2] == 8

    def test_variable_assignment(self):
        """Test that variable assignment works (for future phases)."""
        class VarSandbox(BaseSandbox):
            def func_call(self, func_name, args, location):
                raise Exception(f"Function {func_name} not allowed")

        sandbox = VarSandbox()

        # Test variable assignment
        execute_fpy("x = 42", sandbox=sandbox)
        assert sandbox.variables["x"] == 42

        execute_fpy("y = x * 2", sandbox=sandbox)
        assert sandbox.variables["y"] == 84

    def test_llm_friendly_expressions(self):
        """Test expressions that LLMs commonly generate."""
        result = []

        class LLMSandbox(BaseSandbox):
            def func_call(self, func_name, args, location):
                if func_name == "result":
                    result.append(args[0])
                    return args[0]
                raise Exception(f"Function {func_name} not allowed")

        sandbox = LLMSandbox()

        # Common LLM expressions
        expressions = [
            "result((2 + 3) * 4)",  # Basic arithmetic with parentheses
            "result(10 / 2 + 5)",   # Mixed operations
            "result(2 ** 3)",       # Exponentiation
            "result(17 % 5)",       # Modulo
            "result(15 // 4)",      # Floor division
        ]

        expected = [20, 10.0, 8, 2, 3]

        for i, expr in enumerate(expressions):
            execute_fpy(expr, sandbox=sandbox)
            assert result[i] == expected[i]