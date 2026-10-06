"""A run's wait put to the person who watches its caller (core.run_questions).

A machine that waits for an event while its caller blocks -- a machine agent with ``on_wait: block``, or one
called as another agent's tool -- has its wait put to the person watching that caller, in the form every
client draws: the events the run takes now as choices, the data to send along as text. The answer sends the
event at once (``WaitBroker.take``), so a refusal of the run -- data of the wrong type, the run gone -- is
said to whoever answered, and they can answer again. An event sent elsewhere meanwhile -- the panel, a callback
URL, a tool -- moves the run on, and the question ends. Nobody watches (agent-run, a writer job, a schedule):
nothing is asked, and the run waits as before.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from typing import Any, Callable, Dict, Optional, Sequence, Tuple

from agent_system.core.run_questions import (CANCELLED, GONE, NOT_WAITING, TIMEOUT, AnswerRejected, Question,
                                             QuestionBroker, is_read, put_to_person, status_line)
from agent_system.tools.status import StatusScope, status_bus

logger = logging.getLogger(__name__)

#: The key of the status line's meta that carries the question.
META_KEY = "stategraph"
#: How long a question waits for its answer; the run waits on after it, nobody asks again.
ASK_TIMEOUT = 24 * 3600.0
REASK_SECONDS = 20.0
GONE_AFTER_SECONDS = 15.0
#: What a question ends with when nobody answered it here: the run moved on, or its caller stopped waiting.
MOVED = "moved"
STOPPED = "stopped"


@dataclass(frozen=True)
class Offer:
    """One choice: an event the run takes now, in the frame that takes it."""

    value: str
    event: str
    frame: Optional[str]
    label: str
    description: str
    data: Any


@dataclass
class WaitAnswer:
    event: str
    data: Any
    answered_by: Optional[str] = None


@dataclass
class WaitQuestion(Question):
    """Which event a waiting run gets."""

    machine: str = ""
    run_id: str = ""
    states: Tuple[Tuple[str, str], ...] = ()  # (state, its description)
    offers: Tuple[Offer, ...] = ()
    #: Why the last answer did not move the run (a guard discarded its event): the run waits here again.
    notes: Tuple[str, ...] = ()
    #: The waits asked about (waits_of): an answer holds only while the run still stands in them.
    waits: frozenset = frozenset()

    def form(self) -> Dict[str, Any]:
        where = ", ".join(repr(state) for state, _ in self.states) or "a wait state"
        lines = [*self.notes, *(f"{state}: {description}" for state, description in self.states if description)]
        for offer in self.offers:
            line = f"{offer.label}: {offer.description}" if offer.description else offer.label
            if offer.data:
                line += f" -- data: {json.dumps(offer.data, ensure_ascii=False)[:300]}"
            if offer.description or offer.data:
                lines.append(line)
        takes_data = any(offer.data for offer in self.offers)
        return {"prompt": f"{self.machine} waits for an event in {where}",
                "detail": "\n".join(lines) or None, "warning": None,
                "choices": [{"value": offer.value, "label": offer.label} for offer in self.offers],
                "multi_select": False,
                "text": {"label": "Data to send with it (JSON or text)", "alone": False} if takes_data else None}


class WaitBroker(QuestionBroker):
    """The wait questions of one StateGraphServer. ``send(run_id, event, data, frame, user_id)`` is the service's
    send_event: its answer, or a ServiceError. ``live_row(run_id)`` is the run's row as it stands now while this
    process runs it ({status, view}), else None: only such a run can take an answer here."""

    def __init__(self, send: Callable[..., Dict[str, Any]], live_row: Callable[[str], Optional[Dict[str, Any]]]):
        super().__init__()
        self._send = send
        self.live_row = live_row

    def take(self, question_id: str, choices: Sequence[str], text: str,
             answered_by: Optional[str] = None) -> Question:
        question = self.get(question_id)
        if not isinstance(question, WaitQuestion):
            raise AnswerRejected(404, NOT_WAITING)
        offers = {offer.value: offer for offer in question.offers}
        if len(choices) != 1 or choices[0] not in offers:
            raise AnswerRejected(422, f"pick one event: {', '.join(offer.label for offer in question.offers)}")
        offer = offers[choices[0]]
        # The run moves on before its caller looks again (up to a second): an event for a wait its frame left would
        # be queued, and taken by the next wait that accepts it without anybody being asked. Another frame moving
        # on leaves this one's wait as it was.
        step = dict(question.waits).get(offer.frame)
        if step is None or (offer.frame, step) not in waits_of(self.live_row(question.run_id)):
            raise AnswerRejected(409, f"{question.machine} no longer waits there: this question is out of date")
        data = data_of(text)
        try:
            answer = self._send(question.run_id, offer.event, data, offer.frame, question.owner)
        except Exception as exc:  # ServiceError: the run ended, is gone, or another process holds it now
            raise AnswerRejected(int(getattr(exc, "status", 409)), str(getattr(exc, "message", exc))) from None
        if not answer.get("accepted"):
            raise AnswerRejected(422 if answer.get("data_refused") else 409,
                                 str(answer.get("reason") or "the run did not take it"))
        return self.resolve(question_id, WaitAnswer(offer.event, data, answered_by))


def data_of(text: str) -> Any:
    """What a written answer sends along: JSON when it reads as JSON, else the text; nothing when empty."""
    text = (text or "").strip()
    if not text:
        return None
    try:
        return json.loads(text)
    except ValueError:
        return text


class WaitAsker:
    """The questions of one caller that waits for a run: one at a time, for the wait the run stands in now.

    ``look`` is called whenever the caller looks at the run's row (about once a second); ``close`` when it stops
    waiting. A wait is asked again only after nobody read it (the person left and came back), never after an
    answer or a timeout.
    """

    def __init__(self, broker: WaitBroker, *, request_id: str, session_id: str, owner: Optional[str],
                 agent_name: str, machine: str, token: Any, answer_url: str, store: Any,
                 stopped: Callable[[], bool]):
        self.broker, self.request_id, self.session_id = broker, request_id, session_id
        self.owner, self.agent_name, self.machine = owner, agent_name, machine
        self.token, self.answer_url, self.store, self.stopped = token, answer_url, store, stopped
        self._since: Optional[int] = None  # the run's journal when the last question was put
        self._task: Optional[asyncio.Task] = None
        self._question: Optional[WaitQuestion] = None
        self._asked: frozenset = frozenset()
        self._settled: set[frozenset] = set()

    async def look(self, row: Optional[Dict[str, Any]]) -> None:
        waits = waiting_in(row)
        if self._task is not None:
            if waits == self._asked and not self._task.done():
                return
            await self._finish(MOVED)
        # only what an answer can reach: a run this process runs (not one another process holds, or one whose
        # owner died and whose lease has not run out yet), for a request that is not being cancelled
        if (waits and waits not in self._settled and not self.stopped()
                and waiting_in(self.broker.live_row(row["id"])) == waits
                and is_read(self.request_id, GONE_AFTER_SECONDS)):
            try:
                question = self._open(row)
            except Exception:  # the question must never take the caller down: the run waits on, as unasked
                logger.warning("stategraph: the wait of run %s was not put to the person", row.get("id"),
                               exc_info=True)
                self._settled.add(waits)
                return
            self._asked, self._question = waits, question
            self._task = asyncio.ensure_future(self._put(question, waits))

    async def close(self) -> None:
        """The caller stopped waiting: a question still open ends."""
        if self._task is not None:
            await self._finish(STOPPED)

    async def _finish(self, why: str) -> None:
        task, question, self._task, self._question = self._task, self._question, None, None
        if question is not None and not task.done():
            try:
                self.broker.resolve(question.id, why)
            except AnswerRejected:  # answered a moment ago: that answer is the outcome
                pass
        await asyncio.gather(task, return_exceptions=True)

    def _open(self, row: Dict[str, Any]) -> WaitQuestion:
        waiting = [frame for frame in (row.get("view") or {}).get("frames") or [] if frame.get("accepts")]
        events, states = declared(row, [(frame.get("machine"), frame.get("state")) for frame in waiting])
        takers: Dict[str, list] = {}
        for frame in waiting:
            for name in frame["accepts"]:
                takers.setdefault(name, []).append((frame.get("prefix", ""), frame.get("machine")))
        offers = []
        for name in sorted(takers):
            several = len(takers[name]) > 1
            # always to the frame that waits: a parent's state that invokes it may take the same trigger, and
            # the run would refuse an event two frames take
            for frame, machine in takers[name]:
                spec = event_spec(events, machine, name)
                offers.append(Offer(value=f"{name}@{frame}" if several else name, event=name, frame=frame,
                                    label=f"{name} in {frame!r}" if several else name,
                                    description=str(spec.get("description") or ""), data=spec.get("data")))
        notes = discarded(self.store, row["id"], self._since)
        self._since = last_seq(self.store, row["id"])
        return self.broker.open_question(
            WaitQuestion, owner=self.owner, session_id=self.session_id, request_id=self.request_id,
            agent_name=self.agent_name, timeout=ASK_TIMEOUT, machine=self.machine, run_id=row["id"],
            states=tuple(states), offers=tuple(offers), notes=tuple(notes), waits=waits_of(row))

    async def _put(self, question: WaitQuestion, waits: frozenset) -> None:
        events = ", ".join(offer.label for offer in question.offers)
        line = status_line(f"{question.form()['prompt']}: {events}")
        # A row of its own under the caller's: a line of the caller's id would overwrite the caller's row.
        scope = StatusScope(status_bus, self.agent_name, request_id=f"{self.request_id}_wait_{question.id}",
                            start_msg=line)
        try:
            async with scope:
                outcome = await put_to_person(
                    self.broker, question, scope, meta_key=META_KEY, answer_url=self.answer_url, line=line,
                    token=self.token, reask_seconds=REASK_SECONDS, gone_after_seconds=GONE_AFTER_SECONDS,
                    cut_off=f"no longer asked: the request stopped while {self.machine} waited")
                await self._settle(scope, outcome, waits)
        except asyncio.CancelledError:
            raise
        except Exception:  # the question must never take the caller down: the run waits on, as unasked
            logger.warning("stategraph: the wait of run %s was not put to the person", question.run_id, exc_info=True)
        finally:
            self.broker.close(question)

    async def _settle(self, scope: StatusScope, outcome: Any, waits: frozenset) -> None:
        if isinstance(outcome, WaitAnswer):
            self._settled.add(waits)
            await scope.end(status_line(f"{outcome.event} sent by {outcome.answered_by or 'the user'}"))
        elif outcome == MOVED:
            await scope.end(status_line(f"{self.machine} moved on without an answer here"))
        elif outcome == STOPPED:
            await scope.end(status_line(f"no longer asked: the request stopped waiting for {self.machine}"))
        elif outcome == GONE:  # asked again once somebody reads the run
            await scope.end(status_line(f"not answered, nobody reads the run any more; {self.machine} waits on"))
        elif outcome == CANCELLED:
            await scope.error(status_line("not answered, the request was cancelled"))
        else:
            if outcome != TIMEOUT:  # pragma: no cover - wait_for_answer returns nothing else
                logger.error("stategraph: unexpected outcome %r of a wait question", outcome)
            self._settled.add(waits)
            await scope.end(status_line(f"no answer within {ASK_TIMEOUT:g} s; {self.machine} waits on"))


def waits_of(row: Optional[Dict[str, Any]]) -> frozenset[tuple[str, int]]:
    """The waits a run stands in (frame, step): an answer answers these; a new one is another question."""
    return frozenset((frame.get("prefix", ""), int(frame.get("step") or 0))
                     for frame in ((row or {}).get("view") or {}).get("frames") or [] if frame.get("accepts"))


def waiting_in(row: Optional[Dict[str, Any]]) -> frozenset[tuple[str, int]]:
    """The waits to ask about: of a run that waits, or that runs on in another frame meanwhile -- none while it
    is paused or after it ended."""
    return waits_of(row) if row and row.get("status") in ("waiting", "running") else frozenset()


def event_spec(events: dict[str, dict[str, Any]], machine: Any, name: str) -> dict[str, Any]:
    """An event as the machine of the frame that takes it declares it (the data it checks there); else as the
    first machine of the run that does."""
    own = (events.get(machine) or {}).get(name)
    if own is not None:
        return own
    return next((declares[name] for declares in events.values() if name in declares), {})


def last_seq(store: Any, run_id: str) -> int:
    """The run's last journal row."""
    rows = store.tail(run_id, 1)
    return int(rows[-1]["seq"]) if rows else 0


def discarded(store: Any, run_id: str, since: Optional[int]) -> list[str]:
    """Why a reply did not move the run: the events a dispatch discarded after ``since``, with its guards."""
    if since is None:
        return []
    lines = []
    for row in store.page(run_id, after=since, kinds=["trace"]):
        data = row.get("data") or {}
        if row.get("status") != "event_discarded":
            continue
        guards = "; ".join(f"{guard.get('guard')} -> {guard['error'] if 'error' in guard else guard.get('result')}"
                           for guard in data.get("guards") or [])
        lines.append(f"Not taken: {data.get('event')!r} in {row.get('state')!r} -- no transition took it"
                     + (f" (guards: {guards})" if guards else "") + ".")
    return lines


def declared(row: Dict[str, Any], waits: list[tuple[Any, Any]]) -> tuple[dict[str, dict[str, Any]],
                                                                         list[tuple[str, str]]]:
    """The events each machine of the run declares ({machine id: {name: {description, data}}}; see event_spec),
    and the waiting states with their descriptions -- from the run's own definition."""
    from .model.loader import load_snapshot

    events: dict[str, dict[str, Any]] = {}
    specs: dict[str, Any] = {}
    try:
        tree = load_snapshot(row.get("definition") or {}, execute_python=False)  # descriptions only
        for loaded in tree.files.values():
            spec = loaded.spec
            if spec is None:
                continue
            specs[spec.id] = spec
            events[spec.id] = {name: {"description": event.description, "data": event.data}
                               for name, event in spec.events.items()}
    except Exception:  # a definition that no longer loads: the question names the events without their text
        logger.debug("definition of run %s not read", row.get("id"), exc_info=True)

    def described(states: dict[str, Any], name: str) -> Optional[str]:
        for state_name, state in (states or {}).items():
            if state_name == name:
                return state.description or ""
            found = described(state.states, name)
            if found is not None:
                return found
        return None

    states = [(str(state), described(specs[machine].states, state) or "" if machine in specs else "")
              for machine, state in waits if state]
    return events, states
