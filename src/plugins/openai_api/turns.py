"""One turn of an agent on a session -- what a Responses or Chat Completions call runs.

The agent is the registered instance the web UI runs too, on a session of the
calling user: a stored one (the Responses API continues it by
``previous_response_id``) or a throwaway one (Chat Completions, ``store:
false``).

A stored conversation is held (session presence, core/session_presence/) from
before it is opened until its turn is settled, as /run and /events hold theirs:
a run of it in another process -- an agent-cli run woken by a sub-agent -- would
write it too, and the last save would win. One that another run has in hand, in
another process or in this one, is refused (ConversationBusy). In this process
the agent's session lock says so, and the turn takes it at its opening, by its
run's request id: the run takes it again, and a web chat starting meanwhile is
refused, not this turn's run. A continued one
that does not open as stored -- deleted, archived, unreadable -- is refused
before its run (ConversationGone): the run would start from an empty history.

A turn is kept only once it is delivered: ``close(deliver=...)`` saves it and
then records its response id. Any other end -- the run failed or was stopped,
the client left before the answer went out, the save or the record failed --
puts the conversation back to what it held before (its messages and its
variables), because the agent saves every run, and a client's retry from its
last response must not build on a turn it never got. A new conversation is
deleted then; a continued one is only ever restored -- under the agent's session
lock, and only over what this turn's run left: a request of this process that
has run on the conversation since (the web chat, a /run) keeps its turn, and this
one stays with it. What the user appended meanwhile -- beside the run (the append
endpoint) or into it -- stays either way: it is the user's, not the turn's. The
sessions of the agents the run called as tools, below the conversation, are put
back with it where only the turn's runs wrote them: they ran on the
conversation's own session before, and a client's retry must not find the
exchange it never got in a helper's memory either.
Settled, a stored conversation leaves the agent's tracker --
not one another request has opened since, which runs on what the tracker holds --
and the next turn reads it from disk.

A throwaway session leaves nothing: it is dropped from the agent's tracker, the
sub-agents its run started are stopped (nobody can read what they answer), and
the record the sub-agent manager makes for a parent it does not find on disk
(``Coordinator Session``) is deleted -- as the web UI deletes a session, the
sub-agents' own sessions stay, hidden below it.

The run is a task of its own that feeds a queue. A client that goes away (or a
timeout) cancels the run's request -- its token -- before the task: a cancel
that lands in one of the agent's tool calls becomes a "cancelled" tool result
and the agent would run on; the token is what it stops at. The turn is settled
(saved, put back, dropped) only once the run has stopped; until then its
conversation counts as busy (``on_settled``).
"""

from __future__ import annotations

import asyncio
import copy
import logging
from contextlib import aclosing
from datetime import datetime, timezone
from typing import Any, AsyncIterator, Callable, Optional

import anyio

logger = logging.getLogger(__name__)

#: How long a stopped run gets before the turn gives up waiting and settles once the run has ended.
STOP_GRACE = 12.0

_DONE = object()


class TurnError(Exception):
    """The agent run failed or was cancelled; ``message`` says what the run said, ``error_type`` what its error
    event said (a refusal before the run: Agent.run_events, REFUSED_BEFORE_THE_RUN; a format verdict:
    structured_output_invalid, ...)."""

    def __init__(self, message: str, error_type: Optional[str] = None):
        super().__init__(message)
        self.error_type = error_type


class ConversationBusy(Exception):
    """Another run has the stored conversation in hand -- in another process, or a run of this one."""


class ConversationGone(Exception):
    """The conversation a turn continues does not open as stored: deleted, archived, or unreadable."""


class AgentTurn:
    def __init__(self, agent: Any, service: Any, *, user: str, session_id: str, request_id: str,
                 persist: bool, continues: bool = False, title: Optional[str] = None,
                 response_format: Any = None):
        self.agent = agent
        self.service = service
        self.user = user
        self.session_id = session_id
        self.request_id = request_id
        self.persist = persist
        self.continues = continues  # a stored conversation this turn continues (previous_response_id)
        self.title = title  # a new stored conversation's title: the user's own text, not the instructions
        # The client's structured output (a llm.structured_output.ResponseFormat), for this turn's final answer
        self.response_format = response_format
        self.profile = getattr(getattr(agent, "agent_config", None), "default_llm_profile", None) or "normal"
        self.usage = {"prompt_tokens": 0, "completion_tokens": 0}
        self.completed = False
        self._opened = False
        self._existed = False
        self._before: list[Any] = []
        self._vars_before: dict[str, Any] = {}  # the session's variables (template vars) before the turn
        # The stored sessions of the agents called as tools below the conversation, at the opening:
        # {id: (the runs that wrote it, its record -- None when it could not be read)} (_put_back_below)
        self._below_before: dict[str, tuple[list[str], Optional[dict[str, Any]]]] = {}
        self._opened_at: Optional[datetime] = None  # when open() began: what was made below since is the turn's
        self._work: Optional["asyncio.Future[Any]"] = None
        self._refused = False  # the run was refused before it started: it wrote and set nothing
        self._ended = False  # the run said "end": it is over, only its generator still closes
        self._stopped = False
        self._closed = False
        self._held: Optional[Any] = None  # the presence store that holds the conversation for this turn
        self._locked = False  # this turn took the agent's session lock at open() (a stored conversation)
        self._opening: Optional[int] = None  # the session's opening mark (SessionTracker.mark_opened) after ours
        self._as_opened: list[Any] = []  # the conversation as this turn opened it (with the client's earlier turns)
        self._after: list[Any] = []  # the conversation as this turn's run left it
        self._appended: list[Any] | None = None  # what the user appended since the opening (watch_appends)
        # The sessions anything was appended to since the opening, in any agent's tracker (watch_appended_sessions):
        # one below the conversation among them is left as it is (_put_back_below)
        self._appended_below: Optional[set[str]] = None
        self._settled_callbacks: list[Callable[[], None]] = []
        self._settled = asyncio.Event()
        self._settling: Optional["asyncio.Future[Any]"] = None

    def on_settled(self, callback: Callable[[], None]) -> None:
        """``callback()`` once the turn is settled -- after its run has stopped, however long that took."""
        self._settled_callbacks.append(callback)

    async def settled(self) -> None:
        """Until the turn is settled."""
        await self._settled.wait()

    async def open(self, history: list[Any]) -> None:
        """Ready the session: a stored one is held and restored, ``history`` (earlier turns the client sent) is
        added. Raises ConversationBusy, ConversationGone or SessionPermissionError before anything ran."""
        from agent_system.core.request_context import register_request_user

        register_request_user(self.request_id, self.user)
        self._opened_at = datetime.now(timezone.utc)
        tracker = self.agent._session_tracker
        if self.persist:
            # From the very start: an append to a session below between _keep_below's read of it and a later
            # watch was put back away with the rest (_settle lets go of the watch, also after a failed open)
            from agent_system.servers.agent.components.session_tracking import watch_appended_sessions

            self._appended_below = watch_appended_sessions()
            # The agent's session lock, from here -- not only from when the run takes it (by the same request id,
            # so the run's taking it is a re-entry, and its finalize lets go): a run of this process (the web chat)
            # that started in between made this turn's run fail at the lock, a 500 for a 409.
            if not await tracker.acquire_session_lock(self.session_id, self.request_id, timeout=5.0):
                # A run of this process has it -- known here with session presence off too. Opened under it, the
                # session would be read back into the tracker from under that run.
                raise ConversationBusy("the conversation is running right now")
            self._locked = True
            self._hold()  # before the session is read: what another process wrote until now is read with it
        existed = await self.service.open_for_run(self.agent, self.user, self.session_id, self.profile,
                                                  holding=self.request_id if self._locked else None)
        self._opening = tracker.last_opened(self.session_id)  # another mark later: opened by another since
        if self.continues and not existed:
            # open_for_run started it afresh in the tracker; nothing runs on it, so nothing of it stays
            tracker.discard_session(self.session_id)
            raise ConversationGone(self.session_id)
        self._existed = existed
        self._opened = True
        if self.persist and not existed and self.title:
            # Its first save names it -- by the first user message, and in front of that stand the client's
            # instructions ("Instructions from the client application: ...") when it sent any.
            tracker.carry_title(self.session_id, self.title)
        self._before = list(tracker.get_session_messages(self.session_id))
        self._vars_before = copy.deepcopy(dict(tracker.get_session_template_vars(self.session_id) or {}))
        if self.persist and existed:
            await self._keep_below()
        if history:
            tracker.set_session_messages(self.session_id, [*self._before, *history])
        self._as_opened = self._after = list(tracker.get_session_messages(self.session_id))
        if self.persist:
            self._appended = tracker.watch_appends(self.session_id)

    def _hold(self) -> None:
        """Hold the stored conversation for the turn (session presence), as /run and /events hold theirs."""
        from agent_system.core.session_presence import SessionBusy, presence_for

        presence = presence_for(getattr(self.agent, "system_config", None))
        if presence is None:
            return
        if presence.held_here(self.session_id, self.user):
            # A run of this process has it (the web chat, a sub-agent's caller): holds nest within a process,
            # so taking it would keep that run out of nothing -- and restoring the session under it would undo
            # what it has not saved yet.
            raise ConversationBusy("the conversation is running right now")
        try:
            if presence.hold(self.session_id, self.user, getattr(self.agent, "name", "")):
                self._held = presence
        except SessionBusy as busy:
            if self.continues:
                raise ConversationBusy("the conversation is running in another process") from None
            # A new conversation's id is fresh; like the app with a session its run just made, it goes unheld.
            logger.warning("openai_api: %s; this turn keeps it unheld", busy)

    def _let_go(self) -> None:
        presence, self._held = self._held, None
        if presence is None:
            return
        try:
            presence.release(self.session_id, self.user)
        except Exception:
            logger.warning("openai_api: letting go of session %s failed", self.session_id, exc_info=True)

    async def events(self, message: Any) -> AsyncIterator[dict[str, Any]]:
        """The run's events up to its end. Raises TurnError when it failed or was cancelled from outside, and
        ConversationBusy -- before the run -- when the conversation was read back under this turn since it opened
        it (an opener that does not ask whether a run has it): the client's earlier turns would be gone."""
        tracker = self.agent._session_tracker
        if self.persist and tracker.get_session_messages(self.session_id) != self._as_opened:
            raise ConversationBusy("the conversation was opened by another request meanwhile")
        queue: asyncio.Queue[Any] = asyncio.Queue()

        # Handed on only when asked for: a turn without one runs the agent exactly as before.
        structured = {"response_format": self.response_format} if self.response_format is not None else {}

        async def pump() -> None:
            try:
                async with aclosing(self.agent.run_events(task=message, request_id=self.request_id,
                                                          session_id=self.session_id, **structured)) as run:
                    async for event in run:
                        queue.put_nowait(event)
                        if event.get("type") == "end":
                            break
            finally:
                # What the run left, as soon as it has: a put back only restores what nobody wrote over since.
                self._after = list(tracker.get_session_messages(self.session_id))
                queue.put_nowait(_DONE)

        work = self._work = asyncio.ensure_future(pump())
        work.add_done_callback(lambda done: done.cancelled() or done.exception())  # retrieved, whoever waits
        failure: Optional[str] = None
        failure_type: Optional[str] = None
        try:
            while True:
                event = await queue.get()
                if event is _DONE:
                    break
                kind = event.get("type")
                if kind == "thinking_complete":
                    usage = event.get("usage") or {}
                    self.usage["prompt_tokens"] += int(usage.get("prompt_tokens") or 0)
                    self.usage["completion_tokens"] += int(usage.get("completion_tokens") or 0)
                elif kind == "error":
                    from agent_system.servers.agent.server import refused_before_the_run

                    failure = str(event.get("message") or "the agent run failed")
                    failure_type = event.get("error_type") or None
                    self._refused = self._refused or refused_before_the_run(event)
                elif kind == "cancelled":
                    failure = f"cancelled: {event.get('reason') or event.get('message') or 'from outside'}"
                elif kind == "end":
                    self._ended = True
                    self.completed = failure is None
                yield event
            await work  # its own exception, if the run raised
        except asyncio.CancelledError:
            await self._stop(work)
            raise
        except GeneratorExit:  # the consumer stopped reading (a client that went away)
            await self._stop(work)
            raise
        if failure is not None:
            raise TurnError(failure, failure_type)
        if not self.completed:
            raise TurnError("the agent run ended without an answer")

    async def _stop(self, work: "asyncio.Future[Any]") -> None:
        """Stop the run: its token first, then its task, and wait for it (STOP_GRACE at most).

        Not twice, and not a run that is over: its token went with it, and cancel_request would only log
        that it found none -- a cancel after "end" lands while the run's generator closes."""
        if self._stopped or self._ended or work.done():
            return
        self._stopped = True
        try:
            from agent_system.core.cancellation import get_cancellation_manager

            get_cancellation_manager().cancel_request(self.request_id)
        except Exception:
            logger.debug("openai_api: cancel_request(%s) failed", self.request_id, exc_info=True)
        work.cancel()
        # Shielded: Starlette stops a stream whose client left by cancelling its whole scope, and there every
        # await is cancelled again at once -- the run would get no time at all.
        with anyio.CancelScope(shield=True):
            try:
                await asyncio.wait({work}, timeout=STOP_GRACE)
            except asyncio.CancelledError:
                pass  # a plain task cancel again; stopping already
        if not work.done():
            logger.warning("openai_api: agent run %s did not stop within %.0fs; its turn is settled once it has",
                           self.request_id, STOP_GRACE)

    def answer(self) -> str:
        """The turn's answer: the agent's final message as it wrote it (markdown, not the web UI's HTML)."""
        for message in reversed(self.agent._session_tracker.get_session_messages(self.session_id)):
            role = message.get("role") if isinstance(message, dict) else getattr(message, "role", None)
            if role == "assistant":
                content = message.get("content") if isinstance(message, dict) else getattr(message, "content", "")
                return content if isinstance(content, str) else ""
        return ""

    async def close(self, deliver: Optional[Callable[[], None]] = None) -> None:
        """Settle the turn and release its request.

        Only delivery keeps a stored turn: with ``deliver``, a completed one is saved and then ``deliver()``
        records its response id -- a save that fails or a ``deliver`` that raises puts the conversation back,
        and the error is raised. Every other close puts it back: no ``deliver`` (the client left, the answer
        never went out), or a run that did not complete. A throwaway session is dropped either way.

        Once only (the caller closes before it answers, the scope again on its way out). A run still going
        is stopped; one still stopping is settled -- put back -- once it has stopped.
        """
        if self._closed:
            return
        self._closed = True
        work = self._work
        if work is not None:
            # The consumer may have left without a word to events(): a stream closed while it waited at a
            # yield, not inside events(). Stopping is still the turn's to do.
            await self._stop(work)
            if not work.done():
                self._settling = asyncio.ensure_future(self._settle_once_stopped(work))
                return
        with anyio.CancelScope(shield=True):  # a stream's cancelled scope must not cut the save short
            await self._settle(deliver)

    async def _settle_once_stopped(self, work: "asyncio.Future[Any]") -> None:
        await asyncio.wait({work})
        try:
            await self._settle()
        except Exception:
            logger.warning("openai_api: settling the turn of %s failed", self.request_id, exc_info=True)

    async def _settle(self, deliver: Optional[Callable[[], None]] = None) -> None:
        from agent_system.core.request_context import release_request_user_tree

        try:
            if not self._opened:
                return
            if not self.persist:
                await self._drop()
                return
            try:
                if self.completed and deliver is not None:
                    await self._keep(deliver)
                else:
                    await self._put_back()
            finally:
                self._forget()
        finally:
            if self._appended is not None:
                self.agent._session_tracker.unwatch_appends(self.session_id, self._appended)
            if self._appended_below is not None:
                from agent_system.servers.agent.components.session_tracking import unwatch_appended_sessions

                unwatch_appended_sessions(self._appended_below)
            await self._unlock()
            self._let_go()  # after the put back: no other process writes the conversation before it
            release_request_user_tree(self.request_id)
            for callback in self._settled_callbacks:
                callback()
            self._settled.set()

    async def _keep(self, deliver: Callable[[], None]) -> None:
        """A delivered turn stays: saved, then recorded. Put back when either fails.

        Saved under the agent's session lock, as the put back is: an append saving meanwhile is waited for. Not
        over a run of this process that took the conversation after this turn's run (the web chat): the tracker
        holds that run's live state, a tool call without its result. This turn's run saved the turn before it let
        go of the lock, and the run that has the conversation now runs on it -- kept as it is."""
        tracker = self.agent._session_tracker
        try:
            # As a writer: its run is over, and an append that meets this save waits a moment for it
            if await tracker.acquire_session_lock(self.session_id, self.request_id, timeout=5.0, writer=True):
                try:
                    saved = await self.service.save_session(self.agent, self.user, self.session_id,
                                                            self.agent.name, self.profile,
                                                            was_new_session=not self._existed)
                finally:
                    await tracker.release_session_lock(self.session_id, self.request_id)
                if not saved:
                    raise TurnError("the conversation could not be saved")
            else:
                # A run has it -- or a write did not end within the wait; either writes the turn with it
                logger.info("openai_api: session %s is held by %s; the turn of %s is kept in it",
                            self.session_id, tracker.check_session_locked(self.session_id)[1], self.request_id)
            deliver()
        except BaseException:
            await self._put_back()
            raise

    async def _put_back(self) -> None:
        """The conversation as it was before the turn -- on disk too, where a run may have saved it: its
        messages and its variables (what the turn's tools set, a workflow phase say). A new one goes; a continued
        one is only restored (open() refuses one that does not open as stored).

        Under the agent's session lock, and only over what this turn's run left. The run lets go of the lock after
        its last save, and a run of this process may take the conversation from there before this turn is settled
        -- the web chat, a /run: running, it holds the lock; done, it has written its turn after this one. Put back
        then, its messages and variables were replaced from under it, or its finished turn went with this one; the
        turn stays instead, as part of that conversation. One that opened the conversation and has not run yet runs
        on what the put back leaves.

        What the user appended meanwhile stays in the conversation put back: the append endpoint writes a session no
        run has -- also while this turn's run closes, after its lock and before what it left is taken -- and a
        message handed to the run is in what the run left. Compared without them, and put back after the rest."""
        tracker = self.agent._session_tracker
        # As a writer, as _keep: an append that meets the put back waits for it, and lands after it
        if not await tracker.acquire_session_lock(self.session_id, self.request_id, timeout=5.0, writer=True):
            logger.info("openai_api: session %s is run by another request; the turn of %s stays in it",
                        self.session_id, self.request_id)
            return
        try:
            messages = tracker.get_session_messages(self.session_id)
            if self._others(messages) != self._others(self._after):
                logger.info("openai_api: session %s was written by another request since; the turn of %s stays in "
                            "it", self.session_id, self.request_id)
                return
            await self._restore([message for message in messages if self._is_appended(message)])
        finally:
            await tracker.release_session_lock(self.session_id, self.request_id)

    @property
    def _ran(self) -> bool:
        """Whether a run started: one refused before it started (the role gate, another user's session, another
        run's lock) ran nothing."""
        return self._work is not None and not self._refused

    def _is_appended(self, message: Any) -> bool:
        return any(message is appended for appended in self._appended or ())

    def _others(self, messages: list[Any]) -> list[Any]:
        """The messages that are not the user's appended ones."""
        return [message for message in messages if not self._is_appended(message)]

    async def _restore(self, appended: list[Any]) -> None:
        """The conversation as it was before the turn, with ``appended`` (the user's, since) after it."""
        from agent_system.chat_commands import store_vars

        tracker = self.agent._session_tracker
        if not self._existed and not appended:  # it leaves the tracker with every settled conversation (_forget)
            if self._ran:  # a run may have written it
                await self._delete_record()
                await self._put_back_below()
            return
        tracker.set_session_messages(self.session_id, [*self._before, *appended])
        if not self._ran:
            # No run -- or one refused before it started -- wrote nothing and set no variable (and nothing was
            # appended: the turn held the lock). Saved anyway, the conversation came back with its updated_at
            # moved and its variables written as they were read.
            return
        try:
            if not await self.service.save_session(self.agent, self.user, self.session_id, self.agent.name,
                                                   self.profile, was_new_session=False):
                logger.warning("openai_api: session %s could not be put back after a failed turn", self.session_id)
            # A save only merges variables into the stored ones, so what the turn added would stay: replaced.
            await store_vars(tracker, self.service.session_manager, self.user, self.session_id, self._vars_before)
        except Exception:
            logger.warning("openai_api: session %s could not be put back after a failed turn", self.session_id,
                           exc_info=True)
        await self._put_back_below()

    async def _stored_below(self) -> dict[str, tuple[str, dict[str, Any]]]:
        """The stored sessions of the agents called as tools below the conversation, through every level: {id: (the
        session it is below, its index row)}. Such a session is below its caller's and has the id tool_session_id
        gives for the two -- a sub-agent manager's sub-sessions are not among them, nor what is below one of those."""
        from agent_system.servers.agent.server import tool_session_id

        manager = self.service.session_manager
        found: dict[str, tuple[str, dict[str, Any]]] = {}
        parents = [self.session_id]
        while parents:
            parent = parents.pop()
            try:
                rows = await manager.list_child_sessions(self.user, parent, annotate_children=False)
            except Exception:
                logger.warning("openai_api: the sessions below %s could not be listed", parent, exc_info=True)
                continue
            for row in rows:
                session_id, name = row.get("session_id"), row.get("agent_name")
                if (isinstance(session_id, str) and isinstance(name, str) and session_id not in found
                        and session_id == tool_session_id(parent, name)):
                    found[session_id] = (parent, row)
                    parents.append(session_id)
        return found

    @staticmethod
    def _runs(row: dict[str, Any]) -> list[str]:
        """The runs that wrote a sub-session, by their request ids (its index row, SessionIndex.row)."""
        return [run for run in row.get("runs") or [] if isinstance(run, str)]

    def _ours(self, run: str) -> bool:
        """Whether a run is this turn's: its own, or one started under it (a tool call's request id is its
        caller's with ``_<n>`` after it)."""
        return run == self.request_id or run.startswith(f"{self.request_id}_")

    def _made_by_the_turn(self, row: dict[str, Any]) -> bool:
        """Whether a session's index row says it was created since the turn opened -- asked by its creation, not
        by whether open() found it: a listing that failed there does not make a session stored before the turn's."""
        try:
            created = datetime.fromisoformat(str(row.get("created_at")))
        except ValueError:
            return False
        if created.tzinfo is None:
            created = created.replace(tzinfo=timezone.utc)
        return self._opened_at is not None and created >= self._opened_at

    async def _keep_below(self) -> None:
        """What _put_back_below puts back: each stored session below the conversation (_stored_below) as the turn
        opens -- its record, and the runs that wrote it."""
        manager = self.service.session_manager
        for session_id, (_, row) in (await self._stored_below()).items():
            try:
                record: Optional[dict[str, Any]] = copy.deepcopy(await manager.load_session(self.user, session_id))
            except Exception:
                logger.warning("openai_api: session %s could not be read; a put back leaves it as it is",
                               session_id, exc_info=True)
                record = None
            self._below_before[session_id] = (self._runs(row), record)

    async def _put_back_below(self) -> None:
        """The sessions of the agents the run called as tools, below the conversation, as they were before the
        turn, where only this turn wrote them -- told by the runs that wrote each (its index row), as the
        conversation's own put back yields to another request's writes: one the turn made (created since it
        opened, by its runs alone) goes, one only its runs wrote since the opening gets its record back as the
        turn opened it (_keep_below; a save, as the conversation's own put back is one). A person's run on such a
        session meanwhile, or another agent's call on the conversation, is another request's: that session stays
        as it is. So does one a message was appended to meanwhile -- beside its runs or handed to one; the index
        row names no run for it, and it is the person's, as what they append to the conversation stays. So does
        every session a run of this process has right now (SessionTracker.run_has), and all below it -- that run
        writes on; a write that has one (an append, an /undo saving) is no run, and is waited for. And one that
        could not be read at the opening, or that is gone. Each is asked and put back under its agent's session
        lock, as a writer: an append that comes meanwhile waits for it, and a run that takes it first keeps it.

        Told apart are runs, of any process: each names itself in the session (its opening message, the index
        row's ``runs``). A write that names no run is not, and is put back with the turn's own: an /undo, a
        rename, a variables write, and an append made in another process (the watch sees this one only). A run
        of another process still going on the session writes it again after the put back.

        One it deleted or put back leaves its agent's tracker, so the next call reads it from disk -- or, deleted,
        makes it afresh: the delete's tombstone is lifted there (Agent._open_tool_session), as the id comes back
        with every call in the conversation (tool_session_id)."""
        from agent_system.servers.agent.components.session_tracking import a_run_has, held_below

        stored = await self._stored_below()
        held = held_below(self.session_id, self.agent._session_tracker.agent_name)
        parents = {session_id: parent for session_id, (parent, _) in stored.items()}
        for tracker, session_id in held:
            parents.setdefault(session_id, (tracker.get_session_metadata(session_id) or {}).get("parent_session_id"))

        def below_a_run(session_id: Optional[str]) -> bool:
            seen: set[str] = set()
            while session_id and session_id != self.session_id and session_id not in seen:
                if a_run_has(session_id):
                    return True
                seen.add(session_id)
                session_id = parents.get(session_id)
            return False

        appended = self._appended_below or set()
        put_back: set[str] = set()
        for session_id, (_, row) in stored.items():
            if below_a_run(session_id):
                continue
            # As a writer, as the conversation's own put back: an append to it waits, and lands after -- asked
            # whether one came meanwhile, and put back, in one piece. A run that has it keeps it (refused here).
            owner = self._tracker_of(row.get("agent_name"))
            if owner is not None and not await owner.acquire_session_lock(session_id, self.request_id, timeout=5.0,
                                                                          writer=True):
                continue
            try:
                if session_id not in appended and await self._put_back_one(session_id, row):
                    put_back.add(session_id)
                    for tracker, held_id in held:
                        if held_id == session_id:
                            tracker.discard_session(session_id)
            finally:
                if owner is not None:
                    await owner.release_session_lock(session_id, self.request_id)
        for tracker, session_id in held:
            if session_id not in stored and not below_a_run(session_id):
                tracker.discard_session(session_id)

    def _tracker_of(self, agent_name: Any) -> Any:
        """The session tracker of the agent a session below runs with (its index row's agent_name), as this turn's
        agent reaches it -- its registry -- or None."""
        from agent_system.servers.agent.components.session_tracking import SessionTracker

        find = getattr(self.agent, "_get_server_from_any_registry", None)
        try:
            server = find(agent_name) if callable(find) and isinstance(agent_name, str) and agent_name else None
        except Exception:  # noqa: BLE001 - not an agent of this process: nobody here writes it
            server = None
        tracker = getattr(server, "_session_tracker", None)
        return tracker if isinstance(tracker, SessionTracker) else None

    async def _put_back_one(self, session_id: str, row: dict[str, Any]) -> bool:
        """One session below the conversation as it was before the turn, where only the turn's runs wrote it
        (_put_back_below); whether it did."""
        runs = self._runs(row)
        if session_id not in self._below_before:
            if self._made_by_the_turn(row) and all(self._ours(run) for run in runs):
                await self._delete_record(session_id)
                return True
            return False
        runs_before, record = self._below_before[session_id]
        added = [run for run in runs if run not in runs_before]
        if record is None or not added or not all(self._ours(run) for run in added):
            return False  # unreadable at the opening, no run wrote it since, or another request's did as well
        try:
            await self.service.session_manager.save_session(copy.deepcopy(record))
            return True
        except Exception:
            logger.warning("openai_api: session %s could not be put back after a failed turn", session_id,
                           exc_info=True)
            return False

    def _forget(self) -> None:
        """A stored conversation leaves the agent's tracker once its turn is settled: the next turn reads it from
        disk (open_for_run). Kept, every conversation stayed in memory for the life of the process.

        Not one another request of this process has opened since (SessionTracker.last_opened), even if its run has
        not begun: an opening while a run has the session leaves the tracker as it is (open_for_run, in use), and
        its run then runs on what the tracker holds -- dropped from under it, on nothing, and its save replaced the
        conversation. Nor one another run has right now (its lock)."""
        tracker = self.agent._session_tracker
        locked, owner = tracker.check_session_locked(self.session_id)
        if tracker.last_opened(self.session_id) != self._opening or (locked and owner != self.request_id):
            return
        tracker.discard_session(self.session_id)

    async def _unlock(self) -> None:
        """Let go of the agent's session lock this turn took at open() -- if the run has not already (its
        finalize lets go by the same request id)."""
        if not self._locked:
            return
        self._locked = False
        tracker = self.agent._session_tracker
        if tracker.check_session_locked(self.session_id) == (True, self.request_id):
            await tracker.release_session_lock(self.session_id, self.request_id)

    async def _drop(self) -> None:
        """Nothing of a throwaway session stays. The sub-agents its run started in the background are stopped:
        nobody can continue the session to read what they answer. The sessions of the agents it called as tools
        (throwaway as well, below it: SessionTracker.sessions_below) go with it -- from memory with it
        (discard_session), and a record a sub-agent manager made for one of them is deleted like its own."""
        from agent_system.servers.agent.components.session_tracking import sessions_below

        below = sessions_below(self.session_id, self.agent._session_tracker.agent_name)
        self.agent._session_tracker.discard_session(self.session_id)
        if not self._ran:
            return  # no run (or one refused before it started): nothing started, nothing written
        try:
            from agent_system.core.cancellation import get_cancellation_manager

            get_cancellation_manager().cancel_sub_requests(self.request_id)
        except Exception:
            logger.debug("openai_api: cancel_sub_requests(%s) failed", self.request_id, exc_info=True)
        for session_id in (self.session_id, *below):
            await self._delete_record(session_id)

    async def _delete_record(self, session_id: Optional[str] = None) -> None:
        """The session's record (or *session_id*'s), if a run wrote one -- asked with one stat first: a delete
        takes the session manager's global lock and reads every user's directory."""
        from agent_system.services.session_manager import SessionNotFoundError

        session_id = session_id or self.session_id
        manager = self.service.session_manager
        if not manager.belongs_to(self.user, session_id):
            return  # nothing was written -- the usual case
        try:
            await manager.delete_session(self.user, session_id, create_backup=False)
        except SessionNotFoundError:
            pass  # gone meanwhile
        except Exception:
            logger.warning("openai_api: session %s could not be deleted", session_id, exc_info=True)
