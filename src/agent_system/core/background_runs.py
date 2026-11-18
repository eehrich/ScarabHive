"""Work that outlives the turn that started it, kept where the chat finds it again.

A tool may start work that goes on after its call has answered -- a coding CLI that runs for an hour, a command in
the background. While the turn that started it streams, its view is the box under the call (relay_run_event); once
that turn has ended, nothing listened any more, and a viewer who came back saw nothing: not that it ran, not what it
did, no way to stop it.

A ``BackgroundRun`` keeps that work in two places the chat already reads:

- a **sub-session** of the session whose turn started it: its task first, under the run's request id, then its steps
  as an agent's are stored (an assistant message per step, its tool calls keyed by the request ids they ran under,
  their results, its answer). The session's ``/children`` lists it by that id, and the chat hangs it under the call
  that started it, as it hangs a sub-agent's run (attachStoredSubRuns). Written within ``SAVE_S`` of each tool
  result, and at the end.
- a **run** in the run events (``agent_system.run_events``): its state, the step it is on, a heartbeat while it
  works, its end -- under the sub-session's id, with the session it works below (``parent_session``). A run whose
  process died is ended by the store's sweep; ``/api/sessions/active`` reports the runs still held below the sessions
  it is asked about, and ``POST /api/agent-runs/{id}/cancel`` stops one through the ``on_cancel`` its owner gave.

The parent session lists its runs in ``metadata.background_runs`` (sub-session id -> run id, agent, task, state) --
not in ``sub_agents``: the sub-agent manager shows that list to the model before every call and counts it against
its limits. A parent not on disk yet (a new chat's first turn saves it at its end) is listed at a later save.

A view, never the work's master: whatever fails here is logged, and the work goes on.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from typing import Any, Optional

from agent_system import app_state
from agent_system.run_events import END_TRIES, INTERRUPTED, RunReporter
from agent_system.utils.id import short_id

logger = logging.getLogger(__name__)

#: The metadata key of a parent session that lists its background runs.
METADATA_KEY = "background_runs"
#: The ``source`` of their runs in the run events -- and their ``task``, a short key carried by every event (the
#: title says what the run does).
SOURCE = "background"
#: The task as the parent's listing keeps it.
TASK_SUMMARY = 200
#: A sub-session's steps are written within this many seconds of the result that brought them.
SAVE_S = 2.0

Cancel = Callable[[], Awaitable[Any]]

#: A run that ended before its parent session was on disk tries to be listed there this often, this many times
#: (a new chat's first turn saves its session at its end).
LIST_RETRY_S = 15.0
LIST_TRIES = 120

#: The background runs of this process at work, by sub-session id.
_RUNS: dict[str, "BackgroundRun"] = {}
#: Ended runs waiting for their parent session to be saved, and the task that lists them then.
_LISTING: dict["BackgroundRun", asyncio.Future] = {}


def run_who(system_config: Any, user_id: str) -> str:
    """Who a run is of in the run events: the session's user -- ``local`` while authentication is off, as
    ``/api/agent-runs`` names everyone then (plugins/agent_tasks.user_of)."""
    auth = getattr(system_config, "auth", None)
    return "local" if auth is not None and not getattr(auth, "enabled", False) else user_id


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _message(role: str, content: str, **fields: Any) -> dict[str, Any]:
    return {"role": role, "content": content, "injected_by": SOURCE, "timestamp": _now(), **fields}


def _cut_off(store: Any, row: dict[str, Any]) -> bool:
    """Whether the last the run events heard of the run is the sweep that ended it for a process that was gone."""
    last = (store.events(row["run_id"], after_seq=int(row.get("last_seq") or 1) - 1) or [{}])[-1]
    return last.get("type") == "run.failed" and (last.get("payload") or {}).get("reason") == INTERRUPTED


def _title(task: str) -> str:
    return " ".join(task.split())[:120]


async def cancel(session_id: str) -> bool:
    """Stop the background run of this process whose sub-session is ``session_id``; False when there is none."""
    run = _RUNS.get(session_id or "")
    if run is None or run._on_cancel is None:
        return False
    await run._on_cancel()
    return True


#: The states a run's entry ends in (``_list``).
ENDED = ("done", "failed", "cancelled")


def held(session_id: str) -> bool:
    """Whether this process holds the run, and so can stop it (``cancel``)."""
    return session_id in _RUNS


def keeps_sessions() -> bool:
    """Whether this process can write a run's sub-session: one that keeps no sessions (a CLI) cannot."""
    return getattr(app_state.session_service, "session_manager", None) is not None


class BackgroundRun:
    """One piece of background work: its sub-session, its run, its entry in the parent's listing.

    Without a session service (``service`` None) only the run's state is kept: a process that keeps no sessions (a
    CLI) and took the work over keeps it said at work. Its end is not this process's to write (``kept``): the chat
    reads it from the sub-session, so whoever holds the run leaves the end to a process that keeps sessions."""

    def __init__(self, service: Any, user_id: str, parent: str, session_id: str, run_id: str, agent: str, task: str,
                 reporter: RunReporter, on_cancel: Optional[Cancel]) -> None:
        self._service = service
        self.user_id, self.parent, self.session_id, self.run_id = user_id, parent, session_id, run_id
        self.agent, self.task = agent, task
        self._reporter = reporter
        self._on_cancel = on_cancel
        self._messages: list[dict[str, Any]] = [_message("user", task, request_id=run_id)]
        self._step: Optional[dict[str, Any]] = None
        self._closed = False      # ended: nothing more is kept
        self._detached = False    # let go of without an end: steps are no longer kept, an end still is (close)
        self._entry: dict[str, Any] = {}
        self._parent_listed = False
        self._dirty = False
        self._saving: Optional[asyncio.Task] = None
        self._save_failed = False
        self._again = False       # ended(): the run is said begun again right before its end

    @property
    def kept(self) -> bool:
        """Whether this process writes the run's sub-session, and so can write its end there."""
        return self._service is not None

    @classmethod
    def _of(cls, system_config: Any, *, parent: str, user_id: str, run_id: str, agent: str, task: str,
            session_id: str, on_cancel: Optional[Cancel]) -> "BackgroundRun":
        service = app_state.session_service
        reporter = RunReporter(session_id, agent, who=run_who(system_config, user_id), title=_title(task),
                               source=SOURCE, task=SOURCE, parent=parent)
        return cls(service if getattr(service, "session_manager", None) is not None else None, user_id, parent,
                   session_id, run_id, agent, task, reporter, on_cancel)

    @classmethod
    async def open(cls, *, system_config: Any, parent: str, user_id: str, run_id: str, agent: str, task: str,
                   on_cancel: Optional[Cancel] = None, session_id: str = "") -> Optional["BackgroundRun"]:
        """The run ``run_id`` (the request id its box carries, ``<call>_sub_<x>``), started by a turn of ``parent``.
        ``session_id`` names a sub-session it had before (a process that takes the work over): written anew from
        the task on. None where nothing could be opened, and for a new run in a process that keeps no sessions."""
        manager = getattr(app_state.session_service, "session_manager", None)
        if not parent or not run_id or (manager is None and not session_id):
            return None
        try:
            run = cls._of(system_config, parent=parent, user_id=user_id, run_id=run_id, agent=agent, task=task,
                          session_id=session_id or short_id(), on_cancel=on_cancel)
            if manager is not None:
                try:
                    await manager.load_session(user_id, run.session_id)
                except Exception:
                    # its parent may not be on disk yet: the sub-session lists it all the same
                    # (.subs.<parent>.index.json), the parent lists the run once it is there (_list_in_parent)
                    await manager.create_session(user_id=user_id, title=f"{agent}: {_title(task)[:80]}",
                                                 agent_name=agent, llm_profile="default", session_id=run.session_id,
                                                 parent_session_id=parent)
            await run._save()
            await run._list("running")
            await run._reporter.started()
            run._reporter.keep_alive()
            _RUNS[run.session_id] = run
            return run
        except Exception:  # noqa: BLE001 - see the module docstring
            logger.exception("background run %s: its view could not be opened", run_id)
            return None

    @classmethod
    async def ended(cls, *, system_config: Any, parent: str, user_id: str, run_id: str, agent: str, task: str,
                    session_id: str, state: str, answer: str = "", reason: str = "") -> bool:
        """End a run whose view this process does not hold -- one that ended where no watch kept it (a watch that
        had stopped, a run its reader settled): its sub-session gets its answer after what it holds, its run its
        end. False when the run events did not take the end: asked again, nothing is written twice."""
        try:
            run = cls._of(system_config, parent=parent, user_id=user_id, run_id=run_id, agent=agent, task=task,
                          session_id=session_id, on_cancel=None)
            if run._service is not None:
                try:
                    async with run._lock():
                        kept = await run._service.session_manager.load_session(user_id, session_id)
                except Exception:  # noqa: BLE001 - its sub-session is gone (deleted with its chat): the run ends
                    run._service = None
                else:
                    if ((kept.get("metadata") or {}).get("background") or {}).get("state") in ENDED:
                        # Its end is written already -- by one that died before the run events heard of it, or
                        # before it removed what asks for the end again. Nothing is written twice; the run
                        # events are told where they say something else.
                        store = run._reporter.store
                        row = await asyncio.to_thread(store.run, session_id)
                        if row is None or (row["state"] == state
                                           and not await asyncio.to_thread(_cut_off, store, row)):
                            return True
                        run._service = None
                    else:
                        run._messages = list(kept.get("messages") or run._messages)
            # Ended where this process held no view, it may have been ended for it already: the sweep of a store a
            # restart opened ends what a dead process held, as interrupted. Its own end is the one that counts.
            run._again = True
            return await run.close(state, answer, reason)
        except Exception:  # noqa: BLE001 - see the module docstring
            logger.exception("background run %s: its end was not kept", run_id)
            return False

    async def event(self, event: dict[str, Any], label: str = "") -> None:
        """One event of the run, in the vocabulary its box reads (relay_run_event): ``thinking`` opens a step,
        ``reasoning_delta`` is its thinking, ``tool_call`` and ``tool_result`` a call and its answer. ``label``:
        the step in plain words, for the run's state (the call's status line)."""
        if self._closed or self._detached:
            return
        try:
            kind = event.get("type")
            if kind == "thinking":
                self._at_step(event.get("step"))
            elif kind == "reasoning_delta":
                step = self._at_step(event.get("step"))
                step["reasoning_content"] = (step.get("reasoning_content") or "") + str(event.get("delta") or "")
            elif kind == "tool_call":
                step = self._at_step(event.get("step"))
                call_id = str(event.get("request_id") or "")
                step.setdefault("tool_calls", []).append({"id": call_id, "type": "function", "function": {
                    "name": str(event.get("action") or "tool"),
                    "arguments": json.dumps(event.get("params"), ensure_ascii=False, default=str)}})
                step.setdefault("tool_request_ids", {})[call_id] = call_id
                await self._reporter.step(label or str(event.get("action") or "tool"))
                self._save_soon()     # a call that takes long is in its sub-session while it runs
            elif kind == "tool_result":
                result = event.get("result")
                self._messages.append(_message(
                    "tool", result if isinstance(result, str) else json.dumps(result, ensure_ascii=False, default=str),
                    tool_call_id=str(event.get("request_id") or ""), name=str(event.get("action") or "tool")))
                self._save_soon()
        except Exception:  # noqa: BLE001 - see the module docstring
            logger.exception("background run %s: an event was not kept", self.run_id)

    def _at_step(self, number: Any) -> dict[str, Any]:
        """The assistant message of step ``number``, opened when the run goes on to it."""
        if self._step is None or (number is not None and self._step.get("step") != number):
            self._step = _message("assistant", "", step=number if number is not None else
                                  (self._step or {}).get("step", 0) + 1)
            self._messages.append(self._step)
        return self._step

    def _save_soon(self) -> None:
        """Written within ``SAVE_S``, not at every result: the sub-session is written whole, and a run of hundreds
        of calls would rewrite it hundreds of times."""
        self._dirty = True
        if self._saving is None or self._saving.done():
            self._saving = asyncio.ensure_future(self._save_later())

    async def _save_later(self, wait: Optional[float] = None) -> None:
        wait = SAVE_S if wait is None else wait
        while True:
            await asyncio.sleep(wait)
            if not self._dirty or self._closed:
                return
            try:
                await self._save()
            except Exception:  # noqa: BLE001 - see the module docstring
                # once: a sub-session deleted with its chat fails every save from here on
                (logger.debug if self._save_failed else logger.exception)(
                    "background run %s: its steps were not kept", self.run_id)
                self._save_failed = True
            wait = SAVE_S     # what came while it was written started no save of its own (_save_soon): it is next

    async def close(self, state: str, answer: str = "", reason: str = "") -> bool:
        """The run ended: ``done`` with its ``answer``, ``failed`` with its ``reason``, or ``cancelled`` -- after
        ``detach`` too: an end that comes there after all is the run's. Whether the run events took the end."""
        if self._closed:
            return True
        self._closed = True
        if self._saving is not None and not self._saving.done():
            self._saving.cancel()
        if _RUNS.get(self.session_id) is self:
            del _RUNS[self.session_id]
        try:
            last = self._messages[-1]
            # not twice: an end taken over from a process that died over it may have written the answer already
            if state == "done" and answer and not (last.get("role") == "assistant" and last.get("content") == answer
                                                   and not last.get("tool_calls")):
                self._messages.append(_message("assistant", answer))
            await self._save()
        except Exception:  # noqa: BLE001 - see the module docstring
            logger.exception("background run %s: its end was not kept", self.run_id)
        try:
            await self._list(state, reason)    # also where the save failed: the chat reads how it ended from here
        except Exception:  # noqa: BLE001 - see the module docstring
            logger.exception("background run %s: its end was not listed", self.run_id)
        if self._again and await self._reporter.started(tries=END_TRIES) is None:
            return False      # still ended as something else there: an end is refused on an ended run
        if state == "done":
            said = await self._reporter.done(answer[:4000])
        elif state == "cancelled":
            said = await self._reporter.cancelled()
        else:
            said = await self._reporter.failed(reason or "the run failed")
        return said is not None

    def detach(self) -> None:
        """This process lets go of the run without its end -- the plugin that watched it stopped, and another
        watch takes it over (``open`` with its ``session_id``): no more steps, heartbeats or Stop from here."""
        self._detached = True
        self._reporter.stop_keep_alive()
        if self._dirty:
            # What came last is kept now, not in up to SAVE_S: by then an end written from elsewhere (ended) would
            # be written over with the list from before it.
            if self._saving is not None and not self._saving.done():
                self._saving.cancel()
            self._saving = asyncio.ensure_future(self._save_later(0))
        if _RUNS.get(self.session_id) is self:
            del _RUNS[self.session_id]

    async def _save(self) -> None:
        """The sub-session's messages as they stand: written whole -- this run is its only writer."""
        self._dirty = False
        if self._service is None:
            return
        manager = self._service.session_manager
        async with self._lock():
            data = await manager.load_session(self.user_id, self.session_id)
            data["messages"] = [dict(message) for message in self._messages]
            await manager.save_session(data)
        if self._entry and not self._parent_listed:
            await self._list_in_parent()

    def _lock(self) -> Any:
        """The sub-session's save lock (SessionService.save_lock), or none."""
        lock = self._service.save_lock(self.session_id) if hasattr(self._service, "save_lock") else None
        return lock if lock is not None else contextlib.nullcontext()

    async def _list(self, state: str, reason: str = "") -> None:
        """The run's entry in its parent's ``background_runs``, and its state in its own metadata: what a chat
        reading either knows of the run without the run events."""
        self._entry = {"run_id": self.run_id, "agent": self.agent, "task_summary": self.task[:TASK_SUMMARY],
                       "state": state, "updated_at": _now()}
        if reason:
            self._entry["reason"] = reason[:400]
        if self._service is None:
            return
        await self._service.session_manager.update_session_metadata(
            self.user_id, self.session_id, {"background": self._entry}, touch=False)
        await self._list_in_parent()

    async def _list_in_parent(self) -> None:
        """The entry in the parent's listing -- tried again at the next save while the parent is not on disk yet."""
        if self._service is None:
            return
        try:
            await self._service.session_manager.update_session_metadata(
                self.user_id, self.parent, {METADATA_KEY: {self.session_id: self._entry}}, touch=False)
            self._parent_listed = True
        except Exception as exc:  # noqa: BLE001 - not there yet, as a new chat's first turn is not
            self._parent_listed = False
            logger.debug("background run %s: not listed in %s yet: %s", self.run_id, self.parent, exc)
            if self._closed and self not in _LISTING:
                # ended before its parent was saved: no later save of its own tries again
                _LISTING[self] = asyncio.ensure_future(self._list_when_parent_is_there())

    async def _list_when_parent_is_there(self) -> None:
        try:
            for _ in range(LIST_TRIES):
                await asyncio.sleep(LIST_RETRY_S)
                await self._list_in_parent()
                if self._parent_listed:
                    return
            logger.info("background run %s: its session %s was never saved, the run is not listed there",
                        self.run_id, self.parent)
        finally:
            _LISTING.pop(self, None)
