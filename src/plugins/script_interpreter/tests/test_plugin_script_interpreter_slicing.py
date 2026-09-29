"""Tests for slice support in the script interpreter sandbox (SafeExecutor).

Slicing was the biggest "idiomatic Python" gap found in the tool_script design
review — LLM-written scripts use x[1:3] / s[:100] constantly. These tests pin
the added ast.Slice handling.
"""

import pytest

from src.plugins.script_interpreter.executor import ScriptExecutor


@pytest.fixture
def executor():
    return ScriptExecutor()


def run(executor, code):
    result = executor.execute(code, reset_sandbox=True)
    assert result.get("success"), f"script failed: {result.get('error')}"
    return result["variables"].get("result")


class TestSlicing:
    def test_list_slice(self, executor):
        assert run(executor, "x = [1,2,3,4,5]\nresult = x[1:3]") == [2, 3]

    def test_string_slice_open_start(self, executor):
        assert run(executor, "s = 'hallo welt'\nresult = s[:5]") == "hallo"

    def test_open_end(self, executor):
        assert run(executor, "x = [1,2,3]\nresult = x[1:]") == [2, 3]

    def test_step(self, executor):
        assert run(executor, "x = [1,2,3,4,5,6]\nresult = x[::2]") == [1, 3, 5]

    def test_reverse(self, executor):
        assert run(executor, "result = 'abc'[::-1]") == "cba"

    def test_negative_bounds(self, executor):
        assert run(executor, "x = [1,2,3,4]\nresult = x[-2:]") == [3, 4]

    def test_slice_with_variables(self, executor):
        assert run(executor, "x = [1,2,3,4,5]\ni = 1\nj = 4\nresult = x[i:j]") == [2, 3, 4]

    def test_plain_index_unaffected(self, executor):
        assert run(executor, "x = [1,2,3]\nresult = x[0]") == 1

    def test_slice_on_unsliceable_fails_cleanly(self, executor):
        result = executor.execute("result = (5)[1:2]", reset_sandbox=True)
        assert not result.get("success")
        assert "subscriptable" in str(result.get("error", "")).lower() or \
               "subscriptable" in str(result.get("error", {}).get("message", "")).lower()
