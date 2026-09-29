"""What the user appends to a session is collected for whoever watches it, and only while somebody does.

SessionTracker.watch_appends hands a request that settles a session after its run (openai_api's AgentTurn) the
messages appended since it opened the session -- beside the runs (append_to_session, the app's append endpoint)
and handed to a run of it (append_user_message) -- so a put back keeps them. Nothing is kept for a session
nobody watches: the tracker must not grow with every session the process ever ran.
"""
from __future__ import annotations

import asyncio

from agent_system.llm.models import ChatMessage
from agent_system.servers.agent.components.session_tracking import SessionTracker

SID = "s1"


def tracker_with_a_session() -> SessionTracker:
    tracker = SessionTracker({})
    tracker.set_session_messages(SID, [ChatMessage(role="user", content="first question")])
    return tracker


async def test_a_watcher_sees_what_is_appended_beside_the_runs_and_into_them():
    tracker = tracker_with_a_session()
    tracker.register_request("r1", SID, {"cancel": asyncio.Event(), "appended": [],
                                         "message_event": asyncio.Event()})
    seen = tracker.watch_appends(SID)

    assert await tracker.append_to_session(SID, "beside")
    assert await tracker.append_user_message("r1", "into the run")

    assert [message.content for message in seen] == ["beside", "into the run"]
    assert seen[0] is tracker.get_session_messages(SID)[-1], "not the message that went into the session"
    assert seen[1] is tracker._active_requests["r1"]["appended"][0], "not the message the run takes in"


async def test_only_what_comes_after_the_watching_and_for_its_session():
    tracker = tracker_with_a_session()
    tracker.set_session_messages("other", [ChatMessage(role="user", content="elsewhere")])
    assert await tracker.append_to_session(SID, "before")
    seen = tracker.watch_appends(SID)

    assert await tracker.append_to_session("other", "to another session")
    assert not await tracker.append_to_session("gone", "to no session")

    assert seen == []


async def test_nothing_is_kept_once_nobody_watches():
    tracker = tracker_with_a_session()
    first, second = tracker.watch_appends(SID), tracker.watch_appends(SID)
    tracker.unwatch_appends(SID, first)

    assert await tracker.append_to_session(SID, "one")
    tracker.unwatch_appends(SID, second)
    assert await tracker.append_to_session(SID, "two")

    assert first == [] and [message.content for message in second] == ["one"]
    assert tracker._append_watchers == {}


async def test_clearing_the_tracker_drops_its_watchers():
    tracker = tracker_with_a_session()
    tracker.watch_appends(SID)

    tracker.clear()

    assert tracker._append_watchers == {}
