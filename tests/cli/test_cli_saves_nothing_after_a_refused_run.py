"""agent-cli and agent-run save nothing after a run the agent refused before it started.

Agent.run_events refuses a run before it starts -- its role gate, a session
held for another user, another run's lock (REFUSED_BEFORE_THE_RUN) -- and such a
run ran and wrote nothing. The one-shot run and agent-run saved after every run
that was not cancelled: an existing session's file was rewritten, its
updated_at moved and its agent and profile set to the CLI's entry agent. The
chat saved after every turn.

agent-cli: the harness of test_cli_ctrl_c_is_a_stop (the real main() on a real
Agent, a stored session "s1"); the refusal is Agent.run_events replaced by what
its backstop yields. agent-run: the harness of test_agent_run_session_defaults
with the real run_agent_request.
"""
from __future__ import annotations

import sys

import pytest

import agent_system.agent_cli as agent_cli
from agent_system.servers.agent.server import Agent
from test_cli_ctrl_c_is_a_stop import USER, cli  # noqa: F401 - the harness fixture


async def _refused(self, task, *args, request_id=None, session_id=None, **kwargs):
    yield {"type": "error", "message": "Agent 'test_agent' may not run for this caller",
           "request_id": request_id, "error_type": "agent_role_gate"}
    yield {"type": "end"}


def _stored(tmp_path):
    [path] = list(tmp_path.rglob("s1.json"))
    return path.read_bytes()


def _with_a_conversation(tmp_path):
    """s1 as a conversation holds it: a turn, and a profile of its own. Saved again -- the tracker holds what
    the run loaded -- its record came back with updated_at moved and the CLI's profile; an empty one is not
    saved at all (SessionService), and measured nothing."""
    import asyncio

    from agent_system.services.session_manager import SessionManager

    async def seed():
        manager = SessionManager(storage_path=str(tmp_path))
        session = await manager.load_session(USER, "s1")
        session["messages"] = [{"role": "user", "content": "earlier"}, {"role": "assistant", "content": "noted"}]
        session["llm_profile"] = "the_sessions_own"
        await manager.save_session(session)

    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(seed())
    finally:
        loop.close()
    return _stored(tmp_path)


@pytest.mark.parametrize("raw", [False, True])
def test_a_one_shot_run_refused_before_it_ran_leaves_the_session_file_alone(cli, tmp_path, monkeypatch, raw):
    before = _with_a_conversation(tmp_path)
    monkeypatch.setattr(Agent, "run_events", _refused)
    if raw:
        monkeypatch.setattr(sys, "argv", sys.argv[:1] + ["--raw"] + sys.argv[1:])

    agent_cli.main()

    assert _stored(tmp_path) == before, "the refused run's session was saved"


def test_a_chat_turn_refused_before_it_ran_saves_nothing(cli, tmp_path, monkeypatch):
    from agent_system.cli_utils import chat

    before = _with_a_conversation(tmp_path)
    at_the_prompt = []

    def prompt(*args, **kwargs):   # after the refused turn: the file as it is now, then leave
        at_the_prompt.append(_stored(tmp_path))
        raise EOFError

    monkeypatch.setattr(Agent, "run_events", _refused)
    monkeypatch.setattr(chat.prompt_input, "_read_input", prompt)
    monkeypatch.setattr(sys, "argv", ["agent-cli", "--no-status", "chat", "--session", "s1", "do it"])

    agent_cli.main()

    assert at_the_prompt == [before], "the refused turn's session was saved"


def test_a_ctrl_c_in_a_refused_chat_turn_saves_nothing(cli, tmp_path, monkeypatch):
    """A Ctrl-C that lands after the refusal: the turn counts as cancelled, and a cancelled turn is saved --
    not this one, which ran nothing. The Ctrl-C's own path drops the turn's result; the refusal lives on in
    the turn's state."""
    from agent_system.cli_utils import chat

    before = _with_a_conversation(tmp_path)
    at_the_prompt = []

    async def refused_then_ctrl_c(self, task, *args, request_id=None, session_id=None, **kwargs):
        yield {"type": "error", "message": "refused", "request_id": request_id, "error_type": "agent_role_gate"}
        raise KeyboardInterrupt

    def prompt(*args, **kwargs):
        at_the_prompt.append(_stored(tmp_path))
        raise EOFError

    monkeypatch.setattr(Agent, "run_events", refused_then_ctrl_c)
    monkeypatch.setattr(chat.prompt_input, "_read_input", prompt)
    monkeypatch.setattr(sys, "argv", ["agent-cli", "--no-status", "chat", "--session", "s1", "do it"])

    agent_cli.main()

    assert at_the_prompt == [before], "the refused turn's session was saved after the Ctrl-C"
