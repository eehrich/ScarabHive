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
          user: admin              # whose run it is (default: nobody's -- every user sees it)

One process starts an instance's slots: the one holding its scheduler lease in runs.db, renewed every tick -- the
API while it runs; another process takes over once that lease ran out or was given up (stop). A slot runs once --
its run key; again only after a transient failure (service.start_run), at most ATTEMPTS times and RETRY_AFTER apart.
"""

from __future__ import annotations

import asyncio
import datetime
import logging
import math
import time
from dataclasses import dataclass, field
from typing import Any, Optional

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


def _stamp(seconds: float) -> str:
    return datetime.datetime.fromtimestamp(seconds, datetime.timezone.utc).isoformat(timespec="milliseconds")


class Scheduler:
    """Starts the slots of the schedules of one StateGraphServer, while it holds the scheduler's lease."""

    def __init__(self, server: Any, schedules: list[Schedule]):
        self.server = server
        self.schedules = schedules

    async def tick(self, now: Optional[float] = None) -> list[str]:
        """One look at the clock: the run ids it started."""
        now = time.time() if now is None else now
        store = self.server.run_store
        if not store.take_scheduler(self.server.name, self.server.run_manager.owner, _stamp(now + LEASE),
                                    now=_stamp(now)):
            return []  # another process schedules
        started: list[str] = []
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

    async def loop(self) -> None:
        while True:
            try:
                await self.tick()
            except Exception:
                logger.warning("stategraph: a scheduler tick failed", exc_info=True)
            await asyncio.sleep(TICK)
