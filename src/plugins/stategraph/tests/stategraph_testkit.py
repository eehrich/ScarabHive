"""Shared helpers for the stategraph engine tests (not a test module itself).

Machines are written as YAML text and loaded through the real loader
(``SnapshotSources`` -- the same source class a resume reads a run's
definition snapshot with), validated by the real validator, compiled and run
by the real ``RunManager`` on a ``RunStore`` under ``tmp_path``. The only
thing replaced is the boundary behind the engine: ``FakeBackend`` answers
agent, tool and decision calls, records every call and can hold a call open
on an ``asyncio.Event`` so a test can "crash" the process mid-activity.

Imported as ``plugins.stategraph.tests.stategraph_testkit`` so the module has
one identity whatever import mode pytest runs in.
"""

from __future__ import annotations

import asyncio
import textwrap
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Optional

from plugins.stategraph.engine.journal import RunStore
from plugins.stategraph.engine.runner import RunManager
from plugins.stategraph.kinds import ActivityError
from plugins.stategraph.model.loader import MachineTree, Problem, SnapshotSources, load_tree
from plugins.stategraph.model.validate import ConfigCheck, validate_tree

ROOT = "m.yaml"


# ------------------------------------------------------------------ loading

def sources(files: dict[str, str]) -> SnapshotSources:
    return SnapshotSources({path: textwrap.dedent(text) for path, text in files.items()})


def load(files: dict[str, str], root: str = ROOT, *, execute: bool = False) -> MachineTree:
    return load_tree(root, sources(files), execute_python=execute)


def validate(files: dict[str, str], root: str = ROOT, config_check: Optional[ConfigCheck] = None) -> MachineTree:
    return validate_tree(load(files, root), config_check)


def errors(tree: MachineTree) -> list[Problem]:
    return [p for p in tree.problems if p.level == "error"]


def found(tree: MachineTree, code: str) -> list[Problem]:
    return [p for p in tree.problems if p.code == code]


def runnable(files: dict[str, str], root: str = ROOT) -> MachineTree:
    """What service.start_run does: validate without executing Python, then load for running."""
    checked = validate(files, root)
    assert not errors(checked), f"fixture machine does not validate: {[p.as_dict() for p in errors(checked)]}"
    return load(files, root, execute=True)


# ------------------------------------------------------------------ backend

class FakeBackend:
    """Answers agent/tool/decision calls from per-state-path handlers and records every call.

    A handler is a value, an exception instance (raised), or a callable taking
    the call record and returning either of those (sync or async).
    """

    def __init__(self, handlers: Optional[dict[str, Any]] = None, template_vars: Optional[dict[str, dict]] = None):
        self.handlers: dict[str, Any] = dict(handlers or {})
        self.template_vars = dict(template_vars or {})
        self.calls: list[dict[str, Any]] = []
        self.cancelled: list[str] = []

    def count(self, path: str) -> int:
        return sum(1 for call in self.calls if call["path"] == path)

    async def _answer(self, kind: str, act: Any, **kw: Any) -> Any:
        call = {"kind": kind, "path": act.path, "key": act.key, "run_id": act.run_id, **kw}
        self.calls.append(call)
        handler = self.handlers.get(act.path)
        if handler is None:
            raise ActivityError("agent_failed", f"FakeBackend has no answer for {act.path}")
        try:
            value = handler(call) if callable(handler) else handler
            if asyncio.iscoroutine(value):
                value = await value
        except asyncio.CancelledError:
            self.cancelled.append(act.path)
            raise
        if isinstance(value, BaseException):
            raise value
        return value

    async def agent_create(self, act: Any, *, agent: str, task: str, sam: Optional[str], advanced: bool,
                           vars: dict[str, Any]) -> tuple[Any, Optional[str]]:
        answer = await self._answer("agent_create", act, agent=agent, task=task, vars=dict(vars))
        return answer, f"inst-{act.path}"

    async def agent_continue(self, act: Any, *, instance_id: str, message: str, sam: Optional[str],
                             advanced: bool, vars: dict[str, Any]) -> Any:
        return await self._answer("agent_continue", act, instance_id=instance_id, message=message, vars=dict(vars))

    async def call_tool(self, act: Any, *, tool: str, args: dict[str, Any]) -> Any:
        return await self._answer("call_tool", act, tool=tool, args=dict(args))

    async def decide(self, act: Any, *, questions: dict[str, Any], input: Any, profile: Optional[str]) -> Any:
        return await self._answer("decide", act, questions=questions, input=input, profile=profile)

    def agent_template_vars(self, agent: str) -> dict[str, Any]:
        return dict(self.template_vars.get(agent, {}))


def held(gate: asyncio.Event, value: Any = "released") -> Callable[[dict[str, Any]], Any]:
    """A handler that blocks until ``gate`` is set (a process 'crash' cancels it meanwhile)."""

    async def handler(call: dict[str, Any]) -> Any:
        await gate.wait()
        return value

    return handler


def sequence(*values: Any) -> Callable[[dict[str, Any]], Any]:
    """A handler answering the n-th call with the n-th value (the last one repeats)."""
    answers = list(values)
    seen = {"n": 0}

    def handler(call: dict[str, Any]) -> Any:
        index = min(seen["n"], len(answers) - 1)
        seen["n"] += 1
        return answers[index]

    return handler


# ------------------------------------------------------------------ running

async def until(condition: Callable[[], Any], timeout: float = 5.0, what: str = "condition") -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not condition():
        if loop.time() > deadline:
            raise AssertionError(f"timed out after {timeout}s waiting for {what}")
        await asyncio.sleep(0.01)


async def settle(manager: RunManager, run_id: str, timeout: float = 5.0) -> dict[str, Any]:
    """The run's row once its task has ended (not merely paused or waiting)."""
    live = manager.live.get(run_id)
    if live is not None:
        await asyncio.wait_for(asyncio.shield(live.task), timeout)
    row = manager.store.get_run(run_id)
    assert row is not None, f"no row for run {run_id}"
    return row


class Harness:
    """One RunStore under tmp_path and the RunManagers (= processes) that use it."""

    def __init__(self, tmp_path: Path):
        self.store = RunStore(tmp_path / "runs.db")
        self.managers: list[RunManager] = []

    def manager(self) -> RunManager:
        manager = RunManager(self.store)
        self.managers.append(manager)
        return manager

    async def run(self, files: dict[str, str], *, root: str = ROOT, timeout: float = 5.0,
                  **start: Any) -> dict[str, Any]:
        """Start a machine in a fresh manager and return its final row."""
        manager = self.manager()
        run_id = await manager.start(runnable(files, root), **start)
        return await settle(manager, run_id, timeout)

    async def close(self) -> None:
        for manager in self.managers:
            await manager.shutdown()
        self.store.close()


def activity_rows(store: RunStore, run_id: str) -> dict[str, dict[str, Any]]:
    return {row["key"]: row for row in store.rows(run_id, kinds=("activity",))}


async def crash_in(harness: Harness, files: dict[str, str], path: str, answers: dict) -> tuple[str, FakeBackend]:
    """Start the machine, 'crash' the process while ``path``'s backend call is in flight."""
    backend = FakeBackend({**answers, path: held(asyncio.Event())})
    manager = harness.manager()
    run_id = await manager.start(runnable(files), backend=backend)
    await until(lambda: backend.count(path) == 1, what=f"{path} in flight")
    await manager.shutdown()
    row = harness.store.get_run(run_id)
    assert row["status"] == "interrupted", row["status"]
    assert activity_rows(harness.store, run_id)  # fixture: something was journaled before the crash
    return run_id, backend


async def resume(harness: Harness, run_id: str, backend: FakeBackend, timeout: float = 5.0) -> dict:
    """Resume the run in a NEW manager (= another process) and return its final row."""
    manager = harness.manager()
    await manager.resume(run_id, backend=backend)
    return await settle(manager, run_id, timeout)


# ------------------------------------------------------------------ leases

def utc_at(seconds: float) -> str:
    """An ISO timestamp ``seconds`` from now, in the store's format (a lease end, a sweep's ``now``)."""
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat(timespec="milliseconds")


def seed_run(store: RunStore, run_id: str, files: dict[str, str], *, owner: str, lease: float,
             status: str = "running", run_key: Optional[str] = None) -> None:
    """A run row as another process left it: ``owner`` holds it until ``lease`` seconds from now."""
    snapshot = runnable(files).snapshot()
    store.create_run(run_id, "m", snapshot, params={}, mocks={}, owner=owner, lease_until=utc_at(lease),
                     status=status, run_key=run_key, session_id=f"sg_{run_id}")


def tool_config(tmp_path: Path, **extra: Any) -> Any:
    """The stategraph instance's flat config, all storage under tmp_path."""
    from agent_system.config.models import ToolServerConfig

    machines = tmp_path / "machines"
    machines.mkdir(exist_ok=True)
    values = {"type": "stategraph", "enabled": True, "machine_dirs": [str(machines)],
              "writable_machine_dirs": [str(machines)], "runs_db": str(tmp_path / "runs.db")}
    values.update(extra)
    return ToolServerConfig(**values)  # extra="allow": flat plugin keys, as plugins.yaml delivers them


# ------------------------------------------------------------------ environment

#: The root conftest's autouse ``reset_global_state`` imports ``agent_system.app``;
#: under Python 3.14 FastAPI 0.115 calls the deprecated ``asyncio.iscoroutinefunction``
#: while building its routes, and ``filterwarnings = error`` turns that into a setup
#: error for EVERY test of the repo on this machine. Ignored for exactly this message
#: -- it is not stategraph's warning, and nothing else is silenced.
FASTAPI_PY314 = "ignore:'asyncio.iscoroutinefunction' is deprecated:DeprecationWarning"
