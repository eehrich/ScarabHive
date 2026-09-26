"""One machine instance ("frame"): UML run-to-completion semantics (docs/stategraph_design.md §3).

A frame owns a machine's context, its active configuration (a chain from a
top-level state down to the active leaf), its step counter and visit counts.
The run's root machine is one frame; every submachine activity starts a nested
frame whose journal keys carry the parent's activity key as prefix, so all
keys of a run stay deterministic (§5.2).
"""

from __future__ import annotations

import asyncio
import copy
import json
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Optional

from plugins.stategraph.kinds import ActivityError, parse_activity
from plugins.stategraph.kinds.base import vars_object
from plugins.stategraph.model.code import NAMESPACE_FIELDS, CodeError, Scope, jsonable, namespace_of, plain
from plugins.stategraph.model.spec import GUARD_ELSE, TRIGGER_DONE, TRIGGER_ERROR, ParamSpec, TransitionSpec, parse_duration
from .activity import ActivityRun, ReplayDivergence, RunAbort
from .machine import Machine, Node, lca

if TYPE_CHECKING:
    from .runner import RunContext

_TYPES = {"string": str, "integer": int, "number": (int, float), "boolean": bool, "object": dict, "array": list}

#: Fields ``activity`` and ``error`` always have in code, so a mocked activity reads the same (§2.6).
logger = logging.getLogger(__name__)

#: A finally or close activity without its own timeout gets this long while the run is being cancelled.
FINALLY_CANCEL_TIMEOUT = 60.0
#: How often a running finally looks whether the run's ending began elsewhere (a resumed terminate carried out
#: in another branch reaches it without a cancel): from then on the bound counts.
_WATCH_ENDING = 0.25

ACTIVITY_DEFAULTS = {**{name: None for name in NAMESPACE_FIELDS["activity"]}, "mocked": False}
#: Meta the journal keeps but code must not see: it differs between a live run and its replay.
_HIDDEN_META = ("replayed",)
ERROR_DEFAULTS = {name: None for name in NAMESPACE_FIELDS["error"]}

#: Error types that end the run instead of being dispatched (§3.5).
UNCATCHABLE = frozenset({"step_limit", "timed_out", "cancelled", "diverged"})


@dataclass
class Event:
    name: str
    out: Any = None
    error: Optional[dict[str, Any]] = None
    data: Any = None
    activity: Optional[dict[str, Any]] = None
    source: Optional[Node] = None      # composite completion: the composite whose final was entered
    replayed: bool = False


@dataclass
class FrameResult:
    status: str                         # succeeded | failed
    output: Any
    final_state: Optional[str]
    error: Optional[dict[str, Any]] = None


class MachineFailed(Exception):
    """An error no transition handled, an error while handling an error, or a hard limit."""

    def __init__(self, error: dict[str, Any]):
        super().__init__(error.get("message", ""))
        self.error = error


class _TransitionFailed(Exception):
    def __init__(self, type_: str, message: str, state: Optional[str]):
        super().__init__(message)
        self.type = type_
        self.message = message
        self.state = state


def _activity_view(meta: Optional[dict[str, Any]]) -> dict[str, Any]:
    return {**ACTIVITY_DEFAULTS, **{k: v for k, v in (meta or {}).items() if k not in _HIDDEN_META}}


def bind_params(declared: dict[str, ParamSpec], given: Optional[dict[str, Any]], machine_id: str) -> dict[str, Any]:
    """Parameters with defaults applied and types checked; values are deep copies (§3.6)."""
    given = jsonable(given or {})
    unknown = sorted(set(given) - set(declared))
    if unknown:
        raise ActivityError("params_invalid", f"{machine_id} has no parameter(s) {', '.join(unknown)}")
    bound: dict[str, Any] = {}
    for name, param in declared.items():
        if name in given:
            value = given[name]
        elif param.default is not None:
            value = copy.deepcopy(param.default)
        elif param.required:
            raise ActivityError("params_invalid", f"{machine_id} requires parameter {name!r}")
        else:
            value = None
        expected = _TYPES.get(param.type)
        if value is not None and expected is not None:
            if isinstance(value, bool) and param.type in ("integer", "number"):
                raise ActivityError("params_invalid", f"{machine_id}.{name}: expected {param.type}, got a boolean")
            if not isinstance(value, expected):
                raise ActivityError("params_invalid",
                                    f"{machine_id}.{name}: expected {param.type}, got {type(value).__name__}")
        if param.enum is not None and value is not None and value not in param.enum:
            raise ActivityError("params_invalid", f"{machine_id}.{name}: {value!r} is not one of {param.enum}")
        bound[name] = value
    return bound


class Frame:
    def __init__(self, run: "RunContext", machine: Machine, params: Optional[dict[str, Any]], *,
                 prefix: str = "", path: str = "", parent: Optional["Frame"] = None, finalizer: bool = False,
                 ending_only: bool = False):
        self.run = run
        self.machine = machine
        self.prefix = prefix
        self.path = path                  # state path of the activity that started this frame ("" = root)
        self.parent = parent
        self.params = bind_params(machine.spec.params, params, machine.id)
        self.ctx: dict[str, Any] = copy.deepcopy(machine.spec.context)
        self.vars: dict[str, Any] = {}
        self.config: list[Node] = []
        self.visits: dict[str, int] = {}
        self.entered_step: dict[str, int] = {}   # state -> the step it was (last) entered at
        self.step = 0
        self.pending_error: Optional[dict[str, Any]] = None
        self.last_event: Optional[Event] = None
        self._enter_hooked = False
        self.finalizer = finalizer        # the frame of a finally activity: runs on while the run is cancelled
        self.ending_only = ending_only    # replays into its end, never runs live (a branch a join had cancelled,
                                          # a frame a cancel reached while it replayed, §3.10)
        self.resources: dict[str, Any] = {}
        # the finally activities of the states the last transition left: (state, error, journal key)
        self._pending_finally: list[tuple[Node, Optional[dict[str, Any]], str]] = []
        self._left: dict[str, int] = {}   # state -> times left within step ``_left_step`` (keys stay unique)
        self._left_step = -1
        self._finalized = False
        self._terminating = False       # a terminate reached this frame: finally activities run within the bound

    # ------------------------------------------------------------ helpers
    @property
    def leaf(self) -> Optional[Node]:
        return self.config[-1] if self.config else None

    def key(self, suffix: str = "") -> str:
        return f"{self.prefix}s{self.step}{suffix}"

    def state_path(self, node: Node) -> str:
        return f"{self.path}/{node.name}" if self.path else node.name

    def scope(self, *, event: Optional[Event] = None, writable: bool = False) -> dict[str, Any]:
        """Names for code (§2.6). ``event`` binds out/activity, error or event depending on its trigger."""
        leaf = self.leaf
        scope: dict[str, Any] = {
            "ctx": Scope(self.ctx, frozen=not writable, label="ctx"),
            "params": Scope(self.params, frozen=True, label="params"),
            "run": namespace_of({"id": self.run.id, "origin": self.run.origin, "step": self.step,
                                 "machine": self.machine.id,
                                 "state": leaf.name if leaf else None, "visits": dict(self.visits),
                                 "frame": self.prefix}, "run"),
            "resources": namespace_of(self.resources, "resources"),
            "out": None, "error": None, "event": None, "activity": None,
        }
        if event is not None:
            if event.name == TRIGGER_DONE:
                scope["out"] = event.out
                scope["activity"] = namespace_of(_activity_view(event.activity), "activity")
            elif event.name == TRIGGER_ERROR:
                scope["error"] = namespace_of({**ERROR_DEFAULTS, **(event.error or {})}, "error")
                scope["activity"] = namespace_of(_activity_view(event.activity), "activity")
            else:
                scope["event"] = namespace_of({"name": event.name, "data": event.data}, "event")
        return scope

    def accepts(self) -> set[str]:
        """Named events some active state of this frame has a transition for."""
        names: set[str] = set()
        for node in self.config:
            names.update(t.trigger for t in node.transitions if t.trigger not in (TRIGGER_DONE, TRIGGER_ERROR))
        return names

    def view(self) -> dict[str, Any]:
        return {"machine": self.machine.id, "prefix": self.prefix, "path": self.path, "step": self.step,
                "state": self.leaf.name if self.leaf else None, "config": [n.name for n in self.config],
                "visits": dict(self.visits), "ctx": self.ctx, "params": self.params,
                "accepts": sorted(self.accepts()) if self._waiting() else []}

    def _waiting(self) -> bool:
        leaf = self.leaf
        return leaf is not None and self.is_wait_state(leaf)

    @staticmethod
    def is_wait_state(node: Node) -> bool:
        return (node.type == "state" and not node.composite and node.kind is None
                and not any(t.trigger == TRIGGER_DONE for t in node.transitions))

    def _error(self, type_: str, message: str, state: Optional[str], **extra: Any) -> dict[str, Any]:
        return {"type": type_, "message": message, "state": state, "data": None, "cause": None, **extra}

    # ------------------------------------------------------------ run
    async def execute(self) -> FrameResult:
        self.run.frame_started(self)
        try:
            try:
                await self._open_resources()
                self._init_vars()
                await self._enter_initial()
                while True:
                    self.run.stop_past_journal(self)
                    leaf = self.leaf
                    if leaf is None:
                        raise MachineFailed(self.pending_error or self._error("no_transition", "no active state", None))
                    if leaf.is_final and leaf.parent is None and self.pending_error is None:
                        result = self._finish(leaf)
                        await self._finalize("finished", None)
                        return result
                    event = await self._next_event()
                    await self._dispatch(event)
                    await self.run.after_step(self)
                    await self._run_pending_finally()
            except MachineFailed as failed:
                self.run.trace(self, "failed", state=self.leaf.name if self.leaf else None, data=failed.error)
                await self._finalize("failed", failed.error)
                return FrameResult("failed", None, self.leaf.name if self.leaf else None, failed.error)
            except RunAbort as abort:
                try:
                    await self._finalize("failed", abort.error)
                except asyncio.CancelledError:
                    if not self.run.may_finalize():
                        raise
                    # a terminate during these finally activities: the abort came first, and it ends the run
                raise abort
            except asyncio.CancelledError:
                if self.run.may_finalize():  # not on a halt: a resume ends the frame then
                    await self._finalize("cancelled", self._cancel_error())
                raise
        finally:
            self.run.frame_ended(self)

    # ------------------------------------------------------------ resources and finally (§2.8, §3.10)
    def tokenized(self, value: Any) -> Any:
        """``value`` with every resource value of this frame and its parents replaced by its token.

        Hashes are taken over this form (§5.4): a fork's resources differ from its source's on purpose, and
        the replayed prefix -- rendered with the source's values -- must still match. Only distinctive values
        count (strings of 6+ characters, objects, lists); a short one would also replace unrelated text.
        """
        pairs: list[tuple[Any, str]] = []
        frame: Optional[Frame] = self
        while frame is not None:
            values = dict(frame.resources)
            sources = self.run.resource_sources if frame.parent is None else {}
            for name, real in [*values.items(), *sources.items()]:  # a fork's replayed outputs hold the source's
                pairs.extend(_token_pairs(real, f"resources.{name}"))
            frame = frame.parent
        pairs.sort(key=lambda pair: -len(json.dumps(pair[0], sort_keys=True, default=str)))  # longest first
        return _substitute(value, pairs) if pairs else value

    def _own_path(self, suffix: str) -> str:
        return f"{self.path}/{suffix}" if self.path else suffix

    async def _open_resources(self) -> None:
        """Open this machine's resources in declaration order; a forked run's root frame forks them instead."""
        sources = self.run.resource_sources if self.parent is None else {}
        for name, resource in self.machine.spec.resources.items():
            if resource.fork is not None and name in sources:
                raw, hook, extra = resource.fork, "fork", {"fork_source": sources[name]}
            else:
                raw, hook, extra = resource.open, "open", {}
            kind, spec = parse_activity(raw)
            act = ActivityRun(self, None, f"{self.prefix}r.{name}", self._own_path(f"resources/{name}/{hook}"),
                              raw=raw, extra_scope=extra, finalizer=self.finalizer)
            try:
                self.resources[name] = await act.execute(kind, spec)
            except ActivityError as exc:
                failure = {**exc.as_dict(), "state": None}
                failure["message"] = f"resources.{name}: {exc.message}"
                raise MachineFailed(failure) from exc

    async def _run_pending_finally(self) -> None:
        """The finally activities of the states the last transition left: after it committed, innermost first.

        A cancel that arrives meanwhile lets the running one finish; then the frame ends as cancelled and the rest
        runs in its finalization -- where its journal ends: a replay takes no time, so a cancel that reaches one
        takes effect at the point the crashed run had reached. The frame replays on as far as that and ends
        there (§3.10), instead of ending in states the crashed run had already left.
        """
        while self._pending_finally:
            node, error, key = self._pending_finally[0]
            arrived = await self._run_finally(node.spec.finally_, key, f"{self.state_path(node)}/finally", node.name,
                                              "transition", error)
            self._pending_finally.pop(0)
            if arrived:
                if not self.run.in_journal(self):
                    raise asyncio.CancelledError()
                self.ending_only = True
                if self.prefix:  # the frames it replays into below it end within the bound, as the cancel reached it
                    self.run.cancelled_below.add(self.prefix[:-1])

    def _left_key(self, node: Node) -> str:
        """Journal key of the finally of ``node``, left by this step's transition: ``s<N>.fin.<state>`` -- beside
        the step's activity ``s<N>``, not below it (a key below it would count as that activity's child, §5.5).
        A state left again within the same step (through initial pseudostates) gets ``.2``, ``.3``, ..."""
        if self._left_step != self.step:
            self._left_step, self._left = self.step, {}
        count = self._left[node.name] = self._left.get(node.name, 0) + 1
        key = f"{self.prefix}s{self.step}.fin.{node.name}"
        return key if count == 1 else f"{key}.{count}"

    def _finalizers(self, reason: str, error: Optional[dict[str, Any]]) -> list[tuple[Any, ...]]:
        """What a frame's end runs, in order: the pending finally activities, the finally of every state still
        active (innermost first), the machine's finally, the resources' close (reverse order).

        The end's keys carry its reason (``end.<reason>.<state>``, ``end.<reason>.finally``,
        ``end.<reason>.close.<resource>``): a resumed frame that ends another way than the crashed one (§3.10)
        runs its own finally activities instead of meeting the other ending's.
        """
        steps: list[tuple[Any, ...]] = [
            (node.spec.finally_, key, f"{self.state_path(node)}/finally", node.name, "transition", pending_error)
            for node, pending_error, key in self._pending_finally]
        self._pending_finally = []
        end = f"{self.prefix}end.{reason}"
        steps += [(node.spec.finally_, f"{end}.{node.name}", f"{self.state_path(node)}/finally", node.name, reason,
                   error) for node in reversed(self.config) if node.spec.finally_ is not None]
        if self.machine.spec.finally_ is not None:
            steps.append((self.machine.spec.finally_, f"{end}.finally", self._own_path(f"{self.machine.id}.finally"),
                          None, reason, error))
        for name in reversed(list(self.resources)):
            close = self.machine.spec.resources[name].close
            if close is not None:
                steps.append((close, f"{end}.close.{name}", self._own_path(f"resources/{name}/close"), None,
                              reason, error))
        return steps

    def _cancel_error(self) -> Optional[dict[str, Any]]:
        """``ending.error`` of a cancelled frame: set when limits.timeout ended the run, None on a terminate."""
        return self._error("timed_out", "the run exceeded limits.timeout", None) if self.run.timed_out else None

    async def _finalize(self, reason: str, error: Optional[dict[str, Any]]) -> None:
        """What runs once when this frame ends (§3.10): its finally activities and its resources' close.

        How it ends is journaled first (``<frame>end``). A resumed frame that the run's terminate had ended
        ends that way again, even where the replay reaches another end (a terminate that came while the
        transition into a final state ran a finally left no live step to be carried out at); and a frame
        reaching its end while a journaled terminate waits to be carried out ends as cancelled.

        A terminate that arrives while these run does not cut them: each finishes, within the cancel bound.
        Then a frame that had finished or failed keeps that outcome at the root, and a nested one passes the
        terminate on to its parent. Only a halt -- the process stops, or it lost the run -- stops them.
        """
        if self._finalized:
            return
        if not self.run.may_finalize():
            raise asyncio.CancelledError()
        self._finalized = True
        computed, key = reason, f"{self.prefix}end"
        end = self.run.ends.get(key)
        if end is not None and end.get("reason") == "cancelled" and not end.get("terminated") and not self.ending_only:
            end = None  # a local cancel (a fail-fast join, an activity timeout) is not journaled: decide anew
        if end is not None:
            reason, error = str(end.get("reason")), end.get("error")
            if reason != computed and "cancelled" not in (reason, computed):
                raise self.run.diverged(f"{key}: the frame ended {reason} in the recorded run and {computed} now (a "
                                        "guard, action or template is not deterministic, or the definition changed)")
        else:
            if self.run.cancel_pending and not self.finalizer:  # the journaled terminate came before this end
                self.run.carry_out_cancel()
                reason, error = "cancelled", self._cancel_error()
            end = {"reason": reason, "error": error,
                   "terminated": reason == "cancelled" and self.run.cancelled and not self.finalizer}
            self.run.ends[key] = end
            self.run.write("trace", key, state=self.leaf.name if self.leaf else None, status="end",
                           data={**end, "frame": self.prefix, "machine": self.machine.id})
        if reason == "cancelled":
            self._terminating = True
        arrived = False
        for step in self._finalizers(reason, error):
            if not self.run.may_finalize():
                raise asyncio.CancelledError()
            arrived = await self._run_finally(*step) or arrived
        if reason == "cancelled":
            if computed == "cancelled":
                return  # the caller raises on
            if end.get("terminated") and not self.run.cancelled:
                self.run.carry_out_cancel()
            raise asyncio.CancelledError()  # it ended cancelled before the crash: so it does now
        if arrived or (self.run.ending and not self.finalizer):  # the terminate came during this end -- or a
            if self.parent is not None:                              # sibling carried it out meanwhile
                if self.run.cancel_pending:
                    self.run.carry_out_cancel()
                raise asyncio.CancelledError()  # a nested frame passes it on: its parent is terminated
            self.run.cancel_pending = False  # the root had ended: its outcome stands
            task = asyncio.current_task()
            if arrived and task is not None and task.cancelling():
                task.uncancel()

    async def _run_finally(self, raw: dict[str, Any], key: str, path: str, state: Optional[str], reason: str,
                           error: Optional[dict[str, Any]]) -> bool:
        """One finally or close activity, journaled like any activity; a failure is traced, never raised.

        A terminate that arrives meanwhile does not cut it: it runs on, within the cancel bound, and the result
        says whether one arrived. Only a halt -- the process stops, or it lost the run -- stops it; that raises
        once the activity has ended, so nothing of it writes after the run.
        """
        kind, spec = parse_activity(raw)
        act = ActivityRun(self, state, key, path, raw=raw, finalizer=True, ending_only=False,  # the ending runs live
                          extra_scope={"ending": {"reason": reason, "state": state, "error": error}})
        bound = None if spec.timeout is not None else FINALLY_CANCEL_TIMEOUT  # its own timeout bounds it anyway
        loop = asyncio.get_running_loop()
        work = asyncio.ensure_future(act.execute(kind, spec))
        arrived = False
        deadline: Optional[float] = None
        try:
            while not work.done():
                if (self._terminating or self.run.ending or self.run.cancel_reached(self)) and bound and deadline is None:
                    deadline = loop.time() + bound
                if deadline is not None and loop.time() >= deadline:
                    raise TimeoutError()
                wait = None if not bound else _WATCH_ENDING if deadline is None else deadline - loop.time()
                try:
                    await asyncio.wait({work}, timeout=wait)
                except asyncio.CancelledError:
                    if not self.run.may_finalize():
                        raise
                    self._terminating = arrived = True  # a terminate: this one still finishes, within the bound
                    continue
            try:
                work.result()
            except asyncio.CancelledError as exc:  # it cancelled itself (say, a caller whose token was cancelled)
                if not self.run.may_finalize():
                    raise
                raise ActivityError("cancelled", f"{path}: cancelled") from exc
        except asyncio.CancelledError:
            await _stopped(work)
            raise
        except (ActivityError, RunAbort, TimeoutError) as exc:
            if isinstance(exc, ActivityError):
                failure = exc.as_dict()
            elif isinstance(exc, RunAbort):
                failure = dict(exc.error or {})
            else:
                if await _stopped(work):
                    arrived = True  # a halt meanwhile stops the next one (_finalize checks before each)
                failure = {"type": "timeout", "message": f"no result within {bound:g}s while the run was cancelled"}
                act.cut(failure)  # its outcome: a resume does not start it again
            self.run.trace(self, "finally_failed", state=path, data={"key": key, "error": failure})
            logger.warning("stategraph run %s: %s failed: %s", self.run.id, path, failure.get("message"))
        return arrived

    def _init_vars(self) -> None:
        spec = self.machine.spec
        merged = dict(self.parent.vars) if self.parent else {}
        if spec.vars_from:
            try:
                merged.update(self.run.agent_vars(spec.vars_from))
            except ActivityError as exc:  # an agent that is not configured: a config error, not an engine crash
                raise MachineFailed(self._error("config", exc.message, None)) from exc
        if spec.vars:
            try:
                merged.update(vars_object(self.machine.namespace.render(spec.vars, self.scope(), f"{self.machine.id}.vars"),
                                          f"{self.machine.id}.vars"))
            except CodeError as exc:
                raise MachineFailed(self._error("template_failed", exc.message, None)) from exc
            except ActivityError as exc:
                raise MachineFailed(self._error(exc.type, exc.message, None)) from exc
        self.vars = merged

    async def _enter_initial(self) -> None:
        try:
            await self._enter_to(self.machine.initial, None)
        except _TransitionFailed as failed:
            raise MachineFailed(self._error(failed.type, failed.message, failed.state)) from None

    def _finish(self, leaf: Node) -> FrameResult:
        status = leaf.spec.status or "succeeded"
        output = self._final_output(leaf)
        self.run.trace(self, "final", state=leaf.name, data={"status": status})
        error = None if status == "succeeded" else self._error("final", f"ended in {leaf.name}", leaf.name)
        return FrameResult(status, output, leaf.name, error)

    def _final_output(self, leaf: Node) -> Any:
        if leaf.spec.output is None:
            return None
        try:
            return jsonable(self.machine.namespace.render(leaf.spec.output, self.scope(), f"{leaf.name}.output"))
        except CodeError as exc:
            raise MachineFailed(self._error("template_failed", exc.message, leaf.name)) from exc

    async def _next_event(self) -> Event:
        leaf = self.leaf
        assert leaf is not None
        if self.pending_error is not None:
            error, self.pending_error = self.pending_error, None
            return Event(TRIGGER_ERROR, error=error)
        if leaf.is_final:  # a nested final completes its composite; its output is out
            try:
                out = self._final_output(leaf)
            except MachineFailed as failed:
                return Event(TRIGGER_ERROR, error=failed.error)
            return Event(TRIGGER_DONE, source=leaf.parent, out=out)
        if not self._enter_hooked:  # once per entry, not again after every event a wait state discards
            self._enter_hooked = True
            await self.run.hook("enter", self, leaf)
        if leaf.kind is not None:
            act = ActivityRun(self, leaf.name, self.key(), self.state_path(leaf), raw=leaf.activity_raw or {},
                              visit=self.visits.get(leaf.name, 1), finalizer=self.finalizer)
            try:
                out = await act.execute(leaf.kind, leaf.activity)
            except ActivityError as exc:
                return Event(TRIGGER_ERROR, error={**exc.as_dict(), "state": leaf.name}, activity=act.meta,
                             replayed=bool(act.meta.get("replayed")))
            return Event(TRIGGER_DONE, out=out, activity=act.meta, replayed=bool(act.meta.get("replayed")))
        if self.is_wait_state(leaf):
            name, data, replayed, timed_out = await self.run.wait_event(
                self, leaf, parse_duration(leaf.spec.timeout))
            if timed_out:
                return Event(TRIGGER_ERROR, error=self._error("wait_timeout", f"{leaf.name}: no event within "
                                                              f"{leaf.spec.timeout}", leaf.name), replayed=replayed)
            return Event(name, data=data, replayed=replayed)
        return Event(TRIGGER_DONE)  # a state without do completes right after entry

    # ------------------------------------------------------------ dispatch
    async def _dispatch(self, event: Event) -> None:
        self.last_event = event
        leaf = self.leaf
        assert leaf is not None
        await self.run.hook("error" if event.name == TRIGGER_ERROR else "exit", self, leaf, event)
        self.run.before_step(self)  # an ending run: past the journal the frame ends here, in the state it stands in
        self.step += 1
        limit = self.machine.spec.limits.max_steps
        if self.step > limit:
            raise RunAbort(self._error("step_limit", f"{self.machine.id}: more than {limit} steps", leaf.name))
        if event.name == TRIGGER_DONE:  # completion is local (§3.2)
            owner = event.source or leaf
            candidates = [(owner, t) for t in owner.transitions if t.trigger == TRIGGER_DONE]
        else:
            candidates = [(node, t) for node in reversed(leaf.chain()) for t in node.transitions
                          if t.trigger == event.name]
        snapshot = (copy.deepcopy(self.ctx), list(self.config), dict(self.visits))
        for owner, transition in candidates:
            try:
                segments = self._resolve(owner, transition, event)
            except CodeError as exc:
                self._abort(snapshot, event, "guard_failed", exc.message, owner.name)
                return
            if segments is None:
                continue
            try:
                target = await self._fire(segments, event)
            except _TransitionFailed as failed:
                self._abort(snapshot, event, failed.type, failed.message, failed.state or leaf.name)
                return
            self.run.trace(self, "transition", state=owner.name,
                           data={"from": owner.name, "to": target.name if target else None, "event": event.name,
                                 "index": owner.transitions.index(transition)})
            return
        if event.name == TRIGGER_ERROR:
            raise MachineFailed(event.error or self._error("error", "unhandled error", leaf.name))
        if event.name == TRIGGER_DONE:
            owner = event.source or leaf
            self.pending_error = self._error("no_transition", f"{owner.name} completed and no completion "
                                             "transition is enabled", owner.name)
            return
        self.run.trace(self, "event_discarded", state=leaf.name, data={"event": event.name})

    def _abort(self, snapshot: tuple[dict[str, Any], list[Node], dict[str, int]], event: Event, type_: str,
               message: str, state: Optional[str]) -> None:
        """Atomic transitions: restore ctx, configuration and visits, then raise the error in the source leaf."""
        self.ctx, self.config, self.visits = copy.deepcopy(snapshot[0]), list(snapshot[1]), dict(snapshot[2])
        self._pending_finally = []  # the states were not left after all
        self._left = {}
        error = self._error(type_, message, state)
        if event.name == TRIGGER_ERROR:  # failing while handling an error: nothing is left to handle it
            error["cause"] = event.error
            raise MachineFailed(error)
        self.pending_error = error

    def _guard(self, transition: TransitionSpec, event: Event, where: str) -> bool:
        if transition.guard is None or transition.guard.strip() == GUARD_ELSE:
            return True
        return self.machine.namespace.guard(transition.guard, self.scope(event=event), where)

    def _resolve(self, owner: Node, transition: TransitionSpec, event: Event) -> Optional[list[tuple[Node, TransitionSpec]]]:
        """Guard, then through junctions (static branches). None = not enabled."""
        if not self._guard(transition, event, f"{owner.name}.transitions"):
            return None
        segments = [(owner, transition)]
        target = transition.target
        seen: set[str] = set()
        while target is not None and self.machine.nodes[target].type == "junction":
            junction = self.machine.nodes[target]
            if junction.name in seen:
                raise CodeError(junction.name, "junctions form a cycle")
            seen.add(junction.name)
            for branch in junction.transitions:
                if self._guard(branch, event, f"{junction.name}.transitions"):
                    segments.append((junction, branch))
                    target = branch.target
                    break
            else:
                return None
        return segments

    async def _fire(self, segments: list[tuple[Node, TransitionSpec]], event: Event) -> Optional[Node]:
        first_owner, first = segments[0]
        if first.target is None:  # internal transition: effect only, no exit, no entry
            self._effect(first, event, first_owner)
            return None
        source: Node = first_owner
        target: Node = first_owner
        for segment_owner, transition in segments:
            target = self.machine.nodes[transition.target]
            await self._exit_to(lca(source, target), event)
            self._effect(transition, event, segment_owner)
            source = target
        while target.type == "choice":  # dynamic branch: its guards see the effects already run
            choice = target
            try:
                picked = next((t for t in choice.transitions if self._guard(t, event, f"{choice.name}.transitions")),
                              None)
                more = self._resolve(choice, picked, event) if picked is not None else None
            except CodeError as exc:
                raise _TransitionFailed("guard_failed", exc.message, choice.name) from exc
            if more is None:
                raise _TransitionFailed("no_transition", f"no branch of choice {choice.name!r} holds", choice.name)
            for segment_owner, transition in more:
                target = self.machine.nodes[transition.target]
                await self._exit_to(lca(source, target), event)
                self._effect(transition, event, segment_owner)
                source = target
        await self._enter_to(target, event)
        return target

    def _effect(self, transition: TransitionSpec, event: Event, owner: Node) -> None:
        if not transition.effect:
            return
        try:
            self.machine.namespace.execute(transition.effect, self.scope(event=event, writable=True),
                                           f"{owner.name}.transitions.effect")
        except CodeError as exc:
            raise _TransitionFailed("action_failed", exc.message, owner.name) from exc
        self._check_context(owner)

    def _check_context(self, node: Node) -> None:
        try:
            self.ctx = jsonable_strict(self.ctx)
        except (TypeError, ValueError) as exc:
            raise _TransitionFailed("not_serialisable", f"ctx must stay JSON data: {exc}", node.name) from exc

    # ------------------------------------------------------------ exit / entry
    async def _exit_to(self, ancestor: Optional[Node], event: Optional[Event]) -> None:
        if ancestor is not None and ancestor not in self.config:
            return  # already left below it
        while self.config and self.config[-1] is not ancestor:
            node = self.config[-1]
            if node.spec.exit:
                try:
                    self.machine.namespace.execute(node.spec.exit, self.scope(event=event, writable=True),
                                                   f"{node.name}.exit")
                except CodeError as exc:
                    raise _TransitionFailed("action_failed", exc.message, node.name) from exc
                self._check_context(node)
            self.config.pop()
            self.run.trace(self, "exit", state=node.name)
            if node.spec.finally_ is not None:  # runs once the transition committed (_run_pending_finally)
                self._pending_finally.append((node, event.error if event is not None and event.name == TRIGGER_ERROR
                                              else None, self._left_key(node)))

    async def _enter_to(self, target: Node, event: Optional[Event]) -> None:
        if target.is_pseudo:
            await self._enter_pseudo(target, event)
            return
        chain = target.chain()
        start = 0
        while start < len(self.config) and start < len(chain) and self.config[start] is chain[start]:
            start += 1
        for node in chain[start:]:
            if not self._enter_node(node, event):
                return
        node = target
        while node.composite:
            initial = node.children[node.spec.initial]
            if initial.is_pseudo:
                await self._enter_pseudo(initial, event)
                return
            if not self._enter_node(initial, event):
                return
            node = initial

    async def _enter_pseudo(self, pseudo: Node, event: Optional[Event]) -> None:
        """Entered a choice/junction as an initial state: take its first enabled branch."""
        ev = event or Event(TRIGGER_DONE)
        try:
            picked = next((t for t in pseudo.transitions if self._guard(t, ev, f"{pseudo.name}.transitions")), None)
        except CodeError as exc:
            raise _TransitionFailed("guard_failed", exc.message, pseudo.name) from exc
        if picked is None:
            raise _TransitionFailed("no_transition", f"no branch of {pseudo.name!r} holds", pseudo.name)
        target = self.machine.nodes[picked.target]
        await self._exit_to(lca(pseudo, target), event)  # a branch that leaves the composite leaves it first
        self._effect(picked, ev, pseudo)
        await self._enter_to(target, event)

    def _enter_node(self, node: Node, event: Optional[Event]) -> bool:
        """Activate a state. False stops the descent (loop_limit is raised in the state)."""
        self.config.append(node)
        self._enter_hooked = False
        self.entered_step[node.name] = self.step
        visits = self.visits.get(node.name, 0) + 1
        self.visits[node.name] = visits
        for descendant in node.descendants():  # max_visits counts per activation of the parent region
            self.visits.pop(descendant.name, None)
        self.run.trace(self, "enter", state=node.name, data={"visit": visits})
        limit = node.spec.max_visits
        if limit is not None and visits > limit:
            self.pending_error = self._error("loop_limit", f"{node.name} entered {visits} times "
                                             f"(max_visits {limit})", node.name, visits=visits)
            return False
        if node.spec.entry:
            try:
                self.machine.namespace.execute(node.spec.entry, self.scope(event=event, writable=True),
                                               f"{node.name}.entry")
            except CodeError as exc:
                raise _TransitionFailed("action_failed", exc.message, node.name) from exc
            self._check_context(node)
        return True


async def _stopped(work: "asyncio.Future[Any]") -> bool:
    """Cancel ``work`` and wait until it has ended -- a finally's task must not outlive the run's end.
    Returns whether a cancel reached the caller meanwhile."""
    work.cancel()
    cancelled = False
    while not work.done():
        try:
            await asyncio.wait({work})
        except asyncio.CancelledError:
            cancelled = True
    if not work.cancelled():
        work.exception()  # retrieved: no "exception was never retrieved" warning
    return cancelled


def _token_pairs(real: Any, name: str) -> list[tuple[Any, str]]:
    """(value, token) pairs of one resource value: the whole value, and for an object or a list also each
    distinctive string inside it (6+ characters: a short one would also replace unrelated text)."""
    pairs: list[tuple[Any, str]] = []
    if isinstance(real, str):
        if len(real) >= 6:
            pairs.append((real, f"\u27e8{name}\u27e9"))
    elif isinstance(real, (dict, list)) and real:
        pairs.append((real, f"\u27e8{name}\u27e9"))
        items = real.items() if isinstance(real, dict) else enumerate(real)
        for key, item in items:
            pairs.extend(_token_pairs(item, f"{name}.{key}"))
    return pairs


def _substitute(value: Any, pairs: list[tuple[Any, str]]) -> Any:
    for real, token in pairs:
        if not isinstance(real, str) and value == real:
            return token
    if isinstance(value, str):
        for real, token in pairs:
            if isinstance(real, str) and real in value:
                value = value.replace(real, token)
        return value
    if isinstance(value, dict):
        return {key: _substitute(item, pairs) for key, item in value.items()}
    if isinstance(value, list):
        return [_substitute(item, pairs) for item in value]
    return value


def jsonable_strict(value: Any) -> Any:
    """ctx after an action: must already be JSON data (no default=str rescue); returns a normalised copy."""
    import json

    return json.loads(json.dumps(plain(value), ensure_ascii=False, allow_nan=False))


__all__ = ["Event", "Frame", "FrameResult", "MachineFailed", "ReplayDivergence", "RunAbort", "UNCATCHABLE",
           "bind_params"]
