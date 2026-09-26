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
import copy
import json
import time
import types
from typing import TYPE_CHECKING, Any, Optional

from plugins.stategraph.kinds import ActivityError, ActivityKind, KindSpec, parse_activity
from plugins.stategraph.model.code import CodeError, Scope, fingerprint, jsonable, plain
from plugins.stategraph.model.spec import NOT_RETRIED, parse_duration

if TYPE_CHECKING:
    from .interpreter import Frame

_FAILED_TYPE = {"agent": "agent_failed", "tool": "tool_failed", "decide": "decision_failed", "call": "call_failed"}


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
                 extra_scope: Optional[dict[str, Any]] = None, visit: int = 1):
        self.frame = frame
        self.run = frame.run
        self.state = state
        self.key = key
        self.path = path
        self.raw = raw
        self.extra_scope = jsonable(extra_scope or {})
        self.visit = visit
        self.attempt = 1
        self.meta: dict[str, Any] = {}
        self._detached: Optional[dict[str, Any]] = None

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
    def default_sam(self) -> Optional[str]:
        return self.frame.machine.spec.sam or self.run.default_sam

    @property
    def cancellation_token(self) -> Any:
        return self.run.token

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
        scope.update(bind)
        return scope

    def frozen_scope(self) -> Any:
        return types.SimpleNamespace(**self.scope())

    def render(self, value: Any, where: str) -> Any:
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
        return self.run.next_request_id()

    def child_key(self, label: str) -> str:
        """Journal key of a child in THIS attempt (attempt 1 keeps the plain grammar)."""
        return f"{self.key}/{label}" if self.attempt == 1 else f"{self.key}/a{self.attempt}/{label}"

    def recorded_error(self, label: str) -> Optional[ActivityError]:
        row = self.run.recorded.get(self.child_key(label))
        if row and row["status"] == "error":
            return ActivityError.from_dict((row.get("data") or {}).get("error") or {})
        return None

    async def child(self, label: str, raw: dict[str, Any], extra_scope: Optional[dict[str, Any]] = None) -> Any:
        kind, spec = parse_activity(raw)
        shown = label.split(".", 1)[-1]
        sub = ActivityRun(self.frame, self.state, self.child_key(label), f"{self.path}/{shown}", raw=raw,
                          extra_scope={**self.extra_scope, **(extra_scope or {})}, visit=self.visit)
        return await sub.execute(kind, spec)

    async def run_submachine(self, alias: str, params: dict[str, Any]) -> Any:
        machine = self.frame.machine.imports.get(alias)
        if machine is None:
            raise ActivityError("config", f"{alias!r} is not an import of {self.frame.machine.id}")
        from .interpreter import Frame  # a submachine is a nested frame of the same run

        child = Frame(self.run, machine, params, prefix=self.child_key("m") + "/", path=self.path, parent=self.frame)
        result = await child.execute()
        if result.status != "succeeded":
            raise ActivityError("submachine_failed", f"{alias} ended in {result.final_state} ({result.status})",
                                data=result.output, cause=result.error)
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
        self.run.check_cancelled()
        try:
            inputs = self.inputs(kind, spec)
        except CodeError as exc:
            raise ActivityError("template_failed", exc.message) from exc
        except _NotJson as exc:
            raise ActivityError("template_failed", f"{self.path}: the inputs are not JSON data: {exc}") from exc
        input_hash = fingerprint(inputs)
        base = {"kind": kind.key, "path": self.path, "state": self.state, "input_hash": input_hash}

        recorded = self.run.recorded.get(self.key)
        if recorded is not None:
            return self._replay(recorded, base)

        started = self.run.started.pop(self.key, None)
        if started is not None:
            self._check(started.get("data") or {}, base, "started")
            if not kind.idempotent(spec):
                failure = ActivityError("interrupted", f"{self.path}: was running when the run stopped; not "
                                        "started again because it is not idempotent", data=inputs)
                self._journal("error", {**base, "error": failure.as_dict(), "meta": self.meta})
                raise failure

        mock = self.run.mock_for(self.path)
        if mock is not self.run.NO_MOCK:
            self.meta.update({"mocked": True, "attempts": 1, "duration_s": 0.0})  # read the same as a live run
            if isinstance(mock, dict) and "$error" in mock:
                error = mock["$error"] if isinstance(mock["$error"], dict) else {"message": str(mock["$error"])}
                failure = ActivityError(str(error.get("type", "mocked")), str(error.get("message", "mocked error")),
                                        error.get("data"))
                self._journal("error", {**base, "error": failure.as_dict(), "meta": self.meta})
                raise failure
            try:
                out = _normalised(mock)
            except _NotJson as exc:
                failure = ActivityError("not_serialisable", f"{self.path}: the mock is not JSON data: {exc}")
                self._journal("error", {**base, "error": failure.as_dict(), "meta": self.meta})
                raise failure from exc
            self._journal("done", {**base, "out": out, "meta": self.meta})
            return out

        if self.run.mock_only and kind.external:
            failure = ActivityError("unmocked", f"{self.path}: mock-only run and no mock for this {kind.key} activity")
            self._journal("error", {**base, "error": failure.as_dict(), "meta": self.meta})
            raise failure

        self.run.went_live()
        attempts = spec.retry.attempts if spec.retry else 1
        timeout = parse_duration(spec.timeout)
        # A crash is not a failed attempt: a resumed activity continues the attempt it was in, so the
        # children that attempt finished replay under their keys and the retry budget stays the same.
        first = max(1, int(((started or {}).get("data") or {}).get("attempt") or 1))
        leaf = not (kind.nested_one or kind.nested_map or kind.submachine(spec))
        begun = time.monotonic()
        failure: Optional[ActivityError] = None
        for attempt in range(first, max(attempts, first) + 1):
            failure = None  # this attempt's outcome, not the previous attempt's
            self.attempt = attempt
            self.meta["attempts"] = attempt
            self._journal("started", {**base, "attempt": attempt, "inputs": inputs})
            if leaf:
                self.run.busy += 1
                self.run.refresh_status()
            out: Any = None
            try:
                out = _normalised(await self._attempt(kind, spec, timeout))
            except (asyncio.CancelledError, ReplayDivergence, RunAbort):
                raise
            except ActivityError as exc:
                failure = exc
            except _AttemptTimeout:
                failure = ActivityError("timeout", f"{self.path}: no result within {timeout:g}s")
            except _NotJson as exc:
                failure = ActivityError("not_serialisable", f"{self.path}: the result is not JSON data: {exc}")
            except CodeError as exc:
                failure = ActivityError("template_failed", exc.message)
            except Exception as exc:  # a kind or backend bug must not kill the run silently
                failure = ActivityError(_FAILED_TYPE.get(kind.key, "activity_failed"), f"{type(exc).__name__}: {exc}")
            finally:
                if leaf:
                    self.run.busy -= 1
                    self.run.refresh_status()
            if failure is None:
                self.meta["duration_s"] = round(time.monotonic() - begun, 3)
                self._journal("done", {**base, "out": out, "meta": self.meta})
                return out
            if self.run.cancelled:  # it ended because the run was cancelled: not the activity's outcome
                raise asyncio.CancelledError()
            if attempt < attempts and spec.retry is not None and self._retryable(spec.retry, failure):
                await asyncio.sleep(spec.retry.delay(attempt))
                continue
            break
        assert failure is not None
        self.meta["duration_s"] = round(time.monotonic() - begun, 3)
        self._journal("error", {**base, "error": failure.as_dict(), "meta": self.meta})
        raise failure

    def _retryable(self, retry: Any, failure: ActivityError) -> bool:
        """Whether a retry may run this activity again (docs/stategraph_design.md §2.5 retry, §5.5).

        A composite's retry runs its children again. So it never retries once an activity below it raised
        ``interrupted`` -- whether that error ended the attempt, was handled inside a submachine, or was
        journaled next to another branch's failure: that activity is not idempotent and must not start twice
        (every interrupted error is journaled, so ``run.interrupted`` holds it). A cause the retry rules
        exclude (NOT_RETRIED) in the chain of unhandled causes excludes the composite too, unless ``errors``
        names it.
        """
        below = f"{self.key}/"
        if any(key.startswith(below) for key in self.run.interrupted):
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

    async def _attempt(self, kind: ActivityKind, spec: KindSpec, timeout: Optional[float]) -> Any:
        """One attempt; only THIS attempt's own deadline counts as a timeout (a kind's TimeoutError does not)."""
        if not timeout:
            return await kind.run(spec, self)
        deadline = asyncio.timeout(timeout)
        try:
            async with deadline:
                return await kind.run(spec, self)
        except TimeoutError:
            if deadline.expired():
                raise _AttemptTimeout() from None
            raise

    def _check(self, data: dict[str, Any], base: dict[str, Any], what: str) -> None:
        for field in ("kind", "path", "input_hash"):
            if data.get(field) != base[field]:
                raise ReplayDivergence(
                    f"journal key {self.key}: the {what} row has {field}={data.get(field)!r}, the machine now "
                    f"has {field}={base[field]!r} (a guard, action or template is not deterministic, or the "
                    "definition changed)")

    def _replay(self, row: dict[str, Any], base: dict[str, Any]) -> Any:
        data = row.get("data") or {}
        self._check(data, base, "recorded")
        self.meta.update(data.get("meta") or {})
        self.meta["replayed"] = True
        if row["status"] == "error":
            raise ActivityError.from_dict(data.get("error") or {})
        return data.get("out")

    def _journal(self, status: str, data: dict[str, Any]) -> None:
        self.run.write("activity", self.key, state=self.state, status=status, data=data)  # fenced by the owner
        if status in ("done", "error"):
            self.run.note_outcome(self.key, status, data)


__all__ = ["ActivityRun", "ReplayDivergence", "RunAbort", "Scope"]
