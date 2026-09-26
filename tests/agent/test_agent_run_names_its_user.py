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
import pytest

from agent_system.core.request_context import current_run_user, register_request_user, release_request_user
from agent_system.llm import hook_notify
from agent_system.tools.status import current_request_id
from test_agent_output_cap_note import _agent, _scripted


@pytest.fixture
def dispatched(monkeypatch):
    """The users of the decisions hook_notify hands the registry. A real, empty registry: the
    agent under test takes it too wherever other tests left hooks registered, and must run on it."""
    from agent_system.hooks.registry import HookRegistry
    registry, seen = HookRegistry(default_timeout=5.0), []
    execute = registry.execute_hooks

    async def recording(hook_type, context, **kwargs):
        if context.llm_provider == "decisions":
            seen.append(context.user_id)
        return await execute(hook_type, context, **kwargs)

    registry.execute_hooks = recording
    import agent_system.hooks as hooks_module
    monkeypatch.setattr(hooks_module, "get_hook_registry", lambda: registry)
    return lambda: seen


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


@pytest.mark.asyncio
async def test_a_run_that_fails_to_set_up_leaves_nothing_of_itself_behind():
    """The run sets its request id and user in its caller's context; with no LLM the setup ends
    before the conversation context its cleanup resets them by exists. The consumer stops at
    "end" and closes nothing, as the app's stream and agent_service do."""
    agent = _agent(max_steps=2, output_cap_notes=0)
    agent._session_tracker.set_session_metadata("s-1", {"user_id": "alice", "agent_name": "test_agent"})
    agent.llm = None

    stream, events = agent.run_events("hi", request_id="run-2", session_id="s-1"), []
    async for event in stream:
        events.append(event)
        if event.get("type") == "end":
            break

    assert any(event.get("type") == "error" for event in events), events
    assert (current_request_id.get(), current_run_user.get()) == (None, None)
    await stream.aclose()


@pytest.mark.asyncio
async def test_a_stream_closed_early_leaves_nothing_of_its_run_behind():
    """A consumer may stop reading before the end (sub_agent_manager stops at an error) and close
    the stream: the run's own cleanup then runs later, elsewhere."""
    agent = _agent(max_steps=2, output_cap_notes=0)
    agent._session_tracker.set_session_metadata("s-1", {"user_id": "alice", "agent_name": "test_agent"})
    agent.llm, _ = _scripted([("done", "stop")])

    stream = agent.run_events("hi", request_id="run-3", session_id="s-1")
    async for event in stream:
        if event.get("type") == "final":
            break
    await stream.aclose()

    assert (current_request_id.get(), current_run_user.get()) == (None, None)
