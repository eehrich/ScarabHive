"""A session the API wakes runs its woken turn in the API, as a background job.

presence.notify() wakes a session nobody holds. Rung from the API, that used to start
``agent-cli run --woken``, a process of its own: its events never reach this one, so
the chat could not follow the turn, stop it or write to it -- the session said "being
worked on in another process" for as long as the turn ran, and the process outlived
the API that started it. Here the woken turn is a job like a detached POST /run: the
chat joins it, its Stop stops it, a message typed meanwhile goes into it, and the
API's end ends it.

``install`` hands notify() the waker as the app's lifespan starts -- before the
plugins, one of which rings as it starts (coding_cli's sweep) -- and a turn waits for
``ready`` before it runs. After the stop signal nobody is woken: the input waits for
the next run. ``close`` as the lifespan ends, however it ends: from then on the waker
refuses every ring, so the turns the end cancels, letting go of their sessions, start
no agent-cli that would outlive the API either.
"""
from __future__ import annotations

import asyncio
import contextvars
import logging
from typing import Any, Optional

from fastapi import HTTPException

from agent_system import app_state
from agent_system.core.session_presence import SessionPresence, set_local_waker, woken_at

logger = logging.getLogger(__name__)

#: notify()'s note for a ring after the stop signal.
SHUTTING_DOWN = "the API is shutting down; the session reads the input on its next run"


class _Waker:
    """The waker of one app: takes a woken turn from notify() and runs it on the app's loop."""

    def __init__(self, app: Any, loop: asyncio.AbstractEventLoop) -> None:
        self.app = app
        self.loop = loop
        self.ready = asyncio.Event()
        self.turns: set[asyncio.Future] = set()   # held: the loop's weak reference is not all that keeps them
        #: The app's own context, a copy of which each turn starts in: what the ringer's carries (its run, its
        #: tool call, its caller's profile) is not the turn's.
        self.context = contextvars.copy_context()

    def __call__(self, presence: SessionPresence, session_id: str, user_id: str, depth: int) -> str:
        """From notify(), on whichever thread rang, with this process holding the session for the turn in
        ``presence``: "" when the turn is taken, else why not."""
        stop = app_state.shutdown_event
        if stop is not None and stop.is_set():
            return SHUTTING_DOWN
        # Raises once the loop is closed; notify() then drops the hold and reports it.
        self.loop.call_soon_threadsafe(self._start, presence, session_id, user_id, depth,
                                       context=self.context.copy())
        return ""

    def _start(self, presence: SessionPresence, session_id: str, user_id: str, depth: int) -> None:
        turn = asyncio.ensure_future(self._turn(presence, session_id, user_id, depth))
        self.turns.add(turn)
        turn.add_done_callback(self.turns.discard)

    async def _turn(self, presence: SessionPresence, session_id: str, user_id: str, depth: int) -> None:
        try:
            await self.ready.wait()
            woken_at(depth)   # this task's context: the turn's tool calls and their work inherit it
            await run_woken_turn(self.app, presence, session_id, user_id)
        except Exception:
            logger.exception("Woken turn of session %s failed", session_id)
            # Nobody is on the way for the input any more: without its marker, letting go below does not wake
            # the session again into the same failure. The input waits in its store for the next run.
            presence.take_pending(session_id, user_id)
        finally:
            presence.release(session_id, user_id)   # the hold notify() handed over


_waker: Optional[_Waker] = None


def install(app: Any) -> None:
    """Wake sessions inside this app from now on; their turns run once ``ready`` is called."""
    global _waker
    _waker = _Waker(app, asyncio.get_running_loop())
    set_local_waker(_waker)


def ready() -> None:
    """The app serves: the woken turns waiting for that start."""
    if _waker is not None:
        _waker.ready.set()


def _closed(presence: SessionPresence, session_id: str, user_id: str, depth: int) -> str:
    return SHUTTING_DOWN


_closed.refuses_all = True   # type: ignore[attr-defined]  # notify(): no session goes to agent-cli instead


def close() -> None:
    """The app has ended: nobody is woken in this process any more, and none in a process of its own either."""
    global _waker
    set_local_waker(_closed)
    _waker = None


async def run_woken_turn(app: Any, presence: SessionPresence, session_id: str, user_id: str) -> None:
    """The turn a wake starts, on the agent and model the session ran on -- the choice
    ``agent-cli run --woken`` makes (cli_utils/session_defaults) -- as a job of this process.

    Called with this process holding the session in ``presence`` (notify's hold, which
    the caller lets go of). The run is attended: a question goes to whoever follows it in the chat, and
    after ``tool_approval``'s grace nobody reading it is an unattended run.
    """
    from agent_system.api.run_routes import _mirror_run_as_job
    from agent_system.api.run_start import (
        get_agent_with_overrides,
        open_session_for_run,
        refused_before_the_run,
        save_after_run,
    )
    from agent_system.api.session_writes import claim_session, let_go
    from agent_system.cli_utils.agent_runner import wake_message
    from agent_system.cli_utils.session_defaults import (
        choose_agent_name,
        choose_llm_profile,
        load_session_llm_params,
        load_session_settings,
        usable_session_defaults,
    )
    from agent_system.core.request_context import (
        register_request_user,
        release_request_user_tree,
        release_run_attended,
        set_run_attended,
    )
    from agent_system.servers.agent.result_utils import collect_final_result
    from agent_system.utils.id import short_id

    ctx = app.state.context
    config = ctx.live_config()
    if not presence.pending(session_id, user_id):
        # A run in between took the input with it (the user's, typed meanwhile): a turn
        # for nothing would cost an LLM call and say nothing.
        logger.info("Woken turn of %s: the input was taken already", session_id)
        return

    manager = app_state.session_service.session_manager
    stored_agent, stored_llm = await load_session_settings(manager, user_id, session_id)
    usable_agent, usable_llm = usable_session_defaults(stored_agent, stored_llm, config)
    agent_name = choose_agent_name(None, usable_agent, getattr(config, "default_agent", None) or ctx.agent.name)
    try:
        agent, _, _ = get_agent_with_overrides(ctx, agent_name, requester=user_id)
        llm_profile = choose_llm_profile(None, usable_llm, stored_agent, agent.name,
                                         agent.agent_config.default_llm_profile)
        llm_params = (await load_session_llm_params(manager, user_id, session_id)
                      if stored_agent == agent.name else None) or {}
        if llm_profile or llm_params:
            agent, llm_override, llm_profile_info = get_agent_with_overrides(
                ctx, agent.name, llm_profile, requester=user_id, llm_params=llm_params)
        else:
            llm_override, llm_profile_info = None, None
        llm_choice = {"profile": llm_profile, "params": llm_params}
        await open_session_for_run(agent, user_id, session_id, llm_profile, llm_choice)
    except HTTPException as refused:
        # The agent is gone or the user may not run it: the turn would be refused anyway.
        raise RuntimeError(f"agent '{agent_name}' cannot run it: {refused.detail}") from None

    request_id = short_id()
    mirror = await _mirror_run_as_job(request_id, user_id, agent.name, session_id,
                                      llm_profile or agent.agent_config.default_llm_profile, llm_params)
    held = None
    try:
        register_request_user(request_id, user_id)
        set_run_attended(request_id, True)
        refusal, held = await claim_session(agent, session_id, user_id, force=False)
        if refusal:
            raise RuntimeError(refusal)
        # This turn is told that input waits: what rang for it is delivered (take_for_wake).
        presence.take_for_wake(session_id, user_id)
        refused: list = []

        def on_event(event: dict) -> Any:
            if refused_before_the_run(event):
                refused.append(event)
            return mirror.put(event)

        logger.info("Woke %s in this process: request %s on agent %s", session_id, request_id, agent.name)
        mirror.job.result = await collect_final_result(
            agent, wake_message(), request_id=request_id, session_id=session_id,
            llm_override=llm_override, llm_profile_info_override=llm_profile_info, on_event=on_event)
        if not refused:
            await save_after_run(agent, user_id, session_id, llm_profile, False, llm_choice)
    finally:
        mirror.close()
        let_go(agent, held, user_id)
        release_run_attended(request_id)
        release_request_user_tree(request_id)
