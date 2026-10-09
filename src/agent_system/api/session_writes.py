"""How a request of the API takes a stored session, and writes it beside the runs of this process.

A run, an append and /undo's cut all write a session's conversation, and the
last save wins. What keeps them apart is here, shared by the run routes
(run_routes, event_routes), the appends (run_control_routes) and the chat
commands (chat_routes):

- session presence (core/session_presence.py), across processes:
  ``claim_session``, ``hold_fresh_session``, ``let_go``;
- the agent's session lock, inside this process, for a write no run makes:
  ``beside_the_runs``, ``held_for_a_write``;
- the writes themselves: ``append_and_persist``,
  ``drop_last_exchange_and_persist`` (with the file rewind);
- what a request reads before it claims the session: ``session_record``,
  ``session_agent_name``, ``session_held``, and the agent whose tracker holds it
  (``settling_agent``, ``resolve_agent_for_request``).

They were closures in ``build_app`` and captured nothing of it but the entry
agent, which ``append_and_persist`` takes as an argument.
"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import Any, Optional

from fastapi import HTTPException, Request

from agent_system import app_state
from agent_system.api.app_context import chat_agent, registry_of
from agent_system.core.session_presence import SessionBusy, presence_for
from agent_system.services.background_job_manager import BackgroundJobManager, get_background_job_manager
from agent_system.tools.base import ToolServerRegistry
from agent_system.utils.id import short_id


async def claim_session(target_agent: Any, sid: Optional[str], user_id: str,
                        force: bool) -> tuple[Optional[str], Optional[str]]:
    """Take a stored session for a run of this process, and bring the copy
    in memory up to date. Returns (refusal, held): the refusal goes to the
    client, ``held`` names what let_go has to release afterwards.

    Session presence (core/session_presence.py) refuses a session another
    process runs -- both would write the conversation and the last save
    would win; ``force`` runs it anyway, for the lock of a process that
    hangs. The session is loaded before this (ownership, metadata), so
    whatever another process wrote in between is read again here.
    """
    held = None
    if not sid:
        return None, None
    # A session deleted in this process takes no run: nothing writes it again, so what the run answers would
    # be lost without a word (force does not change that).
    if app_state.session_service and app_state.session_service.session_manager and app_state.session_service.session_manager.is_deleted(sid):
        return f"Session {sid} has been deleted", None
    presence = presence_for(getattr(target_agent, "system_config", None))
    if presence is not None:
        try:
            if presence.hold(sid, user_id, target_agent.name):
                held = sid
        except SessionBusy as busy:
            if not force:
                return f"{busy}. Send force=true if its lock is a leftover.", None
            logging.getLogger(__name__).warning("%s; running it anyway (force=true)", busy)
    try:
        await _bring_the_copy_up_to_date(target_agent, user_id, sid)
    except BaseException:
        # The hold belongs to this function until it hands it back, and
        # nothing else would let it go: in this process it would outlive the
        # request and refuse every later run of that session.
        let_go(target_agent, held, user_id)
        raise
    return None, held


async def _bring_the_copy_up_to_date(target_agent: Any, user_id: str, sid: str) -> None:
    """Re-read a session another process continued while this one had it
    loaded but not yet held.

    SessionManager says whether the file moved since this process last wrote
    it or read it into a tracker -- not since the web UI last showed it, a
    load that puts nothing in memory. Where it has no stamp -- it is bounded with its cache -- the longer
    conversation wins: re-reading unasked undoes a run of this process whose
    save is still to come, and that run's answer is nowhere else.
    """
    if not app_state.session_service:
        return
    tracker = getattr(target_agent, "_session_tracker", None)
    if tracker is not None and tracker.check_session_locked(sid)[0]:
        # A run of this agent has it (its session lock): the copy in memory IS
        # that run's, and the run asking here is refused at that lock anyway
        # -- read back, the running turn lost what it had not saved yet, and
        # its metadata named the asker (as open_for_run leaves it, in_use).
        return
    manager = app_state.session_service.session_manager
    changed = manager.changed_on_disk(user_id, sid)
    if changed is None:
        tracker = getattr(target_agent, "_session_tracker", None)
        in_memory = len(tracker.get_session_messages(sid) or []) if tracker else 0
        try:
            stored = await manager.load_session(user_id, sid)
        except Exception:
            # No readable session there; a permission error comes back out
            # of load_and_restore_session below, where it belongs.
            stored = {}
        changed = len(stored.get("messages") or []) >= in_memory
    if changed:
        await app_state.session_service.load_and_restore_session(target_agent, user_id, sid)


def hold_fresh_session(target_agent: Any, sid: str, user_id: str) -> Optional[str]:
    """Hold a session the running request just created: nothing to re-read
    (its conversation lives in this process) and no one to refuse."""
    presence = presence_for(getattr(target_agent, "system_config", None))
    if presence is None:
        return None
    try:
        return sid if presence.hold(sid, user_id, target_agent.name) else None
    except SessionBusy as busy:
        logging.getLogger(__name__).warning("%s; this run keeps it unheld", busy)
        return None


def let_go(target_agent: Any, sid: Optional[str], user_id: str) -> None:
    """Let go of a held session; input that came in for it wakes it."""
    if not sid:
        return
    presence = presence_for(getattr(target_agent, "system_config", None))
    if presence is not None:
        presence.release(sid, user_id)


@asynccontextmanager
async def beside_the_runs(target_agent: Any, sid: str):
    """The agent's session lock for a write no run makes -- an append, /undo's cut -- from before it reads the
    session until its save is done. Yields None while it holds it, else the request id of the run that has the
    session ("" when it cannot be named): the lock refuses at once while one owns it.

    Beside the runs, such a write raced what a run of this process does with the session after its own end:
    openai_api's AgentTurn puts back a turn its client never got and drops the conversation from the tracker
    -- over the write, or between the write and its save, which then found nothing to write -- and a run that
    took the session meanwhile had it read back from under it. Session presence does not keep them apart
    (holds nest inside a process), and it may be off.

    Taken as a writer: a run, a turn's put back or another write that asks
    for the lock meanwhile waits for it instead of being refused.
    """
    tracker = target_agent._session_tracker
    writer = f"write_{short_id()}"
    if not await tracker.acquire_session_lock(sid, writer, timeout=5.0, writer=True):
        yield tracker.check_session_locked(sid)[1] or ""
        return
    try:
        yield None
    finally:
        await tracker.release_session_lock(sid, writer)


def settling_agent(request: Request, sid: str) -> Any:
    """The agent with a turn settling the session (openai_api's AgentTurn watches for appends while it does),
    or None. It comes before the record, which names the agent of the last SAVED run: a turn whose run saved
    nothing -- it failed on its way in -- puts back its own copy over whatever another agent's tracker took."""
    registry = registry_of(request)
    try:
        names = list(registry.list()) if registry is not None else []
    except Exception as e:  # noqa: BLE001 - no registry to ask, the record decides
        logging.getLogger(__name__).debug("No agents to ask about %s: %s", sid, e)
        names = []
    for name in names:
        candidate = chat_agent(request, name)
        if candidate is not None and candidate._session_tracker.watches_appends(sid):
            return candidate
    return None


async def session_record(sid: str, user_id: str) -> dict:
    """A stored session as it lies on disk, or {} when there is none.

    One read for everything a handler wants off it -- its agent, the LLM
    profile it runs on, its messages. Two helpers doing their own
    load_session read the same file twice for one command.
    """
    if not app_state.session_service or not app_state.session_service.session_manager:
        return {}
    try:
        record = await app_state.session_service.session_manager.load_session(user_id, sid)
    except Exception as e:
        logging.getLogger(__name__).debug("No record for %s: %s", sid, e)
        return {}
    return record or {}


async def session_agent_name(sid: str, user_id: str) -> Optional[str]:
    """The agent a stored session ran with, or None while it has none.

    Read off the record, which is where a session's agent lives
    (cli_utils/session_defaults.py says the same for the terminal) -- and
    without taking it as seen (SessionManager.peek_session): the callers
    claim the session next, and the claim re-reads the copy in memory only
    when the file holds what this process has not seen. Loaded here, what
    another process wrote meanwhile counted as seen: /undo cut the stale
    copy's last exchange, an append was written onto it, and both saved it
    over the other process's turn.
    """
    if not app_state.session_service or not app_state.session_service.session_manager:
        return None
    try:
        record = await app_state.session_service.session_manager.peek_session(user_id, sid)
    except Exception as e:  # noqa: BLE001 - a record that does not read names no agent, as session_record
        logging.getLogger(__name__).debug("No record for %s: %s", sid, e)
        return None
    return record.get("agent_name") or None


async def session_held(sid: str, user_id: str, system_config: Any) -> Optional[str]:
    """What holds *sid* ("running", ...), or None while nothing does.

    Session presence sees every process's holds -- a run, and an agent-cli
    chat that has the session open, which it cannot tell apart. Switched
    off, only this process's own runs are known (its background jobs).
    """
    presence = presence_for(system_config)
    if presence is not None:
        state = presence.get(sid, user_id)
        return state["status"] if state and state["status"] != "idle" else None
    running = await get_background_job_manager().active_sessions()
    return "running" if sid in running else None


async def resolve_agent_for_request(
    request_id: str,
    job_manager: BackgroundJobManager,
    registry: Optional[ToolServerRegistry],
    default_agent: Any,
    agent_name: Optional[str] = None,
) -> Any:
    """Resolve the agent instance that owns an active/recent request.

    Runs started with an agent_name execute on that registered agent; their
    per-request state (mid-run append queue, request→session mapping) lives on
    that instance, not on the global default agent. Falls back to the default
    agent when the job is unknown, names the default placeholder, or its agent
    cannot be resolved. ``agent_name`` names the agent for a run that has no job
    (a /run with files, a sub-agent's session), when the caller knows it.
    """
    try:
        job = await job_manager.get_job(request_id)
    except Exception:
        job = None
    agent_name = getattr(job, "agent_name", None) or agent_name
    if registry is not None and agent_name and agent_name not in ("default", default_agent.name):
        try:
            candidate = registry.get(agent_name)
        except Exception:
            logging.getLogger(__name__).warning(
                "Could not resolve agent '%s' for request %s, using default agent",
                agent_name, request_id,
            )
            return default_agent
        from agent_system.servers.agent.server import Agent as _Agent
        if isinstance(candidate, _Agent):
            return candidate
    return default_agent


async def append_and_persist(owner_agent: Any, sid: str, content: str, user_id: str,
                             force: bool = False, *, entry_agent: Any) -> bool:
    """Append a user message to a session no request of this process runs, and save it.

    The session is held for the append (session presence,
    core/session_presence.py), and its copy in memory is re-read when
    another process wrote the file -- a run woken by a direct message
    continues the session from disk, while re-reading unasked would undo
    what a run of this process has not saved yet -- or when there is none:
    a session this process saved and let go of. From reading it to its
    save, the append holds the agent's session lock (beside_the_runs).

    A session a run of THIS process has is not written beside the run: the
    message goes to the run, which reads it at its next step -- or, when the
    run takes no more (it is finishing), it is refused. Written into the
    session, it answered "appended" and was gone at the run's next save, which
    writes the run's own list; the claim below does not stop it, since holds
    nest inside a process.
    """
    job_manager = get_background_job_manager()
    running = (await job_manager.active_sessions()).get(sid)
    # Another append or /undo holding the session is no run: waited for below (beside_the_runs).
    if running and running.get("request_id") and not owner_agent._session_tracker.held_by_a_writer(sid):
        run_id = running["request_id"]
        # `entry_agent`, not owner_agent: a job on "default" runs on the app's default agent,
        # and owner_agent is the agent of the request the caller named -- its last run.
        run_agent = await resolve_agent_for_request(run_id, job_manager, app_state.app_registry, entry_agent,
                                                    agent_name=running.get("agent_name"))
        if await run_agent.append_user_message(run_id, content):
            return True
        raise HTTPException(
            status_code=409,
            detail=f"Session {sid} is finishing a run -- what it saves would drop the message. "
                   f"Try again once it is done.")

    refusal, held = await claim_session(owner_agent, sid, user_id, force)
    if refusal:
        raise HTTPException(status_code=409, detail=refusal)
    try:
        async with beside_the_runs(owner_agent, sid) as running:
            if running is not None:
                # A run of this agent took the session since it was asked above: the message is its.
                if running and await owner_agent.append_user_message(running, content):
                    return True
                raise HTTPException(
                    status_code=409,
                    detail=f"Session {sid} is running -- what it saves would drop the message. "
                           f"Try again once it is done.")
            tracker = owner_agent._session_tracker
            if not tracker.has_session(sid) and app_state.session_service:
                # Not in memory, and the file unmoved since this process wrote it, so the claim read nothing: a
                # session this process saved and let go of (a settled openai_api turn does). Appended to
                # nothing, the conversation on screen was "not found".
                await app_state.session_service.load_and_restore_session(owner_agent, user_id, sid)
            if not await owner_agent.append_to_session(sid, content):
                return False
            appended = tracker.get_session_messages(sid)[-1]
            if app_state.session_service:
                metadata = tracker.get_session_metadata(sid) or {}
                saved = await app_state.session_service.save_session(
                    owner_agent,
                    user_id,
                    sid,
                    metadata.get("agent_name", owner_agent.name),
                    metadata.get("llm_profile", owner_agent.agent_config.default_llm_profile),
                    was_new_session=False
                )
                if not saved:
                    # Answered "appended", the message was not on disk -- and left in memory, the next run's
                    # save wrote it after all, beside the copy a client that heard the failure sent again.
                    tracker.set_session_messages(
                        sid, [message for message in tracker.get_session_messages(sid) if message is not appended])
                    raise HTTPException(
                        status_code=500, detail=f"Session {sid} could not be saved; the message was not appended.")
                logging.getLogger(__name__).debug("Session %s persisted to disk after append", sid)
            return True
    finally:
        let_go(owner_agent, held, user_id)


@asynccontextmanager
async def held_for_a_write(owner_agent: Any, sid: str, user_id: str, force: bool, why: str):
    """A session held for a write no run makes -- /undo's cut, a rewind of
    the files its turns changed. Yields the conversation as the agent holds
    it; ``why`` finishes the refusal ("what it is writing would ...").

    A session that is RUNNING is refused before anything is touched.
    claim_session alone does not do it: holds nest inside a process, so a
    run of THIS process lets the claim through -- and then writes its whole
    message list back when it finishes (putting a dropped exchange straight
    back while the browser shows it gone), or writes files while they are
    put back.
    """
    held = await session_held(sid, user_id, getattr(owner_agent, "system_config", None))
    if held and not force:
        raise HTTPException(
            status_code=409,
            detail=f"Session {sid} is {held} -- {why}. Try again once it is done.")

    refusal, held = await claim_session(owner_agent, sid, user_id, force)
    if refusal:
        raise HTTPException(status_code=409, detail=refusal)
    try:
        async with beside_the_runs(owner_agent, sid) as running:
            if running is not None:
                # A run of this agent has it (session presence off, or it took the session since the check
                # above).
                raise HTTPException(
                    status_code=409,
                    detail=f"Session {sid} is running -- {why}. Try again once it is done.")
            tracker = owner_agent._session_tracker
            messages = list(tracker.get_session_messages(sid) or [])
            if not messages and app_state.session_service:
                # The copy in memory can be empty although the record is not:
                # claim_session re-reads only when the FILE moved, and a
                # session this process wrote and no longer holds looks
                # unchanged to it. Cutting that would answer "nothing to take
                # back" about a conversation that is plainly on screen.
                await app_state.session_service.load_and_restore_session(owner_agent, user_id, sid)
                messages = list(tracker.get_session_messages(sid) or [])
            yield messages
    finally:
        let_go(owner_agent, held, user_id)


def file_rewinder_or_503() -> Any:
    from agent_system.file_rewind import file_rewinder

    rewinder = file_rewinder()
    if rewinder is None:
        raise HTTPException(
            status_code=503,
            detail="File checkpoints are off: the file_checkpoints plugin is not loaded.")
    return rewinder


def rewind_refusal(report: dict) -> None:
    """Raise what a rewind that changed nothing -- or not everything -- says."""
    from agent_system.file_rewind import PARTIAL, REFUSED, UNKNOWN_CHECKPOINT

    status = report.get("status")
    if status == UNKNOWN_CHECKPOINT:
        raise HTTPException(status_code=404, detail=report.get("text") or "No such checkpoint")
    if status == REFUSED:
        raise HTTPException(status_code=409, detail=report.get("text") or "Files not rewound")
    if status == PARTIAL:
        raise HTTPException(status_code=500, detail=report.get("text") or "Files partly rewound")


async def drop_last_exchange_and_persist(owner_agent: Any, sid: str, user_id: str,
                                         force: bool = False, files: Optional[dict] = None
                                         ) -> tuple[Any, Optional[dict]]:
    """Take the last exchange out of a session, and save what is left.
    Returns (the question dropped or None, the file rewind's report or None).

    The mirror of append_and_persist, claim and save included -- and the
    cut itself is chat_actions.split_off_last_exchange, the one the
    terminal chat uses, so both surfaces end a turn in the same place.

    ``files`` (``{"overwrite": bool}``) puts back the files the exchange
    changed FIRST, under the same hold: a rewind that is refused (files
    changed outside the agent) or goes only partly through leaves the
    exchange where it is, so the person can look and try again.
    """
    from agent_system.chat_actions import split_off_last_exchange

    rewinder = file_rewinder_or_503() if files is not None else None
    async with held_for_a_write(owner_agent, sid, user_id, force,
                                "what it is writing would put the exchange back") as messages:
        tracker = owner_agent._session_tracker
        kept, dropped = split_off_last_exchange(messages)
        if dropped is None:
            return None, None
        report = None
        if rewinder is not None:
            report = await rewinder.rewind(
                user_id=user_id, session_id=sid, messages=messages, checkpoint=None,
                registry=getattr(owner_agent, "registry", None),
                overwrite=bool(files.get("overwrite")))
            rewind_refusal(report)
        tracker.set_session_messages(sid, kept)
        if app_state.session_service:
            metadata = tracker.get_session_metadata(sid) or {}
            # The agent's own default only where the record has none, and
            # read defensively: Agent.agent_config may be None (the agent
            # guards it itself), and reaching through it eagerly turns a
            # /undo into a 500.
            saved = await app_state.session_service.save_session(
                owner_agent,
                user_id,
                sid,
                metadata.get("agent_name") or owner_agent.name,
                metadata.get("llm_profile") or getattr(
                    getattr(owner_agent, "agent_config", None),
                    "default_llm_profile", None) or "default",
                was_new_session=False,
            )
            if not saved:
                # Answered with the exchange gone while the record still has it -- and the cut left in
                # memory for the next save to write after all.
                tracker.set_session_messages(sid, messages)
                put_back = (report or {}).get("restored") or (report or {}).get("removed")
                raise HTTPException(
                    status_code=500,
                    detail=f"Session {sid} could not be saved; the exchange was not taken back"
                           + (" -- its files WERE put back." if put_back else "."))
        return dropped, report
