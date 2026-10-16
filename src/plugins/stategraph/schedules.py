"""Machines a stategraph instance starts by the clock: plugin config ``schedules`` (docs/backlog.md F9).

    stategraph:
      schedules:
        - name: nightly_digest     # a slot's run key: schedule:<name>:<slot>
          machine: digest
          every: 24h               # slots start at 00:00 UTC plus whole multiples of every ...
          offset: 3h               # ... plus offset: 03:00 UTC
          late: 1h                 # a slot first seen later than this after its start is left out (default:
                                   # every, at most 1h; at least 1m -- two looks at the clock)
          params: {topic: news}
          user: admin              # whose run it is (default: nobody's -- only admins see it)

One process starts an instance's slots: the one holding its scheduler lease in runs.db, renewed every tick -- the
API while it runs; another process takes over once that lease ran out or was given up (stop). A slot runs once --
its run key; again only after a transient failure (service.start_run), at most ATTEMPTS times and RETRY_AFTER apart.

The same process -- if it is the API (or STATEGRAPH_DRIVER=1): a short-lived agent-cli would cut what it resumes --
also resumes interrupted runs (FEAT-3, RES-2): an earlier slot's run of a schedule (one whose machine is gone is
ended), and, opted in, the runs of the machines in ``auto_resume`` or with ``resume: auto`` that hold no caller's
live objects. A resume that fails is tried again after a growing pause, never every tick.
"""

from __future__ import annotations

import asyncio
import datetime
import logging
import math
import os
import time
from dataclasses import dataclass, field
from typing import Any, Iterator, Optional

from .model.spec import check_name, parse_duration

logger = logging.getLogger(__name__)

#: How often the scheduler looks at the clock, and how long its lease holds without a look.
TICK = 30.0
LEASE = 90.0
#: The shortest period: a slot must outlast a few ticks.
MIN_EVERY = 60.0
#: The shortest late window: a slot must be seen by two ticks, or one lost tick drops it.
MIN_LATE = 2 * TICK
#: A slot's runs, the first included: after a transient failure it is tried again, RETRY_AFTER after the last ended.
ATTEMPTS = 3
RETRY_AFTER = 300.0
#: A resume by the scheduler that failed: tried again after this, doubled each time up to RESUME_PAUSE_MAX.
RESUME_PAUSE = 60.0
RESUME_PAUSE_MAX = 3600.0
#: How many interrupted runs one page of a tick reads.
RESUME_BATCH = 200
#: Set to 1 in a process that is not the API but drives resumes (the API does by itself).
DRIVER_ENV = "STATEGRAPH_DRIVER"


@dataclass(frozen=True)
class Schedule:
    name: str
    machine: str
    every: float
    offset: float = 0.0
    late: float = 3600.0
    params: dict[str, Any] = field(default_factory=dict)
    user: Optional[str] = None

    def slot(self, now: float) -> tuple[int, float]:
        """The slot ``now`` lies in: its index and when it started."""
        index = math.floor((now - self.offset) / self.every)
        return index, index * self.every + self.offset


def parse_schedules(raw: Any) -> tuple[list[Schedule], list[str]]:
    """The schedules of the plugin config, and what is wrong with the ones left out."""
    schedules: list[Schedule] = []
    problems: list[str] = []
    if raw in (None, [], {}):
        return schedules, problems
    if not isinstance(raw, list):
        return schedules, [f"schedules must be a list of {{name, machine, every, ...}}, not {type(raw).__name__}"]
    names: set[str] = set()
    for index, entry in enumerate(raw):
        where = f"schedules[{index}]"
        try:
            if not isinstance(entry, dict):
                raise ValueError("a mapping {name, machine, every, offset?, late?, params?, user?}")
            unknown = sorted(set(entry) - {"name", "machine", "every", "offset", "late", "params", "user"})
            if unknown:
                raise ValueError(f"unknown key(s) {', '.join(unknown)}")
            name = check_name(entry.get("name"), "schedule name")
            if name in names:
                raise ValueError(f"the name {name!r} is taken by another schedule")
            every = parse_duration(entry.get("every"))
            if every is None or every < MIN_EVERY:
                raise ValueError(f"every must be a duration of at least {MIN_EVERY:g}s (1h, 24h)")
            offset = parse_duration(entry.get("offset")) or 0.0
            late = parse_duration(entry.get("late"))
            if late is not None and late < MIN_LATE:
                raise ValueError(f"late must be at least {MIN_LATE:g}s: a slot is seen at most every {TICK:g}s")
            params = entry.get("params") or {}
            if not isinstance(params, dict):
                raise ValueError("params must be a mapping")
            schedule = Schedule(name=name, machine=check_name(entry.get("machine"), "machine id"), every=every,
                                offset=offset % every, late=min(every, 3600.0) if late is None else late,
                                params=params, user=str(entry["user"]) if entry.get("user") else None)
        except (ValueError, TypeError) as exc:
            problems.append(f"{where}: {exc}")
            continue
        names.add(schedule.name)
        schedules.append(schedule)
    return schedules, problems


def resume_lease(instance: str) -> str:
    """The scheduler lease under which one driving process resumes ``instance``'s interrupted runs."""
    return f"{instance}:resume"


def _drives() -> bool:
    """Whether this process resumes runs by itself: the API (its registry is up), or one told so."""
    if os.environ.get(DRIVER_ENV) == "1":
        return True
    try:
        from agent_system import app_state

        return app_state.app_registry is not None
    except Exception:  # pragma: no cover - defensive
        return False


def _interrupted(store: Any) -> Iterator[dict[str, Any]]:
    """Every interrupted run, newest first, a page at a time: a fixed first page would starve the older ones."""
    before = None
    while True:
        page = store.list_runs(status="interrupted", limit=RESUME_BATCH, before=before)
        yield from page
        if len(page) < RESUME_BATCH:
            return
        before = page[-1]["id"]


def _asks_auto(store: Any, run_id: str) -> bool:
    """Whether the run's snapshot asks for ``resume: auto``."""
    from .model.loader import load_snapshot

    full = store.get_run(run_id) or {}
    spec = load_snapshot(full.get("definition") or {}, execute_python=False).root_file.spec
    return getattr(spec, "resume", None) == "auto"


def _stamp(seconds: float) -> str:
    return datetime.datetime.fromtimestamp(seconds, datetime.timezone.utc).isoformat(timespec="milliseconds")


class Scheduler:
    """Starts the slots of the schedules of one StateGraphServer, while it holds the scheduler's lease."""

    def __init__(self, server: Any, schedules: list[Schedule]):
        self.server = server
        self.schedules = schedules
        self.paused: dict[str, tuple[int, float]] = {}  # run id -> (failed resumes, not before) -- in memory
        self.opted: dict[str, bool] = {}  # interrupted run -> whether its snapshot asks for resume: auto (it never
        # changes): a snapshot loads once per run, not on every tick

    async def tick(self, now: Optional[float] = None) -> list[str]:
        """One look at the clock: the run ids it started."""
        now = time.time() if now is None else now
        store, owner = self.server.run_store, self.server.run_manager.owner
        started: list[str] = []
        # resuming has a lease of its own that only a driving process takes: a worker or agent-cli that holds the
        # slots' lease must not keep the API from resuming (it never resumes by itself)
        if _drives() and store.take_scheduler(resume_lease(self.server.name), owner, _stamp(now + LEASE),
                                              now=_stamp(now)):
            started += await self.resume_interrupted(now)
        if not store.take_scheduler(self.server.name, owner, _stamp(now + LEASE), now=_stamp(now)):
            return started  # another process starts the slots
        for schedule in self.schedules:
            index, start = schedule.slot(now)
            key = f"schedule:{schedule.name}:{index}"
            last = store.latest_by_key(key)
            if last is None and now - start > schedule.late:
                continue  # first seen too late: this slot is left out, the next one runs (a run it has goes on)
            if last is not None and last["status"] == "failed" and (
                    store.count_by_key(key) >= ATTEMPTS
                    or now - datetime.datetime.fromisoformat(last["finished_at"]).timestamp() < RETRY_AFTER):
                continue  # tried often enough, or not long ago: a failure that repeats costs a run per tick
            try:  # the slot's run key: its run is attached to, resumed where nothing runs it, or -- ended -- left;
                # only a transient failure starts it again (service.start_run)
                result = await self.server.service.start_run(schedule.machine, params=dict(schedule.params),
                                                              user_id=schedule.user, run_key=key)
            except Exception as exc:  # ServiceError: the machine is gone, has errors, the params do not fit
                logger.warning("stategraph: schedule %s did not start %s: %s", schedule.name, schedule.machine, exc)
                continue
            if "ended" in result or "attached" in result:
                continue  # it ran, or runs -- here or in another process
            logger.info("stategraph: schedule %s %s run %s of %s", schedule.name,
                        "resumed" if result.get("resumed") else "started", result["run_id"], schedule.machine)
            started.append(result["run_id"])
        return started

    async def resume_interrupted(self, now: float) -> list[str]:
        """The interrupted runs this process resumes by itself: an earlier slot's of a schedule (the current one's
        is the slot loop's) -- or, its machine gone, ended -- and an opted-in machine's (``auto_resume``, ``resume:
        auto``) that holds no caller's live objects (``sg.live``: only its caller can hand them again). The ids."""
        from .engine.runner import LIVE_OBJECTS

        server, store = self.server, self.server.run_store
        resumed: list[str] = []
        seen: set[str] = set()
        for row in _interrupted(store):
            run_id, key = row["id"], row.get("run_key") or ""
            seen.add(run_id)
            failures, not_before = self.paused.get(run_id, (0, 0.0))
            if now < not_before:
                continue
            schedule = next((s for s in self.schedules if key.startswith(f"schedule:{s.name}:")), None)
            if schedule is not None:
                if key == f"schedule:{schedule.name}:{schedule.slot(now)[0]}":
                    continue  # the current slot: the slot loop resumes it
                if server.machines.find(row["machine_id"]) is None:
                    try:
                        server.run_manager.terminate_elsewhere(run_id, f"its machine {row['machine_id']} is gone")
                        server.service._note(run_id, "ended", who=f"scheduler of {server.name}",
                                             why=f"an earlier slot of schedule {schedule.name}; its machine is gone")
                    except Exception as exc:
                        logger.warning("stategraph: schedule %s: run %s not ended: %s", schedule.name, run_id, exc)
                    continue
                why = f"an earlier slot of schedule {schedule.name}"
            else:
                if row["machine_id"] not in server.auto_resume:
                    if run_id not in self.opted:
                        try:  # off the event loop: a large machine's snapshot takes a second (API-2)
                            self.opted[run_id] = await asyncio.to_thread(_asks_auto, store, run_id)
                        except Exception:  # a snapshot that does not load is no machine that asked for it
                            continue
                    if not self.opted[run_id]:
                        continue
                if store.has_row(run_id, "trace", LIVE_OBJECTS):
                    continue  # its caller's objects are gone with its process: the caller resumes it
                why = "auto_resume"
            try:
                await server.service._resume(run_id, who=f"scheduler of {server.name}", why=why)
            except Exception as exc:  # held by a process whose lease still runs, refused, does not load
                pause = min(RESUME_PAUSE_MAX, RESUME_PAUSE * 2 ** failures)
                self.paused[run_id] = (failures + 1, now + pause)
                logger.warning("stategraph: run %s of %s not resumed (%s): %s -- again in %.0fs", run_id,
                               row["machine_id"], why, getattr(exc, "message", exc), pause)
                continue
            self.paused.pop(run_id, None)
            resumed.append(run_id)
        # what it keeps per run, only of the runs still interrupted: one resumed or ended meanwhile is forgotten
        self.opted = {run_id: asks for run_id, asks in self.opted.items() if run_id in seen}
        self.paused = {run_id: wait for run_id, wait in self.paused.items() if run_id in seen}
        return resumed

    async def loop(self) -> None:
        while True:
            try:
                await self.tick()
            except Exception:
                logger.warning("stategraph: a scheduler tick failed", exc_info=True)
            await asyncio.sleep(TICK)
