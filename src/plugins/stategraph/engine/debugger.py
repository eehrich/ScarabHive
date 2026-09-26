"""Breakpoints, watchpoints and cooperative pausing (docs/stategraph_design.md §5).

Hooks sit at step boundaries only -- ``enter`` (a state is entered, its
activity not started), ``exit`` (the activity finished, ``out`` is known, no
transition chosen yet) and ``error``. A running agent call is never frozen:
``pause`` takes effect at the next hook. Command names follow the Debug
Adapter Protocol so a VS Code adapter can be added later.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import asdict, dataclass, field
from typing import TYPE_CHECKING, Any, Optional

from plugins.stategraph.model.code import CodeError, jsonable, plain

if TYPE_CHECKING:
    from .interpreter import Event, Frame
    from .machine import Node
    from .runner import RunContext

logger = logging.getLogger(__name__)

HOOKS = ("enter", "exit", "error")
MODES = ("run", "pause", "step")
_MISSING = object()


def _fields(raw: dict[str, Any], names: tuple[str, ...], what: str) -> dict[str, Any]:
    """The known fields of a point, each of its type -- a wrong one fails here, not later in the live run."""
    known = {}
    for name in names:
        value = raw.get(name)
        if value is None:  # absent or null: the default
            continue
        kind = bool if name == "enabled" else str
        if not isinstance(value, kind):
            raise ValueError(f"{what} {name} must be {'true or false' if kind is bool else 'a string'}, "
                             f"not {value!r}")
        known[name] = value
    return known


@dataclass
class Breakpoint:
    state: str
    at: str = "enter"
    machine: Optional[str] = None
    condition: Optional[str] = None
    enabled: bool = True
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:8])

    @classmethod
    def parse(cls, raw: Any) -> "Breakpoint":
        if isinstance(raw, str):  # "state" or "state@exit"
            state, _, at = raw.partition("@")
            raw = {"state": state, "at": at or "enter"}
        if not isinstance(raw, dict) or not raw.get("state"):
            raise ValueError(f"a breakpoint names a state: {raw!r}")
        point = cls(**_fields(raw, ("state", "at", "machine", "condition", "enabled", "id"), "breakpoint"))
        if point.at not in HOOKS:
            raise ValueError(f"breakpoint at must be one of {', '.join(HOOKS)}, not {point.at!r}")
        return point


@dataclass
class Watchpoint:
    expr: str
    machine: Optional[str] = None
    condition: Optional[str] = None
    enabled: bool = True
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:8])

    @classmethod
    def parse(cls, raw: Any) -> "Watchpoint":
        if isinstance(raw, str):
            raw = {"expr": raw}
        if not isinstance(raw, dict) or not raw.get("expr"):
            raise ValueError(f"a watchpoint has an expression: {raw!r}")
        return cls(**_fields(raw, ("expr", "machine", "condition", "enabled", "id"), "watchpoint"))


def parse_points(raw: Any, kind: type[Breakpoint] | type[Watchpoint]) -> list[Any]:
    """Breakpoints or watchpoints from a list of their forms; anything else is a ValueError (a string is not
    taken apart into one point per character)."""
    if raw is None:
        return []
    if not isinstance(raw, (list, tuple)):
        raise ValueError(f"{kind.__name__.lower()}s must be a list, not {type(raw).__name__}")
    return [kind.parse(item) for item in raw]


def stored_points(raw: Any, kind: type[Breakpoint] | type[Watchpoint], run: str = "") -> list[Any]:
    """The points a run's row holds. One that no longer parses -- stored before points were checked -- is dropped
    with a warning: else neither a resume nor a terminate (which resumes) of its run would get past it."""
    if raw is not None and not isinstance(raw, (list, tuple)):  # a string would be one point per character
        logger.warning("stategraph: run %s: dropped stored %ss that are no list: %r", run or "?",
                       kind.__name__.lower(), raw)
        return []
    points = []
    for item in raw or ():
        try:
            points.append(kind.parse(item))
        except ValueError as exc:
            logger.warning("stategraph: run %s: dropped a stored %s that is no longer valid: %s", run or "?",
                           kind.__name__.lower(), exc)
    return points


class Debugger:
    def __init__(self, *, breakpoints: Any = (), watchpoints: Any = (), pause_at_start: bool = False):
        self.breakpoints: list[Breakpoint] = parse_points(breakpoints, Breakpoint)
        self.watchpoints: list[Watchpoint] = parse_points(watchpoints, Watchpoint)
        self.mode = "pause" if pause_at_start else "run"   # run | pause | step
        self.run_to: Optional[str] = None
        self.run_to_machine: Optional[str] = None
        self.paused: Optional[dict[str, Any]] = None
        self.paused_frame: Optional["Frame"] = None
        self.paused_event: Optional["Event"] = None
        self._resume = asyncio.Event()
        self._pausing = asyncio.Lock()   # one visible pause at a time; a second frame waits its turn
        self._values: dict[tuple[str, str], Any] = {}
        self.watch: dict[str, dict[str, Any]] = {}

    # ------------------------------------------------------------ state
    def state(self) -> dict[str, Any]:
        return {"breakpoints": [asdict(b) for b in self.breakpoints],
                "watchpoints": [asdict(w) for w in self.watchpoints],
                "watch": self.watch, "paused": self.paused, "mode": self.mode, "run_to": self.run_to,
                "run_to_machine": self.run_to_machine}

    @classmethod
    def from_state(cls, debug: Optional[dict[str, Any]], run: str = "") -> "Debugger":
        """The debugger a run's row stored: its points (``stored_points``), and what held the run -- a run that
        stopped while paused pauses again at its first live hook; a pending pause, step or run_to still applies (§6).
        """
        debug = debug or {}
        debugger = cls()
        debugger.breakpoints = stored_points(debug.get("breakpoints"), Breakpoint, run)
        debugger.watchpoints = stored_points(debug.get("watchpoints"), Watchpoint, run)
        mode = debug.get("mode")
        debugger.mode = "pause" if debug.get("paused") else (mode if mode in MODES else "run")
        debugger.run_to = debug.get("run_to") if isinstance(debug.get("run_to"), str) else None
        debugger.run_to_machine = (debug.get("run_to_machine") if isinstance(debug.get("run_to_machine"), str)
                                   else None)
        return debugger

    def set_points(self, *, breakpoints: Any = None, watchpoints: Any = None) -> None:
        if breakpoints is not None:
            self.breakpoints = parse_points(breakpoints, Breakpoint)
        if watchpoints is not None:
            self.watchpoints = parse_points(watchpoints, Watchpoint)
            live = {w.id for w in self.watchpoints}
            self._values = {k: v for k, v in self._values.items() if k[0] in live}
            self.watch = {k: v for k, v in self.watch.items() if k in live}

    # ------------------------------------------------------------ hooks
    async def at_hook(self, run: "RunContext", frame: "Frame", node: Optional["Node"], point: str,
                      event: Optional["Event"]) -> None:
        reason = None
        if self.mode in ("pause", "step"):
            reason = "paused" if self.mode == "pause" else "step"
        elif (self.run_to and node is not None and node.name == self.run_to and point == "enter"
              and self.run_to_machine in (None, frame.machine.id)):
            reason = f"reached {self.run_to}"
            self.run_to = None
            self.run_to_machine = None
        else:
            for point_ in self.breakpoints:
                if not point_.enabled or point_.at != point or node is None or point_.state != node.name:
                    continue
                if point_.machine not in (None, frame.machine.id):
                    continue
                if point_.condition:
                    try:
                        hit = frame.machine.namespace.guard(point_.condition, frame.scope(event=event),
                                                            f"breakpoint {point_.id}")
                    except CodeError as exc:  # a broken condition stops rather than being ignored
                        reason = f"breakpoint {point_.id}: condition failed ({exc.message})"
                        break
                    if not hit:
                        continue
                reason = f"breakpoint {point_.id} at {point} of {node.name}"
                break
        if reason:
            await self.pause(run, frame, node, point, reason, event)

    async def after_step(self, run: "RunContext", frame: "Frame", *, silent: bool) -> None:
        """Update the watchpoints' values; pause on a change unless ``silent`` (replay, or the run is ending)."""
        for point in self.watchpoints:
            if not point.enabled or point.machine not in (None, frame.machine.id):
                continue
            scope = frame.scope()
            try:
                value = _data(frame.machine.namespace.evaluate(point.expr, scope, f"watchpoint {point.id}"))
                shown: dict[str, Any] = {"value": value}
            except CodeError as exc:
                value, shown = {"$error": exc.message}, {"error": exc.message}
            key = (point.id, frame.prefix)
            old = self._values.get(key, _MISSING)
            self._values[key] = value
            self.watch[point.id] = {**shown, "frame": frame.prefix or "top", "step": frame.step}
            if old is _MISSING or old == value or silent:
                continue
            if point.condition:
                check = dict(scope, old=old, new=value)
                try:
                    if not frame.machine.namespace.guard(point.condition, check, f"watchpoint {point.id}"):
                        continue
                except CodeError:
                    pass
            await self.pause(run, frame, frame.leaf, "watch",
                             f"watchpoint {point.id}: {point.expr} changed {_short(old)} -> {_short(value)}", None)

    async def pause(self, run: "RunContext", frame: "Frame", node: Optional["Node"], point: str, reason: str,
                    event: Optional["Event"]) -> None:
        async with self._pausing:
            if run.ending:  # the run began to end while this pause waited its turn: nothing holds it (§3.10)
                return
            self.mode = "run"
            record = {"frame": frame.prefix or "", "machine": frame.machine.id,
                      "state": node.name if node else None, "hook": point, "step": frame.step, "reason": reason,
                      "out": plain(event.out) if event is not None else None,
                      "error": event.error if event is not None else None}
            self.paused = record
            self.paused_frame = frame
            self.paused_event = event
            self._resume.clear()
            run.refresh_status()
            run.trace(frame, "paused", state=node.name if node else None, data={"reason": reason, "hook": point})
            try:
                await self._resume.wait()
            finally:
                # never wipe another frame's pause -- nor one the process stops in: the row keeps it, and the
                # resume pauses there again (from_state) instead of running on
                if self.paused is record and not run.stopping:
                    self.paused = None
                    self.paused_frame = None
                    self.paused_event = None
                run.refresh_status()

    # ------------------------------------------------------------ commands
    def command(self, action: str, *, state: Optional[str] = None, machine: Optional[str] = None) -> None:
        if action == "continue":
            self.mode = "run"
            self._resume.set()
        elif action == "step":
            self.mode = "step"
            self._resume.set()
        elif action == "pause":
            self.mode = "pause"
        elif action == "run_to":
            if not state:
                raise ValueError("run_to needs a state")
            self.run_to = state
            self.run_to_machine = machine  # a state of that machine only, not a same-named state of a submachine
            self.mode = "run"
            self._resume.set()
        else:
            raise ValueError(f"unknown debugger action {action!r}")

    def release(self) -> None:
        """The run is ending: a pause lets go, and no step or run_to holds it again."""
        self.mode = "run"
        self.run_to = None
        self.run_to_machine = None
        self._resume.set()

    def evaluate(self, expr: str) -> Any:
        frame = self._paused()
        return _data(frame.machine.namespace.evaluate(expr, frame.scope(event=self.paused_event), "evaluate"))

    def assign(self, run: "RunContext", path: str, expr: str) -> Any:
        """``ctx.<path> = <expr>`` in the paused frame; journaled so replay applies it again."""
        frame = self._paused()
        value = jsonable(frame.machine.namespace.evaluate(expr, frame.scope(event=self.paused_event), "set"))
        check_edit(frame.ctx, path)  # a bad path fails before anything is journaled
        run.record_edit(frame, self.paused["hook"] if self.paused else "enter", path, value)  # fenced
        apply_edit(frame.ctx, path, value)  # only once the journal took it: a lost run keeps its ctx
        return value

    def _paused(self) -> "Frame":
        if self.paused_frame is None:
            raise ValueError("the run is not paused")
        return self.paused_frame


def check_edit(ctx: dict[str, Any], path: str) -> list[str]:
    """The parts of a dotted path inside a context dict (``a.b.0.c``) once every hop is valid; else ValueError."""
    parts = [p for p in path.removeprefix("ctx.").split(".") if p]
    if not parts:
        raise ValueError("set needs a path below ctx, e.g. ctx.draft")
    target: Any = ctx
    walked = "ctx"
    for part in parts[:-1]:
        target = _step(target, part, walked, create=False)
        walked += f".{part}"
    _step(target, parts[-1], walked, create=True)  # validates the last hop
    return parts


def apply_edit(ctx: dict[str, Any], path: str, value: Any) -> None:
    """Assign ``value`` at a dotted path inside a context dict; a bad path is a ValueError.

    The whole path is checked before anything is written, so a refused edit leaves ctx untouched.
    """
    parts = check_edit(ctx, path)
    target: Any = ctx
    for part in parts[:-1]:
        if isinstance(target, dict) and part not in target:
            target[part] = {}
        target = target[int(part)] if isinstance(target, list) else target[part]
    last = parts[-1]
    if isinstance(target, list):
        target[int(last)] = value
    else:
        target[last] = value


def _step(target: Any, part: str, walked: str, *, create: bool) -> Any:
    if isinstance(target, list):
        if not part.isdigit() or int(part) >= len(target):
            raise ValueError(f"{walked} is a list of {len(target)}; {part!r} is not an index in it")
        return target[int(part)]
    if isinstance(target, dict):
        return target.get(part, {}) if not create else None
    raise ValueError(f"{walked} is a {type(target).__name__}; set cannot assign below it")


def _data(value: Any) -> Any:
    """What an expression of the operator's gave, as JSON data: a function, a module or any other object becomes
    its text -- an answer and a watch value are shown and stored, and must never fail the call or the run."""
    try:
        return jsonable(value)
    except Exception:  # not even deep-copyable (a module, a lock)
        return repr(value)


def _short(value: Any) -> str:
    text = repr(value)
    return text if len(text) <= 60 else text[:57] + "..."
