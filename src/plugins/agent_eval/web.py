"""The Evals panel and its API: suites to start, runs to read, baselines to set.

A run is started as the CLI does it (``python -m plugins.agent_eval run``), in a process of its own that
outlives the request and the API. The panel only names a suite the discovery found -- never a path of its
own -- since a suite's checks run commands.
"""
from __future__ import annotations

import os
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import HTTPException, Request
from pydantic import BaseModel

from agent_system.paths import PROJECT_ROOT
from agent_system.plugins.schema_router import create_schema_router
from agent_system.ui.resources import ui_templates

from . import store
from .__main__ import build_report
from .proc import GROUP, tail
from .store import alive as _alive, live_status as _status
from .suite import discover_suites, load_suite


class StartRun(BaseModel):
    suite: str
    arms: Optional[List[str]] = None



class AgentEvalEndpoints:
    def __init__(self, plugin):
        self.plugin = plugin
        self.templates = ui_templates(Path(__file__).parent / "templates")

    def get_web_router(self):
        return create_schema_router(plugin_name=self.plugin.name, schema=self.plugin.get_schema_data(),
                                    handler_class=self)

    def _suites(self) -> List[Path]:
        return discover_suites(self.plugin.system_config.plugins.plugin_dirs, PROJECT_ROOT)

    async def render_panel(self, request: Request):
        return self.templates.TemplateResponse(request, "panel.html", {"plugin": self.plugin.name})

    async def list_suites(self, request: Request):
        out = []
        for path in self._suites():
            try:
                suite = load_suite(path)
                out.append({"path": str(path), "name": suite.name, "agent": suite.agent,
                            "description": suite.description, "arms": list(suite.arms),
                            "tasks": len(suite.tasks), "repeats": suite.repeats,
                            "baseline": store.baseline_for(suite.name)})
            except ValueError as e:
                out.append({"path": str(path), "error": str(e)})
        return {"suites": out}

    async def list_runs(self, request: Request):
        base = store.baselines()
        runs = []
        for meta in store.list_runs():
            records = store.read_records(meta["run_id"])
            scored = [r for r in records if r.get("scored")]
            runs.append({**meta, "status": _status(meta), "baseline": base.get(meta["suite"]) == meta["run_id"],
                         "done": len(records), "scored": len(scored),
                         "passed": sum(1 for r in scored if r.get("passed")),
                         "cost": round(sum(float(r.get("cost") or 0) for r in records), 4)})
        return {"runs": runs}

    async def get_run(self, request: Request, run_id: str, against: str = ""):
        try:
            meta = store.read_meta(run_id)
        except ValueError:
            raise HTTPException(status_code=404, detail=f"no run {run_id}")
        except FileNotFoundError:
            self._not_started(run_id)
            raise HTTPException(status_code=404, detail=f"no run {run_id}")
        if against == "baseline":
            against = store.baseline_for(meta["suite"]) or ""
        try:
            rep = build_report(run_id, against or None)
        except (ValueError, FileNotFoundError) as e:
            raise HTTPException(status_code=400, detail=str(e))
        rep["meta"]["status"] = _status(meta)
        rep["records"] = store.read_records(run_id)
        rep["baseline"] = store.baseline_for(meta["suite"])
        return rep

    @staticmethod
    def _not_started(run_id: str) -> None:
        """A run the panel started whose runner is gone without having set the run up (a suite that broke
        after it was offered, an arm that cannot run): its reason is in the runner's log, not in a run."""
        logs = store.root() / "logs"
        pid_file = logs / f"{run_id}.pid"
        if pid_file.is_file() and not _alive(pid_file.read_text(encoding="utf-8").strip()):
            raise HTTPException(status_code=500, detail="the run did not start: "
                                + tail(logs / f"{run_id}.log", 1500).strip())

    async def start_run(self, request: Request, body: StartRun):
        offered = {str(p) for p in self._suites()}
        if body.suite not in offered:
            raise HTTPException(status_code=400, detail="not a suite the panel offers")
        try:
            suite = load_suite(body.suite)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        unknown = [a for a in body.arms or [] if a not in suite.arms]
        if unknown:
            raise HTTPException(status_code=400, detail=f"unknown arms {unknown}")
        run_id = store.new_run_id()
        logs = store.root() / "logs"
        logs.mkdir(parents=True, exist_ok=True)
        args = [sys.executable, "-m", "plugins.agent_eval", "run", body.suite, "--run-id", run_id]
        for arm in body.arms or []:
            args += ["--arm", arm]
        env = dict(os.environ)
        env["PYTHONPATH"] = os.pathsep.join(filter(None, [str(PROJECT_ROOT / "src"), env.get("PYTHONPATH")]))
        # The configuration this API runs on, also when it was named on its command line: the runner and its
        # agent runs must read the same one -- and store the run where this panel reads it.
        source = getattr(self.plugin.system_config, "_source_path", None)
        if source and not env.get("AGENT_CONFIG_PATH"):
            env["AGENT_CONFIG_PATH"] = str(source)
        # A group of its own, so it outlives this request and the API; no console window (its own, and through
        # that one none for every agent run it starts).
        with open(logs / f"{run_id}.log", "w", encoding="utf-8") as log:
            proc = subprocess.Popen(args, cwd=PROJECT_ROOT, env=env, stdout=log, stderr=subprocess.STDOUT,
                                    stdin=subprocess.DEVNULL, **GROUP)
        (logs / f"{run_id}.pid").write_text(str(proc.pid), encoding="utf-8")
        # Reaped when it ends: a zombie would count as alive and show the run as running for ever.
        threading.Thread(target=proc.wait, name=f"agent-eval-{run_id}", daemon=True).start()
        return {"run_id": run_id}

    async def set_baseline(self, request: Request, run_id: str):
        try:
            meta = store.read_meta(run_id)
        except (ValueError, FileNotFoundError):
            raise HTTPException(status_code=404, detail=f"no run {run_id}")
        try:
            store.set_baseline(meta["suite"], run_id)
        except ValueError as e:
            raise HTTPException(status_code=409, detail=str(e))
        return {"suite": meta["suite"], "baseline": run_id}

    async def stop_run(self, request: Request, run_id: str):
        try:
            store.read_meta(run_id)
        except (ValueError, FileNotFoundError):
            raise HTTPException(status_code=404, detail=f"no run {run_id}")
        store.request_stop(run_id)
        return {"run_id": run_id, "stopping": True}
