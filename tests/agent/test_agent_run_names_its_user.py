"""A call no agent wires is its run's user's.

A decision or a TTS call inside a run reaches the hooks through
llm/hook_notify.py, with no agent to ask. The API registers the runs it
starts under the user asking (request_user_map), but agent-cli, a wake and a
job in its own process start runs nobody registered, and the map lets go of
a run's whole tree when the request that started it ends -- while its
background sub-agents run on. Such a call was nobody's, and the message
debugger showed it to admins only. The run now names its user in a context
variable, where it names its request id.
"""
from unittest.mock import AsyncMock, Mock

import pytest

from agent_system.core.request_context import current_run_user, register_request_user, release_request_user
from agent_system.llm import hook_notify
from test_agent_output_cap_note import _agent, _scripted


@pytest.fixture
def dispatched(monkeypatch):
    """The contexts hook_notify hands the registry."""
    registry = Mock(execute_hooks=AsyncMock())
    import agent_system.hooks as hooks_module
    monkeypatch.setattr(hooks_module, "get_hook_registry", lambda: registry)
    return lambda: [call.args[1].user_id for call in registry.execute_hooks.call_args_list]


@pytest.mark.asyncio
@pytest.mark.parametrize("registered", [None, "released"])
async def test_a_decision_inside_a_run_is_the_user_of_its_session(dispatched, registered):
    agent = _agent(max_steps=2, output_cap_notes=0)
    agent._session_tracker.set_session_metadata("s-1", {"user_id": "alice", "agent_name": "test_agent"})
    llm, _ = _scripted([("done", "stop")])
    stream = llm.chat_tools_streaming

    async def deciding(*args, **kwargs):
        if registered == "released":  # the request that started it has ended meanwhile
            release_request_user("run-1")
        await hook_notify.notify_request(provider="decisions", model="m", url="u")
        async for event in stream(*args, **kwargs):
            yield event

    llm.chat_tools_streaming = deciding
    agent.llm = llm
    if registered:
        register_request_user("run-1", "alice")
    try:
        [event async for event in agent.run_events("hi", request_id="run-1", session_id="s-1")]
    finally:
        release_request_user("run-1")

    assert dispatched() == ["alice"]
    assert current_run_user.get() is None, "the run's user outlived the run"
