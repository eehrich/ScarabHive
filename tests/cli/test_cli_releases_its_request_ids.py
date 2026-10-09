"""agent-cli and agent-run let go of what a run registered under its request id.

A run's tool calls, a preloaded tool and its sub-agents register the run's
request id (and ids derived from it, ``<id>_...``) for its user in the
process-wide ownership map (core/request_context.py). The API lets go of that
tree when the request ends (app.py); the CLIs never did, so every turn of a
chat and every run left its entries behind until the map's cap pushed them out.

Drives the real main() and chat loop (the harness of test_cli_ctrl_c_is_a_stop:
a real Agent, the LLM, the bootstrap and wakes replaced). The run's own
registration is stood in for: a wrapper around Agent.run_events registers the
request id and a derived one as a tool call would.
"""
from __future__ import annotations

import sys

import pytest

import agent_system.agent_cli as agent_cli
from agent_system.core.request_context import register_request_user, request_user_map
from agent_system.servers.agent.server import Agent
from test_cli_ctrl_c_is_a_stop import USER, cli  # noqa: F401 - the harness fixture


def _left_behind(request_id):
    return sorted(key for key in request_user_map if key == request_id or key.startswith(f"{request_id}_"))


@pytest.fixture
def runs(monkeypatch):
    """The request ids the runs were started under; each registers itself and a derived id, as a tool
    call does."""
    real = Agent.run_events
    started = []

    async def registering(self, task, *args, **kwargs):
        request_id = kwargs.get("request_id")
        assert request_id, "fixture: the run was started without a request id the caller knows"
        started.append(request_id)
        register_request_user(request_id, USER)
        register_request_user(f"{request_id}_tool1", USER)
        async for event in real(self, task, *args, **kwargs):
            yield event

    monkeypatch.setattr(Agent, "run_events", registering)
    return started


def _answers(cli):
    async def chat_tools(messages, tools, **kwargs):
        return {"assistant": {"role": "assistant", "content": "done"}}

    cli.llm.chat_tools = chat_tools


@pytest.mark.parametrize("raw", [False, True])
def test_a_one_shot_run_leaves_nothing_registered(cli, runs, monkeypatch, raw):
    _answers(cli)
    if raw:
        monkeypatch.setattr(sys, "argv", sys.argv[:1] + ["--raw"] + sys.argv[1:])

    agent_cli.main()

    [request_id] = runs
    assert _left_behind(request_id) == []


def test_a_chat_turn_leaves_nothing_registered_by_the_next_prompt(cli, runs, monkeypatch):
    from agent_system.cli_utils import chat

    _answers(cli)
    at_the_prompt = []

    def prompt(*args, **kwargs):   # after the first turn: what is still registered, then leave
        at_the_prompt.append([_left_behind(request_id) for request_id in runs])
        raise EOFError

    monkeypatch.setattr(chat.prompt_input, "_read_input", prompt)
    monkeypatch.setattr(sys, "argv", ["agent-cli", "--no-status", "chat", "--session", "s1", "do it"])

    agent_cli.main()

    assert len(runs) == 1, "fixture: the chat ran no turn before its prompt"
    assert at_the_prompt == [[[]]]


async def test_an_agent_run_request_leaves_nothing_registered(cli, runs):
    from agent_system.agent_run import run_agent_request

    _answers(cli)
    agent = agent_cli.entry_agent()

    await run_agent_request(agent, "do it", "s1")

    [request_id] = runs
    assert _left_behind(request_id) == []
