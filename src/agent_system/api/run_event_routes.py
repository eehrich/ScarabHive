"""The run events of the person asking (agent_system.run_events): the runs as they stand, one run's timeline, and a
stream of everything the runs say from now on.

``GET /api/agent-runs`` -- the runs, those not ended first: ``{"runs": [{run_id, agent, title, state, step_index,
step_total, step_label, progress, eta_s, silent, ...}]}``.
``GET /api/agent-runs/{run_id}/events?after_seq=n`` -- the run's events after ``n``: what a client missed (a gap in
``seq``) or a run's whole timeline (``n`` = 0).
``POST /api/agent-runs/{run_id}/ack`` -- the person has dealt with a run that needs them: a result checked ends it as
done, a failure or handover seen stops asking (409 when the run waits for nobody).
``POST /api/agent-runs/{run_id}/cancel`` -- stop a run at work (409 when no run of this server works there).
``GET /api/agent-runs/stream`` -- Server-Sent Events. Each event is one ``data:`` line, its SSE ``id`` the
``event_id``. A client that reconnects sends ``Last-Event-ID`` (the browser does by itself) and reads on from there;
a new client first gets ``{"type": "runs.snapshot", "runs": [...]}``. A comment line every ``KEEPALIVE_S`` keeps
proxies from closing a quiet stream.

Each person sees their own runs only -- the runs they started (``who``); while authentication is off everyone is
the one user ``local``.
"""
from __future__ import annotations

import asyncio
import json
import time
from collections.abc import AsyncIterator
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request

from agent_system import app_state
from agent_system.api.run_start import sse_response
from agent_system.core import turn_runs
from agent_system.plugins.agent_tasks import cancel_run, user_of
from agent_system.run_events import HELD, run_event_bus, run_event_store
from agent_system.run_tree import render

router = APIRouter(prefix="/api/agent-runs", tags=["agent-runs"])

#: A comment line on a quiet stream this often, in seconds (tests set it shorter).
KEEPALIVE_S = 15.0
#: Events replayed to a client that reconnects, at most; it reads the rest per run (``/events``).
REPLAY_MAX = 1000
#: The runs a page starts with: those not ended first, then the newest -- every turn and sub-agent is one, and the
#: page draws them as a tree with what just ended.
SNAPSHOT_RUNS = 60
#: How often a stream reads what OTHER processes wrote, in seconds. The bus hands out this process's events only;
#: an agent-cli's turn, a run a CLI holds, say theirs into the store.
TAIL_S = 2.0
#: How often the streams have the store end the runs whose process is gone, in seconds: an agent-cli closed mid-turn
#: says no end, and the store is swept only when a process opens it.
SWEEP_S = 30.0
#: The runs a stream remembers how far it told them, at most.
TOLD_RUNS = 2000
#: The events a run begins with: with one of them its count of events may start again (its row was pruned).
BEGINS = frozenset({"run.queued", "run.started"})
_swept = 0.0


def _sweep_due() -> bool:
    """Whether it is time for a sweep; says so once per SWEEP_S, to the stream that asks first."""
    global _swept
    if time.monotonic() - _swept < SWEEP_S:
        return False
    _swept = time.monotonic()
    return True


_STOPPING = object()


async def _next_or_stop(queue: asyncio.Queue, wait: float) -> Any:
    """The next event; None after ``wait`` seconds without one; _STOPPING once the server stops.

    This stream never ends by itself, and every open tab holds one: a server that stops waited for it until
    uvicorn's graceful timeout ran out and cancelled it (app_state.shutdown_event, set as the stop signal comes).
    """
    stopping = app_state.shutdown_event  # one set already ends the wait at once, once the queue is empty
    getter = asyncio.ensure_future(queue.get())
    waits = {getter} if stopping is None else {getter, asyncio.ensure_future(stopping.wait())}
    try:
        done, _ = await asyncio.wait(waits, timeout=wait, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for waiting in waits:
            waiting.cancel()  # a getter that has an event is done: cancelling it takes nothing from the queue
    if getter in done:
        return getter.result()
    return _STOPPING if done else None


def _line(event: dict[str, Any]) -> str:
    """One event as SSE. A heartbeat is not kept (no ``cursor``) and carries no ``id``: the browser's Last-Event-ID
    stays at the last kept event, the one a reconnect reads on from."""
    shown = {key: value for key, value in event.items() if key not in ("cursor", "who")}
    id_line = f"id: {event['event_id']}\n" if event.get("cursor") is not None else ""
    return f"{id_line}data: {json.dumps(shown, ensure_ascii=False)}\n\n"


@router.get("")
async def agent_runs(request: Request, limit: int = Query(default=SNAPSHOT_RUNS, ge=1, le=200)) -> dict[str, Any]:
    """The asking person's runs, those not ended first, then the newest."""
    who = await user_of(request)
    return {"runs": await asyncio.to_thread(run_event_store().runs, who, limit)}


@router.get("/text")
async def agent_runs_text(request: Request, width: int = Query(default=100, ge=40, le=240),
                          lang: str = Query(default="en", max_length=16)) -> dict[str, Any]:
    """The asking person's runs as a terminal reads them (``agent_system.run_tree``): what the chat's ``/runs``
    shows, in the words ``agent-cli runs`` prints. ``lang``: the language the steps are told in where an agent
    names them so, as the list above the sessions tells them."""
    who = await user_of(request)
    runs = await asyncio.to_thread(run_event_store().runs, who, SNAPSHOT_RUNS)
    return {"text": "\n".join(render(runs, width=width, lang=lang[:2].lower()))}


@router.get("/stream")
async def agent_runs_stream(request: Request, after: str = Query(default="", max_length=64)):
    """Everything the asking person's runs say from now on (or after ``Last-Event-ID`` / ``after``)."""
    who = await user_of(request)
    store, bus = run_event_store(), run_event_bus()
    last = request.headers.get("last-event-id") or after

    async def stream() -> AsyncIterator[str]:
        # Subscribed here, not in the handler: a response whose body is never
        # started (the client left first) never runs this generator's finally,
        # and its queue stayed subscribed, filling, for the life of the process.
        # And before reading the store: an event written in between comes twice, never not at all.
        queue = bus.subscribe()
        try:
            cursor = await asyncio.to_thread(store.cursor_of, last) if last else None
            missed = await asyncio.to_thread(store.since, cursor, who, REPLAY_MAX) if cursor is not None else []
            # How far the store is read, BEFORE the runs are: what is kept between the two is read on from there
            # (and is in the runs already -- told twice, never not at all).
            seen = missed[-1]["cursor"] if missed else (cursor if cursor is not None else
                                                        await asyncio.to_thread(store.last_cursor))
            snapshot = None if cursor is not None else await asyncio.to_thread(store.runs, who, SNAPSHOT_RUNS)
            yield "retry: 3000\n\n"
            if snapshot is not None:
                yield f"data: {json.dumps({'type': 'runs.snapshot', 'runs': snapshot}, ensure_ascii=False)}\n\n"
            # How far each run has been told on this stream. A step kept as its run ended is read from the store
            # after the end went out by the bus (core/turn_runs.py: the end is not held up for a step still being
            # written): sent after it, it would put the run back to work for a client that has let it go.
            told_to: dict[str, int] = {}

            def tell(kept: dict[str, Any]) -> bool:
                """Whether a kept event is news of its run on this stream; counted as told if so."""
                run_id, seq = kept["run_id"], kept.get("seq") or 0
                if kept.get("type") not in BEGINS and seq < told_to.get(run_id, 0):
                    return False
                told_to.pop(run_id, None)
                told_to[run_id] = seq       # the run told last stands last
                while len(told_to) > TOLD_RUNS:
                    del told_to[next(iter(told_to))]
                return True

            for event in missed:
                tell(event)
                yield _line(event)
            tailed = said = time.monotonic()
            beat_after = time.time()
            # The kept events the bus brought before the store was read up to them. ``seen`` is how far the store
            # has been read, and only reading it moves it: moved by an event of this process, an earlier one of
            # another process -- written, not yet read -- would never be sent.
            early: set[int] = set()
            while True:
                event = await _next_or_stop(queue, min(TAIL_S, KEEPALIVE_S))
                if event is _STOPPING:  # the browser reconnects by itself (retry), to the server that comes next
                    return
                if event is not None and event.get("who") == who:
                    cursor = event.get("cursor")
                    if cursor is None:      # a heartbeat: not kept, only passed on
                        said = time.monotonic()
                        yield _line(event)
                    elif cursor > seen:
                        early.add(cursor)
                        if tell(event):
                            said = time.monotonic()
                            yield _line(event)
                if time.monotonic() - tailed >= TAIL_S:
                    # What other processes wrote since: their events in the order they were kept, and a sign of
                    # life of each run they hold. Both read before either is sent: a client slow to take a line
                    # must not move what is asked.
                    tailed = time.monotonic()
                    if _sweep_due():
                        # ended in the store, read just below like anything another process wrote
                        await asyncio.to_thread(store.sweep)
                    written = await asyncio.to_thread(store.since, seen, who, REPLAY_MAX)
                    asked = time.time()
                    beats = await asyncio.to_thread(store.beats, who, beat_after)
                    beat_after = asked
                    told = {kept["run_id"] for kept in written}     # an event says that its run is alive
                    for kept in written:
                        seen = kept["cursor"]
                        if seen in early or not tell(kept):   # the bus was the faster one, or is past it
                            continue
                        said = time.monotonic()
                        yield _line(kept)
                    early = {cursor for cursor in early if cursor > seen}
                    for beat in beats:
                        if beat["run_id"] not in told:
                            said = time.monotonic()
                            yield _line(beat)
                if time.monotonic() - said >= KEEPALIVE_S:
                    said = time.monotonic()
                    yield ": keepalive\n\n"
        finally:
            bus.unsubscribe(queue)

    return sse_response(stream(), media_type="text/event-stream",
                        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@router.get("/{run_id}/events")
async def agent_run_events(run_id: str, request: Request,
                           after_seq: int = Query(default=0, ge=0)) -> dict[str, Any]:
    """The events of one of the asking person's runs after ``after_seq``, in order."""
    run = await _own_run(run_id, await user_of(request))
    return {"run": run, "events": await asyncio.to_thread(run_event_store().events, run_id, after_seq)}


async def _own_run(run_id: str, who: str) -> dict[str, Any]:
    """The run, if it is one of *who*'s; 404 alike for one that is not there and one that is somebody else's."""
    run = await asyncio.to_thread(run_event_store().run, run_id)
    if run is None or run["who"] != who:
        raise HTTPException(status_code=404, detail=f"No run {run_id}")
    return run


@router.post("/{run_id}/ack")
async def agent_run_ack(run_id: str, request: Request) -> dict[str, Any]:
    """The asking person has checked the run's result, or seen its failure or handover."""
    who = await user_of(request)
    await _own_run(run_id, who)
    event = await asyncio.to_thread(run_event_store().acknowledge, run_id, who)
    if event is None:
        raise HTTPException(status_code=409, detail="This run does not wait for anyone")
    run_event_bus().publish(event)
    return {"run": await asyncio.to_thread(run_event_store().run, run_id)}


@router.post("/{run_id}/cancel")
async def agent_run_cancel(run_id: str, request: Request) -> dict[str, Any]:
    """Stop one of the asking person's runs at work or waiting for an answer; its end comes as ``run.cancelled``
    on the stream."""
    who = await user_of(request)
    run = await _own_run(run_id, who)
    if run["state"] not in HELD:
        raise HTTPException(status_code=409, detail="This run has ended")
    if not await cancel_run(run_id, who):
        store = run_event_store()
        # it ended as the stop was on its way -- or is a turn writing its end, at work in the store for that moment
        if (await _own_run(run_id, who))["state"] not in HELD or turn_runs.ending(run_id):
            raise HTTPException(status_code=409, detail="This run has ended")
        if await asyncio.to_thread(store.held_here, run_id):
            # this server's, with nothing here to stop it by (work it holds without a way to cancel it): the tree
            # offers no Stop for it
            raise HTTPException(status_code=409, detail="This run takes no stop",
                                headers={"X-Run-Takes-No-Stop": "1"})
        # held by another process (an agent-cli's turn): the tree says so and offers no Stop for it
        raise HTTPException(status_code=409, detail="This run is not at work on this server",
                            headers={"X-Run-Held-Elsewhere": "1"})
    return {"status": "cancelling", "run_id": run_id}
