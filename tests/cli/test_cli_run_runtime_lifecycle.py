"""`agent-cli run`: the session-store commands run before the runtime, and every
other way out shuts the runtime down exactly once.

The listing and the archive commands need the session store alone. Run after
the entry agent was built, a default agent that could not be built hid
--list-sessions; run after the runtime was started, they returned without
shutting it down -- and so did every sys.exit(1) on the way to the run.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from agent_system.cli_utils.commands import run


@pytest.fixture
def steps(monkeypatch):
    """run_agent_command with its steps recorded instead of taken."""
    calls: list[str] = []
    boot = SimpleNamespace(session_manager=None, registry=None, init_service=None, session_service=None)
    monkeypatch.setattr(run, "setup_cli_logging", lambda config, verbose: None)
    monkeypatch.setattr(run, "bootstrap", lambda config, vprint: boot)
    monkeypatch.setattr(run, "resolve_session_ref", lambda manager, user, typed: typed)

    def start(config, registry, vprint):
        calls.append("start")
        run._runtime_up = True

    async def shutdown_batch():
        calls.append("shutdown")

    async def shutdown_tools():
        pass

    monkeypatch.setattr(run, "start_runtime", start)
    monkeypatch.setattr(run, "shutdown_batch_system", shutdown_batch)
    monkeypatch.setattr(run, "shutdown_tools", shutdown_tools)
    monkeypatch.setattr(run, "_runtime_up", False)
    return calls


def _args(**kw):
    return SimpleNamespace(verbose=False, session_user="u", session_id=None, task="t",
                           subcommand="run", **kw)


CONFIG = SimpleNamespace(default_agent="broken", plugins=None, llm_system=None)


def test_list_sessions_needs_neither_the_runtime_nor_the_agent(steps, monkeypatch):
    def build(*a, **kw):
        raise AssertionError("the agent was built for a listing")

    monkeypatch.setattr(run, "build_entry_agent", build)
    monkeypatch.setattr(run, "run_session_store_command",
                        lambda args, config, boot, entry_name, vprint: steps.append(f"list:{entry_name}") or True)
    run.run_agent_command(_args(list_sessions="5"), CONFIG, None, lambda line: None)
    assert steps == ["list:broken"]


def test_an_agent_that_cannot_be_built_still_shuts_the_runtime_down(steps, monkeypatch):
    def build(*a, **kw):
        raise SystemExit(1)

    monkeypatch.setattr(run, "build_entry_agent", build)
    with pytest.raises(SystemExit):
        run.run_agent_command(_args(), CONFIG, None, lambda line: None)
    assert steps == ["start", "shutdown"]


def test_a_run_that_shut_down_itself_is_not_shut_down_twice(steps, monkeypatch):
    monkeypatch.setattr(run, "build_entry_agent", lambda *a, **kw: SimpleNamespace())
    monkeypatch.setattr(run, "apply_max_steps", lambda *a, **kw: None)
    monkeypatch.setattr(run, "choose_llm", lambda **kw: run.LLMChoice())
    monkeypatch.setattr(run, "build_task_input", lambda *a, **kw: ("t", {}))
    monkeypatch.setattr(run, "run_in_session", lambda *a, **kw: run.shut_down_runtime(lambda line: None))
    run.run_agent_command(_args(), CONFIG, None, lambda line: None)
    assert steps == ["start", "shutdown"]
