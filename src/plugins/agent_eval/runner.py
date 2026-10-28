"""Running a suite: every arm on every task, ``repeats`` times, each run a child process of its own.

The one place a run is executed -- the CLI calls ``run_suite``, the panel starts the CLI.

Order: for every repeat and task the arms start next to each other in a shuffled order, so they meet the same
provider load and routing (arms run hours apart measure the time of day). With ``chain`` the same holds, and the
next task of a chain starts only once every arm has finished the one before. A run that times out or crashes
scores 0 and counts -- that is what the arm did; a run never started (budget, stop, a failed setup) or one that
failed for the runner's own reasons does not count.
"""
from __future__ import annotations

import hashlib
import json
import os
import random
import shutil
import sqlite3
import sys
import threading
import traceback
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence

from agent_system.paths import DATA_DIR_ENV, PROJECT_ROOT, data_path

from . import store
from .checks import run_check
from .overlay import apply_overlay, layer_agents
from .proc import run_bounded, tail
from .suite import Suite, Task, load_suite

#: The session user of eval runs: the local operator's, the one the agent role gate trusts for a local entry
#: point (auth/agent_access.local_operator_trusted). With a fresh data directory its sessions are the run's own.
SESSION_USER = "cli_user"
#: Names a session store apart from the data directory (core/session_presence.sessions_dir).
SESSION_STORE_ENV = "AGENT_SESSION_STORAGE_PATH"
#: Characters of the final answer kept in a record (the session holds all of it).
_ANSWER_CHARS = 4000


class _Budget:
    """Dollars spent so far. Checked before a run starts, so up to ``parallel`` runs in flight may still go
    over it. A run whose cost could not be read cannot count: said once, since then the limit is not kept."""

    def __init__(self, limit: Optional[float], progress: Callable[[str], None]):
        self.limit = limit
        self.spent = 0.0
        self.progress = progress
        self.blind = False
        self.lock = threading.Lock()

    def exhausted(self) -> bool:
        with self.lock:
            return self.limit is not None and self.spent >= self.limit

    def add(self, cost: Optional[float]) -> None:
        with self.lock:
            if cost is not None:
                self.spent += cost
            elif self.limit is not None and not self.blind:
                self.blind = True
                self.progress("budget_usd cannot be kept: a run's cost could not be read "
                              "(is the context_usage_tracker plugin on?)")


def run_suite(suite_path: str | Path, run_id: Optional[str] = None, arms: Optional[List[str]] = None,
              progress: Callable[[str], None] = print) -> str:
    """Run the suite at *suite_path* (only *arms*, when given -- in the suite's order, its first arm stays the
    reference); returns the run id. Raises ValueError before anything runs when an arm cannot run at all."""
    suite = load_suite(suite_path)
    if arms:
        unknown = [a for a in arms if a not in suite.arms]
        if unknown:
            raise ValueError(f"unknown arms {unknown}; the suite has {list(suite.arms)}")
        suite.arms = {name: arm for name, arm in suite.arms.items() if name in arms}
    check_arms(suite)
    run_id = run_id or store.new_run_id()
    rdir = store.run_dir(run_id)
    rdir.mkdir(parents=True, exist_ok=False)
    shutil.copy2(suite_path, rdir / "suite.yaml")
    meta = {"run_id": run_id, "suite": suite.name, "suite_path": str(Path(suite_path).resolve()),
            "suite_sha": hashlib.sha256(Path(suite_path).read_bytes()).hexdigest(),
            "agent": suite.agent, "arms": list(suite.arms), "reference_arm": suite.reference_arm,
            "tasks": len(suite.tasks), "repeats": suite.repeats, "status": "running", "started": store.now(),
            "pid": os.getpid()}
    store.write_meta(run_id, meta)
    try:
        frozen = {t.id: _setup(suite, t, run_id, progress) for t in suite.tasks if t.setup}
        budget = _Budget(suite.budget_usd, progress)
        waves = _schedule(suite, run_id)
        progress(f"{run_id}: {sum(map(len, waves))} agent run(s), {len(suite.arms)} arm(s), {len(suite.tasks)} task(s)")
        with ThreadPoolExecutor(max_workers=suite.parallel) as pool:
            for wave in waves:
                list(pool.map(lambda job: _job_or_error(suite, run_id, job, frozen, budget, progress), wave))
        # Done only when every agent run counted: a baseline with tasks missing would make every later
        # comparison quietly run on the rest.
        missed = {r.get("status") for r in store.read_records(run_id)} & set(store.NOT_COUNTED)
        meta["status"] = "stopped" if "stopped" in missed else "incomplete" if missed else "done"
    except BaseException as e:
        meta["status"] = "failed"
        meta["error"] = f"{type(e).__name__}: {e}"
        raise
    finally:
        meta["finished"] = store.now()
        store.write_meta(run_id, meta)
    return run_id


def check_arms(suite: Suite) -> None:
    """Refuse a suite an arm of which cannot run: an agent that is not one, an overlay that does not validate,
    a profile the configuration does not know. Each would end every run of the arm before the agent saw its task
    -- and a crashed run scores 0, so the arm would read as worse instead of as broken."""
    from agent_system.config.settings import get_tool_server_config, load_settings
    from agent_system.servers.agent.entry import agent_entry_names

    config = load_settings()
    agents = set(agent_entry_names(config))
    profiles = set((config.llm_system.profiles or {}) if config.llm_system else {})

    def unknown_profiles(agent_config) -> List[str]:
        named = agent_config.llm_profile if isinstance(agent_config.llm_profile, list) else [agent_config.llm_profile]
        return [p for p in named if p not in profiles]

    problems = []
    for name, arm in suite.arms.items():
        agent = arm.agent or suite.agent
        if agent not in agents:
            problems.append(f"arm {name}: '{agent}' is not an agent of this configuration")
            continue
        stray = [other for other in arm.agents if other not in agents]
        if stray:
            problems.append(f"arm {name}: agents: {stray} are not agents of this configuration")
            continue
        try:
            layered = layer_agents(config, arm.agents)
        except Exception as e:  # noqa: BLE001 - an agent it names that is none, an overlay that does not validate
            problems.append(f"arm {name}: agents: {e}")
            continue
        for other in arm.agents:
            unknown = unknown_profiles(get_tool_server_config(other, layered).agent_config)
            if unknown:
                problems.append(f"arm {name}, agent {other}: unknown llm_profile {unknown}")
        base = get_tool_server_config(agent, layered).agent_config
        # What a run gets: the arm over the task's material, the task's first (as the child applies them).
        combos = [(f"arm {name}", {})] + [(f"arm {name}, task {t.id}", t.agent_config)
                                         for t in suite.tasks if t.agent_config]
        for who, task_overlay in combos:
            try:
                merged = apply_overlay(apply_overlay(base, task_overlay), arm.agent_config)
            except Exception as e:  # noqa: BLE001 - reported with the arm's (and the task's) name
                problems.append(f"{who}: the overlay does not validate: {e}")
                if not task_overlay:
                    break  # the arm's own overlay: every task would only repeat it
                continue
            unknown = unknown_profiles(merged)
            if unknown:
                problems.append(f"{who}: unknown llm_profile {unknown}")
    if problems:
        raise ValueError("; ".join(problems))


def _schedule(suite: Suite, run_id: str) -> List[List[Dict[str, Any]]]:
    """The runs as waves; a wave's runs may run at the same time, a wave starts when the one before is done.
    Without a chain one wave holds everything; with one, a wave is one task of one repeat for every arm."""
    rng = random.Random(run_id)
    arms = list(suite.arms)
    waves: List[List[Dict[str, Any]]] = []
    for rep in range(suite.repeats):
        for index, task in enumerate(suite.tasks):
            order = arms[:]
            rng.shuffle(order)
            wave = [{"arm": arm, "rep": rep, "task": task.id, "first": index == 0} for arm in order]
            if suite.chain or not waves:
                waves.append(wave)
            else:
                waves[0] += wave
    n = 0
    for wave in waves:
        for job in wave:
            n += 1
            job["n"] = n
            job["dir"] = (f"chain-{job['arm']}-r{job['rep'] + 1}" if suite.chain
                          else f"{n:04d}-{job['arm']}-r{job['rep'] + 1}")
    return waves


def _dirs(suite: Suite, run_id: str, *parts: str) -> tuple:
    """(workspace, data) of a run or a setup at *parts* in run *run_id*. With a fresh data directory the
    workspace is its ``workspace/`` -- where agents' file tools point by default (``data/workspace``), so an
    agent finds its task without a config of its own. With the shared one it sits beside it. Either way it is
    the directory the agent is started in, what its tools take for "."."""
    data = store.run_dir(run_id, *parts, "data")
    workspace = data / "workspace" if suite.data == "fresh" else store.run_dir(run_id, *parts, "workspace")
    return workspace, data


def _setup(suite: Suite, task: Task, run_id: str, progress: Callable[[str], None]) -> Optional[tuple]:
    """Run a task's setup once and freeze it: where it lies in the run, None when it failed or timed out (its
    runs are then not started)."""
    parts = ("setup", task.id)
    base = store.run_dir(run_id, *parts)
    workspace, data = _dirs(suite, run_id, *parts)
    data.mkdir(parents=True, exist_ok=True)
    _fill_workspace(suite, task, workspace)
    log = base / "setup.log"
    env = child_env(suite, data)
    # `python` in the command is the runner's own interpreter, also when its environment was never activated.
    # The setup's only: the agent under test gets the PATH it would get in production.
    env["PATH"] = os.pathsep.join(filter(None, [str(Path(sys.executable).parent), env.get("PATH")]))
    code = run_bounded(task.setup, cwd=workspace, log=log, timeout=suite.timeout_s, env=env, shell=True)
    if code != 0:
        why = f"timed out after {suite.timeout_s:.0f}s" if code is None else f"exit {code}"
        progress(f"setup of {task.id} failed ({why}), see {log}")
        return None
    return parts


def child_env(suite: Suite, data: Path) -> Dict[str, str]:
    """The environment of a run's child or a task's setup. With a fresh data directory everything the run
    stores goes there -- its sessions too, so a session store named by AGENT_SESSION_STORAGE_PATH (the API
    hands it to processes it starts) must not pull them back into the installation's own."""
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(filter(None, [str(PROJECT_ROOT / "src"), env.get("PYTHONPATH")]))
    if suite.data == "fresh":
        env[DATA_DIR_ENV] = str(data)
        env.pop(SESSION_STORE_ENV, None)
    return env


def _fill_workspace(suite: Suite, task: Task, workspace: Path) -> None:
    workspace.mkdir(parents=True, exist_ok=True)
    if task.workspace:
        shutil.copytree(suite.path(task.workspace), workspace, dirs_exist_ok=True)


def _job_or_error(suite: Suite, run_id: str, job: Dict[str, Any], frozen: Dict[str, Optional[tuple]],
                  budget: _Budget, progress: Callable[[str], None]) -> None:
    """One run; a failure of the runner itself (a locked file, a broken result) becomes that run's record --
    unscored, the arm did not cause it -- instead of ending the suite."""
    try:
        _job(suite, run_id, job, frozen, budget, progress)
    except Exception as e:  # noqa: BLE001 - recorded with its traceback
        store.append_record(run_id, {"run_id": run_id, "n": job["n"], "arm": job["arm"], "task": job["task"],
                                     "rep": job["rep"] + 1, "dir": f"runs/{job['dir']}", "scored": False,
                                     "status": "error", "detail": f"{type(e).__name__}: {e}",
                                     "errors": [traceback.format_exc()[-2000:]]})
        progress(f"#{job['n']} {job['arm']}/{job['task']}: runner error {type(e).__name__}: {e}")


def _job(suite: Suite, run_id: str, job: Dict[str, Any], frozen: Dict[str, Optional[tuple]], budget: _Budget,
         progress: Callable[[str], None]) -> None:
    task = next(t for t in suite.tasks if t.id == job["task"])
    jdir = store.run_dir(run_id, "runs", job["dir"])
    workspace, data = _dirs(suite, run_id, "runs", job["dir"])
    record = {"run_id": run_id, "n": job["n"], "arm": job["arm"], "task": task.id, "kind": task.kind,
              "rep": job["rep"] + 1, "dir": f"runs/{job['dir']}", "scored": False}
    if store.stop_requested(run_id):
        store.append_record(run_id, {**record, "status": "stopped"})
        return
    if budget.exhausted():
        store.append_record(run_id, {**record, "status": "budget"})
        return
    if task.setup and frozen.get(task.id) is None:
        store.append_record(run_id, {**record, "status": "setup_failed"})
        return
    if job["first"] or not suite.chain:
        shutil.rmtree(jdir, ignore_errors=True)
    data.mkdir(parents=True, exist_ok=True)
    source = frozen.get(task.id)
    if source is not None:
        frozen_workspace, frozen_data = _dirs(suite, run_id, *source)
        shutil.copytree(frozen_data, data, dirs_exist_ok=True)
        shutil.copytree(frozen_workspace, workspace, dirs_exist_ok=True)
    else:
        _fill_workspace(suite, task, workspace)
    exported = Path(task.fork.source) / "data" if task.fork else None
    if exported is not None and exported.is_dir():
        # What the source's fork hooks read (a store's documents), as new files: a store that drops them by age
        # would else find an export of yesterday's run expired.
        shutil.copytree(exported, data, dirs_exist_ok=True, copy_function=shutil.copy)
    result = _agent_run(suite, run_id, job, task, jdir, workspace, data)
    record.update(result)
    if result["status"] in ("done", "timeout", "crashed"):
        record["scored"] = True
        verdict = (run_check(task.check, suite, task, workspace, result) if result["status"] == "done"
                   else {"score": 0.0, "passed": False, "detail": result.get("detail") or result["status"]})
        record.update(verdict)
    if "answer" in record:  # the check saw all of it
        record["answer"] = (record["answer"] or "")[-_ANSWER_CHARS:]
    budget.add(record.get("cost"))
    store.append_record(run_id, record)
    progress(f"#{job['n']} {job['arm']}/{task.id} r{job['rep'] + 1}: {record['status']}"
             + (f" score {record['score']:.2f}" if record["scored"] else "")
             + (f" ${record['cost']:.4f}" if record.get("cost") else ""))


def _agent_run(suite: Suite, run_id: str, job: Dict[str, Any], task: Task, jdir: Path, workspace: Path,
               data: Path) -> Dict[str, Any]:
    arm = suite.arms[job["arm"]]
    request_id = f"ev-{run_id.replace('-', '')[-12:]}-{job['n']}-{task.id}"[:64]
    result_path = jdir / f"result-{task.id}.json"
    job_file = jdir / f"job-{task.id}.json"
    job_file.write_text(json.dumps({
        # In order: the task's material first, the arm's change over it.
        "agent": arm.agent or suite.agent, "overlays": [task.agent_config, arm.agent_config],
        "agents": arm.agents, "input": task.input if task.fork is None else f"fork at {task.fork.at}",
        "fork": _fork_job(task), "request_id": request_id, "session_user": SESSION_USER,
        "result_path": str(result_path),
    }, ensure_ascii=False), encoding="utf-8")
    log = jdir / f"child-{task.id}.log"
    started = datetime.now(timezone.utc).isoformat(timespec="milliseconds")
    exit_code = run_bounded([sys.executable, "-m", "plugins.agent_eval.child", str(job_file)], cwd=workspace,
                            log=log, timeout=suite.timeout_s, env=child_env(suite, data))
    # From the usage store, not from the child: a run killed at its timeout wrote nothing, and is often the
    # costliest one. With it what a check reads the run's products from: the data directory (its stores) and the
    # machine runs it started -- those of a fresh one only: in the installation's, every run since would count.
    fresh = suite.data == "fresh"
    usage = {**usage_of(request_id, data if fresh else None),
             "data_dir": str(data if fresh else PROJECT_ROOT / data_path()),  # absolute: a check runs elsewhere
             "stategraph_runs": stategraph_runs(data, started) if fresh else None}
    if exit_code is None:
        return {"status": "timeout", "request_id": request_id, "exit": None, **usage,
                "detail": f"timed out after {suite.timeout_s:.0f}s"}
    if not result_path.exists():
        return {"status": "crashed", "request_id": request_id, "exit": exit_code, **usage,
                "detail": f"exit {exit_code} without a result\n{tail(log)}"}
    result = json.loads(result_path.read_text(encoding="utf-8"))
    if result.get("exception"):
        # The run raised: the arm's agent failed it, like a crash -- with the reason.
        return {**result, **usage, "status": "crashed", "exit": exit_code, "detail": result["exception"]}
    # Refused before it ran (role gate, a session held elsewhere): the setup is wrong, the arm did nothing.
    return {**result, **usage, "status": "refused" if result.get("refused") else "done", "exit": exit_code}


def _fork_job(task: Task) -> Optional[Dict[str, Any]]:
    """What the child forks for a fork task: the source, its step, where to stop."""
    fork = task.fork
    if fork is None:
        return None
    return {"source": str(Path(fork.source) / "source.json"), "at_step": fork.step, "until": fork.until,
            "definition": fork.definition, "mocks": fork.mocks, "stategraph": fork.stategraph}


def stategraph_runs(data: Path, since: str) -> List[Dict[str, Any]]:
    """The stategraph runs in *data* that began or moved on at *since* or later (UTC, as the journal writes it): a
    chain keeps its data directory from task to task, and a task's are the runs its own agent run started or drove
    on. Only the child writes that journal: nothing else moves them."""
    db = data / "stategraph" / "runs.db"
    if not db.is_file():
        return []
    try:
        with sqlite3.connect(f"{db.resolve().as_uri()}?mode=ro", uri=True, timeout=10) as conn:
            rows = conn.execute("SELECT id, machine_id, status, final_state, caller_run, parent_run FROM runs "
                                "WHERE created_at >= ? OR updated_at >= ? ORDER BY created_at",
                                (since, since)).fetchall()
    except sqlite3.Error:
        return []
    return [dict(zip(("id", "machine", "status", "final_state", "caller_run", "parent_run"), row)) for row in rows]


def usage_of(request_id: str, data: Optional[Path]) -> Dict[str, Any]:
    """Calls, tokens and cost of one run, sub-agents included: the usage tracker's records whose request id is
    the run's or starts with ``<run's>_`` (sub-requests). *data* is the run's own data directory; None means the
    installation's. Unknown (None) when the store is not where the tracker keeps it by default."""
    db = (data or data_path()) / "context_usage_tracker" / "usage.db"
    unknown = {"calls": None, "cost": None, "prompt_tokens": None, "completion_tokens": None}
    if not db.is_file():
        return unknown
    try:
        # as_uri: a '#' or '%' in the path would otherwise cut the URI -- and open a stray database read-write
        with sqlite3.connect(f"{db.resolve().as_uri()}?mode=ro", uri=True, timeout=10) as conn:
            # '`' sorts right after '_': the range is exactly the ids that start with "<id>_".
            row = conn.execute(
                "SELECT COUNT(*), SUM(cost), SUM(cost IS NULL), SUM(prompt_tokens), SUM(completion_tokens) "
                "FROM usage_snapshots WHERE request_id = ? OR (request_id > ? AND request_id < ?)",
                (request_id, request_id + "_", request_id + "`")).fetchone()
    except sqlite3.Error:
        return unknown
    calls, cost, unpriced, prompt, completion = row
    return {"calls": calls, "cost": round(cost, 6) if cost is not None else (0.0 if calls == 0 else None),
            "unpriced_calls": unpriced or 0, "prompt_tokens": prompt or 0, "completion_tokens": completion or 0}


def export_run(run_id: str, out: Path, data: Optional[Path] = None, include: Sequence[str] = ()) -> Path:
    """Freeze stategraph run *run_id* for fork tasks: its row and journal in ``<out>/source.json``, and every data
    path *include* names (relative to the data directory -- what the machine's fork hooks read, a store's
    documents) under ``<out>/data/``. *data*: the data directory that holds the run, default the installation's;
    a copy of another host's works the same."""
    from plugins.stategraph.engine.journal import TERMINAL_STATUSES, RunStore

    data = (data or data_path()).resolve()
    db = data / "stategraph" / "runs.db"
    if not db.is_file():
        raise ValueError(f"{db} does not exist")
    if out.exists() and (not out.is_dir() or any(out.iterdir())):
        raise ValueError(f"{out} is not an empty directory")
    paths = []
    for rel in include:
        path = (data / rel).resolve()
        if not path.is_relative_to(data) or not path.exists():
            raise ValueError(f"{rel} is no path in {data}")
        paths.append(path)
    # a path inside another one named comes with it
    paths = [p for p in paths if not any(p != q and p.is_relative_to(q) for q in paths)]
    runs = RunStore.read_only(db)
    try:
        exported = runs.export_run(run_id)
        if exported is None:
            raise ValueError(f"no run {run_id} in {db}")
        # A live run changes under the export; a compacted one has no journal left to replay.
        if exported["run"]["status"] not in TERMINAL_STATUSES:
            raise ValueError(f"run {run_id} is {exported['run']['status']}: export a run that ended")
        if runs.compacted(run_id):
            raise ValueError(f"run {run_id} is compacted (retention): its journal is gone")
    finally:
        runs.close()
    # Who ran it and where it hung in a session: an import sets its own, and an export next to a suite may be
    # published with it. Params, outputs and the definition stay -- the fork replays them.
    exported["run"].update(dict.fromkeys(_UNEXPORTED))
    out.mkdir(parents=True, exist_ok=True)
    try:
        for path in dict.fromkeys(paths):
            target = out / "data" / path.relative_to(data)
            if path.is_dir():
                shutil.copytree(path, target)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(path, target)
        (out / "source.json").write_text(json.dumps(exported, ensure_ascii=False), encoding="utf-8")
    except BaseException:
        shutil.rmtree(out, ignore_errors=True)  # it was empty: no half export that blocks the next try
        raise
    return out


#: The columns of a source's row an export leaves empty (runs.db's).
_UNEXPORTED = ("user_id", "session_id", "run_key", "owner", "lease_until", "nesting", "control", "control_answer",
               "caller_run", "caller_request")
