"""One eval run: an agent-run, in a process of its own, with the arm's overlay.

    python -m plugins.agent_eval.child <job.json>

Started by the runner in the run's workspace (what the agent's tools take for ".") with the run's data
directory in AGENT_DATA_DIR. It drives agent-run's own main_async, so a run opens, runs and saves its session
exactly as `agent-run` does; two of its steps are wrapped: the agent it builds gets the overlay (the reload
path, the same one `agent-cli reload` takes), and the finished run writes its result -- answer, errors, the model
it ran on -- to ``job["result_path"]``. Cost is the runner's to read, from the usage store by request id: a run
killed at its timeout writes nothing here.
"""
from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path
from typing import Any, Dict, Optional


def main(job_path: str) -> None:
    job = json.loads(Path(job_path).read_text(encoding="utf-8"))
    result_path = Path(job["result_path"])

    from agent_system import agent_run
    from agent_system.cli_utils.agent_runner import run_as_local_operator
    from agent_system.config.settings import get_tool_server_config
    from agent_system.paths import enter_project
    from plugins.agent_eval.overlay import apply_overlay

    build = agent_run.create_agent
    run = agent_run.run_agent_request

    async def create_agent(config, registry, agent_name, session_service=None):
        agent = await build(config, registry, agent_name, session_service=session_service)
        overlay = job.get("agent_config") or {}
        if overlay:
            merged = get_tool_server_config(agent_name, config)
            entry = merged.model_copy(update={"agent_config": apply_overlay(merged.agent_config, overlay)})
            agent.reload_config(entry, config)
        return agent

    async def run_agent_request(agent, request, session_id, llm_override=None, llm_profile_info=None,
                                request_id=None):
        started = time.monotonic()
        result: Dict[str, Any] = {}
        failure: Optional[BaseException] = None
        try:
            result = await run(agent, request, session_id, llm_override, llm_profile_info, request_id=request_id)
            return result
        except BaseException as e:
            failure = e
            raise
        finally:
            ok = isinstance(result, dict)
            record = {
                "answer": result.get("summary", "") if ok else str(result),
                "errors": [str(e) for e in (result.get("errors") or [])] if ok else [],
                "cancelled": bool(result.get("cancelled")) if ok else False,
                "refused": result.get("refused") if ok else None,
                "tool_calls": len(result.get("calls") or []) if ok else None,
                # A run that raised is no finished run: the runner counts it as crashed, with this reason.
                "exception": f"{type(failure).__name__}: {failure}" if failure is not None else None,
                "duration_s": round(time.monotonic() - started, 2),
                # What the arm ran on: the check that an llm_profile overlay took effect.
                "model": getattr(getattr(agent, "llm", None), "model", None),
                "session_id": session_id,
                "request_id": request_id,
            }
            result_path.write_text(json.dumps(record, ensure_ascii=False, default=str), encoding="utf-8")

    def entry() -> None:
        enter_project()
        agent_run.setup_basic_logging(verbose=False)
        asyncio.run(agent_run.main_async(
            request=job["input"], agent_name=job["agent"], show_status=False,
            session_id=job.get("session_id"), session_user=job["session_user"],
            request_id=job["request_id"]))

    # Put back afterwards: in the child it makes no difference, in-process (a test) it would outlive the run.
    agent_run.create_agent, agent_run.run_agent_request = create_agent, run_agent_request
    try:
        run_as_local_operator(entry)
    finally:
        agent_run.create_agent, agent_run.run_agent_request = build, run


if __name__ == "__main__":
    main(sys.argv[1])
