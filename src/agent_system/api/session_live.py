"""What a session is doing right now, beside what its file says: GET /api/sessions/{id} served from a run in flight,
and the runs of other processes the ``/active`` poll cannot see.

Split from session_endpoints, which keeps the routes: what is here reads the
trackers, the background jobs and session presence, not the session store.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, List, Optional

from agent_system.services.background_job_manager import get_background_job_manager

logger = logging.getLogger(__name__)


async def _run_progress(request_id: Optional[str]) -> tuple[Optional[int], bool]:
    """How many events the run behind ``request_id`` has sent so far, and whether it has answered.

    (None, False) for a run with no background job -- a ``/run`` carrying files, a
    sub-agent's run. Those stream inline and cannot be reconnected to anyway, so
    there is nothing for the numbers to be used for. Both read at one moment: the
    answer a client skips past is the one this says was sent.
    """
    if not request_id:
        return None, False
    try:
        job = await get_background_job_manager().get_job(request_id)
    except Exception as err:  # noqa: BLE001 -- a missing job manager is not a failed session load
        logger.debug("Could not read the event count for %s: %s", request_id, err)
        return None, False
    return (job.events_emitted, job.answered) if job is not None else (None, False)


async def serve_live_state(session: Dict[str, Any], session_id: str, session_agent: Any) -> None:
    """Put into *session* (a record as loaded) what the agent running it holds and the file does not yet.

    A session with a run still in flight: hand out what the run has, not
    what is on disk. The persisted list ends at the last FINISHED turn --
    _finalize_request writes at the end of a request -- so opening such a
    session mid-run showed the history up to that point and then the live
    stream, with everything in between missing. Same "in-flight means the
    tracker holds the truth" rule the context_vars merge below applies,
    and gated on the session lock rather than on the live state being
    present: that state outlives the run it belongs to.

    The lock alone is too narrow a gate: the run releases it after its final
    save but BEFORE its session-end hooks, and its job runs on until those are
    done -- an LLM call, for lessons_learned. A load in that window read the
    turn off disk with no count of what the run had sent, and the chat,
    joining the job, replayed the whole run below it: the turn twice. So a run the tracker maps to this session counts as
    in flight while its job runs. Its live state is its conversation: the
    session's next run sets its own only once it holds the lock.
    """
    tracker = session_agent._session_tracker
    try:
        is_running, owner_request_id = tracker.check_session_locked(session_id)
        if is_running and getattr(tracker, "held_by_a_writer", lambda _: False)(session_id):
            # An append or /undo holds it for a moment: no run, and whatever live state the
            # session has left is a finished run's -- a turn put back since came back with it.
            is_running, owner_request_id = False, None
        if not is_running:
            running = (await get_background_job_manager().active_sessions()).get(session_id) or {}
            if (running.get("attachable")
                    and tracker.get_session_for_request(running["request_id"]) == session_id):
                is_running, owner_request_id = True, running["request_id"]
    except Exception as lock_err:
        logger.debug(f"Could not read the session lock for {session_id}: {lock_err}")
        is_running, owner_request_id = False, None
    if is_running:
        try:
            live = session_agent.get_live_conversation(session_id)
        except Exception as live_err:
            logger.debug(f"Could not read live messages for {session_id}: {live_err}")
            live = None
        if live:
            from agent_system.services.session_service import _msg_to_dict, _add_estimated_tokens
            messages = [_msg_to_dict(m) for m in live]
            # How much of the run's stream these messages already account for, so a
            # client attaching next can ask the run to skip just that much. Without
            # it the reconnect either replays turns the client has (duplicates) or
            # drops the buffer whole -- which loses whatever the run emitted between
            # this response and the attach, up to and including its final answer.
            # Read right after the messages, before the await below: what the run
            # sends meanwhile would count as seen without being in them.
            # Whether those include its answer goes with them: a client joining past
            # the answer is sent nothing that says the run has answered.
            events_seen, answered = await _run_progress(owner_request_id)
            # The panel sums estimated_tokens and counts how many messages carried
            # one; the persisted path adds them, so the live one has to as well or
            # the token figure reads 0 for exactly the sessions worth watching.
            # Off the loop, as there: the estimator probes media files.
            await asyncio.to_thread(_add_estimated_tokens, messages)
            session["messages"] = messages
            session["live_events_seen"] = events_seen
            session["live_run_answered"] = answered

    # Inject live runtime template_vars from the agent's session tracker.
    # save_session persists context_vars only after messages are committed
    # (see services/session_service.py). For sessions still in-flight (no
    # messages saved yet, e.g. a Pipeline run currently in phase 3) the
    # tracker holds the truth — without this merge the Session Info panel
    # shows "No context variables set" while the run is active.
    try:
        runtime_vars = tracker.get_session_template_vars(session_id)
    except Exception as tracker_err:
        logger.debug(f"Could not read runtime template_vars for {session_id}: {tracker_err}")
        runtime_vars = None
    if runtime_vars:
        existing = session.get("context_vars")
        if not isinstance(existing, dict):
            existing = {}
        session["context_vars"] = {**existing, **runtime_vars}


def _add_sessions_running_elsewhere(wanted: List[str], user_id: str, agent,
                                    answer: Dict[str, Any]) -> None:
    """The runs this process cannot see, from the one place that spans processes.

    ``active_sessions()`` is process-internal by its own account -- background
    jobs and this process's session trackers. A WOKEN run is neither: presence
    starts it as ``agent-cli run --woken``, a process of its own
    (``core/session_presence.spawn_wake``). So a session that is working hard
    right after a wake answers this poll as idle, and the panel has no way to
    know the turn it is missing exists. That is what "the agent never wakes up"
    looked like from the outside: measured on session jrbqugnco7, 13 messages
    on disk, six on screen.

    The lock files are the cross-process truth, and reading them is why they
    exist. What comes back is deliberately NOT attachable and carries no
    request id: that run's events never reach this process at all -- its status
    bus is its own, and ``GET /events`` would not know the id. All a client can
    do is say so, and read the session again once it is over.

    Asked of the DIRECTORY, not of each id: a lock file exists only while
    somebody holds the session, so the usual answer costs one ``scandir`` and
    no file read at all. Per id it would be ``presence.get``, which parses the
    whole session file to tell an idle session from none -- measured here at
    0.4 ms over a 92 KB median, so a sidebar poll over its rows would spend
    tens of milliseconds parsing JSON inside the event loop, for an answer
    that is almost always "nobody holds anything".

    What that leaves out is a SUB-agent's session: ``list_for_user`` keeps
    those out, because they are run by the orchestrator that spawned them.
    In this process that orchestrator is the one above, so such a session is
    already in ``answer``; one being run from outside would go unmarked.

    Ownership needs no separate check: presence looks in the asking user's own
    directory, so a session of somebody else is simply not found.
    """
    from agent_system.core.session_presence import presence_for

    # No guard on `agent` being None: presence_for(None) is None, which is the
    # same answer one line further down. A second check would read like it
    # caught something this one does not.
    presence = presence_for(getattr(agent, "system_config", None))
    if presence is None:      # off, or no agent registered: nothing holds anything
        return
    # No catch: list_for_user swallows the OSErrors it can meet (an unreadable
    # directory answers "nobody holds anything") -- one here would never run.
    held = {s["session_id"]: s for s in presence.list_for_user(user_id)}
    for session_id in wanted:
        if session_id in answer:
            continue          # this process knows it better, and can be followed
        state = held.get(session_id)
        if state is None:
            continue
        # Held by THIS process, with no job or tracker entry for it: the gaps
        # between a run's tracker letting go and its presence hold doing so --
        # the save, the session-end hooks -- and an append being persisted. A
        # probe of the lock cannot tell that from another process, and reading
        # it as one refused a delete "because another process works on it".
        if presence.held_here(session_id, user_id):
            continue
        answer[session_id] = {"request_id": None,
                              "agent_name": state.get("agent") or None,
                              "attachable": False,
                              "answered": False,
                              "elsewhere": True}
