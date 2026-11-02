"""Running a suite: every arm on every task, ``repeats`` times, each run a child process of its own.

The one place a run is executed -- the CLI calls ``run_suite``, the panel starts the CLI.

Order: for every repeat and task the arms start next to each other in a shuffled order, so they meet the same
provider load and routing (arms run hours apart measure the time of day). With ``chain`` the same holds, and the
next task of a chain starts only once every arm has finished the one before. A run that times out or crashes
scores 0 and counts -- that is what the arm did; a run never started (budget, stop, a failed setup) or one that
failed for the runner's own reasons does not count.
"""
from __future__ import annotations

import contextlib
import copy
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
from typing import Any, Callable, Dict, Iterator, List, Optional, Sequence

from agent_system.paths import DATA_DIR_ENV, PROJECT_ROOT, data_path

from . import store
from .checks import product_of, run_check
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


def config_layer(suite: Suite) -> Dict[str, Any]:
    """The suite's ``config:`` as this process's layer takes it: a ``./`` template path resolved against the suite's
    directory, as the loader does for the file that names it."""
    from agent_system.config.layers import _resolve_relative_paths

    return _resolve_relative_paths(copy.deepcopy(suite.config), suite.root)


@contextlib.contextmanager
def layered(suite: Suite) -> Iterator[None]:
    """The suite's configuration layer in place in this process (its checks load settings), and put back after."""
    from agent_system.config import layers

    before, layers.process_layer = layers.process_layer, config_layer(suite)
    try:
        yield
    finally:
        layers.process_layer = before


def run_suite(suite_path: str | Path, run_id: Optional[str] = None, arms: Optional[List[str]] = None,
              progress: Callable[[str], None] = print) -> str:
    """Run the suite at *suite_path* (only *arms*, when given -- in the suite's order, its first arm stays the
    reference); returns the run id. Raises ValueError before anything runs when an arm cannot run at all."""
    suite = load_suite(suite_path)
    with layered(suite):
        return _run_suite(suite, suite_path, run_id, arms, progress)


def _run_suite(suite: Suite, suite_path: str | Path, run_id: Optional[str], arms: Optional[List[str]],
               progress: Callable[[str], None]) -> str:
    if arms:
        unknown = [a for a in arms if a not in suite.arms]
        if unknown:
            raise ValueError(f"unknown arms {unknown}; the suite has {list(suite.arms)}")
        suite.arms = {name: arm for name, arm in suite.arms.items() if name in arms}
    check_arms(suite)
    expected = expected_models(suite)
    check_judge(suite, expected)
    run_id = run_id or store.new_run_id()
    rdir = store.run_dir(run_id)
    rdir.mkdir(parents=True, exist_ok=False)
    shutil.copy2(suite_path, rdir / "suite.yaml")
    meta = {"run_id": run_id, "suite": suite.name, "suite_path": str(Path(suite_path).resolve()),
            "suite_sha": hashlib.sha256(Path(suite_path).read_bytes()).hexdigest(),
            "agent": suite.agent, "arms": list(suite.arms), "reference_arm": suite.reference_arm,
            "tasks": len(suite.tasks), "repeats": suite.repeats, "status": "running", "started": store.now(),
            "pid": os.getpid(), "expected_models": expected}
    store.write_meta(run_id, meta)
    try:
        frozen = {t.id: _setup(suite, t, run_id, progress) for t in suite.tasks if t.setup}
        budget = _Budget(suite.budget_usd, progress)
        waves = _schedule(suite, run_id)
        progress(f"{run_id}: {sum(map(len, waves))} agent run(s), {len(suite.arms)} arm(s), {len(suite.tasks)} task(s)")
        with ThreadPoolExecutor(max_workers=suite.parallel) as pool:
            for wave in waves:
                list(pool.map(lambda job: _job_or_error(suite, run_id, job, frozen, budget, progress), wave))
        if suite.judge is not None:
            _judge(suite, run_id, progress, meta)
        meta["status"] = _status(run_id, meta)
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


def expected_models(suite: Suite) -> Dict[str, Dict[str, Any]]:
    """Per arm, its suite agent's name and, for each agent it names -- that one with the arm's overlay, every agent
    of its ``agents`` --, the model it runs on first and, for an agent that escalates by itself
    (auto_escalate_on_stuck), the first of its advanced chain: what an agent ran on besides, it fell back to, and
    the run did not measure the arm's choice (the report says so). Without escalation an advanced model is one
    more fallback (fallback_chain runs the advanced chain after the normal one). ``tasks``: the suite agent's
    first model where a task's own overlay changes it. An agent or a profile the configuration lacks is left out
    (check_arms refuses what cannot run)."""
    from agent_system.config.settings import get_tool_server_config, load_settings

    try:
        config = load_settings()
    except Exception:  # noqa: BLE001 - no expectation is no reason to refuse a suite
        return {}
    def first(chain: Any) -> Optional[str]:
        return _first_model(config, chain)

    def runs_on(agent_config) -> Dict[str, Optional[str]]:
        return {"model": first(agent_config.llm_profile),
                "advanced": first(agent_config.llm_profile_advanced) if agent_config.auto_escalate_on_stuck else None}

    out: Dict[str, Dict[str, Any]] = {}
    for name, arm in suite.arms.items():
        agent = arm.agent or suite.agent
        try:
            layered = layer_agents(config, arm.agents)
            found = {other: runs_on(get_tool_server_config(other, layered).agent_config) for other in arm.agents}
            # last: the suite's agent gets the arm's overlay over its layer, as the child builds it
            own = get_tool_server_config(agent, layered).agent_config
            found[agent] = runs_on(apply_overlay(own, arm.agent_config))
            # as the child applies them: the task's material first, the arm's change over it
            by_task = {task.id: first(apply_overlay(apply_overlay(own, task.agent_config),
                                                    arm.agent_config).llm_profile)
                       for task in suite.tasks if task.agent_config}
        except Exception:  # noqa: BLE001 - an agent this configuration lacks (a stand-in in a test)
            found, by_task = {}, {}
        out[name] = {"agent": agent, "models": {who: runs for who, runs in found.items() if runs["model"]},
                     "tasks": {task: model for task, model in by_task.items()
                               if model and model != (found.get(agent) or {}).get("model")}}
    return out


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
    exported = Path(task.fork.source) / "data" if task.fork else None  # not-the-data-dir: an export's own folder
    if exported is not None and exported.is_dir():
        # What the source's fork hooks read (a store's documents), as new files: a store that drops them by age
        # would else find an export of yesterday's run expired.
        shutil.copytree(exported, data, dirs_exist_ok=True, copy_function=shutil.copy)
    result = _agent_run(suite, run_id, job, task, jdir, workspace, data)
    record.update(result)
    if result["status"] == "done" and task.check.judge:
        record.update(_product(suite, task, jdir, workspace, result))
    elif result["status"] in ("done", "timeout", "crashed"):
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
    return _child(suite, {
        # In order: the task's material first, the arm's change over it.
        "agent": arm.agent or suite.agent, "overlays": [task.agent_config, arm.agent_config],
        "agents": arm.agents, "input": task.input if task.fork is None else f"fork at {task.fork.at}",
        "fork": _fork_job(task), "request_id": f"ev-{run_id.replace('-', '')[-12:]}-{job['n']}-{task.id}"[:64],
        "tool_bindings": task.tool_bindings,
    }, jdir, task.id, workspace, data, suite.timeout_s)


def _child(suite: Suite, job: Dict[str, Any], jdir: Path, name: str, workspace: Path, data: Path,
           timeout: float) -> Dict[str, Any]:
    """Run *job* (child.py's job file) in a child process: its result with its usage, and a ``status`` of done,
    refused, crashed or timeout. *name* tells its files in *jdir* apart."""
    request_id = job["request_id"]
    result_path = jdir / f"result-{name}.json"
    job_file = jdir / f"job-{name}.json"
    job_file.write_text(json.dumps({**job, "session_user": SESSION_USER, "result_path": str(result_path),
                                    "config": config_layer(suite)}, ensure_ascii=False), encoding="utf-8")
    result_path.unlink(missing_ok=True)  # one of an earlier call (a judge's again) is not this one's answer
    log = jdir / f"child-{name}.log"
    started = datetime.now(timezone.utc).isoformat(timespec="milliseconds")
    exit_code = run_bounded([sys.executable, "-m", "plugins.agent_eval.child", str(job_file)], cwd=workspace,
                            log=log, timeout=timeout, env=child_env(suite, data))
    # From the usage store, not from the child: a run killed at its timeout wrote nothing, and is often the
    # costliest one. With it what a check reads the run's products from: the data directory (its stores) and the
    # machine runs it started -- those of a fresh one only: in the installation's, every run since would count.
    fresh = suite.data == "fresh"
    usage = {**usage_of(request_id, data if fresh else None),
             "data_dir": str(data if fresh else PROJECT_ROOT / data_path()),  # absolute: a check runs elsewhere
             "stategraph_runs": stategraph_runs(data, started) if fresh else None}
    if exit_code is None:
        return {"status": "timeout", "request_id": request_id, "exit": None, **usage,
                "detail": f"timed out after {timeout:.0f}s"}
    if not result_path.exists():
        return {"status": "crashed", "request_id": request_id, "exit": exit_code, **usage,
                "detail": f"exit {exit_code} without a result\n{tail(log)}"}
    result = json.loads(result_path.read_text(encoding="utf-8"))
    if result.get("exception"):
        # The run raised: the arm's agent failed it, like a crash -- with the reason.
        return {**result, **usage, "status": "crashed", "exit": exit_code, "detail": result["exception"]}
    # Refused before it ran (role gate, a session held elsewhere): the setup is wrong, the arm did nothing.
    return {**result, **usage, "status": "refused" if result.get("refused") else "done", "exit": exit_code}


def _product(suite: Suite, task: Task, jdir: Path, workspace: Path, result: Dict[str, Any]) -> Dict[str, Any]:
    """A finished run of a judged task: its product kept for the judge, the run unscored until judged. One that
    cannot be read scores 0, as a check that fails to run does -- the run left nothing to judge."""
    try:
        text = product_of(suite, task, workspace, result)
    except Exception as e:  # noqa: BLE001 - scored with its reason
        return {"scored": True, "score": 0.0, "passed": False,
                "detail": f"product failed: {type(e).__name__}: {e}"[-2000:]}
    path = jdir / f"product-{task.id}.txt"
    path.write_text(text, encoding="utf-8")
    return {"judge": "pending", "product": str(path), "product_chars": len(text)}


def check_judge(suite: Suite, expected: Dict[str, Dict[str, Any]]) -> None:
    """Refuse a judge that cannot run, or one whose model is of a family an arm's agent runs on (*expected*,
    expected_models): a model favours text of its own family, and would score that arm up."""
    if suite.judge is None:
        return
    from agent_system.config.settings import get_tool_server_config, load_settings
    from agent_system.servers.agent.entry import agent_entry_names

    from .stats import families

    config = load_settings()
    if suite.judge.agent not in set(agent_entry_names(config)):
        raise ValueError(f"judge: '{suite.judge.agent}' is not an agent of this configuration")
    try:
        merged = apply_overlay(get_tool_server_config(suite.judge.agent, config).agent_config,
                               suite.judge.agent_config)
    except Exception as e:  # noqa: BLE001 - reported as the judge's
        raise ValueError(f"judge: the overlay does not validate: {e}") from e
    model = _first_model(config, merged.llm_profile)
    if model is None:
        raise ValueError(f"judge: llm_profile {merged.llm_profile} names no model of this configuration")
    candidates = [(f"arm {arm}, agent {agent}", m) for arm, wants in expected.items()
                  for agent, runs in (wants.get("models") or {}).items()
                  for m in (runs.get("model"), runs.get("advanced")) if m]
    candidates += [(f"arm {arm}, task {task}", m) for arm, wants in expected.items()
                   for task, m in (wants.get("tasks") or {}).items()]
    shared = sorted(f"{who} ({m})" for who, m in candidates if families(m) & families(model))
    if shared:
        raise ValueError(f"judge: its model {model} is of a family {'; '.join(shared)} runs on -- a judge "
                         "favours its own family's text; take one of another")


def _first_model(config: Any, chain: Any) -> Optional[str]:
    """The model the first profile of *chain* names, None when the configuration lacks it."""
    llm = config.llm_system
    profiles, models = (llm.profiles or {}, llm.models or {}) if llm else ({}, {})
    chain = chain if isinstance(chain, list) else [chain] if chain else []
    profile = profiles.get(chain[0]) if chain else None
    return getattr(models.get(profile.model_ref), "model", None) if profile is not None else None


def _judge(suite: Suite, run_id: str, progress: Callable[[str], None], meta: Dict[str, Any]) -> None:
    """Judge every task's runs that wait for it or that it failed to judge, a call per task, each call written to
    the meta's ``judge.calls`` as soon as it is made -- a runner that dies on the next keeps what it spent. Also
    after a stop, and past the budget: those runs ran, unjudged they would count as never run. A call that
    raises (a broken result, a product file gone) fails its task's runs with the reason, not the others'."""
    calls = meta.setdefault("judge", {"agent": suite.judge.agent, "calls": []}).setdefault("calls", [])
    records = store.read_records(run_id)
    for task in suite.tasks:
        pending = [r for r in records if r["task"] == task.id and r.get("judge") in ("pending", "failed")]
        if not pending:
            continue
        try:
            calls.append(_judge_task(suite, run_id, task, pending, progress))
        except Exception as e:  # noqa: BLE001 - recorded on every run it left unjudged
            why = f"the judge call failed: {type(e).__name__}: {e}"[-2000:]
            for record in pending:
                store.append_judgment(run_id, {"n": record["n"], "judge": "failed", "status": "judge_failed",
                                               "detail": why})
            calls.append({"task": task.id, "status": "error", "products": len(pending), "judged": 0, "cost": None,
                          "problem": why[-500:]})
            progress(f"judging {task.id} failed: {type(e).__name__}: {e}")
        store.write_meta(run_id, meta)


def rejudge(run_id: str, progress: Callable[[str], None] = print) -> None:
    """Judge again what run *run_id* left unjudged -- its runner ended before the judge did, a judge call failed --
    with the suite as it ran (refused when its file changed since); the run's status follows."""
    meta = store.read_meta(run_id)
    if store.live_status(meta) == "running":
        raise ValueError(f"{run_id} is still running")
    path = Path(meta["suite_path"])
    if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != meta.get("suite_sha"):
        raise ValueError(f"{path} changed since {run_id} ran: its judge would ask something else")
    suite = load_suite(path)
    if suite.judge is None:
        raise ValueError(f"suite {suite.name} has no judge")
    with layered(suite):
        expected = meta.get("expected_models") or expected_models(suite)  # a run from before it kept them
        check_judge(suite, {arm: wants for arm, wants in expected.items() if arm in meta["arms"]})
        # running while it judges: a second judge on the run is refused, the panel shows it at work
        meta.update(status="running", pid=os.getpid())
        store.write_meta(run_id, meta)
        try:
            _judge(suite, run_id, progress, meta)
        finally:
            meta["status"] = _status(run_id, meta)
            store.write_meta(run_id, meta)


def _status(run_id: str, meta: Dict[str, Any]) -> str:
    """done when every agent run counted: a baseline with tasks missing would make every later comparison quietly
    run on the rest. Every run the meta planned, too: one that ended before all had their record (a runner
    stopped by hand) is incomplete, judged again or not."""
    records = store.read_records(run_id)
    missed = {r.get("status") for r in records} & set(store.NOT_COUNTED)
    if "stopped" in missed:
        return "stopped"
    planned = len(meta["arms"]) * meta["tasks"] * meta["repeats"]
    return "incomplete" if missed or len(records) < planned else "done"


def _judge_task(suite: Suite, run_id: str, task: Task, pending: List[Dict[str, Any]],
                progress: Callable[[str], None]) -> Dict[str, Any]:
    """One judge call over a task's products, shuffled (by the run id: the same run asks the same) and labelled
    P1..Pn; each run's judgment appended. A run whose product the answer leaves out is not counted."""
    order = sorted(pending, key=lambda r: r["n"])
    random.Random(f"{run_id}:{task.id}").shuffle(order)
    labelled = {f"P{i}": r for i, r in enumerate(order, 1)}
    criteria = suite.criteria(task)
    products = {label: Path(r["product"]).read_text(encoding="utf-8") for label, r in labelled.items()}
    jdir = store.run_dir(run_id, "judge", task.id)
    workspace, data = _dirs(suite, run_id, "judge", task.id)
    workspace.mkdir(parents=True, exist_ok=True)
    # the task's how-manyth call, by the calls on disk: each its own request id -- the usage store would count an
    # earlier one's cost again under the same --, also after a runner that died before its meta knew of them
    attempt = 1 + len(list(jdir.glob("job-judge*.json")))
    request_id = f"ev-{run_id.replace('-', '')[-12:]}-judge{attempt}-{task.id}"[:64]
    result = _child(suite, {"agent": suite.judge.agent, "overlays": [suite.judge.agent_config], "agents": {},
                            "input": judge_prompt(suite, task, criteria, products), "fork": None,
                            "request_id": request_id}, jdir, f"judge{attempt}", workspace, data,
                    suite.judge.timeout_s)
    verdicts, problem = ({}, result.get("detail") or result["status"]) if result["status"] != "done" \
        else parse_verdicts(result.get("answer") or "", list(labelled), list(criteria))
    for label, record in labelled.items():
        said = verdicts.get(label)
        if said is None:
            store.append_judgment(run_id, {"n": record["n"], "judge": "failed", "status": "judge_failed",
                                           "detail": f"the judge did not judge it: {problem}"[-2000:]})
            continue
        yes = sum(said.values())
        store.append_judgment(run_id, {
            "n": record["n"], "judge": "done", "scored": True, "score": yes / len(said), "of": len(said),
            "passed": yes == len(said), "verdicts": said,
            "detail": ", ".join(f"{c}: {'yes' if v else 'no'}" for c, v in said.items())})
    progress(f"judged {task.id}: {len(verdicts)} of {len(labelled)} product(s)"
             + (f" ${result['cost']:.4f}" if result.get("cost") else ""))
    return {"task": task.id, "request_id": request_id, "status": result["status"], "products": len(labelled),
            "judged": len(verdicts), "cost": result.get("cost"), "models": result.get("models"),
            **({"problem": str(problem)[-500:]} if problem else {})}


def judge_prompt(suite: Suite, task: Task, criteria: Dict[str, str], products: Dict[str, str]) -> str:
    """What the judge is asked: the products under labels that say nothing of where they come from."""
    parts = [suite.judge.instructions.strip(),
             f"The task the products were made for:\n{task.input.strip()}" if task.input else "",
             "Judge every product below on its own against every criterion: answer true (it has it) or false. The "
             "labels and the order say nothing about where a product comes from, and a longer product is no better "
             "for its length.",
             "Criteria:\n" + "\n".join(f"- {cid}: {question}" for cid, question in criteria.items()),
             *(f"=== {label} ===\n{text.strip()}\n=== end of {label} ===" for label, text in products.items()),
             "Answer with one JSON object and nothing after it: a key per product, in it a key per criterion with "
             'true or false -- {"P1": {"<criterion>": true, "<another>": false, ...}, "P2": {...}, ...}']
    return "\n\n".join(p for p in parts if p)


def parse_verdicts(answer: str, labels: List[str], criteria: List[str]) -> tuple:
    """({label: {criterion: bool}}, problem): the last JSON object of the judge's answer; a product counts only
    with a true or false for every criterion. The problem says what was missing, None when nothing was."""
    found = None
    decoder = json.JSONDecoder()
    for start in (i for i, ch in enumerate(answer) if ch == "{"):
        try:
            value, _ = decoder.raw_decode(answer, start)
        except ValueError:
            continue
        if isinstance(value, dict) and any(label in value for label in labels):
            found = value  # the last one that is an answer: a model may think aloud in JSON before it
    if found is None:
        return {}, f"no JSON object with the products in the answer: {answer[-300:]!r}"
    out, missing = {}, []
    for label in labels:
        said = found.get(label)
        if isinstance(said, dict) and all(isinstance(said.get(c), bool) for c in criteria):
            out[label] = {c: said[c] for c in criteria}
        else:
            missing.append(label)
    return out, (f"no true/false for every criterion of {missing}" if missing else None)


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
    unknown = {"calls": None, "cost": None, "prompt_tokens": None, "completion_tokens": None, "models": None}
    if not db.is_file():
        return unknown
    try:
        # as_uri: a '#' or '%' in the path would otherwise cut the URI -- and open a stray database read-write
        with sqlite3.connect(f"{db.resolve().as_uri()}?mode=ro", uri=True, timeout=10) as conn:
            # '`' sorts right after '_': the range is exactly the ids that start with "<id>_".
            mine = "FROM usage_snapshots WHERE request_id = ? OR (request_id > ? AND request_id < ?)"
            ids = (request_id, request_id + "_", request_id + "`")
            row = conn.execute("SELECT COUNT(*), SUM(cost), SUM(cost IS NULL), SUM(prompt_tokens), "
                               f"SUM(completion_tokens) {mine}", ids).fetchone()
            try:
                by_agent = conn.execute(f"SELECT agent_name, model, COUNT(*) {mine} GROUP BY agent_name, model",
                                        ids).fetchall()
            except sqlite3.OperationalError:  # a store without those columns: its cost still counts
                by_agent = None
    except sqlite3.Error:
        return unknown
    calls, cost, unpriced, prompt, completion = row
    models: Optional[Dict[str, Dict[str, int]]] = None if by_agent is None else {}
    for agent, model, n in by_agent or ():  # what each agent really ran on: a fallback in its chain shows here
        models.setdefault(agent or "?", {})[model or "?"] = n
    return {"calls": calls, "cost": round(cost, 6) if cost is not None else (0.0 if calls == 0 else None),
            "unpriced_calls": unpriced or 0, "prompt_tokens": prompt or 0, "completion_tokens": completion or 0,
            "models": models}


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
            target = out / "data" / path.relative_to(data)  # not-the-data-dir: the export's own folder
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
