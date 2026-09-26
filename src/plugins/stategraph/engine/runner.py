"""Runs: the context the frames of one run share, and the manager that starts, resumes and forks runs.

A run executes as an asyncio task in the process that started it; the owning
process holds a lease on the run row (docs/stategraph_design.md §5.7).
Everything another process needs to continue it lives in the store: the
definition snapshot, the journal (activity outcomes, consumed events, timers,
debugger edits, context hashes) and the breakpoints. ``resume`` re-runs the
interpreter against that journal (§5.3); ``fork`` does the same with the
journal cut at a top-level step (§5.6).
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import socket
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, NoReturn, Optional

from plugins.stategraph.kinds import ActivityError
from plugins.stategraph.model.code import fingerprint, jsonable
from plugins.stategraph.model.loader import MachineTree, load_snapshot
from plugins.stategraph.model.spec import parse_duration
from .activity import ReplayDivergence
from .backend import NoBackend
from .debugger import Debugger, apply_edit
from .interpreter import Frame, RunAbort, bind_params
from .journal import ACTIVE_STATUSES, TERMINAL_STATUSES, RunStore, utc_now
from .machine import Machine, compile_tree

logger = logging.getLogger(__name__)

JOURNAL_FORMAT = 1
LEASE_SECONDS = 60
HEARTBEAT_SECONDS = 20
_STEP = re.compile(r"s(\d+)")
#: A frame's keys without a step of their own: its resources (``r.<name>``) and its end (``end.<reason>...``).
#: A submachine frame below one of them still counts its steps.
_STEPLESS = ("r.", "end.")
NO_MOCK = object()


def key_steps(key: str) -> list[tuple[str, int]]:
    """``s3/b.x/m/s5`` -> ``[("", 3), ("s3/b.x/m/", 5)]``: every frame a key passes and its step there."""
    found: list[tuple[str, int]] = []
    prefix, rest = "", key
    while True:
        match = _STEP.match(rest)
        if match:
            found.append((prefix, int(match.group(1))))
        elif not rest.startswith(_STEPLESS):
            break
        cut = rest.find("/m/")
        if cut < 0:
            break
        prefix, rest = prefix + rest[:cut + 3], rest[cut + 3:]
    return found


def _head(key: str) -> str:
    """``s3/m/s2:event`` -> ``s3/m/s2`` (suffixes follow the last colon; child labels use dots)."""
    return key.rpartition(":")[0] if ":" in key else key


def _utc(seconds_from_now: float = 0.0) -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds_from_now)).isoformat(timespec="milliseconds")


@dataclass
class _Pending:
    key: str
    name: str
    data: Any
    frame: Optional[str]          # target frame prefix; None = any frame that accepts it
    arrived: float = field(default_factory=time.monotonic)


class RunContext:
    """What every frame of one run shares."""

    NO_MOCK = NO_MOCK

    def __init__(self, *, run_id: str, store: RunStore, machine: Machine, backend: Any = None,
                 mocks: Optional[dict[str, Any]] = None, mock_only: bool = False,
                 debugger: Optional[Debugger] = None, token: Any = None,
                 owner: Optional[str] = None, origin: Optional[str] = None):
        self.id = run_id
        self.origin = origin or run_id   # the first run of a fork chain: stable across forks (run.origin)
        self.store = store
        self.machine = machine
        self.backend = backend or NoBackend()
        self.mocks = dict(mocks or {})
        self.mock_only = mock_only
        self.debugger = debugger or Debugger()
        self.token = token
        self.owner = owner
        self.frames: list[Frame] = []
        self.root: Optional[Frame] = None
        self.status = "running"
        self.finished = False
        self.cancelled = False           # the run is ending: a terminate, or limits.timeout (§3.10)
        self.timed_out = False           # ... and it was limits.timeout: the run ends failed, not cancelled
        self.cancel_pending = False      # a terminate journaled before a crash: carried out once replay is done
        self.stopping = False            # the process stops: frames end without their finally (the run resumes)
        self.divergence: Optional[str] = None  # the first replay divergence: no finally runs any more (§3.10)
        self.begun = False               # the run's task ran its first line (a cancel before that would lose the run)
        self.resource_sources: dict[str, Any] = {}  # a fork: the source run's resource values (§2.8)
        self.token_watch: Optional[asyncio.Future] = None  # terminates the run when its token is cancelled
        self.lost = False                # another process owns the run now (fenced write failed)
        self.on_lost: Optional[Callable[[str], None]] = None
        self.waiting: set[str] = set()   # prefixes of frames in a wait state
        self.busy = 0                    # leaf activities running live right now
        self.running_seconds = 0.0
        self._running_since: Optional[float] = time.monotonic()
        self._request_seq = 0
        self._edit_seq = 0
        # journal state for replay
        self.recorded: dict[str, dict[str, Any]] = {}
        self.ends: dict[str, dict[str, Any]] = {}   # "<frame>end" -> how that frame ended (§3.10)
        self.cancelled_below: set[str] = set()  # keys whose frames a cancel had reached: their endings are bounded
        self.interrupted: set[str] = set()   # keys of activities that raised interrupted (never retried, §5.5)
        self.started: dict[str, dict[str, Any]] = {}
        self.consumed: dict[str, dict[str, Any]] = {}
        self.timers: set[str] = set()
        self.edits: dict[str, list[dict[str, Any]]] = {}
        self.step_hashes: dict[str, str] = {}
        self.var_snapshots: dict[str, dict[str, Any]] = {}
        self.deadlines: dict[str, float] = {}
        self.frontier: dict[str, int] = {}
        self.mock_uses: dict[str, int] = {}   # path -> mocks consumed (restored from the journal on resume)
        # events
        self.inbox: list[_Pending] = []
        self._inbox_changed = asyncio.Event()
        self._load_journal()
        self.live = not self.frontier

    # ------------------------------------------------------------ journal
    def note_outcome(self, key: str, status: str, data: dict[str, Any]) -> None:
        """An activity's outcome, journaled now or loaded from the journal: what replay and retries read."""
        self.recorded[key] = {"status": status, "data": data}
        if status == "error" and (data.get("error") or {}).get("type") == "interrupted":
            self.interrupted.add(key)

    def _load_journal(self) -> None:
        for row in self.store.rows(self.id, kinds=("activity", "event", "edit", "timer", "trace")):
            kind, key, data = row["kind"], row["key"], row.get("data") or {}
            if kind == "activity":
                if row["status"] in ("done", "error"):
                    self.note_outcome(key, row["status"], data)
                    self._advance(key)
                    if (data.get("meta") or {}).get("mocked") and data.get("path"):
                        self.mock_uses[data["path"]] = self.mock_uses.get(data["path"], 0) + 1
                elif row["status"] == "started":
                    self.started[key] = {"data": data}
            elif kind == "event":
                if key.startswith("pending:"):
                    self.inbox.append(_Pending(key, data.get("name"), data.get("data"), data.get("frame")))
                else:
                    self.consumed[key] = data
                    self._advance(_head(key))
            elif kind == "timer":
                self.timers.add(key)
                self._advance(_head(key))
            elif kind == "edit":
                self.edits.setdefault(data.get("at", ""), []).append(data)
                self._edit_seq += 1
                self._advance(_head(data.get("at", "")))
            elif kind == "trace":
                if row["status"] == "step" and "ctx_hash" in data:
                    self.step_hashes[key] = data["ctx_hash"]
                    self._advance(_head(key))
                elif row["status"] == "vars_from":
                    self.var_snapshots[data.get("agent", "")] = data.get("vars") or {}
                elif row["status"] == "wait":
                    self.deadlines[key] = float(data.get("deadline") or 0)
                elif row["status"] == "cancel":
                    self.cancel_pending = True
                    self.timed_out = bool(data.get("timed_out"))
                elif row["status"] == "end":
                    self.ends[key] = data
                elif row["status"] == "resource_sources":
                    self.resource_sources = dict(data)

    def _advance(self, key: str) -> None:
        for prefix, step in key_steps(key):
            if step > self.frontier.get(prefix, -1):
                self.frontier[prefix] = step

    def is_replay_point(self, frame: Frame) -> bool:
        """Whether this frame's current step already happened in the journal (debugger stays silent)."""
        reached = self.frontier.get(frame.prefix)
        if reached is None:
            return False
        if frame.step < reached:
            return True
        return frame.step == reached and (frame.key() in self.recorded or frame.key(":event") in self.consumed
                                          or frame.key(":timer") in self.timers)

    def in_journal(self, frame: Frame) -> bool:
        """Whether the frame's journal goes on at its current step: a replay point, an activity that was in
        flight at the crash (a composite's children replay into their ends), an edit made at the step's enter
        hook (the frame paused there), or an end of its own the replay reaches."""
        return (self.is_replay_point(frame) or frame.key() in self.started
                or f"{frame.prefix}s{frame.step}:enter" in self.edits or self.ends_on_its_own(frame))

    def cancel_reached(self, frame: Frame) -> bool:
        """Whether a cancel reached this frame -- a join's before the crash, or one that reached the replay of a
        frame above it -- and it replays into that ending: its finally and close activities run within the bound,
        as they would have in the live run (§3.10)."""
        return any(frame.prefix.startswith(f"{key}/") for key in self.cancelled_below)

    def ends_on_its_own(self, frame: Frame) -> bool:
        """Whether the frame's journaled end is its own -- finished or failed, not ended by a cancel: its replay
        reaches that end, and a terminate that came meanwhile does not stop it on the way (§3.10)."""
        end = self.ends.get(f"{frame.prefix}end")
        return end is not None and end.get("reason") in ("finished", "failed")

    # ------------------------------------------------------------ for frames and activities
    def stop_past_journal(self, frame: Frame) -> None:
        """A frame of an ending run -- or one that only replays into its end -- goes on only as far as its journal
        goes: it replays to the point it stood at when its end came, and ends there (§3.10)."""
        if (frame.ending_only or (self.ending and not frame.finalizer)) and not self.in_journal(frame):
            self.stop_here()

    def went_live(self) -> None:
        """The run passes its journal here."""
        self.live = True

    def stop_here(self) -> NoReturn:
        """A frame of an ending run is where its journal ends: it ends here (§3.10). A terminate journaled before
        a crash is carried out now -- at a leaf activity, a wait, a new step or a frame's end, never at a
        composite that goes on."""
        if self.cancel_pending:
            self.carry_out_cancel()
        raise asyncio.CancelledError()

    def carry_out_cancel(self) -> None:
        """A terminate (or timeout) journaled before a crash takes effect now, past the replayed prefix."""
        self.cancel_pending = False
        self._begin_ending()

    @property
    def ending(self) -> bool:
        """A terminate or timeout reached the run, or is journaled and on its way: nothing pauses it any more."""
        return self.cancelled or self.cancel_pending

    def may_finalize(self) -> bool:
        """Whether an ending frame runs its finally and close activities: not on a halt -- the process stops,
        or it lost the run -- where a resume ends the frame instead; not once a replay diverged, since the run's
        state is not trusted then (§3.10)."""
        return not self.stopping and not self.lost and self.divergence is None

    def diverged(self, message: str) -> ReplayDivergence:
        """The divergence to raise: from here on no finally runs, in this frame or any other, and the run ends
        diverged even where something catches it (§3.10, §5.4)."""
        if self.divergence is None:
            self.divergence = message
        return ReplayDivergence(message)

    def next_request_id(self) -> str:
        self._request_seq += 1
        return f"{self.id}_{self._request_seq:03d}"

    def mock_for(self, path: str) -> Any:
        """The mock for an activity path; ``$visits`` counts uses of the path across the whole run, so a
        submachine called again (a new frame each time) still gets the next answer."""
        if path not in self.mocks:
            return NO_MOCK
        value = self.mocks[path]
        used = self.mock_uses.get(path, 0) + 1
        self.mock_uses[path] = used
        if isinstance(value, dict) and isinstance(value.get("$visits"), list) and value["$visits"]:
            sequence = value["$visits"]
            return sequence[min(used, len(sequence)) - 1]
        return value

    def agent_vars(self, agent: str) -> dict[str, Any]:
        """``vars_from``: the agent's configured template vars, snapshotted in the journal for replay."""
        if agent in self.var_snapshots:
            return dict(self.var_snapshots[agent])
        lookup: Optional[Callable[[str], dict[str, Any]]] = getattr(self.backend, "agent_template_vars", None)
        values = jsonable(lookup(agent) if lookup else {})
        self.var_snapshots[agent] = values
        self.write("trace", f"vars_from:{agent}", status="vars_from",
                          data={"agent": agent, "vars": values})
        return dict(values)

    async def hook(self, point: str, frame: Frame, node: Any, event: Any = None) -> None:
        for edit in self.edits.get(f"{frame.prefix}s{frame.step}:{point}", ()):
            apply_edit(frame.ctx, edit["path"], edit["value"])
        if self.is_replay_point(frame) or (event is not None and getattr(event, "replayed", False)):
            return
        if self.ending or frame.ending_only:  # a terminate is under way (no breakpoint holds it, not even one in
            return                             # a finally), or the frame only replays into its end
        await self.debugger.at_hook(self, frame, node, point, event)

    def before_step(self, frame: Frame) -> None:
        """Before a frame of an ending run takes a step: only as far as its journal goes (§3.10). Past it -- the
        frame stood at its exit or error hook when the run was terminated -- it ends in the state it stood in,
        before the transition runs its actions and leaves states. A frame whose journaled end is its own
        (finished, failed) replays into it: this step raises that end."""
        if not (frame.ending_only or (self.ending and not frame.finalizer)):
            return
        if f"{frame.prefix}s{frame.step + 1}:step" in self.step_hashes or self.ends_on_its_own(frame):
            return
        self.stop_here()

    async def after_step(self, frame: Frame) -> None:
        key = frame.key(":step")
        digest = fingerprint(frame.tokenized({"ctx": frame.ctx, "config": [node.name for node in frame.config]}))
        recorded = self.step_hashes.get(key)
        if recorded is not None and recorded != digest:
            raise self.diverged(f"after {key} the context or the active states differ from the recorded run "
                                f"(hash {digest} != {recorded}): a guard, action or template is not "
                                "deterministic, or the definition changed")
        if recorded is None:
            if frame.ending_only or (self.ending and not frame.finalizer):  # past the journal (before_step let it
                self.stop_here()                                           # through for an end of its own)
            self.step_hashes[key] = digest
            self.write("trace", key, state=frame.leaf.name if frame.leaf else None, status="step",
                              data={"ctx_hash": digest, "frame": frame.prefix, "step": frame.step})
        for edit in self.edits.get(f"{frame.prefix}s{frame.step}:watch", ()):
            apply_edit(frame.ctx, edit["path"], edit["value"])
        replaying = self.frontier.get(frame.prefix, -1) >= frame.step
        await self.debugger.after_step(self, frame, silent=replaying or self.ending)
        self.persist()

    # ------------------------------------------------------------ events
    def frame_by_prefix(self, prefix: str) -> Optional[Frame]:
        return next((f for f in self.frames if f.prefix == prefix), None)

    def send_event(self, name: str, data: Any = None, frame: Optional[str] = None) -> dict[str, Any]:
        """Route an event (§3.4): to ``frame``, or to the one frame that accepts it; else keep it in the inbox."""
        declared = self._declared_everywhere()
        if name not in declared:
            return {"accepted": False, "reason": f"no machine of this run declares event {name!r} "
                                                 f"(declared: {', '.join(sorted(declared)) or 'none'})"}
        if frame is not None:
            if self.frame_by_prefix(frame) is None:
                return {"accepted": False, "reason": f"no active frame {frame!r}"}
            target = frame
        else:
            accepting = [f.prefix for f in self.frames if name in f.accepts()]
            if len(accepting) > 1:
                return {"accepted": False, "reason": f"several frames accept {name!r}: {', '.join(accepting)}; "
                                                     "name one with frame"}
            target = accepting[0] if accepting else None
        problem = self._check_payload(name, data, target)
        if problem:
            return {"accepted": False, "reason": problem}
        pending = _Pending(f"pending:{uuid.uuid4().hex[:12]}", name, jsonable(data), target)
        if not self.lost and not self.store.record(self.id, "event", pending.key, fence=self.owner, status="pending",
                                                   data={"name": name, "data": pending.data, "frame": target}):
            self.lose()  # the fence refused it: this copy stops, like after any refused write
        if self.lost:
            return {"accepted": False, "reason": "another process owns this run now; send the event there"}
        self.inbox.append(pending)
        self._inbox_changed.set()
        return {"accepted": True, "frame": target, "queued": target is None}

    def _check_payload(self, name: str, data: Any, target: Optional[str]) -> Optional[str]:
        """The event's declared data schema (of the target frame's machine, else of every declaring machine)."""
        from plugins.stategraph.engine.backend import validate_answer

        frame = self.frame_by_prefix(target) if target is not None else None
        machines = [frame.machine] if frame is not None else self._machines()
        schemas = [m.spec.events[name].data for m in machines if name in m.spec.events and m.spec.events[name].data]
        for schema in schemas:
            problem = validate_answer(jsonable(data), schema)
            if problem:
                return f"event {name!r}: data does not match its declared schema: {problem}"
        return None

    def _machines(self) -> list[Machine]:
        found: list[Machine] = []

        def walk(machine: Machine) -> None:
            if any(m is machine for m in found):
                return
            found.append(machine)
            for child in machine.imports.values():
                walk(child)

        walk(self.machine)
        return found

    def _declared_everywhere(self) -> set[str]:
        names: set[str] = set()
        seen: set[int] = set()

        def walk(machine: Machine) -> None:
            if id(machine) in seen:
                return
            seen.add(id(machine))
            names.update(machine.spec.events)
            for child in machine.imports.values():
                walk(child)

        walk(self.machine)
        return names

    async def wait_event(self, frame: Frame, leaf: Any, timeout: Optional[float]) -> tuple[str, Any, bool, bool]:
        """``(name, data, replayed, timed_out)`` for a frame in a wait state."""
        key = frame.key(":event")
        recorded = self.consumed.get(key)
        if recorded is not None:
            return recorded.get("name"), recorded.get("data"), True, False
        if frame.key(":timer") in self.timers:
            return "", None, True, True
        if frame.ending_only or (self.ending and not frame.finalizer):  # its frame ended here before the crash
            self.stop_here()
        self.went_live()
        deadline = None
        if timeout is not None:  # one deadline per entry: an internal transition does not restart the wait
            wait_key = f"{frame.prefix}s{frame.entered_step.get(leaf.name, frame.step)}:wait"
            deadline = self.deadlines.get(wait_key)
            if deadline is None:
                deadline = time.time() + timeout
                self.deadlines[wait_key] = deadline
                self.write("trace", wait_key, state=leaf.name, status="wait",
                                  data={"deadline": deadline, "frame": frame.prefix})
        accepts = frame.accepts()
        self.waiting.add(frame.prefix)
        self.refresh_status()
        try:
            while True:
                self._inbox_changed.clear()  # before looking: an event arriving after the look still wakes us
                pending = next((p for p in self.inbox if p.name in accepts and p.frame in (None, frame.prefix)), None)
                if pending is not None:
                    self.inbox.remove(pending)
                    if not self.store.rekey(self.id, "event", pending.key, key, state=leaf.name, status="consumed",
                                            fence=self.owner):
                        self.lose()
                        raise asyncio.CancelledError()
                    self.consumed[key] = {"name": pending.name, "data": pending.data}
                    self.trace(frame, "event", state=leaf.name, data={"event": pending.name})
                    return pending.name, pending.data, False, False
                remaining = None if deadline is None else deadline - time.time()
                if remaining is not None and remaining <= 0:
                    self.timers.add(frame.key(":timer"))
                    self.write("timer", frame.key(":timer"), state=leaf.name, status="fired",
                                      data={"frame": frame.prefix})
                    return "", None, False, True
                try:
                    await asyncio.wait_for(self._inbox_changed.wait(), remaining)
                except asyncio.TimeoutError:
                    continue
        finally:
            self.waiting.discard(frame.prefix)
            self.refresh_status()

    # ------------------------------------------------------------ debugger edits, trace, view
    def record_edit(self, frame: Frame, hook: str, path: str, value: Any) -> None:
        self._edit_seq += 1
        at = f"{frame.prefix}s{frame.step}:{hook}"
        self.write("edit", f"{at}:{self._edit_seq}", state=frame.leaf.name if frame.leaf else None,
                          status="applied", data={"at": at, "path": path, "value": value})

    def trace(self, frame: Frame, what: str, *, state: Optional[str] = None, data: Any = None) -> None:
        key = f"{frame.prefix}s{frame.step}:{what}:{state or ''}"
        self.write("trace", key, state=state, status=what,
                          data={"machine": frame.machine.id, "frame": frame.prefix, "step": frame.step,
                                **(data or {})})

    def frame_started(self, frame: Frame) -> None:
        self.frames.append(frame)
        self.persist()

    def frame_ended(self, frame: Frame) -> None:
        if frame in self.frames and frame is not self.root:
            self.frames.remove(frame)
        self._inbox_changed.set()
        self.persist()

    def set_status(self, status: str) -> None:
        now = time.monotonic()
        if self.status == "running" and status != "running" and self._running_since is not None:
            self.running_seconds += now - self._running_since
            self._running_since = None
        elif status == "running" and self.status != "running":
            self._running_since = now
        self.status = status
        self.persist()

    def refresh_status(self) -> None:
        """The run's status from its frames (§5): paused > running (a leaf activity works) > waiting > running."""
        if self.finished:
            return
        if self.debugger.paused is not None:
            status = "paused"
        elif self.busy > 0:
            status = "running"
        elif self.waiting:
            status = "waiting"
        else:
            status = "running"
        if status != self.status:
            self.set_status(status)

    def elapsed_running(self) -> float:
        extra = time.monotonic() - self._running_since if self._running_since is not None else 0.0
        return self.running_seconds + extra

    def view(self) -> dict[str, Any]:
        return {"frames": [frame.view() for frame in self.frames], "live": self.live,
                "running_seconds": round(self.elapsed_running(), 1),
                "inbox": [{"name": p.name, "frame": p.frame} for p in self.inbox]}

    def persist(self, **extra: Any) -> None:
        if self.lost:
            return
        try:
            status = extra.pop("status", self.status)
            changed = self.store.update_run(self.id, fence=self.owner, status=status, view=self.view(),
                                            debug=self.debugger.state(),
                                            lease_until=_utc(LEASE_SECONDS) if not self.finished else _utc(-1),
                                            **extra)  # a finished run releases its lease: takeable at once
        except Exception:  # the view is for display; a failed write must not stop the run
            logger.warning("stategraph: could not persist the view of run %s", self.id, exc_info=True)
            return
        if changed == 0 and self.owner is not None:
            self.lose()

    def lose(self) -> None:
        """Another process took this run (our lease had expired): stop here without writing anything more."""
        if self.lost:
            return
        self.lost = True
        logger.warning("stategraph: run %s was taken over by another process; stopping the local copy", self.id)
        if self.on_lost is not None:
            self.on_lost(self.id)

    def write(self, kind: str, key: str, **fields: Any) -> None:
        """Every journal write of the run, fenced by its owner: a lost run stops instead of overwriting."""
        if self.lost:
            raise asyncio.CancelledError()
        if not self.store.record(self.id, kind, key, fence=self.owner, **fields):
            self.lose()
            raise asyncio.CancelledError()

    def cancel(self, *, timed_out: bool = False) -> None:
        """The run's ending begins: a terminate, or ``limits.timeout`` (§3.10). The first one counts; it is
        journaled, so a crash while the finally activities run still ends the run the same way."""
        if self.cancelled:
            return
        if self.cancel_pending:  # a journaled one is still on its way: it counts, and takes effect now
            self.carry_out_cancel()
            return
        if not self.lost:
            try:
                if not self.store.record(self.id, "trace", "cancel", fence=self.owner, status="cancel",
                                         data={"timed_out": timed_out}):
                    self.lose()
            except Exception:
                logger.warning("stategraph: could not journal the terminate of run %s", self.id, exc_info=True)
        self.timed_out = timed_out
        self._begin_ending()

    def _begin_ending(self) -> None:
        self.cancelled = True
        self.debugger.release()  # a pause -- say in a finally's submachine -- must not hold the ending
        if self.token is not None and hasattr(self.token, "cancel"):
            try:
                self.token.cancel()
            except Exception:
                logger.debug("cancel token of %s raised", self.id, exc_info=True)


@dataclass
class LiveRun:
    ctx: RunContext
    task: asyncio.Task
    root: Frame


def _lost(run_id: str) -> ValueError:
    return ValueError(f"another process owns run {run_id} now; control it there")


def owner_id() -> str:
    return f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:6]}"


class RunManager:
    """Starts, resumes, forks and controls the runs this process owns."""

    def __init__(self, store: RunStore, *, on_cancel: Optional[Callable[[str], None]] = None,
                 on_finish: Optional[Callable[[str], None]] = None):
        self.store = store
        self.on_cancel = on_cancel
        self.on_finish = on_finish
        self.owner = owner_id()
        self.live: dict[str, LiveRun] = {}
        self._stopping = False
        self._heartbeat: Optional[asyncio.Task] = None

    # ------------------------------------------------------------ lifecycle
    async def start(self, tree: MachineTree, *, params: Optional[dict[str, Any]] = None,
                    mocks: Optional[dict[str, Any]] = None, mock_only: bool = False, breakpoints: Any = (),
                    watchpoints: Any = (), pause_at_start: bool = False, backend: Any = None,
                    backend_factory: Optional[Callable[[str], Any]] = None, token_factory: Optional[Callable[[str], Any]] = None,
                    user_id: Optional[str] = None, run_id: Optional[str] = None, run_key: Optional[str] = None,
                    parent_run: Optional[str] = None, fork_step: Optional[int] = None,
                    copy_rows: Optional[list[dict[str, Any]]] = None,
                    resource_sources: Optional[dict[str, Any]] = None) -> str:
        machine = compile_tree(tree)
        try:
            bind_params(machine.spec.params, params, machine.id)
        except ActivityError as exc:
            raise ValueError(exc.message) from exc
        run_id = run_id or uuid.uuid4().hex[:12]
        debugger = Debugger(breakpoints=breakpoints, watchpoints=watchpoints, pause_at_start=pause_at_start)
        self.store.create_run(run_id, machine.id, tree.snapshot(), params=params or {},
                              mocks={"mocks": mocks or {}, "mock_only": mock_only}, debug=debugger.state(),
                              user_id=user_id, session_id=f"sg_{run_id}", parent_run=parent_run,
                              fork_step=fork_step, run_key=run_key, owner=self.owner, lease_until=_utc(LEASE_SECONDS),
                              journal_format=JOURNAL_FORMAT)
        if copy_rows:
            self.store.copy_rows(parent_run or "", run_id, copy_rows)
        if resource_sources:  # journaled, so a resume of the fork can still run the fork hooks
            self.store.record(run_id, "trace", "resource_sources", fence=self.owner, status="resource_sources",
                              data=resource_sources)
        self._launch(run_id, machine, params, mocks, mock_only, debugger,
                     backend if backend is not None else (backend_factory(run_id) if backend_factory else None),
                     token_factory(run_id) if token_factory else None, origin=self._origin(parent_run) or run_id)
        return run_id

    def _origin(self, run_id: Optional[str]) -> Optional[str]:
        """The first run of a fork chain (a fork's origin is its source's origin)."""
        seen = set()
        while run_id and run_id not in seen:
            seen.add(run_id)
            row = self.store.get_run(run_id)
            if row is None or not row.get("parent_run"):
                return run_id
            run_id = row["parent_run"]
        return run_id

    async def resume(self, run_id: str, *, backend: Any = None, backend_factory: Optional[Callable[[str], Any]] = None,
                     token_factory: Optional[Callable[[str], Any]] = None, cancel: bool = False) -> str:
        """Continue a run from its journal. ``cancel``: resume it into its termination -- the journaled prefix
        replays, then it ends as cancelled and its finally and close activities run (§3.10)."""
        row = self.store.get_run(run_id)
        if row is None:
            raise KeyError(run_id)
        if run_id in self.live:
            raise ValueError(f"run {run_id} is active in this process")
        if row["status"] in ("succeeded", "cancelled"):
            raise ValueError(f"run {run_id} is {row['status']}; fork it to run again from a step")
        if int(row.get("journal_format") or JOURNAL_FORMAT) != JOURNAL_FORMAT:
            raise ValueError(f"run {run_id} has journal format {row.get('journal_format')}; this engine reads "
                             f"{JOURNAL_FORMAT}")
        if not self.store.take_lease(run_id, self.owner, _utc(LEASE_SECONDS), now=_utc()):
            raise ValueError(f"run {run_id} is owned by {row.get('owner')} until {row.get('lease_until')}")
        row = self.store.get_run(run_id) or row  # as of the lease: breakpoints stored meanwhile apply
        if row["status"] in ("succeeded", "cancelled"):  # it ended between the first read and the lease
            self.store.update_run(run_id, fence=self.owner, lease_until=_utc(-1))
            raise ValueError(f"run {run_id} is {row['status']}; fork it to run again from a step")
        if cancel and not self.store.has_row(run_id, "trace", "cancel"):  # an earlier one (a timeout) counts
            self.store.record(run_id, "trace", "cancel", fence=self.owner, status="cancel", data={"timed_out": False})
        tree = load_snapshot(row["definition"])
        machine = compile_tree(tree)
        debug = row.get("debug") or {}
        debugger = Debugger(breakpoints=debug.get("breakpoints") or (), watchpoints=debug.get("watchpoints") or ())
        options = row.get("mocks") or {}
        self.store.update_run(run_id, status="running", finished_at=None, error=None, output=None, final_state=None)
        self._launch(run_id, machine, row.get("params") or {}, options.get("mocks"), bool(options.get("mock_only")),
                     debugger, backend if backend is not None else (backend_factory(run_id) if backend_factory else None),
                     token_factory(run_id) if token_factory else None, origin=self._origin(run_id),
                     running_seconds=float((row.get("view") or {}).get("running_seconds") or 0.0))
        return run_id

    async def fork(self, run_id: str, *, at_step: Optional[int] = None, tree: Optional[MachineTree] = None,
                   backend: Any = None, backend_factory: Optional[Callable[[str], Any]] = None,
                   token_factory: Optional[Callable[[str], Any]] = None, breakpoints: Any = None,
                   watchpoints: Any = None, pause_at_start: bool = False, user_id: Optional[str] = None) -> str:
        """A new run that replays ``run_id``'s journal before top-level step ``at_step`` and continues live.

        ``tree`` is the definition to use (``definition: current``); default: the source run's snapshot.
        """
        row = self.store.get_run(run_id)
        if row is None:
            raise KeyError(run_id)
        keep = []
        sources: dict[str, Any] = {}  # the source's root resources, for the fork hooks (§2.8)
        for journal_row in self.store.rows(run_id, kinds=("activity", "event", "edit", "timer", "trace")):
            kind, key = journal_row["kind"], journal_row["key"]
            name = key[2:] if key.startswith("r.") else ""
            if kind == "activity" and journal_row["status"] == "done" and name and "." not in name and "/" not in name:
                sources[name] = (journal_row.get("data") or {}).get("out")
            if kind == "trace" and journal_row["status"] not in ("step", "vars_from", "wait"):
                continue
            if kind == "activity" and journal_row["status"] not in ("done", "error"):
                continue
            if kind == "event" and key.startswith("pending:"):
                continue
            anchor = (journal_row.get("data") or {}).get("at", key) if kind == "edit" else key
            if anchor.startswith(_STEPLESS):  # the root's resources and its end: a fork opens (or forks) its own
                continue                      # and ends its own way -- also below a root finally's submachine
            steps = key_steps(_head(anchor) if kind != "activity" else anchor)
            if at_step is not None and steps and steps[0][1] >= at_step:
                continue
            if kind == "trace" and journal_row["status"] == "vars_from":
                keep.append(journal_row)
                continue
            if steps:
                keep.append(journal_row)
        debug = row.get("debug") or {}
        options = row.get("mocks") or {}
        return await self.start(
            tree if tree is not None else load_snapshot(row["definition"]), params=row.get("params") or {},
            mocks=options.get("mocks"), mock_only=bool(options.get("mock_only")),
            breakpoints=debug.get("breakpoints") if breakpoints is None else breakpoints,
            watchpoints=debug.get("watchpoints") if watchpoints is None else watchpoints,
            pause_at_start=pause_at_start, backend=backend, backend_factory=backend_factory,
            token_factory=token_factory, user_id=user_id or row.get("user_id"), parent_run=run_id,
            fork_step=at_step, copy_rows=keep, resource_sources=sources)

    def _launch(self, run_id: str, machine: Machine, params: Optional[dict[str, Any]],
                mocks: Optional[dict[str, Any]], mock_only: bool, debugger: Debugger, backend: Any,
                token: Any, *, origin: Optional[str] = None, running_seconds: float = 0.0) -> None:
        ctx = RunContext(run_id=run_id, store=self.store, machine=machine, backend=backend, mocks=mocks,
                         mock_only=mock_only, debugger=debugger, token=token,
                         owner=self.owner, origin=origin)
        ctx.running_seconds = running_seconds  # limits.timeout counts running time across resumes
        root = Frame(ctx, machine, params)
        ctx.root = root
        ctx.on_lost = self._drop
        task = asyncio.ensure_future(self._execute(ctx, root))
        self.live[run_id] = LiveRun(ctx, task, root)
        watched = getattr(ctx.backend, "token", None)
        if watched is not None and hasattr(watched, "wait_for_cancellation"):
            ctx.token_watch = asyncio.ensure_future(self._watch_token(run_id, watched))
        self._ensure_heartbeat()

    async def _watch_token(self, run_id: str, token: Any) -> None:
        """A cancel from outside reaches the run's token (a caller whose request id prefixes the run id, e.g.
        a book cancel above the agent facade): the run is terminated like by the debugger, so its finally
        activities run and no agent activity fails as agent_failed (§5.8)."""
        await token.wait_for_cancellation()
        live = self.live.get(run_id)
        if live is None or live.ctx.cancelled or live.task.done():
            return  # ended, or already ending
        try:
            self.control(run_id, "terminate")
        except (KeyError, ValueError):  # gone meanwhile, or another process owns it now
            logger.debug("stategraph: token cancel of run %s not applied", run_id, exc_info=True)

    def _drop(self, run_id: str) -> None:
        live = self.live.get(run_id)
        if live is not None:
            live.task.cancel()

    async def _execute(self, ctx: RunContext, root: Frame) -> None:
        ctx.begun = True
        try:  # status lines of the run's activities route to the run, not to whoever started it (§5.8)
            from agent_system.tools.status import current_request_id

            current_request_id.set(ctx.id)
        except Exception:
            pass
        try:  # whose run this is: a decision an activity asks is captured under the run's user
            # (llm/hook_notify.py) -- nobody registered the run's own id. This task's context only.
            # The backend's user first: a resumed run's agents run as whoever resumed it
            # (service._resume), and its decisions go with them.
            from agent_system.core.request_context import current_run_user

            run_user = getattr(ctx.backend, "user_id", None) or (ctx.store.get_run(ctx.id) or {}).get("user_id")
            if run_user:
                current_run_user.set(run_user)
        except Exception:
            logger.debug("stategraph: the user of run %s not set", ctx.id, exc_info=True)
        timeout = parse_duration(ctx.machine.spec.limits.timeout)
        fields: dict[str, Any] = {}
        watchdog = asyncio.ensure_future(self._watch_timeout(ctx, timeout)) if timeout else None
        try:
            result = await root.execute()
            fields = {"status": result.status, "output": result.output, "final_state": result.final_state,
                      "error": result.error}
            if result.error and result.error.get("type") == "step_limit":
                fields["status"] = "failed"
        except asyncio.CancelledError:
            if ctx.divergence is not None:  # a divergence a join's cleanup took in, and the run ended on
                fields = {"status": "failed", "error": {"type": "diverged", "message": ctx.divergence}}
            elif ctx.stopping:  # also mid-terminate or mid-timeout: a resume completes the ending
                fields = {"status": "interrupted", "error": None}
            elif ctx.timed_out:
                fields = {"status": "failed", "error": {"type": "timed_out",
                                                        "message": f"run exceeded {timeout:g}s of running time"}}
            else:
                fields = {"status": "cancelled", "error": {"type": "cancelled", "message": "terminated"}}
        except ReplayDivergence as exc:
            fields = {"status": "failed", "error": {"type": "diverged", "message": str(exc)}}
        except RunAbort as exc:
            fields = {"status": "failed", "error": exc.error}
        except Exception as exc:  # an engine bug must end the run visibly, not leave it "running"
            logger.exception("stategraph run %s crashed", ctx.id)
            fields = {"status": "failed", "error": {"type": "internal", "message": f"{type(exc).__name__}: {exc}"}}
        finally:
            if watchdog is not None:
                watchdog.cancel()
            if ctx.token_watch is not None:
                ctx.token_watch.cancel()
            if not ctx.lost:  # a run another process took over is not ours to finish
                ctx.finished = True
                ctx.set_status(fields.get("status", "failed"))
                ctx.persist(finished_at=utc_now() if fields.get("status") != "interrupted" else None,
                            **{k: v for k, v in fields.items() if k != "status"})
            self.live.pop(ctx.id, None)
            if self.on_finish is not None:
                try:
                    self.on_finish(ctx.id)
                except Exception:
                    logger.debug("on_finish(%s) raised", ctx.id, exc_info=True)

    async def _watch_timeout(self, ctx: RunContext, timeout: float) -> None:
        """``limits.timeout``: the run ends like on a terminate, but failed. Once a terminate came first it
        bounds the finally activities; a second cancel from here would only make the run fail."""
        while not ctx.finished:
            await asyncio.sleep(0.5)
            if ctx.cancelled:
                return
            if ctx.cancel_pending or ctx.elapsed_running() <= timeout:  # a journaled one takes effect first
                continue
            ctx.cancel(timed_out=True)
            live = self.live.get(ctx.id)
            if live is not None and not ctx.lost:
                live.task.cancel()
            return

    def _ensure_heartbeat(self) -> None:
        if self._heartbeat is None or self._heartbeat.done():
            self._heartbeat = asyncio.ensure_future(self._renew_leases())

    async def _renew_leases(self) -> None:
        while self.live:
            await asyncio.sleep(HEARTBEAT_SECONDS)
            self.renew_leases()

    def renew_leases(self) -> None:
        """Extend our runs' leases; a run whose owner changed is lost and stops locally (§5.7)."""
        for run_id, live in list(self.live.items()):
            try:
                kept = self.store.renew(run_id, self.owner, _utc(LEASE_SECONDS), live.ctx.status)
            except Exception:
                logger.debug("lease renewal of %s failed", run_id, exc_info=True)
                continue
            if not kept:
                live.ctx.lose()

    def sweep_expired(self) -> list[str]:
        """Mark runs whose owner's lease ran out as interrupted (never a run of a live process)."""
        return self.store.mark_expired(now=_utc())

    async def shutdown(self) -> None:
        self._stopping = True
        for live in self.live.values():
            live.ctx.stopping = True
        tasks = [live.task for live in self.live.values()]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        if self._heartbeat is not None:
            self._heartbeat.cancel()

    # ------------------------------------------------------------ control
    def _live(self, run_id: str) -> LiveRun:
        live = self.live.get(run_id)
        if live is None:
            row = self.store.get_run(run_id)
            if row is None:
                raise KeyError(run_id)
            raise ValueError(f"run {run_id} is {row['status']} and not active in this process")
        if live.ctx.lost:  # its task is stopping; the run goes on in the process that took it over
            raise _lost(run_id)
        return live

    def find_by_key(self, run_key: str) -> Optional[dict[str, Any]]:
        return self.store.find_by_key(run_key)

    def control(self, run_id: str, action: str, *, state: Optional[str] = None,
                machine: Optional[str] = None) -> None:
        if action == "terminate" and run_id not in self.live:
            self._terminate_elsewhere(run_id)
            return
        live = self._live(run_id)
        if action == "terminate":
            if live.ctx.cancelled:  # already on its way; a second cancel would cut its finally activities
                return
            live.ctx.cancel()
            if live.ctx.lost:  # the journaled terminate was refused: another process owns the run now
                raise _lost(run_id)
            if self.on_cancel is not None:
                try:
                    self.on_cancel(run_id)
                except Exception:
                    logger.debug("on_cancel(%s) raised", run_id, exc_info=True)
            if live.ctx.begun:  # a task cancelled before its first line ends without running one, and the run
                live.task.cancel()  # would stay 'running': it carries the terminate out itself when it begins
            return
        live.ctx.debugger.command(action, state=state, machine=machine)
        self._persist_control(run_id, live)

    def _terminate_elsewhere(self, run_id: str) -> None:
        """End a run no process runs (interrupted): take its lease, then write it cancelled."""
        row = self.store.get_run(run_id)
        if row is None:
            raise KeyError(run_id)
        if row["status"] in TERMINAL_STATUSES:
            return
        if not self.store.take_lease(run_id, self.owner, _utc(), now=_utc()):
            raise ValueError(f"run {run_id} is owned by {row.get('owner')} until {row.get('lease_until')}; "
                             "terminate it there")
        self.store.update_run(run_id, fence=self.owner, status="cancelled", finished_at=utc_now(),
                              error={"type": "cancelled", "message": "terminated while interrupted"})

    def set_points(self, run_id: str, *, breakpoints: Any = None, watchpoints: Any = None) -> None:
        if run_id in self.live:
            live = self._live(run_id)
            live.ctx.debugger.set_points(breakpoints=breakpoints, watchpoints=watchpoints)
            self._persist_control(run_id, live)
            return
        row = self.store.get_run(run_id)
        if row is None:
            raise KeyError(run_id)
        debug = row.get("debug") or {}
        debugger = Debugger(breakpoints=debug.get("breakpoints") or (), watchpoints=debug.get("watchpoints") or ())
        debugger.set_points(breakpoints=breakpoints, watchpoints=watchpoints)
        if not self.store.update_debug_unowned(run_id, debugger.state(), now=_utc()):
            raise ValueError(f"run {run_id} is running in {row.get('owner')}; change its breakpoints and "
                             "watchpoints there")

    @staticmethod
    def _persist_control(run_id: str, live: LiveRun) -> None:
        """Store a control's effect; a refused write means another process took the run meanwhile."""
        live.ctx.persist()
        if live.ctx.lost:
            raise _lost(run_id)

    def evaluate(self, run_id: str, expr: str) -> Any:
        return self._live(run_id).ctx.debugger.evaluate(expr)

    def assign(self, run_id: str, path: str, expr: str) -> Any:
        live = self._live(run_id)
        try:
            value = live.ctx.debugger.assign(live.ctx, path, expr)
        except asyncio.CancelledError:
            if live.ctx.lost:  # the fenced edit was refused: this process lost the run, not the caller's task
                raise _lost(run_id) from None
            raise
        live.ctx.persist()
        return value

    def send_event(self, run_id: str, name: str, data: Any = None, frame: Optional[str] = None) -> dict[str, Any]:
        return self._live(run_id).ctx.send_event(name, data, frame)

    async def wait(self, run_id: str, timeout: Optional[float] = None) -> dict[str, Any]:
        """Until the run ends, pauses or waits for an event (or ``timeout``); the run's row."""
        loop = asyncio.get_running_loop()
        deadline = None if timeout is None else loop.time() + timeout
        await asyncio.sleep(0)
        while True:
            live = self.live.get(run_id)
            if live is not None:
                if live.task.done() or live.ctx.status in ("paused", "waiting"):
                    break
                pause = 0.05
            else:  # owned by another process (or finished): read its row, but do not spin on the database
                row = self.store.get_run(run_id)
                if row is None:
                    raise KeyError(run_id)
                if row["status"] != "running":
                    return row
                pause = 0.5
            if deadline is not None and loop.time() >= deadline:
                break
            await asyncio.sleep(pause if deadline is None else max(0.0, min(pause, deadline - loop.time())))
        row = self.store.get_run(run_id)
        if row is None:
            raise KeyError(run_id)
        return row

    def describe(self, run_id: str) -> dict[str, Any]:
        row = self.store.get_run(run_id)
        if row is None:
            raise KeyError(run_id)
        row.pop("definition", None)
        row["active"] = run_id in self.live
        row["terminal"] = row["status"] in TERMINAL_STATUSES
        return row


__all__ = ["ACTIVE_STATUSES", "JOURNAL_FORMAT", "LiveRun", "RunContext", "RunManager", "key_steps"]
