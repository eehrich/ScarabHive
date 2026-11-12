"""Running one activity: mocks, replay, journal, retries, timeout, nesting (docs/stategraph_design.md §5).

``ActivityRun`` is what an activity kind's ``run`` receives. Everything that
makes an activity durable happens here, not in the kinds:

* its inputs (the rendered template fields) are hashed first;
* a **recorded** outcome under its journal key answers instead of the kind --
  after checking kind, state path and input hash (a mismatch is a divergence);
* a ``started`` row without outcome means it was in flight at a crash: an
  idempotent activity continues that attempt (its finished children replay),
  a non-idempotent one raises ``interrupted``;
* a **mock** for its path answers instead of the kind (test runs);
* otherwise the kind runs live, with retries and a per-attempt timeout, and
  its normalised result is journaled before the machine sees it.

Children of a composite activity (parallel branches, map items, a
submachine's frame) are keyed per attempt: attempt 1 as ``<key>/b.x``,
attempt n > 1 as ``<key>/a<n>/b.x`` -- a retry runs its children again
instead of replaying the failed attempt's outcomes (§5.2).
"""

from __future__ import annotations

import asyncio
import collections.abc
import copy
import itertools
import json
import logging
import re
import threading
import time
import traceback
import types
from typing import TYPE_CHECKING, Any, Optional

from plugins.stategraph.kinds import REGISTRY, ActivityError, ActivityKind, KindSpec, parse_activity
from plugins.stategraph.model.code import CodeError, Scope, fingerprint, jsonable, namespace_of, plain
from plugins.stategraph.model.spec import NOT_RETRIED

if TYPE_CHECKING:
    from .interpreter import Frame

logger = logging.getLogger(__name__)
_FAILED_TYPE = {"agent": "agent_failed", "tool": "tool_failed", "decide": "decision_failed", "call": "call_failed"}
#: A key below an activity that lies in a frame's ending -- its finally or close activities, or what runs inside one.
_IN_ENDING = re.compile(r"(?:^|/)(?:end\.|s\d+\.fin\.)")
#: How much of a submachine's error its submachine_failed message repeats (the whole error is error.cause).
_CAUSE_CHARS = 500
#: The meta a ``started`` row carries over a crash or into the next attempt: what was spent, and under which ids.
_CARRIED_META = ("cost", "tokens", "cost_is_estimate", "cost_unpriced_calls", "model", "request_id", "request_ids")
#: The attempt a key below a composite belongs to: ``a<n>/`` for attempt n > 1, no such segment for attempt 1.
_ATTEMPT = re.compile(r"a(\d+)/")


class ReplayDivergence(Exception):
    """Replay met a recorded outcome that does not belong to what the machine does now (§5.4)."""


class RunAbort(Exception):
    """An uncatchable error (step_limit) in any frame: it ends the whole run, no handler sees it (§3.5)."""

    def __init__(self, error: dict[str, Any]):
        super().__init__(error.get("message", ""))
        self.error = error


class _AttemptTimeout(Exception):
    """The attempt's own deadline fired."""


class _NotJson(Exception):
    """A value that must cross the journal boundary is not JSON data."""


def _normalised(value: Any) -> Any:
    try:
        return jsonable(value)
    except (TypeError, ValueError, OverflowError, RecursionError) as exc:
        raise _NotJson(str(exc)) from exc


class ActivityRun:
    """One activity execution (or one nested child of it)."""

    def __init__(self, frame: "Frame", state: str, key: str, path: str, *, raw: dict[str, Any],
                 extra_scope: Optional[dict[str, Any]] = None, visit: int = 1, literal: bool = False,
                 finalizer: bool = False, ending_only: Optional[bool] = None):
        self.frame = frame
        self.run = frame.run
        self.state = state
        self.key = key
        self.path = path
        self.raw = raw
        self.extra_scope = jsonable(extra_scope or {})
        self.visit = visit
        self.literal = literal  # sg.tool(): the arguments are data from Python, never templates
        self.finalizer = finalizer  # finally / resource close: runs even while the run is being cancelled
        # replays into its frames' ends, never runs live -- like its frame, unless it is that frame's own finally
        # or close (the ending itself runs live)
        self.ending_only = frame.ending_only if ending_only is None else ending_only
        self.attempt = 1
        self.meta: dict[str, Any] = {}
        #: what _keep_spent set aside of this activity's meta (USD, output tokens): an outcome takes it back over
        self._aside: tuple[float, int] = (0.0, 0)
        self._detached: Optional[dict[str, Any]] = None
        self._base: Optional[dict[str, Any]] = None

    # ------------------------------------------------------------ for kinds
    @property
    def backend(self) -> Any:
        return self.run.backend

    @property
    def namespace(self) -> Any:
        return self.frame.machine.namespace

    @property
    def machine_id(self) -> str:
        return self.frame.machine.id

    @property
    def run_id(self) -> str:
        return self.run.id

    @property
    def stopped(self) -> bool:
        """Whether nothing new may start here: it only replays into its frames' ends, or the run is ending (a
        finally or close activity runs on then, §3.10)."""
        return self.ending_only or (self.run.ending and not self.finalizer)

    @property
    def cancellation_token(self) -> Any:
        """The token this activity's calls pass on: the run's -- none for a finally or close activity, which
        runs on after a terminate (a halt stops it by cancelling its task, §3.10)."""
        return None if self.finalizer else self.run.token

    def scope(self, **bind: Any) -> dict[str, Any]:
        """The read-only scope an activity sees -- DETACHED copies of ctx and params.

        An activity's only effect on the machine is its journaled result; a kind
        or a ``call`` function that mutated the live context would change the run
        without a journal entry, and every resume or fork would diverge (§3.6).
        ctx cannot change while the activity runs, so the copy is made once.
        """
        if self._detached is None:
            self._detached = {"ctx": copy.deepcopy(self.frame.ctx), "params": copy.deepcopy(self.frame.params)}
        scope = self.frame.scope()
        scope["ctx"] = Scope(self._detached["ctx"], frozen=True, label="ctx")
        scope["params"] = Scope(self._detached["params"], frozen=True, label="params")
        scope.update(copy.deepcopy(self.extra_scope))
        if isinstance(scope.get("ending"), dict):
            scope["ending"] = namespace_of(scope["ending"], "ending")
        scope.update(bind)
        return scope

    def sg_api(self, prefix: str = "") -> Any:
        """What a call's first parameter ``sg`` sees: the read-only scope, and ``sg.tool()`` (§2.5 call).

        Every ``await sg.tool(name, args)`` is a child tool activity of this call, keyed ``<key>/t.<n>`` in
        call order within the attempt and journaled like a ``tool`` activity: on resume the function runs
        again from the top and the finished calls replay. The path is ``<path>/<tool>`` (for mocks).

        ``sg.cancelled`` (a threading.Event) is set once nobody waits for the answer any more -- a timeout, a
        terminate, or the call returned: a sync function's thread cannot be stopped, a long one checks it (or
        waits on it instead of sleeping) and returns early.

        ``sg.live`` (read-only): the objects the caller handed to the run in this process (``start_run(live=)``)
        -- an instance, a connection, anything that cannot go through the journal. Empty after a resume
        without them (another process, the panel): a function that needs one fails loudly instead of guessing.
        """
        counter = itertools.count()
        closed = False
        cancelled = threading.Event()
        loop = asyncio.get_running_loop()

        threaded: list[_ThreadedTool] = []

        def tool(name: str, args: Optional[dict[str, Any]] = None, *, idempotent: bool = False) -> Any:
            """Checked when it is called, not when awaited: a sync function (a worker thread, CallKind) gets an
            awaitable it can only return -- using its value raises instead of reading a coroutine object."""
            if closed:
                raise RuntimeError(f"{self.path}: sg.tool() called after the call returned")
            if not isinstance(name, str) or not name:
                raise TypeError(f"{self.path}: sg.tool() needs a tool name, got {name!r}")
            if args is not None and not isinstance(args, dict):
                raise TypeError(f"{self.path}: sg.tool({name!r}) args must be a dict, got {type(args).__name__}")
            raw = {"tool": name, "args": dict(args or {}), "idempotent": bool(idempotent)}
            label = f"{prefix}t.{next(counter)}"

            async def run() -> Any:
                if asyncio.get_running_loop() is not loop:
                    raise RuntimeError(f"{self.path}: sg.tool() runs on the run's event loop: make the function async")
                if closed:
                    raise RuntimeError(f"{self.path}: sg.tool() called after the call returned")
                return await self.child(label, raw, shown=name, literal=True)

            try:
                on_loop = asyncio.get_running_loop() is loop
            except RuntimeError:  # no loop in this thread: a sync function's worker
                on_loop = False
            if on_loop:
                return run()
            threaded.append(_ThreadedTool(run, f"{self.path}: sg.tool({name!r})"))
            return threaded[-1]

        def close() -> None:
            nonlocal closed
            closed = True
            cancelled.set()

        def unreturned(result: Any) -> None:
            """After a sync function returned ``result``: an sg.tool() it called but did not return never ran."""
            lost = [call for call in threaded if call is not result]
            if lost:
                raise RuntimeError(f"{lost[0].where} in a sync function runs only when the function returns it -- "
                                   "to use its value or call several tools, make the function async")

        api = types.SimpleNamespace(**self.scope(), tool=tool, Error=ActivityError,  # except sg.Error as exc
                                    cancelled=cancelled, live=types.MappingProxyType(self.run.live_objects))
        api._close = close
        api._unreturned = unreturned
        return api

    def render(self, value: Any, where: str) -> Any:
        if self.literal:
            return value
        return self.namespace.render(value, self.scope(), f"{self.path}.do.{where}")

    def evaluate(self, source: str, where: str, **bind: Any) -> Any:
        return self.namespace.evaluate(source, self.scope(**bind), f"{self.path}.do.{where}")

    def frame_vars(self) -> dict[str, Any]:
        return dict(self.frame.vars)

    @staticmethod
    def plain(value: Any) -> Any:
        return plain(value)

    @staticmethod
    def text(value: Any) -> str:
        value = plain(value)
        if isinstance(value, str):
            return value
        return json.dumps(value, ensure_ascii=False, default=str)

    def request_id(self) -> str:
        """A new ``<run>_NNN`` for one request of this activity (an agent run, each feedback round and retry its
        own; a call or a decision); ``meta.request_ids`` lists every one -- the message debugger finds their LLM
        calls by them."""
        request_id = self.run.next_request_id()
        self.meta.setdefault("request_ids", []).append(request_id)
        return request_id

    def child_key(self, label: str) -> str:
        """Journal key of a child in THIS attempt (attempt 1 keeps the plain grammar)."""
        return f"{self.key}/{label}" if self.attempt == 1 else f"{self.key}/a{self.attempt}/{label}"

    def recorded_error(self, label: str) -> Optional[ActivityError]:
        """A child's recorded error. A join that ends on it without replaying the child counts its cost with
        ``run.count_recorded(act.child_key(label))``, as a replay of the child would."""
        row = self.run.recorded.get(self.child_key(label))
        if row and row["status"] == "error":
            return ActivityError.from_dict((row.get("data") or {}).get("error") or {})
        return None

    async def child(self, label: str, raw: dict[str, Any], extra_scope: Optional[dict[str, Any]] = None, *,
                    shown: Optional[str] = None, literal: bool = False, ending_only: bool = False) -> Any:
        kind, spec = parse_activity(raw)
        shown = shown or label.split(".", 1)[-1]
        sub = ActivityRun(self.frame, self.state, self.child_key(label), f"{self.path}/{shown}", raw=raw,
                          extra_scope={**self.extra_scope, **(extra_scope or {})}, visit=self.visit,
                          literal=literal, finalizer=self.finalizer, ending_only=ending_only or self.ending_only)
        return await sub.execute(kind, spec)

    async def run_submachine(self, alias: str, params: dict[str, Any]) -> Any:
        machine = self.frame.machine.imports.get(alias)
        if machine is None:
            raise ActivityError("config", f"{alias!r} is not an import of {self.frame.machine.id}")
        from .interpreter import Frame  # a submachine is a nested frame of the same run

        child = Frame(self.run, machine, params, prefix=self.child_key("m") + "/", path=self.path, parent=self.frame,
                      finalizer=self.finalizer, ending_only=self.ending_only)
        result = await child.execute()
        if result.status != "succeeded":
            cause = result.error or {}  # named in the message too: the caller's answer and a finally read that
            if cause.get("resumable"):  # it ends the caller too, saying what it said (a resume runs it again, §5.3)
                raise ActivityError("submachine_failed", str(cause.get("message") or ""), data=result.output,
                                    cause=result.error, resumable=True)
            why = f"{cause.get('type')}: {cause.get('message')}"
            failure = ActivityError("submachine_failed", f"{alias} ended in {result.final_state} ({result.status}): "
                                    f"{why[:_CAUSE_CHARS]}", data=result.output, cause=result.error)
            failure.original = result.exception
            raise failure
        return result.output

    # ------------------------------------------------------------ execution
    def inputs(self, kind: ActivityKind, spec: KindSpec) -> dict[str, Any]:
        """The rendered template fields plus the literal kind value: what the input hash covers."""
        rendered: dict[str, Any] = {kind.key: self.raw.get(kind.key)}
        for name in kind.template_fields:
            if name in self.raw:
                rendered[name] = self.render(self.raw[name], name)
        rendered.update(kind.extra_inputs(spec, self))
        return _normalised(rendered)

    async def execute(self, kind: ActivityKind, spec: KindSpec) -> Any:
        """Result of this activity: replayed, mocked, or run live (journaled)."""
        if self.run.divergence is not None:  # a replay diverged and something caught that: nothing runs any more
            raise ReplayDivergence(self.run.divergence)
        if self.stopped and self.key not in self.run.recorded and self.key not in self.run.started:
            self.run.stop_here()  # past its frame's journal: nothing new starts (§3.10)
        try:
            inputs = self.inputs(kind, spec)
            timeout = self.frame.duration(spec.timeout, f"{self.path}.do.timeout")
        except CodeError as exc:
            raise ActivityError("template_failed", exc.message, **exc.location) from exc
        except _NotJson as exc:
            raise ActivityError("template_failed", f"{self.path}: the inputs are not JSON data: {exc}") from exc
        input_hash = fingerprint(self.frame.tokenized(inputs))  # resource values as tokens: forks replay
        base = {"kind": kind.key, "path": self.path, "state": self.state, "input_hash": input_hash}
        self._base = base
        self._inputs = inputs  # kept in the done or error row: what the activity was given (§6)

        recorded = self.run.recorded.get(self.key)
        if recorded is not None:
            try:
                out = self._replay(recorded, base)
            except ActivityError:  # what its children hold, held all the same: the frame goes on with it
                self._replayed_below(None)
                raise
            self._replayed_below(None)
            if not self.meta.get("mocked"):  # what it holds outside the journal, held again (acquire)
                kind.replayed(spec, self, out)
            return out
        # In flight when its frame ended (the run's terminate, or the end a fail-fast join gave it): a leaf ends
        # here -- also before it would end interrupted. A composite goes on, so its children replay into their
        # frames' ends first; then it ends without an outcome of its own (§3.10).
        leaf = not (kind.nested_one or kind.nested_map or kind.submachine(spec))
        started = self.run.started.get(self.key)
        begun_row = (started or {}).get("data") or {}
        # A crash in a retry's backoff: the failed attempt had ended and the next one was not due yet, so nothing
        # was in flight -- the next attempt starts when the rest of its backoff has passed. A composite stopped
        # there ends at once like a leaf: the frames of the failed attempt had ended, the next attempt never began.
        due = begun_row.get("backoff_until")
        if self.stopped and (leaf or due is not None):
            self.run.stop_here()
        # A crash is not a failed attempt: a resumed activity continues the attempt it was in, so the
        # children that attempt finished replay under their keys and the retry budget stays the same.
        first = max(1, int(begun_row.get("attempt") or 1))
        if started is not None and not leaf and first > 1:  # the attempts before it replay no child: counted here
            self.run.add_cost(*(row["data"] or {} for _, row in self._replayed_below(first)))
        await self.run.went_live(self.key)

        self.run.started.pop(self.key, None)
        if started is not None:
            self._check(begun_row, base, "started")
            # what the attempts before the crash spent and their request ids: the outcome's row keeps counting them
            self.meta.update({k: v for k, v in (begun_row.get("meta") or {}).items() if k in _CARRIED_META})
            if due is None and not self.stopped and not kind.idempotent(spec):
                if not leaf:  # the frames it had opened end first: their finally and close activities run
                    await self._end_in_flight(kind, spec, started)
                failure = ActivityError("interrupted", f"{self.path}: was running when the run stopped; not "
                                        "started again because it is not idempotent", data=inputs)
                self._journal("error", {**base, "error": failure.as_dict(), "meta": self.meta})
                raise failure

        budgeted = kind.spends and leaf and not self.finalizer and not self.literal  # limits.max_cost, max_output_tokens
        over = self._over_budget() if budgeted else None
        if over is not None:
            self._journal("error", {**base, "error": over.as_dict(), "meta": self.meta})
            raise over

        # A mock answers each attempt in place of the kind -- a $error goes through retry as a live failure would
        # (no backoff), and the next attempt takes the next $visits entry

        mocked = self.path in self.run.mocks
        if mocked:
            self.meta["mocked"] = True
        elif self.run.mock_only and kind.external:
            failure = ActivityError("unmocked", f"{self.path}: mock-only run and no mock for this {kind.key} activity")
            self._journal("error", {**base, "error": failure.as_dict(), "meta": self.meta})
            raise failure

        attempts = spec.retry.attempts if spec.retry else 1
        if begun_row.get("failures"):  # the attempts that failed before the crash
            self.meta["failures"] = list(begun_row["failures"])
        task = asyncio.current_task()
        cancels = task.cancelling() if task is not None else 0
        try:
            return await self._attempts(kind, spec, base, inputs, timeout, first, attempts, leaf, mocked, budgeted, due)
        except asyncio.CancelledError:
            # cut outside an attempt -- in a retry's backoff, waiting for its turn: what the attempts before spent
            # counts all the same. Its row says so already (the backoff's started row carries it) and is left as it
            # is -- a halt's resume reads that row for where it stopped; only the limits learn it here. Cut inside an
            # attempt, _keep_spent set it aside already: only the rest goes
            if self._cancelled_by_run(task, cancels):
                self._set_aside_rest()
            raise

    async def _attempts(self, kind: ActivityKind, spec: KindSpec, base: dict[str, Any], inputs: dict[str, Any],
                        timeout: Optional[float], first: int, attempts: int, leaf: bool, mocked: bool,
                        budgeted: bool, due: Any) -> Any:
        """The attempts of a live activity (execute): retries, backoff, its turn, its outcome journaled."""
        begun = time.monotonic()
        failure: Optional[ActivityError] = None
        for attempt in range(first, max(attempts, first) + 1):
            if budgeted and attempt > first:  # a retry spends again: none starts past limits.max_cost
                failure = self._over_budget()
                if failure is not None:
                    break
            if due is not None:  # the retry's backoff (a resume waits out what is left)
                await asyncio.sleep(max(0.0, float(due) - time.time()))
            failure = None  # this attempt's outcome, not the previous attempt's
            self.attempt = attempt
            self.meta["attempts"] = attempt
            # limits.concurrency: a leaf waits for its turn before it counts as started -- one that waited when the
            # run stopped was not in flight. sg.tool() of a call that holds a turn takes none: it would wait for it
            gate = self.run.gate if leaf and not self.literal and not mocked and not kind.waits else None
            # the backend's own place for an agent (a dynamic workflow's process-wide bound); None for every other run
            offer = getattr(self.run.backend, "agent_place", None)
            runs_agent = kind.key == "agent" or (kind.key == "decide" and bool(getattr(spec, "by", None)))
            place = offer() if offer is not None and runs_agent and leaf and not mocked else None
            if gate is not None:
                await gate.acquire()
            if place is not None:
                try:
                    await place.acquire()
                except BaseException:
                    if gate is not None:
                        gate.release()
                    raise
            if budgeted:
                # limits.max_cost again, now it holds its turn: the items of a wide map all passed the check above at
                # a cost of 0, then waited here while the first ones spent
                failure = self._over_budget()
                if failure is not None:
                    if place is not None:
                        place.release()
                    if gate is not None:
                        gate.release()
                    break
            if not mocked:  # a mock's answer is at hand: nothing is in flight that a crash could cut
                try:
                    self._journal("started", {**base, "attempt": attempt, "inputs": inputs, **self._carried()})
                except BaseException:  # the row did not land (a locked database): the turn is given back all the same
                    if place is not None:
                        place.release()
                    if gate is not None:
                        gate.release()
                    raise
            counted = leaf and not mocked and not kind.waits  # a slot wait: the run shows waiting (take_slot)
            if counted:
                self.run.busy += 1
                self.run.refresh_status()
            out: Any = None
            task = asyncio.current_task()
            cancels = task.cancelling() if task is not None else 0
            try:
                out = _normalised(self._mocked() if mocked else await self._attempt(kind, spec, timeout))
            except (ReplayDivergence, RunAbort):
                raise
            except asyncio.CancelledError:
                if self._cancelled_by_run(task, cancels):
                    self._keep_spent(base, attempt, inputs)
                    raise
                failure = ActivityError(_FAILED_TYPE.get(kind.key, "activity_failed"),
                                        f"{self.path}: cancelled from inside, not by the run")
            except ActivityError as exc:
                failure = exc
            except _AttemptTimeout:
                failure = ActivityError("timeout", f"{self.path}: no result within {timeout:g}s")
            except _NotJson as exc:
                failure = ActivityError("not_serialisable",
                                        f"{self.path}: the {'mock' if mocked else 'result'} is not JSON data: {exc}")
            except CodeError as exc:
                failure = ActivityError("template_failed", exc.message, **exc.location)
            except Exception as exc:  # a kind or backend bug -- or a call's own code -- must not kill the run silently
                logger.warning("stategraph: run %s: %s raised", self.run.id, self.path, exc_info=exc)
                failure = ActivityError(_FAILED_TYPE.get(kind.key, "activity_failed"), f"{type(exc).__name__}: {exc}",
                                        data={"traceback": _traceback(exc)}, where=self.path)
                failure.original = exc  # an in-process caller of the run gets it back (RunContext.exception)
            finally:
                if counted:
                    self.run.busy -= 1
                    self.run.refresh_status()
                if place is not None:
                    place.release()
                if gate is not None:
                    gate.release()
            if self.run.divergence is not None:  # a replay below it diverged and something caught that (a call's
                raise ReplayDivergence(self.run.divergence)  # except, a join's cleanup): it ends the run all the same
            if self.stopped and (failure is not None or not leaf):  # the run ended while it ran (a composite's
                self._keep_spent(base, attempt, inputs)  # no outcome: what it spent stays in its started row
                self.run.stop_here()    # children replayed into their ends, a leaf
            if failure is None:                                     # failed with it): not its outcome (§3.10)
                self.meta["duration_s"] = 0.0 if mocked else round(time.monotonic() - begun, 3)
                self._journal("done", {**base, "out": out, "meta": self.meta})
                return out
            if attempt < attempts and spec.retry is not None and self._retryable(spec.retry, failure):
                # the attempts that failed stay readable: the row keeps only the last outcome -- with the request
                # and instance each one ran under (the message debugger finds its LLM calls by the request id)
                self.meta.setdefault("failures", []).append(
                    {"attempt": attempt, "type": failure.type, "message": failure.message[:_CAUSE_CHARS],
                     **{name: self.meta[name] for name in ("request_id", "instance_id") if self.meta.get(name)}})
                if mocked:
                    due = time.time()
                    continue
                # journaled with the time the next attempt is due: a resume within the backoff does not take this
                # attempt for in flight (interrupted, or run once more over the budget) (§5.5)
                due = time.time() + spec.retry.delay(attempt)
                self._journal("started", {**base, "attempt": attempt + 1, "inputs": inputs, "backoff_until": due,
                                          **self._carried()})
                continue
            break
        assert failure is not None
        self.meta["duration_s"] = 0.0 if mocked else round(time.monotonic() - begun, 3)
        self._journal("error", {**base, "error": failure.as_dict(), "meta": self.meta})
        raise failure

    def _replayed_below(self, before: Optional[int]) -> list[tuple[str, dict[str, Any]]]:
        """The recorded rows below this composite that no child replays -- every one (``before`` None: it is
        answered from the journal), or those of the attempts before ``before`` (it resumes in that attempt) -- in
        journal order. What they hold outside the journal is held again (``ActivityKind.replayed_below``: an
        acquire's slot, a release given back), unless it lay in a frame of its own (``m``), which has ended."""
        below = f"{self.key}/"
        rows = []
        for name, row in self.run.recorded.items():
            if not name.startswith(below):
                continue
            rest = name[len(below):]
            if before is not None:
                match = _ATTEMPT.match(rest)
                if (int(match[1]) if match else 1) >= before:
                    continue
            rows.append((name, row))
            data = row.get("data") or {}
            kind = REGISTRY.get(str(data.get("kind")))
            if (kind is not None and row["status"] == "done" and not (data.get("meta") or {}).get("mocked")
                    and "m" not in rest.split("/")):
                kind.replayed_below(name, data, self)
        return rows

    def _over_budget(self) -> Optional[ActivityError]:
        """budget_exceeded once the run's spend -- with what this activity's attempts so far spent, not counted into
        run.cost / run.output_tokens before its outcome is journaled -- reached limits.max_cost or
        limits.max_output_tokens; None below both, or without either."""
        limits = self.run.machine.spec.limits
        cost, tokens = self.run.spent  # set aside too: what a stop cut, what a rewind set aside (cost_aside)
        spent = cost + self.run.cost_of(self.meta)
        if limits.max_cost is not None and spent >= limits.max_cost:
            return ActivityError("budget_exceeded", f"{self.path}: the run has spent {spent:.4f} USD, "
                                 f"limits.max_cost is {limits.max_cost:g}")
        produced = tokens + self.run.output_tokens_of(self.meta)
        if limits.max_output_tokens is not None and produced >= limits.max_output_tokens:
            return ActivityError("budget_exceeded", f"{self.path}: the run has produced {produced} output tokens, "
                                 f"limits.max_output_tokens is {limits.max_output_tokens}")
        return None

    def _mocked(self) -> Any:
        """This attempt's mock: its answer, or its ``$error`` raised (a non-JSON mock fails not_serialisable)."""
        mock = self.run.mock_for(self.path)
        if isinstance(mock, dict) and "$error" in mock:
            error = mock["$error"] if isinstance(mock["$error"], dict) else {"message": str(mock["$error"])}
            raise ActivityError(str(error.get("type", "mocked")), str(error.get("message", "mocked error")),
                                error.get("data"))
        return mock

    def _carried(self) -> dict[str, Any]:
        """What a ``started`` row carries over a crash: the failed attempts, and what they and this one have spent
        (``meta``: cost, tokens, request ids) -- the run's spend counts every row's meta, an outcome's row then
        takes it over."""
        meta = {k: v for k, v in self.meta.items() if k in _CARRIED_META}
        return {**({"failures": list(self.meta["failures"])} if self.meta.get("failures") else {}),
                **({"meta": meta} if meta else {})}

    def _keep_spent(self, base: dict[str, Any], attempt: int, inputs: dict[str, Any]) -> None:
        """A stop cut this attempt: what it spent so far goes into its ``started`` row -- the run's spend counts it,
        and a resume that runs it again adds to it. Never raises: the stop goes on."""
        if not any(k in self.meta for k in ("cost", "tokens", "cost_unpriced_calls")):
            return
        try:
            self._journal("started", {**base, "attempt": attempt, "inputs": inputs, **self._carried()})
        except (Exception, asyncio.CancelledError):  # a lost run refuses every write: the cut cost goes uncounted
            logger.debug("stategraph: run %s: the spend of the cut %s not kept", self.run.id, self.path, exc_info=True)
            return
        self._set_aside_rest()  # no outcome carries it: the limits count it now (a resume: from this row)

    def _set_aside_rest(self) -> None:
        """Set aside what this activity's meta spent and is not set aside yet: a cut seen twice (inside its attempt,
        then around them) counts once; an outcome later takes it back over (_journal)."""
        cost, tokens = self.run.cost_of(self.meta), self.run.output_tokens_of(self.meta)
        self.run.move_aside(cost - self._aside[0], tokens - self._aside[1])
        self._aside = (cost, tokens)

    def _cancelled_by_run(self, task: Optional["asyncio.Task[Any]"], cancels: int) -> bool:
        """Whether a CancelledError out of an attempt is the run's (§5.8): this task is being cancelled (a
        terminate, a halt, limits.timeout, a timeout above it, a fail-fast sibling's failure), or the run is
        ending or its token was cancelled from outside. Any other -- a future some library cancelled -- is the
        activity's own failure. (A frame the engine stops below a composite raises past the attempt anyway: the
        ``stopped`` check after it; so does a run that lost its lease, whose every write raises.)"""
        return (task is not None and task.cancelling() > cancels) or self.run.ending or self.run.token_cancelled()

    def _retryable(self, retry: Any, failure: ActivityError) -> bool:
        """Whether a retry may run this activity again (docs/stategraph_design.md §2.5 retry, §5.5).

        A composite's retry runs its children again. So it never retries once an activity below it raised
        ``interrupted`` -- whether that error ended the attempt, was handled inside a submachine, or was
        journaled next to another branch's failure: that activity is not idempotent and must not start twice
        (every interrupted error is journaled, so ``run.interrupted`` holds it). One in a frame's ending does
        not count: that finally or close belongs to the attempt's frame, and the retry's frame has its own.
        A cause the retry rules exclude (NOT_RETRIED) in the chain of unhandled causes excludes the composite
        too, unless ``errors`` names it.
        """
        if failure.extra.get("resumable"):  # it would be refused again: a resume runs it, once that changed (§5.3)
            return False
        below = f"{self.key}/"
        if any(key.startswith(below) and not _IN_ENDING.search(key[len(below):]) for key in self.run.interrupted):
            return False
        chain = [failure.type]
        cause: Any = failure.cause
        while isinstance(cause, dict) and len(chain) < 50:
            chain.append(str(cause.get("type")))
            cause = cause.get("cause")
        named = set(retry.errors or ())
        if any(t in NOT_RETRIED and t not in named for t in chain[1:]):
            return False
        return retry.retries(failure.type)

    async def _end_in_flight(self, kind: ActivityKind, spec: KindSpec, started: dict[str, Any]) -> None:
        """A composite in flight at the crash that is not started again (not idempotent): the frames below it
        replay into their ends -- never live -- so their finally and close activities run before it raises
        interrupted (§3.10, §5.5). A cancel from outside meanwhile ends it without that outcome."""
        self.attempt = max(1, int((started.get("data") or {}).get("attempt") or 1))
        self.ending_only = True
        task = asyncio.current_task()
        cancels = task.cancelling() if task is not None else 0
        try:
            await kind.run(spec, self)
        except asyncio.CancelledError:  # the frames below reached their ends -- unless the cancel came from outside
            if not self.run.may_finalize() or self.run.ending or (task is not None and task.cancelling() > cancels):
                raise
        except (ReplayDivergence, RunAbort):
            raise
        except Exception:  # a failure journaled below it: it ends interrupted all the same
            pass
        if self.run.divergence is not None:  # a replay below it diverged (a join took it in): not interrupted
            raise ReplayDivergence(self.run.divergence)

    def cut(self, error: dict[str, Any]) -> None:
        """The cancel bound stopped this finally or close activity: that is its outcome, journaled so a resume
        does not start it again (§3.10)."""
        if self._base is not None and self.key not in self.run.recorded:
            self._journal("error", {**self._base, "error": error, "meta": self.meta})

    async def _attempt(self, kind: ActivityKind, spec: KindSpec, timeout: Optional[float]) -> Any:
        """One attempt; only THIS attempt's own deadline counts as a timeout (a kind's TimeoutError does not)."""
        if not timeout:
            return await kind.run(spec, self)
        deadline = asyncio.timeout(timeout)
        mine = (asyncio.current_task(), deadline)  # its cancel is the engine's (RunContext.halted_from_outside)
        self.run.timeouts.append(mine)
        try:
            async with deadline:
                return await kind.run(spec, self)
        except TimeoutError:
            if deadline.expired():
                raise _AttemptTimeout() from None
            raise
        finally:
            self.run.timeouts.remove(mine)

    def _check(self, data: dict[str, Any], base: dict[str, Any], what: str) -> None:
        for field in ("kind", "path", "input_hash"):
            if data.get(field) != base[field]:
                differ = ""
                if field == "input_hash" and isinstance(data.get("inputs"), dict):  # rows since G1 keep them
                    found = _differences(self.frame.tokenized(data["inputs"]), self.frame.tokenized(self._inputs))
                    differ = f"; {'; '.join(found)}" if found else ""
                raise self.run.diverged(
                    f"journal key {self.key}: the {what} row has {field}={data.get(field)!r}, the machine now "
                    f"has {field}={base[field]!r}{differ} (a guard, action or template is not deterministic, or "
                    "the definition changed)")

    def _replay(self, row: dict[str, Any], base: dict[str, Any]) -> Any:
        data = row.get("data") or {}
        self._check(data, base, "recorded")
        self.meta.update(data.get("meta") or {})
        self.meta["replayed"] = True
        self.run.count_recorded(self.key)
        if row["status"] == "error":
            raise ActivityError.from_dict(data.get("error") or {})
        return data.get("out")

    def _journal(self, status: str, data: dict[str, Any]) -> None:
        if status in ("done", "error") and getattr(self, "_inputs", None) is not None:
            data = {**data, "inputs": self._inputs}  # the started row had them; its end replaces it
        self.run.write("activity", self.key, state=self.state, status=status, data=data)  # fenced by the owner
        if status in ("done", "error"):
            self.run.note_outcome(self.key, status, data)
            self.run.add_cost(data)
            if self._aside != (0.0, 0):  # an outcome after a cut after all (a finally the cancel bound cut): its row
                self.run.move_aside(-self._aside[0], -self._aside[1])  # carries the spend, counted above -- once
                self._aside = (0.0, 0)


class _ThreadedTool(collections.abc.Coroutine):
    """``sg.tool()`` called by a sync function in its worker thread: it can only run once the function returns it
    (CallKind and a check await what they get back). Using its value raises -- it is no answer yet; run anywhere
    but on the run's loop, it raises there."""

    def __init__(self, run: Any, where: str):
        self._run = run
        self._coro: Any = None
        self.where = where

    def _started(self) -> Any:
        if self._coro is None:
            self._coro = self._run()
        return self._coro

    def __await__(self) -> Any:
        return self._started().__await__()

    def send(self, value: Any) -> Any:
        return self._started().send(value)

    def throw(self, *args: Any) -> Any:
        return self._started().throw(*args)

    def close(self) -> None:
        if self._coro is not None:
            self._coro.close()

    def __repr__(self) -> str:
        return f"<{self.where}: not run yet>"

    def _misused(self, *_: Any, **__: Any) -> Any:
        raise RuntimeError(f"{self.where} in a sync function is no answer yet: it runs only when the function "
                           "returns it -- to use its value, make the function async")

    __bool__ = __eq__ = __ne__ = __str__ = __iter__ = __len__ = __contains__ = __getitem__ = _misused
    __hash__ = None  # type: ignore[assignment]


def _differences(then: Any, now: Any, path: str = "inputs", limit: int = 3) -> list[str]:
    """The first ``limit`` paths where two input values differ, each with both values (cut to 80 characters)."""
    if isinstance(then, dict) and isinstance(now, dict):
        found: list[str] = []
        for key in sorted(set(then) | set(now), key=str):
            if then.get(key, _GONE) != now.get(key, _GONE):
                found += _differences(then.get(key, _GONE), now.get(key, _GONE), f"{path}.{key}", limit - len(found))
            if len(found) >= limit:
                break
        return found[:limit]
    if isinstance(then, list) and isinstance(now, list) and len(then) == len(now):
        found = []
        for index, (old, new) in enumerate(zip(then, now)):
            if old != new:
                found += _differences(old, new, f"{path}[{index}]", limit - len(found))
            if len(found) >= limit:
                break
        return found[:limit]

    def shown(value: Any) -> str:
        text = "(none)" if value is _GONE else json.dumps(value, ensure_ascii=False, default=str)
        return text if len(text) <= 80 else f"{text[:77]}..."

    return [f"{path} was {shown(then)}, now {shown(now)}"]


_GONE = object()


def _traceback(exc: BaseException, frames: int = 8) -> str:
    """The last frames of an exception's traceback (where a call's own code raised), at most 2000 characters --
    from the end, where the error line is (a tool answer cuts longer texts at their end)."""
    text = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__, limit=-frames))
    return text[-2000:]


__all__ = ["ActivityRun", "ReplayDivergence", "RunAbort", "Scope"]
