"""Waking a session for work that outlived the turn which started it.

wake_blocked() says up front whether a wake is ruled out; wake_session() rings
the session once the work is over, rings again while somebody holds it, and
stops once nobody waits for the news. The plugins whose work runs on in the
background call them (terminal, ssh_control, sub_agent_manager, coding_cli,
n8n, basic_operations), on an event loop that serves every other request of the
process. Its own module because it is that async layer over
SessionPresence.notify() (presence.py), not part of the rules it calls.
"""
from __future__ import annotations

import asyncio
import inspect
import logging
from typing import Any, Awaitable, Callable, Optional, Union

from .presence import STOPPED, presence_for, stopped_by_user
from .process import wake_depth

logger = logging.getLogger(__name__)

#: The one reason that is a setting rather than a fault, so it is the one
#: reason a blocked wake is not worth a warning. Named, because the log level
#: is decided by the REASON -- a reason nobody has thought of yet should be
#: visible, not quietly demoted.
PRESENCE_OFF = "session presence is off (config: session_presence.enabled)"


def wake_blocked(system_config: Any, session_id: str, user_id: str) -> str:
    """Why waking this session is ruled out already, or "" when it is not.

    Note the direction: here "" is the GOOD answer, while ``wake_session``
    returns "" when nothing happened.

    Asked BEFORE long work starts, so whoever wants to be woken can be told to
    poll instead. The wording reaches the model that asked, so it says what to
    change rather than only what failed.

    "" is not a promise. It means everything that CAN be checked up front came
    out fine. Two of ``notify``'s exits cannot be:

    * Whether the process holding the work is still there when the work ends.
      A one-shot ``agent-cli run`` ends its turn and takes its background work
      with it, and nothing in this tree marks which kind of process this is.
    * Whether this is a sub-agent's session, which is never woken -- the run
      that spawned it hands its result over. Reading that means parsing the
      whole session file (``_stored_session``), which is the conversation, on
      the caller's event loop for every armed wake. Deliberately not done
      here; ``wake_session`` reports it as a wake that did NOT happen.

    So a caller told the wake is armed must still treat being woken as the
    good case and polling as the fallback.
    """
    presence = presence_for(system_config)
    if presence is None:
        return PRESENCE_OFF
    if not session_id or not user_id:
        # Both are injected per tool call (servers/agent/components/
        # tool_execution.py). Missing means there is no session behind this.
        return "this call belongs to no session, so there is nobody to wake"
    # Free to ask -- an env var and an int -- and it covers the setting that
    # turns waking off entirely (max_wake_depth: 0, "0 = never wake"), which
    # would otherwise answer "armed" to every caller and wake none of them.
    depth = wake_depth()
    if depth >= presence.max_wake_depth:
        if presence.max_wake_depth == 0:
            return "waking is switched off (config: session_presence.max_wake_depth is 0)"
        return (f"this run was itself woken, {depth} deep, and the wake chain stops at "
                f"max_wake_depth={presence.max_wake_depth}")
    return ""


#: A session that is HELD when the work ends gets rung again, because the ring
#: it got is thrown away (see the loop below). Five minutes of ringing at ten
#: seconds covers an ordinary turn; a longer one ends with the marker in place,
#: and release() wakes it on that.
WAKE_RETRY_SECONDS = 10.0
WAKE_RETRIES = 30

#: The answers from notify() that mean "the session is busy, ring again". Every
#: other answer ends the ringing, which is deliberate: a session somebody HOLDS
#: lets go at some point, and one a wake run is ALREADY on its way to is being
#: read right now -- ringing on through the second starts a second wake run for
#: news somebody is reading. notify() gave both the same answer until 20.09.2026;
#: "being_woken" ends the ringing here by not being in this set. A session its
#: user stopped is rung on as well (the loop below): the next run the user starts
#: lifts its mark, and work of a run nobody stopped is still news then.
RING_AGAIN = frozenset({"delivered_next_step"})


async def _asks_for_it(still_needed: Callable[[], Union[bool, Awaitable[bool]]],
                       session_id: str, what: str) -> bool:
    """``still_needed``'s answer, awaited where it is an awaitable. A guard that
    raises -- a session file read half-written, a PermissionError of a Windows
    replace -- counts as still needed: it used to end the whole wake."""
    try:
        needed = still_needed()
        if inspect.isawaitable(needed):
            needed = await needed
        return bool(needed)
    except Exception as error:
        logger.warning("Could not ask whether %s still waits for %s, ringing on: %s",
                       session_id, what or "finished work", error)
        return True


async def wake_session(system_config: Any, session_id: str, user_id: str,
                       what: str = "",
                       still_needed: Optional[Callable[[], Union[bool, Awaitable[bool]]]] = None,
                       started_by: Optional[str] = None) -> str:
    """Tell a session that something it has been waiting for is over.

    For work that outlives the turn which started it -- a sub-agent, a
    background command -- so its caller can end the turn instead of polling.
    ``what`` names the work in the log. Returns notify()'s status, or "" when
    nothing was done -- the opposite direction from ``wake_blocked``, where ""
    is the good answer.

    THIS IS FOR NEWS THAT LIVES IN NO STORE. notify() only rings a bell: a
    session that is held takes the marker at its next step (``take_pending``)
    because a pre-LLM hook is expected to hand the waiting input over. Nothing
    hands over "your background command finished", so that ring is lost. It is
    therefore repeated while the session stays held.

    ``still_needed`` is what keeps the repetition from costing a turn. Asked
    before every ring after the first, it ends the ringing once the caller has
    read the result by itself -- otherwise a ring can land after a wake run has
    already delivered and read the news, and start a second run for it. Pass it
    whenever the caller can tell; without it the loop rings its full budget. It
    may return an awaitable, for a caller that has to look the answer up -- the
    reader can be a run of another process, whose reading only its stored state
    shows. One that raises is logged and counts as still needed: ringing on costs
    at most a woken run, stopping would lose the news. The ringing also ends
    once a turn was woken for the waiting input (<session>.woken changed, see
    take_for_wake): an agent-cli chat holds its session for the whole REPL and
    starts a turn per ring, and a woken run holds it like any run.

    A wake only reaches a session whose work still runs somewhere, and
    background work lives in the process that started it: the API, or an
    ``agent-cli chat`` whose prompt waits on the same loop, where the wake
    arrives at the prompt. A one-shot ``agent-cli run`` ends its turn and
    takes the work with it, and then nothing is left to wake anybody.

    Work of a run its user stopped rings nobody: the user stopped it, and the
    next run the user starts is no reason to deliver it either. The run is the
    one the caller's task inherited (tools/status.current_request_id) -- the
    work's task is started from the run's tool call, and its tool calls and
    sub-agents carry the run's id as a prefix. A task started outside any run
    (a sweeper) carries whichever run started it, or none, and a task that
    iterated another run's stream carries that one's -- a caller that knows the
    run passes it as ``started_by``. Work of another run
    rings on while the session is marked stopped, within the same budget: the
    user's next run lifts the mark.

    A failed wake costs its caller a poll, never the operation: by the time
    this runs the operation is over and recorded, and letting the failure
    through would have the caller's error handling record a finished job as
    failed. Cancellation is deliberately NOT caught -- a caller being torn
    down should not be held up over a message.
    """
    try:
        blocked = wake_blocked(system_config, session_id, user_id)
        if blocked:
            # By the REASON, not by a stand-in for it: presence being off is a
            # setting somebody chose, everything else is somebody asking for a
            # wake that was never going to happen -- including a reason added
            # later, which should be seen rather than quietly demoted.
            report = logger.debug if blocked == PRESENCE_OFF else logger.warning
            report("Not waking %s for %s: %s", session_id, what or "finished work", blocked)
            return ""
        presence = presence_for(system_config)
        if started_by is None:
            from ...tools.status import current_request_id
            started_by = current_request_id.get() or ""
        # notify() reads and writes lock files and may start a process. The
        # loop this runs on serves every other request of the process, so it
        # does not wait for that here.
        state, note = "", ""
        # Read before the first ring, after the caller recorded its news: a turn that changes the stamp from
        # here on started once the news was there, and was told that input waits.
        stamp = await asyncio.to_thread(presence.wake_stamp, session_id, user_id)
        # One bound, the range: two would make neither of them measurable.
        for ring in range(WAKE_RETRIES + 1):
            if ring:
                # The previous ring found the session held, and the marker it
                # left will be taken by that session's next step without
                # anything handing the news over. Wait for it to let go.
                await asyncio.sleep(WAKE_RETRY_SECONDS)
                # Asked BEFORE ringing again, never after: a ring that goes out
                # while a wake run is already reading the news starts a SECOND
                # one, and that is a whole turn on the user's money.
                if still_needed is not None and not await _asks_for_it(still_needed, session_id, what):
                    # "not needed" covers read-by-its-caller AND gone-from-the
                    # registry (pruned, stopped). Naming only the first would be
                    # a reason this cannot know.
                    logger.debug("Stopped ringing %s for %s: nobody waits for it any more",
                                 session_id, what or "finished work")
                    return state
                # A turn was woken for the waiting input since the first ring -- a held chat's prompt, a woken
                # run (take_for_wake): that turn is the delivery. Ringing on gave the session a turn per ring.
                if await asyncio.to_thread(presence.wake_stamp, session_id, user_id) not in ("", stamp):
                    logger.debug("Stopped ringing %s for %s: a turn was woken for it",
                                 session_id, what or "finished work")
                    return state
            # Before every ring, the first included: the user may start a run
            # meanwhile, which lifts the session's mark but does not want this.
            if stopped_by_user(started_by):
                state, note = "queued", STOPPED
                break
            state, note = await asyncio.to_thread(presence.notify, session_id, user_id)
            if state not in RING_AGAIN and note != STOPPED:
                break

        # notify() has exits that wake nobody -- a sub-agent's session, a wake
        # chain at its limit, a session that is not on disk. Reporting those as
        # "woke" made the only operator-visible signal read like success.
        # delivered_next_step counts as woken only once it is the LAST word:
        # the session let go, and release() wakes it on the marker. being_woken
        # is a run already on its way, which is the same good outcome as having
        # started one -- reported as a failure it would make the one signal an
        # operator has read like an error for the case that worked best.
        # ONE decision, used twice. `report is logger.info` was never true --
        # every attribute access on a logger builds a fresh bound method -- so
        # the line said "Did NOT wake" for a wake that worked.
        woke = state in ("woke_session", "delivered_next_step", "being_woken")
        report = logger.info if woke or note == STOPPED else logger.warning
        report("%s %s for %s: %s%s", "Woke" if woke else "Did NOT wake",
               session_id, what or "finished work", state, f" ({note})" if note else "")
        return state
    except Exception as e:
        logger.warning("Could not wake %s for %s: %s", session_id,
                       what or "finished work", e)
        return ""
