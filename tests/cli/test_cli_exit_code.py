"""agent-cli run: a run that failed exits 1, a run that answered exits 0.

A cover batch read agent-cli's exit code and counted four runs that died on an
HTTP 413 ("Agent execution failed") as done: the CLI printed the error and
exited 0. Drives the real main() on a real Agent with the harness of
test_cli_ctrl_c_is_a_stop; only the LLM call is replaced.
"""
from __future__ import annotations

import sys

import pytest

import agent_system.agent_cli as agent_cli
from test_cli_ctrl_c_is_a_stop import cli  # noqa: F401 - the harness fixture


@pytest.mark.parametrize("raw", [False, True])
def test_a_run_whose_llm_call_fails_exits_1(cli, monkeypatch, raw):
    async def chat_tools(messages, tools, **kwargs):
        raise RuntimeError("HTTP 413: Request body exceeds the provider maximum size")

    cli.llm.chat_tools = chat_tools
    if raw:
        monkeypatch.setattr(sys, "argv", sys.argv[:1] + ["--raw"] + sys.argv[1:])

    with pytest.raises(SystemExit) as exited:
        agent_cli.main()

    assert exited.value.code == 1


@pytest.mark.parametrize("raw", [False, True])
def test_a_run_that_answers_exits_0(cli, monkeypatch, raw):
    async def chat_tools(messages, tools, **kwargs):
        return {"assistant": {"role": "assistant", "content": "done"}}

    cli.llm.chat_tools = chat_tools
    if raw:
        monkeypatch.setattr(sys, "argv", sys.argv[:1] + ["--raw"] + sys.argv[1:])

    try:
        agent_cli.main()
    except SystemExit as exited:
        assert exited.code in (0, None), f"a run that answered exited {exited.code}"
