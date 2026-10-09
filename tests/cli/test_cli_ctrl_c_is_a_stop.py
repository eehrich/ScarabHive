"""agent-cli: a Ctrl-C is its user stopping the run (core/session_presence/).

The session is let go marked, so input that came in meanwhile does not start it
again by itself. Drives the real main() on a real Agent -- only the LLM, the
bootstrap and the start of a woken run are replaced -- through the ways a
Ctrl-C lands:

* inside the run's own frames: the run lets go of its hold before the CLI
  hears of the stop;
* out of the event loop: the run is left where it was and lets go only at
  exit -- close_cli_loop() here stands in for the atexit hook;
* in chat, where the session stays held after the turn and the prompt's wake
  watcher asks whether input waits.

Either way the stop is noted where the Ctrl-C is caught, and the order in which
the holds let go does not matter.
"""
from __future__ import annotations

import asyncio
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import agent_system.agent_cli as agent_cli
from agent_system.cli_utils.commands import run as run_cmd
from agent_system.cli_utils.event_loop import close_cli_loop
from agent_system.config.models import (
    AgentConfig,
    AgentSystemConfig,
    LLMModelConfig,
    LLMProfile,
    LLMSystemConfig,
    PluginsConfig,
    SessionPresenceConfig,
    ToolServerConfig,
)
from agent_system.core import session_presence as sp
from agent_system.servers.agent.server import Agent
from agent_system.services.initialization_service import InitializationService
from agent_system.services.session_manager import SessionManager
from agent_system.services.session_service import SessionService
from agent_system.tools.base import ToolServerRegistry

USER = "cli_user"


@pytest.fixture
def cli(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_SESSION_STORAGE_PATH", str(tmp_path))
    agent_server = ToolServerConfig(type="agent", enabled=True, agent_config=AgentConfig(max_steps=3))
    config = AgentSystemConfig(
        llm_system=LLMSystemConfig(
            models={"m": LLMModelConfig(provider="openai", model="m", api_key="fake-key")},
            profiles={"normal": LLMProfile(model_ref="m")},
            default_profile="normal"),
        session_presence=SessionPresenceConfig(enabled=True))
    config.plugins = PluginsConfig(servers={"test_agent": agent_server})
    config.default_agent = "test_agent"

    manager = SessionManager(storage_path=str(tmp_path))
    service = SessionService(manager)
    agent = Agent("test_agent", config, agent_server, ToolServerRegistry(), session_service=service)
    llm = AsyncMock()
    llm.supports_streaming = lambda: False
    agent.llm = llm

    def initialize_for_cli(self):
        return ToolServerRegistry(), service

    async def nothing(*args, **kwargs):
        return None

    monkeypatch.setattr(agent_cli, "load_settings", lambda path=None: config)
    monkeypatch.setattr(run_cmd, "setup_role_logging", lambda *args, **kwargs: None)
    monkeypatch.setattr(InitializationService, "initialize_for_cli", initialize_for_cli)
    monkeypatch.setattr(InitializationService, "session_manager", property(lambda self: manager))
    for name in ("initialize_tools", "init_batch_system", "shutdown_tools", "shutdown_batch_system"):
        monkeypatch.setattr(run_cmd, name, nothing)
    monkeypatch.setattr(run_cmd, "entry_agent", lambda *args, **kwargs: agent)
    woken = []
    monkeypatch.setattr(sp.presence, "spawn_wake", lambda session_id, user_id, depth: woken.append(session_id) or (0, 0.0))
    monkeypatch.setattr(sys, "argv", ["agent-cli", "--no-status", "run", "--session", "s1", "do it"])

    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(manager.create_session(user_id=USER, session_id="s1", agent_name="test_agent"))
    finally:
        loop.close()
    monkeypatch.setattr(sp.presence, "_stops", set())
    yield SimpleNamespace(llm=llm, woken=woken, presence=sp.presence_for(config))
    close_cli_loop()


def _input_comes_in(cli):
    assert cli.presence.notify("s1", USER)[0] == "delivered_next_step", "fixture: the run does not hold it"


@pytest.mark.parametrize("raw", [False, True])
def test_a_ctrl_c_inside_the_run_leaves_the_session_marked(cli, monkeypatch, raw):
    def step(self, session_id, request_id):   # the run's own frames, before its LLM call
        _input_comes_in(cli)
        raise KeyboardInterrupt

    monkeypatch.setattr(Agent, "_presence_step", step)
    if raw:
        monkeypatch.setattr(sys, "argv", sys.argv[:1] + ["--raw"] + sys.argv[1:])
    agent_cli.main()

    assert cli.woken == []
    assert cli.presence.notify("s1", USER)[0] == "queued"


def test_a_ctrl_c_out_of_the_loop_leaves_the_session_marked_when_the_run_unwinds(cli):
    async def chat_tools(messages, tools, **kwargs):
        _input_comes_in(cli)

        def interrupt():   # a signal that lands while the loop waits
            raise KeyboardInterrupt

        asyncio.get_running_loop().call_soon(interrupt)
        await asyncio.sleep(60)

    cli.llm.chat_tools = chat_tools
    with pytest.raises(KeyboardInterrupt):
        agent_cli.main()
    assert cli.presence.status("s1", USER) == "running", "fixture: the run let go before exit"

    close_cli_loop()   # what atexit does

    assert cli.woken == []
    assert cli.presence.notify("s1", USER)[0] == "queued"


def test_a_ctrl_c_between_the_runs_events_with_raw_output_leaves_the_session_marked(cli, monkeypatch):
    # The Ctrl-C lands in the frame that collects the events: the run is dropped
    # there and unwinds on the loop next, after the CLI's hold is gone.
    real = Agent.run_events
    asked = []

    async def chat_tools(messages, tools, **kwargs):
        _input_comes_in(cli)
        asked.append(True)
        return {"assistant": {"role": "assistant", "content": "done"}}

    async def collected_until_ctrl_c(self, *args, **kwargs):
        async for event in real(self, *args, **kwargs):
            yield event
            if asked:
                raise KeyboardInterrupt

    cli.llm.chat_tools = chat_tools
    monkeypatch.setattr(Agent, "run_events", collected_until_ctrl_c)
    monkeypatch.setattr(sys, "argv", sys.argv[:1] + ["--raw"] + sys.argv[1:])
    agent_cli.main()
    assert asked, "fixture: the run never asked its model"
    close_cli_loop()   # what atexit does

    assert cli.woken == []
    assert cli.presence.notify("s1", USER)[0] == "queued"


def test_a_ctrl_c_in_a_chat_turn_leaves_the_session_marked(cli, monkeypatch):
    from agent_system.cli_utils import chat

    def step(self, session_id, request_id):   # the run's own frames: it lets go first
        _input_comes_in(cli)
        raise KeyboardInterrupt

    pending_at_the_prompt = []

    def prompt(*args, **kwargs):   # what the prompt's wake watcher would see, then leave
        pending_at_the_prompt.append(cli.presence.pending("s1", USER))
        raise EOFError

    monkeypatch.setattr(Agent, "_presence_step", step)
    monkeypatch.setattr(chat.prompt_input, "_read_input", prompt)
    monkeypatch.setattr(sys, "argv", ["agent-cli", "--no-status", "chat", "--session", "s1", "do it"])
    agent_cli.main()

    assert pending_at_the_prompt == [False]
    assert cli.woken == []
    assert cli.presence.notify("s1", USER)[0] == "queued"


def test_a_ctrl_c_while_the_session_loads_is_a_stop(cli, monkeypatch):
    from agent_system.services.session_service import SessionService

    async def interrupted_load(self, agent, user_id, session_id):
        assert sp.SessionPresence(cli.presence.root).notify(session_id, user_id)[0] == "delivered_next_step"
        raise KeyboardInterrupt

    monkeypatch.setattr(SessionService, "load_and_restore_session", interrupted_load)
    with pytest.raises(KeyboardInterrupt):
        agent_cli.main()

    assert cli.woken == []
    assert cli.presence.notify("s1", USER)[0] == "queued"


def test_a_ctrl_c_before_the_turns_run_takes_the_session_is_a_stop(cli, monkeypatch):
    # The chat claims the turn before it starts: a stop before its run holds the
    # session is the turn's, not the business of the turn before.
    from agent_system.cli_utils import chat

    async def not_yet(self, task, **kwargs):
        _input_comes_in(cli)
        raise KeyboardInterrupt
        yield  # an async generator, as run_events is

    pending_at_the_prompt = []

    def prompt(*args, **kwargs):
        pending_at_the_prompt.append(cli.presence.pending("s1", USER))
        raise EOFError

    monkeypatch.setattr(Agent, "run_events", not_yet)
    monkeypatch.setattr(chat.prompt_input, "_read_input", prompt)
    monkeypatch.setattr(sys, "argv", ["agent-cli", "--no-status", "chat", "--session", "s1", "do it"])
    agent_cli.main()

    assert pending_at_the_prompt == [False]
    assert cli.woken == []
    assert cli.presence.notify("s1", USER)[0] == "queued"


def test_a_chat_turn_is_named_before_it_starts(cli, monkeypatch):
    # So that a Ctrl-C before its start event can stop it too.
    from agent_system.cli_utils import chat

    real, named = Agent.run_events, []

    async def recorded(self, task, request_id=None, **kwargs):
        named.append(request_id)
        async for event in real(self, task, request_id=request_id, **kwargs):
            yield event

    async def chat_tools(messages, tools, **kwargs):
        return {"assistant": {"role": "assistant", "content": "done"}}

    cli.llm.chat_tools = chat_tools
    monkeypatch.setattr(Agent, "run_events", recorded)
    monkeypatch.setattr(chat.prompt_input, "_read_input", lambda *args, **kwargs: (_ for _ in ()).throw(EOFError()))
    monkeypatch.setattr(sys, "argv", ["agent-cli", "--no-status", "chat", "--session", "s1", "do it"])
    agent_cli.main()

    assert len(named) == 1 and named[0], f"the turn started without a name: {named}"


def test_a_chat_that_ends_after_a_normal_turn_is_woken_by_input_that_came_meanwhile(cli, monkeypatch):
    from agent_system.cli_utils import chat

    async def chat_tools(messages, tools, **kwargs):
        _input_comes_in(cli)
        return {"assistant": {"role": "assistant", "content": "done"}}

    cli.llm.chat_tools = chat_tools
    monkeypatch.setattr(chat.prompt_input, "_read_input", lambda *args, **kwargs: (_ for _ in ()).throw(EOFError()))
    monkeypatch.setattr(sys, "argv", ["agent-cli", "--no-status", "chat", "--session", "s1", "do it"])
    agent_cli.main()

    assert cli.woken == ["s1"]


def test_a_run_that_ends_normally_is_woken_by_input_that_came_meanwhile(cli):
    async def chat_tools(messages, tools, **kwargs):
        _input_comes_in(cli)
        return {"assistant": {"role": "assistant", "content": "done"}}

    cli.llm.chat_tools = chat_tools
    agent_cli.main()

    assert cli.woken == ["s1"]
