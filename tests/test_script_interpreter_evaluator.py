# This test file now uses the global fake module from conftest.py
# No local fake module needed - conftest.py provides the sandboxed_python shim


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
