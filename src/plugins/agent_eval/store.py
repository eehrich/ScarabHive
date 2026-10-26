"""Where eval runs live: one directory per run under the data directory.

    <data>/agent_eval/runs/<run_id>/
        meta.json        suite, agent, arms, status, timestamps
        suite.yaml       the suite as it was run
        records.jsonl    one line per agent run, appended as runs finish
        setup/<task>/    a task's frozen setup (workspace, data)
        runs/<n>-.../    one agent run: workspace, data, job.json, result.json, child.log
    <data>/agent_eval/baselines/<suite>.txt   the suite's baseline run id
"""
from __future__ import annotations

import json
import os
import re
import secrets
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from agent_system.paths import data_path

_RUN_ID = re.compile(r"\d{8}-\d{6}-[0-9a-f]{4}")
_lock = threading.Lock()


def root() -> Path:
    """Absolute: every run's child starts in its own workspace, where a relative path means something else."""
    return data_path("agent_eval").resolve()


def run_dir(run_id: str, *parts: str) -> Path:
    """The directory of *run_id*, or *parts* in it. Raises ValueError for anything that is not a run id (a web
    caller's input)."""
    if not _RUN_ID.fullmatch(run_id or ""):
        raise ValueError(f"not a run id: {run_id!r}")
    return data_path("agent_eval", "runs", run_id, *parts).resolve()


def new_run_id() -> str:
    return f"{datetime.now(timezone.utc):%Y%m%d-%H%M%S}-{secrets.token_hex(2)}"


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def write_meta(run_id: str, meta: Dict[str, Any]) -> None:
    path = run_dir(run_id) / "meta.json"
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)


def read_meta(run_id: str) -> Dict[str, Any]:
    return json.loads((run_dir(run_id) / "meta.json").read_text(encoding="utf-8"))


def append_record(run_id: str, record: Dict[str, Any]) -> None:
    line = json.dumps(record, ensure_ascii=False, default=str)
    with _lock, open(run_dir(run_id) / "records.jsonl", "a", encoding="utf-8") as f:
        f.write(line + "\n")


def read_records(run_id: str) -> List[Dict[str, Any]]:
    path = run_dir(run_id) / "records.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def list_runs() -> List[Dict[str, Any]]:
    """Every run's meta, newest first; a directory without readable meta is skipped."""
    runs_root = root() / "runs"
    if not runs_root.is_dir():
        return []
    out = []
    for d in sorted(runs_root.iterdir(), reverse=True):
        if d.is_dir() and _RUN_ID.fullmatch(d.name):
            try:
                out.append(read_meta(d.name))
            except (OSError, ValueError):
                continue
    return out


#: Records that did not count, by status: never started, refused before the agent ran, or the runner's own error.
NOT_COUNTED = ("budget", "stopped", "setup_failed", "refused", "error")


def baselines() -> Dict[str, str]:
    """Suite name -> its baseline run id."""
    folder = root() / "baselines"
    if not folder.is_dir():
        return {}
    return {p.stem: p.read_text(encoding="utf-8").strip() for p in sorted(folder.glob("*.txt"))}


def set_baseline(suite: str, run_id: str) -> None:
    """Make *run_id* the baseline of *suite*. Only a finished run with every agent run counted: a stopped or
    incomplete one holds part of the tasks, and every later comparison would quietly run on that part."""
    status = read_meta(run_id).get("status")
    if status != "done":
        raise ValueError(f"{run_id} is {status}; only a finished run can be a baseline")
    folder = root() / "baselines"
    folder.mkdir(parents=True, exist_ok=True)
    tmp = folder / f"{suite}.{os.getpid()}.{threading.get_ident()}.tmp"
    tmp.write_text(run_id, encoding="utf-8")
    tmp.replace(folder / f"{suite}.txt")


def baseline_for(suite: str) -> Optional[str]:
    return baselines().get(suite)


def stop_requested(run_id: str) -> bool:
    return (run_dir(run_id) / "STOP").exists()


def request_stop(run_id: str) -> None:
    (run_dir(run_id) / "STOP").write_text(now(), encoding="utf-8")
