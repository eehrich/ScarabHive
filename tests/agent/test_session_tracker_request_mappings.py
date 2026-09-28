"""An ended request keeps its request -> session mapping for a while, not for the life of the process.

The mapping outlives its request on purpose: an append to a run that ended a moment ago still finds the run's
session (app: /events/{id}/append falls back to it), and the admin view and the job manager name a finished job's
session. Kept for every request -- a one-shot call's included, hundreds per book -- it grew without bound.
"""
from __future__ import annotations

import asyncio

from agent_system.servers.agent.components.session_tracking import SessionTracker


class _Answers:
    """A model that answers at once."""

    model = "test/model"

    def supports_streaming(self):
        return True

    async def chat_tools_streaming(self, messages, tools, cancellation_token=None, status_scope=None):
        from test_reasoning_loop_wiring import FINAL
        yield FINAL


async def test_the_agent_keeps_the_mappings_of_its_last_ended_requests_only(monkeypatch):
    from test_reasoning_loop_wiring import _real_agent

    monkeypatch.setattr(SessionTracker, "ENDED_REQUESTS_KEPT", 3)
    agent = _real_agent()
    agent.llm = _Answers()
    request_ids = []
    for n in range(6):
        events = [event async for event in agent.run_events("hello", session_id=f"s{n}")]
        request_ids.append(next(event["request_id"] for event in events if event.get("type") == "start"))

    tracker = agent._session_tracker
    assert list(tracker._request_to_session) == request_ids[-3:], "not the last three that ended"
    assert tracker.get_session_for_request(request_ids[-1]) == "s5"


async def test_an_active_requests_mapping_is_never_forgotten(monkeypatch):
    monkeypatch.setattr(SessionTracker, "ENDED_REQUESTS_KEPT", 2)
    tracker = SessionTracker({})
    tracker.register_request("running", "s-run", {"cancel": asyncio.Event(), "appended": []})
    for n in range(5):
        tracker.register_request(f"ended{n}", f"s{n}", {"cancel": asyncio.Event(), "appended": []})
        tracker._active_requests.pop(f"ended{n}")  # as its end does (AgentRequestManager.unregister_active_request)

    assert tracker.get_session_for_request("running") == "s-run"
    assert list(tracker._request_to_session) == ["running", "ended4"]
