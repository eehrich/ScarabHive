"""What a check reads a run's products with: the run record (``run.json``, the dict a ``python:`` check gets) names
the run's data directory and the machine runs it started or forked; these read them, read-only.

    from plugins.agent_eval.evidence import entered, spend, store_doc

All of them take the record; ``run_id`` picks one of several machine runs (default: the fork's, else the one the
agent started -- refused when it started several).
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional


def data_dir(run: Dict[str, Any]) -> Path:
    """The run's data directory (its own with ``data: fresh``, the installation's with ``shared``)."""
    if not run.get("data_dir"):
        raise ValueError("the record names no data directory (a run of an agent_eval before data_dir)")
    return Path(run["data_dir"])


def store_doc(run: Dict[str, Any], doc: str, namespace: Optional[str] = None, server: str = "json_store") -> Any:
    """The value of json_store document *doc* (of *namespace*, any when None -- refused when several have it) as
    the run left it on disk; None when there is none. *server*: the store's instance name."""
    root = data_dir(run) / "json_store" / server
    found = []
    for path in sorted(root.glob("*/*.json")) if root.is_dir() else ():
        try:
            payload = json.loads(path.read_text(encoding="utf-8", errors="surrogatepass"))  # as json_store writes
        except (OSError, ValueError):
            continue  # a file a write left half done is no document
        if (isinstance(payload, dict) and payload.get("doc") == doc
                and namespace in (None, payload.get("namespace"))):
            found.append(payload)
    if len(found) > 1:
        raise ValueError(f"document {doc!r} is in namespaces {sorted(p.get('namespace') for p in found)}: name one")
    return found[0].get("data") if found else None


def machine_run(run: Dict[str, Any]) -> str:
    """The machine run a check reads by default: the fork of a fork task, else the one run the agent started."""
    if (run.get("fork") or {}).get("run_id"):
        return run["fork"]["run_id"]
    started = [r["id"] for r in run.get("stategraph_runs") or () if not r.get("caller_run")]
    if len(started) != 1:
        raise ValueError(f"the run started {len(started)} machine runs at the top ({started}): pass run_id")
    return started[0]


def _runs(run: Dict[str, Any]):
    from plugins.stategraph.engine.journal import RunStore

    db = data_dir(run) / "stategraph" / "runs.db"
    if not db.is_file():
        raise ValueError(f"{db} does not exist: the run started no machine")
    return RunStore.read_only(db)


def entered(run: Dict[str, Any], run_id: Optional[str] = None) -> List[Dict[str, Any]]:
    """The states the machine run entered, in order -- a composite, then the state inside it: ``{"state", "key",
    "ts"}``, ``key`` the journal's (``s<step>:enter:<state>``), ``ts`` UTC. Durations are the gaps."""
    runs = _runs(run)
    try:
        exported = runs.export_run(run_id or machine_run(run))
    finally:
        runs.close()
    if exported is None:
        raise ValueError(f"no machine run {run_id or machine_run(run)} in the run's data directory")
    return [{"state": row["state"], "key": row["key"], "ts": row["ts"]} for row in exported["journal"]
            if row["kind"] == "trace" and row["status"] == "enter"]


def spend(run: Dict[str, Any], run_id: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """What the machine run spent, the runs it called included: stategraph's own sum (``cost``, ``tokens``, and
    ``by_state`` once more than one state spent -- the cost per state path, a state above its children). None
    when nothing reported any."""
    runs = _runs(run)
    try:
        return runs.spend(run_id or machine_run(run), children=True)
    finally:
        runs.close()
