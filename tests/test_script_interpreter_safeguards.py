from plugins.script_interpreter.executor import ScriptExecutor
from plugins.script_interpreter.config import ScriptInterpreterConfig


def test_detect_while_true_rejected():
    cfg = ScriptInterpreterConfig(max_loop_iterations=1000)
    executor = ScriptExecutor(cfg)

    code = "while True:\n    x = 1\n"

    res = executor.execute(code)
    assert res["success"] is False
    assert res["error"]["category"] in ("runtime", "security", "syntax")


def test_large_range_rejected():
    cfg = ScriptInterpreterConfig(max_loop_iterations=10)
    executor = ScriptExecutor(cfg)

    code = "sum(range(1000000))"

    res = executor.execute(code)
    assert res["success"] is False
    assert res["error"]["category"] == "security"


def test_small_range_allowed():
    cfg = ScriptInterpreterConfig(max_loop_iterations=1000)
    executor = ScriptExecutor(cfg)

    code = "sum(range(5))"

    res = executor.execute(code)
    assert res["success"] is True
    assert "output" in res
