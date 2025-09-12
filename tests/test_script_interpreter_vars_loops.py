import sys
import types

# inject fake sandboxed_python to avoid installing runtime dependency in tests
fake = types.ModuleType("sandboxed_python")

class FPyException(Exception):
    pass

class Undef:
    undef = object()

class PySandbox:
    pass

class SourceLocation:
    pass


def execute_fpy(code: str, sandbox):
    """Simple executor shim used in unit tests:
    - Eval expressions -> sandbox.display(result)
    - Exec code -> exec in sandbox.variables
    """
    try:
        try:
            compiled = compile(code, '<string>', 'eval')
            result = eval(compiled, {}, sandbox.variables)
            sandbox.display(result)
            return
        except SyntaxError:
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


def test_variable_assignment_and_persistence():
    cfg = ScriptInterpreterConfig()
    executor = ScriptExecutor(cfg)

    # Assign a variable
    res = executor.execute("x = 10", reset_sandbox=True)
    assert res["success"] is True

    # Evaluate expression using variable
    res2 = executor.execute("x * 2")
    assert res2["success"] is True
    assert "20" in res2["output"]


def test_loop_sum():
    cfg = ScriptInterpreterConfig()
    executor = ScriptExecutor(cfg)

    code = "total = 0\nfor i in range(5):\n    total += i"
    res = executor.execute(code, reset_sandbox=True)
    assert res["success"] is True

    # Read variable total
    res2 = executor.execute("total")
    assert res2["success"] is True
    assert "10" in res2["output"]  # 0+1+2+3+4 = 10


def test_simple_function_definition_and_call():
    cfg = ScriptInterpreterConfig()
    executor = ScriptExecutor(cfg)

    code = "def add(a, b):\n    return a + b"
    res = executor.execute(code, reset_sandbox=True)
    assert res["success"] is True

    res2 = executor.execute("add(3,4)")
    assert res2["success"] is True
    assert "7" in res2["output"]
