"""An aborted sub-agent run must not leave a green line in the status stream.

Audited 2026-09-02. The create and continue paths ended with "Created/Continued
sub-agent ..." no matter how the run finished -- an ``error`` or ``cancelled``
event produced the same END as a successful one, so the only line that stays in
the WebUI (END replaces the progress line) claimed success for a run that never
delivered.

The outcome was never missing: ``result_text`` already carries it as
"Error: ..." / "Cancelled: ...", and the retry gate a few lines above tests for
exactly that prefix.

Second finding covered here: handlers share ONE StatusScope per tool call, so
``wait`` calling ``_handle_poll(params)`` let the inner poll close the scope --
``wait``'s own verdict was silently dropped (``StatusScope.ended``).
"""
from __future__ import annotations

from unittest.mock import AsyncMock, Mock

import pytest

from agent_system.config.models import AgentSystemConfig, MCPConfig
from agent_system.mcp.status import StatusPhase, get_status_bus
from plugins.sub_agent_manager.server import SubAgentManagerServer


@pytest.fixture
def system_config():
    config = Mock(spec=AgentSystemConfig)
    config.llm_system = Mock()
    config.llm_system.profiles = {}
    return config


@pytest.fixture
def server(system_config):
    return SubAgentManagerServer("test_manager", system_config, MCPConfig(
        type="sub_agent_manager", enabled=True, allowed_agents=["basic_agent"]))


def _wire(server, events):
    """Give the server a sub-agent whose run emits `events`."""
    agent = Mock()
    agent.agent_config = Mock()
    agent.agent_config.llm_profile = "normal"
    agent.agent_config.default_llm_profile = "normal"
    agent._session_service = None
    agent._session_tracker = Mock()
    agent._session_tracker.set_session_metadata = Mock()

    async def run_events(*args, **kwargs):
        for event in events:
            yield event

    agent.run_events = run_events

    session_service = Mock()
    session_service.session_manager = Mock()
    session_service.session_manager.load_session = AsyncMock(side_effect=FileNotFoundError())
    session_service.session_manager.create_session = AsyncMock()
    session_service.save_session = AsyncMock()

    manager = Mock()
    manager.create_sub_session = AsyncMock(return_value="sub_session_1")
    manager.update_sub_session_metadata = AsyncMock()
    manager.update_sub_agent_activity = AsyncMock()
    manager._extract_user_id = Mock(return_value="user_1")
    manager._session_service = session_service

    registry = Mock()
    registry.get = Mock(return_value=agent)
    server._extract_registry = Mock(return_value=registry)
    server._extract_session_service = Mock(return_value=session_service)
    server._get_manager = Mock(return_value=manager)


async def _create(server, task="do the thing"):
    """One real tool call; returns (result, published events)."""
    bus = get_status_bus()
    queue = await bus.subscribe(server="test_manager.manage_sub_agent()")
    try:
        result = await server.call_with_status("test_manager_manage_sub_agent", {
            "operation": "create",
            "agent_type": "basic_agent",
            "task": task,
            "_session_id": "parent_session",
        })
    finally:
        bus.unsubscribe(queue)
    events = []
    while not queue.empty():
        events.append(queue.get_nowait())
    assert events, "no status events arrived -- the subscription is vacuous"
    return result, events


def _closing(events):
    closing = [e for e in events if e.phase in (StatusPhase.END, StatusPhase.ERROR)]
    assert len(closing) == 1, [(e.phase, e.message) for e in events]
    return closing[0]


@pytest.mark.parametrize("event,expected,verdict", [
    ({"type": "error", "message": "LLM refused the request"}, "LLM refused", "error"),
    ({"type": "cancelled", "reason": "operator stopped it"}, "operator stopped", "cancelled"),
])
async def test_an_aborted_run_closes_with_an_error(server, event, expected, verdict):
    _wire(server, [{"type": "start"}, event])

    result, events = await _create(server)

    closing = _closing(events)
    assert closing.phase is StatusPhase.ERROR, closing.message
    assert expected in closing.message
    # ... and the answer says the same thing, in the field that carries the
    # verdict. `status` keeps meaning "the run is over": the caller raises on
    # a failing status, and it raises before the transport counter runs.
    assert result["status"] == "completed"
    assert result["outcome"] == verdict
    assert result["result"].startswith(("Error:", "Cancelled:"))


async def test_a_successful_run_still_ends(server):
    """Counter-check: the outcome check must not turn good runs into errors."""
    _wire(server, [{"type": "start"},
                   {"type": "final", "summary": "here is the answer"}])

    _, events = await _create(server)

    closing = _closing(events)
    assert closing.phase is StatusPhase.END
    assert "Created sub-agent" in closing.message


async def test_wait_all_rejects_a_string_with_the_type_it_got(server):
    bus = get_status_bus()
    queue = await bus.subscribe(server="test_manager.manage_sub_agent()")
    try:
        result = await server.call_with_status("test_manager_manage_sub_agent", {
            "operation": "wait_all",
            "instance_ids": "sub_1",  # the typical LLM confusion with instance_id
            "_session_id": "parent_session",
        })
    finally:
        bus.unsubscribe(queue)
    events = []
    while not queue.empty():
        events.append(queue.get_nowait())

    assert result["status"] == "error"
    closing = _closing(events)
    assert closing.phase is StatusPhase.ERROR
    assert "str" in closing.message, closing.message


def test_internal_substeps_do_not_inherit_the_scope():
    """The helper that keeps a sub-step from closing our status scope."""
    from plugins.sub_agent_manager.server import _without_status

    params = {"_status": object(), "_session_id": "s1", "instance_id": "i1"}
    stripped = _without_status(params)

    assert "_status" not in stripped
    assert stripped == {"_session_id": "s1", "instance_id": "i1"}
    assert "_status" in params, "the caller's own params must stay intact"


# --- the verdict a caller reads -------------------------------------------
#
# `status` is the lifecycle ("the run is over"), `outcome` is how it ended.
# They are separate on purpose: agent_caller raises on a failing `status`, and
# it raises BEFORE the transport-failure counter runs -- so putting the verdict
# there would switch that counter off. These pin both halves.

@pytest.mark.parametrize(
    "result_text, expected",
    [
        ("Error: Agent execution failed: Error code: 429 - {'error': {...}}", "error"),
        ("Cancelled: user stopped the run", "cancelled"),
        ("Here is the answer.", "completed"),
        # A sub-agent may legitimately WRITE about an error without failing.
        ("The log shows Error: 429 in line 12.", "completed"),
        ("", "completed"),
    ],
)
def test_the_verdict_follows_the_text_the_caller_gets(result_text, expected):
    from plugins.sub_agent_manager.server import _outcome_status
    assert _outcome_status(result_text) == expected


async def test_an_aborted_continuation_reports_the_same_verdict(server):
    """Continue must not be the softer path: same text, same verdict.

    Every other continue test mocks _handle_continue away, so this return
    value had no cover at all -- a hardcoded "completed" here would have
    survived a green suite.
    """
    _wire(server, [{"type": "start"}, {"type": "error", "message": "LLM refused it"}])
    manager = server._get_manager(None, None)
    manager._session_service.session_manager.load_session = AsyncMock(return_value={
        "session_id": "sub_session_1",
        "agent_name": "basic_agent",
        "messages": [],
        "metadata": {},
        "parent_session": {"session_id": "parent_session"},  # ownership is checked
    })

    result = await server.call_with_status("test_manager_manage_sub_agent", {
        "operation": "continue",
        "instance_id": "sub_session_1",
        "message": "carry on",
        "_session_id": "parent_session",
    })

    assert result["status"] == "completed", result
    assert result["outcome"] == "error", result
    assert result["result"].startswith("Error:")
