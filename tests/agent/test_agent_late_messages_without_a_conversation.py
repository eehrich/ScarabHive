"""A message handed to a run that fails on its way in stays in the session.

The run registers its request -- from then on an append goes to it (append_user_message) -- and builds its
conversation after that. When that fails, _finalize_request has no conversation to take late messages into, and the
message went with the request entry: answered "appended", and gone.
"""
from __future__ import annotations

from test_reasoning_loop_wiring import _real_agent


async def test_a_message_handed_to_a_run_that_failed_to_start_stays():
    agent = _real_agent()
    tracker = agent._session_tracker

    async def fails_on_its_way_in(*, task, request_id, session_id, **kwargs):
        assert await tracker.append_user_message(request_id, "sent while it started"), \
            "fixture: the run's request was not registered yet"
        raise RuntimeError("No LLM available")

    agent._initialize_request_and_conversation = fails_on_its_way_in
    events = [event async for event in agent.run_events("hello", session_id="s1")]

    assert any(event.get("type") == "error" for event in events), "fixture: the run did not fail"
    assert [m.content for m in tracker.get_session_messages("s1")] == ["sent while it started"]
