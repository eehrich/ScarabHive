import sys
import types

# Create a fake `sandboxed_python` module to avoid installing external dependency during tests.
fake = types.ModuleType("sandboxed_python")


class FPyException(Exception):
    pass


class PySandbox:
    pass


class SourceLocation:
    pass


class Undef:
    undef = object()


def execute_fpy(code: str, sandbox):
    """Very small shim to mimic sandboxed-python execution for tests.

    - If code represents an expression, evaluate and call sandbox.display(result).
    - Otherwise execute with exec in an empty globals dict and the sandbox's variables as locals.
    """
    try:
        # Try eval first (expressions)
        try:
            compiled = compile(code, '<string>', 'eval')
            result = eval(compiled, {}, sandbox.variables)
            sandbox.display(result)
            return
        except SyntaxError:
            # Not an expression, try exec
            compiled = compile(code, '<string>', 'exec')
            exec(compiled, {}, sandbox.variables)
            return
    except Exception as e:
        raise FPyException(str(e))


fake.execute_fpy = execute_fpy
fake.FPyException = FPyException
fake.PySandbox = PySandbox
fake.SourceLocation = SourceLocation
fake.Undef = Undef

sys.modules['sandboxed_python'] = fake


from plugins.script_interpreter.executor import ScriptExecutor
from plugins.script_interpreter.config import ScriptInterpreterConfig


def test_simple_arithmetic():
    cfg = ScriptInterpreterConfig()
    executor = ScriptExecutor(cfg)

    result = executor.execute("(2+3)*4")
    assert result["success"] is True
    # Output may be in output buffer; variables empty
    assert "output" in result
    # Evaluate numeric result in variables or output
    # sandboxed environment may put last expression in output
    assert any(s in result["output"] or s in str(result["variables"]) for s in ["20", "20.0"])


def test_syntax_error():
    cfg = ScriptInterpreterConfig()
    executor = ScriptExecutor(cfg)

    result = executor.execute("for i in range(")
    assert result["success"] is False
    assert result["error"]["category"] == "syntax"
