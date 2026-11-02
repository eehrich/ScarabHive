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
from collections.abc import AsyncIterator
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request

from agent_system.api.run_start import sse_response
from agent_system.plugins.agent_tasks import cancel_run, user_of
from agent_system.run_events import HELD, run_event_bus, run_event_store

router = APIRouter(prefix="/api/agent-runs", tags=["agent-runs"])

#: A comment line on a quiet stream this often, in seconds (tests set it shorter).
KEEPALIVE_S = 15.0
#: Events replayed to a client that reconnects, at most; it reads the rest per run (``/events``).
REPLAY_MAX = 1000


def _line(event: dict[str, Any]) -> str:
    """One event as SSE. A heartbeat is not kept (no ``cursor``) and carries no ``id``: the browser's Last-Event-ID
    stays at the last kept event, the one a reconnect reads on from."""
    shown = {key: value for key, value in event.items() if key not in ("cursor", "who")}
    id_line = f"id: {event['event_id']}\n" if event.get("cursor") is not None else ""
    return f"{id_line}data: {json.dumps(shown, ensure_ascii=False)}\n\n"


@router.get("")
async def agent_runs(request: Request, limit: int = Query(default=20, ge=1, le=200)) -> dict[str, Any]:
    """The asking person's runs, those not ended first, then the newest."""
    who = await user_of(request)
    return {"runs": await asyncio.to_thread(run_event_store().runs, who, limit)}


@router.get("/stream")
async def agent_runs_stream(request: Request, after: str = Query(default="", max_length=64)):
    """Everything the asking person's runs say from now on (or after ``Last-Event-ID`` / ``after``)."""
    who = await user_of(request)
    store, bus = run_event_store(), run_event_bus()
    last = request.headers.get("last-event-id") or after
    # subscribed before reading the store: an event written in between comes twice, never not at all
    queue = bus.subscribe()
    try:
        cursor = await asyncio.to_thread(store.cursor_of, last) if last else None
        missed = await asyncio.to_thread(store.since, cursor, who, REPLAY_MAX) if cursor is not None else []
        snapshot = None if cursor is not None else await asyncio.to_thread(store.runs, who)
        seen = missed[-1]["cursor"] if missed else (cursor if cursor is not None else
                                                    await asyncio.to_thread(store.last_cursor))
    except BaseException:
        bus.unsubscribe(queue)
        raise

    async def stream() -> AsyncIterator[str]:
        nonlocal seen
        try:
            yield "retry: 3000\n\n"
            if snapshot is not None:
                yield f"data: {json.dumps({'type': 'runs.snapshot', 'runs': snapshot}, ensure_ascii=False)}\n\n"
            for event in missed:
                yield _line(event)
            while True:
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=KEEPALIVE_S)
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"
                    continue
                if event.get("who") != who:
                    continue
                if event.get("cursor") is not None:   # a kept event; a heartbeat (none) is only passed on
                    if event["cursor"] <= seen:
                        continue
                    seen = event["cursor"]
                yield _line(event)
        finally:
            bus.unsubscribe(queue)

    return sse_response(stream(), media_type="text/event-stream",
                        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@router.get("/{run_id}/events")
async def agent_run_events(run_id: str, request: Request,
                           after_seq: int = Query(default=0, ge=0)) -> dict[str, Any]:
    """The events of one of the asking person's runs after ``after_seq``, in order."""
    who = await user_of(request)
    store = run_event_store()
    run = await asyncio.to_thread(store.run, run_id)
    if run is None or run["who"] != who:
        raise HTTPException(status_code=404, detail=f"No run {run_id}")
    return {"run": run, "events": await asyncio.to_thread(store.events, run_id, after_seq)}


async def _own_run(run_id: str, who: str) -> dict[str, Any]:
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
    if run["state"] not in HELD or not await cancel_run(run_id, who):
        raise HTTPException(status_code=409, detail="This run is not at work on this server")
    return {"status": "cancelling", "run_id": run_id}
