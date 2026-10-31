"""One eval run: an agent-run, in a process of its own, with the task's and the arm's overlay.

    python -m plugins.agent_eval.child <job.json>

Started by the runner in the run's workspace (what the agent's tools take for ".") with the run's data
directory in AGENT_DATA_DIR. It drives agent-run's own main_async, so a run opens, runs and saves its session
exactly as `agent-run` does. The suite's ``config:`` (``job["config"]``) is this process's configuration layer,
over the local one. Three of its steps are wrapped: the configuration it loads gets the arm's
overlays for other agents (``job["agents"]``, before anything is built from it), the agent it builds gets the
overlays, the task's then the arm's (the reload path, the same one `agent-cli reload` takes), and the finished
run writes its result
-- answer, errors, the model it ran on -- to ``job["result_path"]``. Cost is the runner's to read, from the usage
store by request id: a run killed at its timeout writes nothing here.

A fork task (``job["fork"]``) asks no agent: the request forks the source run into this data directory's
stategraph and lets the machine run on, until it enters ``until`` (then it is terminated) or ends.
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
    from agent_system.config import layers
    from agent_system.config.settings import get_tool_server_config
    from agent_system.paths import enter_project
    from plugins.agent_eval.overlay import apply_overlay, layer_agents

    build = agent_run.create_agent
    run = agent_run.run_agent_request
    load = agent_run.load_settings
    built: Dict[str, Any] = {}

    def load_settings(*args, **kwargs):
        # The arm's overlays for other agents (the sub-agents of a machine, a panel) are a layer of the
        # configuration: in place before the registry is built from it, so every agent built here sees them.
        return layer_agents(load(*args, **kwargs), job.get("agents") or {})

    async def create_agent(config, registry, agent_name, session_service=None):
        agent = await build(config, registry, agent_name, session_service=session_service)
        built["registry"] = registry
        # A job a runner from before 'overlays' wrote (a pull while a suite runs: the runner lives on, each child
        # imports the new code) still carries its arm -- else every arm would quietly run as the agent.
        overlays = [o for o in job.get("overlays", [job.get("agent_config")]) or [] if o]
        if overlays:
            merged = get_tool_server_config(agent_name, config)
            agent_config = merged.agent_config
            for overlay in overlays:  # one by one: each '+x'/'!x' acts on what the one before left
                agent_config = apply_overlay(agent_config, overlay)
            agent.reload_config(merged.model_copy(update={"agent_config": agent_config}), config)
        return agent

    async def run_agent_request(agent, request, session_id, llm_override=None, llm_profile_info=None,
                                request_id=None):
        started = time.monotonic()
        result: Dict[str, Any] = {}
        failure: Optional[BaseException] = None
        try:
            if job.get("fork"):
                result = await run_fork(built["registry"], job["fork"], request_id, job["session_user"])
            else:
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
                # (none for a fork: the agent built here never ran, the machine's agents did)
                "model": None if job.get("fork") else getattr(getattr(agent, "llm", None), "model", None),
                "session_id": session_id,
                "request_id": request_id,
                **({"fork": result.get("fork")} if ok and result.get("fork") else {}),
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
    agent_run.create_agent, agent_run.run_agent_request, agent_run.load_settings = (
        create_agent, run_agent_request, load_settings)
    # The suite's layer (its `config:`), under every load of the settings in this run.
    layer, layers.process_layer = layers.process_layer, job.get("config") or {}
    try:
        run_as_local_operator(entry)
    finally:
        agent_run.create_agent, agent_run.run_agent_request, agent_run.load_settings = build, run, load
        layers.process_layer = layer


async def run_fork(registry: Any, spec: Dict[str, Any], request_id: str, user: str) -> Dict[str, Any]:
    """Fork the exported run at its step and let it run until it enters ``until`` or stops by itself; the result
    in run_agent_request's shape. Refused (``refused``) when the fork cannot start: the suite is wrong, not the arm."""
    from plugins.agent_eval.suite import is_composite, until_machine

    server = registry.get(spec["stategraph"])
    if server is None or not hasattr(server, "service"):
        return {"refused": f"no stategraph instance {spec['stategraph']!r} is running"}
    server.use_registry(registry)  # its runs resolve their runner agent here
    exported = json.loads(Path(spec["source"]).read_text(encoding="utf-8"))
    source, machine = exported["run"]["id"], exported["run"]["machine_id"]
    fork_id = f"{request_id}_sg1"  # <request id>_...: its agents' requests count as this run's cost
    until = spec.get("until")
    try:
        # The eval's user owns it here; nobody's lease from the process that ran it at the source.
        server.run_store.import_run(exported, user_id=user, owner=None, lease_until=None, control=None,
                                    control_answer=None)
        composite = bool(until) and is_composite(
            until_machine(exported["run"]["definition"] if spec["definition"] == "snapshot"
                          else server.service.store.load(machine).snapshot()), machine, until)
        # In place of the source's own (a run someone debugged): only until holds the fork. A composite has no enter
        # of its own: its entry shows in run.entered, after the step that entered it, before the inner state's work
        # (visits would not do: they start anew when the composite's parent is entered again).
        await server.service.fork_run(
            source, user_id=user, fork_id=fork_id, at_step=spec["at_step"], definition=spec["definition"],
            mocks=spec.get("mocks") or None,
            breakpoints=[{"state": until, "machine": machine}] if until and not composite else [],
            watchpoints=[{"expr": f"run.entered.get({until!r})", "machine": machine}] if composite else [])
    except Exception as e:  # noqa: BLE001 - ServiceError (a state the machine lacks), an export of a newer stategraph
        return {"refused": f"the fork did not start: {type(e).__name__}: {e}"}
    while True:
        row, status, paused = await _stopped(server, fork_id)
        # Held at the fork point itself (until is at, or the state at enters first): until is its next entry.
        if not (status == "paused" and paused is not None and paused.get("frame") == ""
                and paused.get("step") == spec["at_step"]):
            break
        await server.service.control_run(fork_id, "continue", user_id=user)
    errors = [json.dumps(row["error"], ensure_ascii=False, default=str)] if row["status"] == "failed" else []
    if status in ("paused", "waiting"):
        try:
            await server.service.control_run(fork_id, "terminate", user_id=user)
        except Exception as e:  # noqa: BLE001 - what it made up to here is still the run's to check
            errors.append(f"the fork was not terminated: {type(e).__name__}: {e}")
    output = row.get("output")
    answer = output if isinstance(output, str) or output is None else json.dumps(output, ensure_ascii=False,
                                                                                default=str)
    reached = status == "paused" and paused is not None and (paused.get("machine"), paused.get("frame")) == (
        machine, "") and (paused.get("hook") == "watch" if composite
                          else (paused.get("hook"), paused.get("state")) == ("enter", until))
    return {"summary": answer or "", "errors": errors,
            "fork": {"run_id": fork_id, "source": source, "at_step": spec["at_step"], "status": status,
                     "until_reached": reached}}


async def _stopped(server: Any, run_id: str) -> tuple:
    """(row, status, where the debugger holds it) once the run ended, paused, or waits for an event -- nobody sends
    one here. A wait that takes no event goes on by itself (a timer state, a semaphore slot) and is waited out, as
    the stategraph server's own run_machine does; one that takes an event stops the fork, with a timeout too (an
    approval that expires in a day would else run into the task's timeout, its work never checked)."""
    from plugins.stategraph.engine.runner import row_event_due

    while True:
        row = await server.run_manager.wait(run_id)
        live = server.run_manager.live.get(run_id)
        running = live is not None and not live.task.done()
        status = live.ctx.status if running else row["status"]
        frames = (row.get("view") or {}).get("frames") or []
        goes_on = row_event_due(row) or not any(f.get("accepts") for f in frames)
        if status != "waiting" or not goes_on:
            return row, status, live.ctx.debugger.paused if running else None
        await asyncio.sleep(1.0)


if __name__ == "__main__":
    main(sys.argv[1])
