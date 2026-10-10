"""The agent called as a tool: the session of its own it runs on, one per caller session.

tool_session_id names that session; ToolSessionMixin readies it for a call (tool_session,
_open_tool_session): refused when the agent runs above the call already (RECURSIVE_CALL), read back
or filed with the caller's sub-agent budget, and refused when that cannot be done
(TOOL_SESSION_UNAVAILABLE). Its own module: a protocol of its own with the session manager and the
tracker, used by Agent.call and by every ``execute_task`` that runs the agent.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import TYPE_CHECKING, Any, Optional

from ..refusals import FOREIGN_SESSION, RECURSIVE_CALL, TOOL_SESSION_UNAVAILABLE

if TYPE_CHECKING:
    from ..server import Agent

logger = logging.getLogger(__name__)


#: The longest id tool_session_id gives. Every level below a caller adds the agent's name and a digest to
#: the id; uncut, a few levels of long names made file names the file system refuses (``<id>.json``, and
#: ``.subs.<id>.index.json`` with its lock and temporary files, SessionManager).
TOOL_SESSION_ID_MAX = 128


class _ToolSessionUnavailable(RuntimeError):
    """The session of an agent called as a tool could not be made ready with its caller's sub-agent budget
    (Agent._file_tool_session); answered as TOOL_SESSION_UNAVAILABLE."""


def tool_session_id(caller_session_id: str, agent_name: str) -> str:
    """The session an agent called as a tool runs on: one of its own per caller session and agent.

    It ran on its caller's session id before: it saved its own transcript into the caller's
    session file (a new one it created, with its own agent name and a title from the
    sub-task; a stored one it replaced until the caller's next save), and its tracker kept
    every caller session for the life of the process. Now it keeps its own conversation
    with that caller -- a second call in the same caller session sees the first one's turn
    -- stored under the caller's user and below the caller's session, like a sub-agent
    manager's sub-session.

    From the framework's injected ``_session_id`` and the agent's own name only, never from
    what a model passes. Readable -- the caller's id, then the agent's name -- and apart per
    pair: the name is cut to what a session id may hold (``[A-Za-z0-9_-]``,
    SessionManager._validate_session_id), and a digest of both keeps two names that cut to
    the same text apart. It starts with the caller's id, so a throwaway caller
    (EPHEMERAL_SESSION_PREFIX) gives a throwaway session.

    At most TOOL_SESSION_ID_MAX long: where the caller's id and the name would not fit, the
    caller's id is cut to its start (the throwaway prefix stays) and a longer digest of the
    whole of it keeps the id apart. Only what it is called from counts -- the same caller's
    id and name give the same id, in every process.
    """
    import hashlib
    import re

    name = re.sub(r"[^A-Za-z0-9_-]", "-", agent_name)[:40] or "agent"
    digest = hashlib.sha1(f"{caller_session_id}\0{agent_name}".encode("utf-8")).hexdigest()
    session_id = f"{caller_session_id}--{name}-{digest[:8]}"
    if len(session_id) <= TOOL_SESSION_ID_MAX:
        return session_id
    tail = f"--{name}-{digest[:16]}"
    return caller_session_id[:TOOL_SESSION_ID_MAX - len(tail)] + tail


class ToolSessionMixin:
    """The session an agent called as a tool runs on (see the module docstring).

    Relies on Agent.__init__ for ``name``, ``agent_config``, ``_session_tracker``,
    ``_session_service`` and ``_tool_session_openings``, and asks AccessMixin._foreign_session.
    """

    async def tool_session(self: Agent, params: dict[str, Any], request_id: Optional[str] = None
                           ) -> tuple[Optional[str], Optional[dict[str, str]]]:
        """(the session a tool call of this agent runs on, why it may not run -- or None).

        For every tool that runs this agent -- Agent.call, BasicAgent.execute_task, an
        ``execute_task`` of your own: never the caller's session, but one of this agent's own
        below it, from the injected ``_session_id`` (never a model's) and this agent's name
        (tool_session_id), opened for the call (_open_tool_session). *request_id*: the id the
        run goes under, when it is not the call's own (_request_for_injected_user). No
        caller session: (None, None) -- the run starts a session of its own.

        The refusal is ``{"error": <what to tell the caller>, "error_type": FOREIGN_SESSION,
        RECURSIVE_CALL or TOOL_SESSION_UNAVAILABLE}``, for the tool's answer as it is: nothing ran.
        It raises when the session cannot be read (SessionService.open_for_run) -- a failure, as
        any other of the call.
        """
        caller_session_id = params.get("_session_id")
        if not caller_session_id:
            return None, None
        session_id = tool_session_id(caller_session_id, self.name)
        if request_id is None:
            request_id = params.get("request_id") or params.get("_request_id")
        task = params.get("task") or params.get("query") or params.get("prompt") or ""
        return session_id, await self._open_tool_session(
            request_id, caller_session_id, session_id, caller_agent=params.get("_agent_name"), title=str(task))

    async def _open_tool_session(self: Agent, request_id: Optional[str], caller_session_id: str, session_id: str, *,
                                 caller_agent: Optional[str] = None, title: str = "") -> Optional[dict[str, str]]:
        """Ready the sub-session this agent runs on when called as a tool (tool_session_id); why not
        (tool_session's refusal), or None.

        Refused first when this agent runs above the call already (RECURSIVE_CALL): it would
        call itself, directly or through other agents called as tools. On its caller's session such a call
        waited at that session's lock; on a session of its own below it, every level gets a
        new one, and nothing stopped it (_runs_above).

        Its user is the call's registered owner (the tool user), as a sub-agent
        manager's sub-session is its caller's. Held in this process since an earlier
        call, it runs on what the tracker holds -- if it is that user's
        (_foreign_session). Otherwise a stored one is read back (SessionService.open_for_run,
        which refuses another user's), and a new one is filed right away (_file_tool_session):
        a sub-agent manager this agent calls in its first step files its sub-agents in that
        record, and found none, made one of its own at the top of the session list, with no
        nesting budget. Filed with the caller's budget or not at all: failing that, the call is
        refused (TOOL_SESSION_UNAVAILABLE), and a caller's session of another user refuses it
        (FOREIGN_SESSION). A throwaway one (below a throwaway caller) is never saved.

        One this process deleted (SessionManager.is_deleted: the person deleted it, DELETE
        /sessions/<id>) is forgotten and made afresh -- its id is the same for every call in the
        caller's session, and tombstoned, it was never stored again: with a caller's budget no call
        ran any more. The copy in the tracker goes with it, and the delete's tombstone, when no run
        has the session (while one does, the delete stands, and that run is refused its saves;
        _forget_deleted). One opening of a session at a time (_opening_of). Either way
        the metadata names the caller's session and agent as its parent: it leaves this
        agent's tracker when the caller's session leaves the caller's
        (SessionTracker.discard_session), and the calls below it find the chain above them
        (session_chain). Held already, it is named again -- a session read back meanwhile
        (SessionService.load_and_restore_session) got metadata without it -- and by the agent
        that calls now.
        """
        above = self._runs_above(caller_session_id)
        if above:
            logger.warning("[%s] call from session %s refused: it runs above it, in session %s",
                           self.name, caller_session_id, above)
            return {"error": f"{self.name} is running above this call already (in session {above}): an agent "
                             f"does not call itself, directly or through other agents called as tools",
                    "error_type": RECURSIVE_CALL}
        # One opening of the session at a time: two calls of the caller's session at once (parallel tool
        # calls) both find it new. The second found the first's half-opened copy in the tracker -- no parent,
        # not filed yet -- ran on it, and its save made the record without the caller's budget, before the
        # first's filing met "already exists". Waited for, it finds the session held, filed and named.
        async with self._opening_of(session_id):
            return await self._ready_tool_session(request_id, caller_session_id, session_id,
                                                  caller_agent=caller_agent, title=title)

    @contextlib.asynccontextmanager
    async def _opening_of(self: Agent, session_id: str):
        """The lock one opening of a tool session holds (_open_tool_session); kept while anybody holds or waits
        for it, so nothing grows with the sessions of the process."""
        openings = self._tool_session_openings
        entry = openings.setdefault(session_id, [asyncio.Lock(), 0])
        entry[1] += 1
        try:
            async with entry[0]:
                yield
        finally:
            entry[1] -= 1
            if not entry[1]:
                openings.pop(session_id, None)

    async def _ready_tool_session(self: Agent, request_id: Optional[str], caller_session_id: str, session_id: str, *,
                                  caller_agent: Optional[str], title: str) -> Optional[dict[str, str]]:
        """_open_tool_session, one opening of the session at a time."""
        from ....core.request_context import get_request_user
        from ....services.session_manager import SessionPermissionError
        from ....services.session_service import is_ephemeral_session

        tracker = self._session_tracker
        service = getattr(self, "_session_service", None)
        sessions = getattr(service, "session_manager", None) if service is not None else None
        if sessions is not None and sessions.is_deleted(session_id) is True:
            await self._forget_deleted(service, sessions, session_id)
        parent = {"parent_session_id": caller_session_id}
        if caller_agent:
            parent["parent_agent"] = caller_agent
        held = tracker.get_session_metadata(session_id)
        if held:
            foreign = self._foreign_session(request_id, session_id)
            if foreign:
                return {"error": foreign, "error_type": FOREIGN_SESSION}
            tracker.set_session_metadata(session_id, {**held, **parent})
            return None
        owner = (get_request_user(str(request_id), default=None) if request_id else None) or "anonymous"
        profile = self.agent_config.default_llm_profile if self.agent_config else "normal"
        if sessions is not None:
            try:
                exists = await service.open_for_run(self, owner, session_id, profile)
            except SessionPermissionError:
                return {"error": f"Session {session_id} belongs to another user", "error_type": FOREIGN_SESSION}
            if not exists and not is_ephemeral_session(session_id):
                try:
                    await self._file_tool_session(sessions, owner, caller_session_id, session_id, profile, title)
                except BaseException as exc:
                    # Opened in the tracker, it would be the next call's to run on -- unfiled, without the budget
                    # (discard_session leaves one a run has, until it lets go)
                    tracker.discard_session(session_id)
                    if isinstance(exc, SessionPermissionError):
                        return {"error": str(exc), "error_type": FOREIGN_SESSION}
                    if isinstance(exc, _ToolSessionUnavailable):
                        return {"error": str(exc), "error_type": TOOL_SESSION_UNAVAILABLE}
                    raise
        tracker.set_session_metadata(session_id, {
            "user_id": owner, "agent_name": self.name, "llm_profile": profile,
            "parent_session_id": caller_session_id, "parent_agent": caller_agent})
        return None

    async def _forget_deleted(self: Agent, service: Any, sessions: Any, session_id: str) -> None:
        """A tool session this process deleted, forgotten (_open_tool_session): the tracker's copy goes, and the
        delete's tombstone -- unless a run has it. Under the session's save lock (SessionService.save_lock), the
        one the saves no run lock covers take -- the API's save after a run, a compaction's: one that read the
        deleted history before the delete writes it now, into the tombstone, or after, into nothing."""
        from ..components.session_tracking import a_run_has

        save_lock = getattr(service, "save_lock", None)
        async with (save_lock(session_id) if save_lock is not None else contextlib.nullcontext()):
            if sessions.is_deleted(session_id) is True and not a_run_has(session_id):
                self._session_tracker.discard_session(session_id)
                sessions.lift_tombstone(session_id)

    def _runs_above(self: Agent, caller_session_id: str) -> Optional[str]:
        """The session above a tool call from *caller_session_id* -- that one or one of the sessions it runs
        below (session_chain) -- that a run of this agent has right now (SessionTracker.run_has), or None.

        Who runs, not who called: a session's metadata names the agent that called it there last
        (``parent_agent``), which need not run now -- nor is a caller that injects no ``_agent_name`` named at
        all. The run's lock says who runs, and it is what refused such a call when it ran on its caller's
        session. Not any holder of the lock: an append or an opening that writes such a session is no run of
        this agent, and the call is none to itself."""
        from ..components.session_tracking import session_chain

        tracker = self._session_tracker
        return next((session_id for session_id in session_chain(caller_session_id)
                     if tracker.run_has(session_id)), None)

    async def _file_tool_session(self: Agent, sessions: Any, owner: str, caller_session_id: str, session_id: str,
                                 profile: str, title: str) -> None:
        """The record of a new tool session, before its run: below the caller's session (hidden from the
        session list, never woken -- a sub-agent's session), and at the caller's place in a sub-agent
        tree. Its nesting budget is the caller's own, not one level less: the agent ran on the caller's
        session before, where a sub-agent manager it called counted from the caller -- so a strict manager
        above keeps bounding the whole subtree across the hop (SubAgentManager._create_sub_session).
        ``depth`` is one more, for where it is shown.

        Written once, with the budget (create_session). Without it -- the caller has none (a new session,
        one at the top) -- this is best effort: the run's first save files it below the caller as well.
        With one it is not: a record made later (by that save, or by a sub-agent manager that finds no
        parent) has no budget, and a manager below counted from its own maximum. So when the caller's
        budget cannot be read, or the record cannot be written with it, this raises
        (_ToolSessionUnavailable), and the call does not run (TOOL_SESSION_UNAVAILABLE)."""
        from ....services.session_manager import SessionNotFoundError, SessionPermissionError

        try:
            caller = await sessions.load_session(owner, caller_session_id)
        except SessionNotFoundError:  # no stored caller (a new one): no tree above to count from
            caller = {}
        except SessionPermissionError as exc:  # another user's: _open_tool_session refuses the call as such
            raise SessionPermissionError(f"Session {caller_session_id} belongs to another user") from exc
        except Exception as exc:
            raise _ToolSessionUnavailable(f"the sub-agent budget of session {caller_session_id} could not be "
                                          f"read, and {self.name} does not run without it: {exc}") from exc
        depth, budget = caller.get("depth"), caller.get("depth_budget")
        place: dict[str, int] = {}
        if isinstance(depth, int) or isinstance(budget, int):
            place["depth"] = (depth if isinstance(depth, int) else 1) + 1
            if isinstance(budget, int):
                place["depth_budget"] = budget
        try:
            await sessions.create_session(
                user_id=owner, session_id=session_id, title=(title or self.name)[:50], agent_name=self.name,
                llm_profile=profile, parent_session_id=caller_session_id, **place)
            return
        except ValueError as exc:
            if "already exists" in str(exc):
                # Made meanwhile -- by a run of another process, say. Without the budget it is not this
                # call's to run on either.
                await self._budget_on_record(sessions, owner, session_id, place)
                return
            failure: Exception = exc
        except Exception as exc:  # noqa: BLE001 - see docstring
            failure = exc
        if "depth_budget" in place:
            raise _ToolSessionUnavailable(f"the session of {self.name} below {caller_session_id} could not be "
                                          f"stored with its caller's sub-agent budget, and it does not run "
                                          f"without it: {failure}") from failure
        logger.warning("[%s] tool session %s could not be filed: %s", self.name, session_id, failure)

    async def _budget_on_record(self: Agent, sessions: Any, owner: str, session_id: str, place: dict[str, int]) -> None:
        """A tool session's record found made meanwhile (_file_tool_session): with the caller's budget on it.
        Written when it lacks it -- only the two keys (SessionManager.set_session_place), in turn with the
        session's saves (SessionService.save_lock): a whole record written back took with it what a save wrote
        since it was read. When it cannot be read or written, the call does not run (_ToolSessionUnavailable), as
        when the filing failed. Another user's record refuses it (SessionPermissionError)."""
        from ....services.session_manager import SessionPermissionError

        if "depth_budget" not in place:
            return
        service = getattr(self, "_session_service", None)
        save_lock = getattr(service, "save_lock", None) if service is not None else None
        async with (save_lock(session_id) if save_lock is not None else contextlib.nullcontext()):
            try:
                record = await sessions.load_session(owner, session_id)
            except SessionPermissionError as exc:
                raise SessionPermissionError(f"Session {session_id} belongs to another user") from exc
            except Exception as exc:
                raise _ToolSessionUnavailable(f"the session of {self.name} ({session_id}) could not be read for "
                                              f"its caller's sub-agent budget, and it does not run without it: "
                                              f"{exc}") from exc
            if all(record.get(key) == value for key, value in place.items()):
                return
            try:
                await sessions.set_session_place(owner, session_id, **place)
            except Exception as exc:
                raise _ToolSessionUnavailable(f"the session of {self.name} ({session_id}) could not be stored with "
                                              f"its caller's sub-agent budget, and it does not run without it: "
                                              f"{exc}") from exc
