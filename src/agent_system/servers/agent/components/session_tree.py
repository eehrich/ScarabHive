"""The sessions of every agent's tracker taken together: which run below which.

An agent called as a tool runs on a session of its own below its caller's
(Agent.tool_session_id); its metadata names the caller's session and agent as
its parent. These queries walk that tree across every SessionTracker of the
process, whichever agent's tracker holds each level. Kept apart from the
tracker itself, which answers for one agent's sessions only; session_tracking
still exports every name here.
"""
from __future__ import annotations

import weakref
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from .session_tracking import SessionTracker


#: Every tracker of this process -- one per agent. A run's messages wait in the
#: tracker of the agent that runs it, and a question asked in a sub-run (another
#: agent) looks up whether the person wrote to a run above it (ask_user). A
#: session discarded takes the sessions other agents run below it along
#: (discard_session).
_trackers: "weakref.WeakSet[SessionTracker]" = weakref.WeakSet()


#: The sets watch_appended_sessions hands out, each collecting the ids of the sessions a message is appended to.
_append_watches: list[set[str]] = []


def watch_appended_sessions() -> set[str]:
    """A set that collects the id of every session a message is appended to from now on, in any agent's tracker --
    beside its runs (append_to_session) or handed to a run of it (append_user_message) -- until
    unwatch_appended_sessions. For a request that puts back sessions it does not know yet: openai_api's turn, for
    the sessions of the agents its run calls as tools (a message the person appended there is theirs). Kept only
    while somebody watches."""
    seen: set[str] = set()
    _append_watches.append(seen)
    return seen


def unwatch_appended_sessions(seen: set[str]) -> None:
    _append_watches[:] = [other for other in _append_watches if other is not seen]


def session_chain(session_id: str) -> list[str]:
    """*session_id* and every session above it, nearest first: an agent called as a tool runs on one of its
    own below its caller's (Agent.tool_session_id; its metadata names the caller's as its parent), through
    every level, whichever agent's tracker holds each. The last one is where the chain started; just
    *session_id* for any other session (none for an empty id)."""
    chain: list[str] = []
    while session_id and session_id not in chain:
        chain.append(session_id)
        session_id = next((metadata["parent_session_id"] for tracker in list(_trackers)
                           for metadata in (tracker._session_metadata.get(session_id),)
                           if isinstance(metadata, dict) and metadata.get("parent_session_id")), None)
    return chain


def callers_session(session_id: str) -> str:
    """The session a session run below another belongs to (the top of its session_chain), through every
    level. Itself for any other session."""
    chain = session_chain(session_id)
    return chain[-1] if chain else session_id


def _children(session_id: str, agent_name: Optional[str]) -> list[tuple["SessionTracker", str]]:
    """The sessions run below *session_id* of *agent_name* -- the agents it called as tools: their metadata names
    that session and that agent as their parent (Agent._open_tool_session) -- with the tracker of each. The agent
    counts: another agent's run on the same session id has children of its own."""
    return [(tracker, child) for tracker in list(_trackers)
            for child, metadata in list(tracker._session_metadata.items())
            if isinstance(metadata, dict) and metadata.get("parent_session_id") == session_id
            and metadata.get("parent_agent") == agent_name]


def held_below(session_id: str, agent_name: Optional[str]) -> list[tuple["SessionTracker", str]]:
    """Every session run below *session_id* of *agent_name*, through every level, with the tracker that holds
    it: what goes with it (discard_session), and what openai_api's turn deletes or puts back with it."""
    below: list[tuple["SessionTracker", str]] = []
    for tracker, child in _children(session_id, agent_name):
        below.append((tracker, child))
        below.extend(held_below(child, tracker.agent_name))
    return below


def sessions_below(session_id: str, agent_name: Optional[str]) -> list[str]:
    """The ids of held_below."""
    return [child for _, child in held_below(session_id, agent_name)]


def a_run_has(session_id: str) -> bool:
    """Whether a run of this process has *session_id* right now, in whichever agent's tracker (run_has)."""
    return any(tracker.run_has(session_id) for tracker in list(_trackers))


def _parent_held(metadata: dict) -> bool:
    """Whether the session a child session runs below is still in its agent's tracker."""
    parent, agent_name = metadata.get("parent_session_id"), metadata.get("parent_agent")
    return any(tracker.agent_name == agent_name and parent in tracker._session_metadata
               for tracker in list(_trackers))


def message_waits_for(request_id: str) -> bool:
    """Whether a message the person wrote into the active run ``request_id``
    waits for that run's next step, whichever agent runs it."""
    return bool(request_id) and any(tracker.has_appended(request_id) for tracker in list(_trackers))
