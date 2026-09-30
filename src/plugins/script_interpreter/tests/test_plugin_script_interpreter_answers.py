"""What the execute tool answers, and the server settings that reach it.

Measured before the fixes: a failed run answered the interpreter's own
Python traceback (the server's source paths) twice and the error dict twice
-- about 2.5 KB for ``1/0`` and 590 KB for an unbounded recursion; a lambda
was listed as ``<plugins.script_interpreter.safe_executor...LambdaFunction
object at 0x...>``; what a failed script printed before its error was
dropped; ``session_ttl_seconds`` and ``max_tracked_sessions`` were
read by the server but dropped by the config before they got there.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock

import pytest

from agent_system.config.models import ToolServerConfig
from plugins.script_interpreter.server import ScriptInterpreterServer


def make_server(**settings) -> ScriptInterpreterServer:
    config = ToolServerConfig(type="script_interpreter", enabled=True,
                              script_interpreter=settings)
    return ScriptInterpreterServer("script_interpreter", None, config)


async def execute(server, code: str) -> dict:
    return await server.call("script_interpreter_execute",
                             {"code": code, "_status": AsyncMock(), "_session_id": "s"})


@pytest.mark.asyncio
async def test_an_error_answer_carries_no_interpreter_traceback():
    answer = await execute(make_server(), "x = 1\nq = x / 0")
    text = json.dumps(answer, default=str)
    assert "Traceback" not in text and "safe_executor.py" not in text, text[:500]
    assert "error_details" not in answer
    assert "code" not in answer["error"]
    # What the model does need is still there.
    assert answer["error"]["line_number"] == 2
    assert "division by zero" in answer["error_message"]
    assert "q = x / 0" in answer["error"]["code_context"]


@pytest.mark.asyncio
async def test_an_error_answer_keeps_what_was_printed_before():
    answer = await execute(make_server(), "print('step 1')\nprint('step 2')\nq = 1 / 0")
    assert answer["output"] == "step 1\nstep 2", answer
    assert "division by zero" in answer["error_message"]


@pytest.mark.asyncio
async def test_an_unsupported_statement_keeps_the_output_and_the_context():
    # UnsupportedFeatureError was re-raised past the normal error path (for a
    # fallback interpreter that no longer exists): the output was dropped and
    # the answer had no code context.
    answer = await execute(make_server(), "print('before')\ny = 1\ndel y")
    assert answer.get("output") == "before", answer
    error = answer["error"]
    assert error["category"] == "unsupported_feature"
    assert error["line_number"] == 3
    assert ">>> 3: del y" in error["code_context"]


@pytest.mark.parametrize("code", [
    "a, b = [1, 2, 3]",
    'raise ValueError("expected a number")',
], ids=["unpacking", "message-with-expected"])
@pytest.mark.asyncio
async def test_a_runtime_error_is_not_called_a_syntax_error(code):
    # Classified by words in the message ("expected", "incomplete"), both
    # became syntax errors without a line.
    answer = await execute(make_server(), code)
    assert answer["error"]["type"] in ("RuntimeError", "ValueError"), answer
    assert answer["error"]["category"] != "syntax"
    assert answer["error"]["line_number"] == 1
    assert answer["error_message"].startswith("Runtime error on line 1:")


@pytest.mark.asyncio
async def test_an_unbounded_recursion_answers_a_small_error():
    answer = await execute(make_server(), "def g(n):\n    return g(n + 1)\ng(0)")
    assert "recursion" in answer["error"]["message"]
    assert len(json.dumps(answer, default=str)) < 3000


@pytest.mark.asyncio
async def test_a_lambda_is_listed_as_a_function():
    answer = await execute(make_server(), "g = lambda z: z * 2\nn = g(2)")
    assert "g=<function>" in answer["result"], answer
    assert "n=4" in answer["result"]
    assert "object at 0x" not in answer["result"]


def test_the_session_settings_reach_the_server():
    server = make_server(session_ttl_seconds=60, max_tracked_sessions=5)
    assert server._session_ttl_seconds == 60
    assert server._max_tracked_sessions == 5
