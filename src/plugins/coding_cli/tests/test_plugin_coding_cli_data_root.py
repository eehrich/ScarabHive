"""Whose runs an instance sweeps: CODING_CLI_DATA_ROOT's, else data/coding_cli.

Every started instance sweeps its root (start_plugin): it adopts the runs
whose owner is gone and rings the wakes of the ended ones -- and a rung wake
is used up. The root hung off the project, not the working directory, so an
app a test starts in a temp directory swept the operator's data/coding_cli
and could ring a wake the operator's API was to deliver. The variable gives
such a process a root of its own; unset, nothing changes.
"""
import asyncio
import json

import pytest

from agent_system.config.models import AgentSystemConfig, ToolServerConfig
from plugins.coding_cli import server as server_module
from plugins.coding_cli.server import DATA_ROOT_ENV, CodingCliServer

USER = "admin"


def _ended_run_with_wake(root, run_id, session_id):
    """An ended run whose session asked to be woken, as its owner leaves it on disk."""
    runs = root / "runs"
    runs.mkdir(parents=True)
    (runs / f"{run_id}.json").write_text(
        json.dumps({"run_id": run_id, "state": "done", "user_id": USER}), encoding="utf-8")
    wake = runs / f"{run_id}.wake"
    wake.write_text(json.dumps({"session_id": session_id, "user_id": USER}), encoding="utf-8")
    return wake


@pytest.fixture
def roots(tmp_path, monkeypatch):
    """Four roots, each with one ended run waiting to ring its own session."""
    project = tmp_path / "project"
    monkeypatch.setattr(server_module, "PROJECT_ROOT", project)
    monkeypatch.setattr(server_module, "DATA_ROOT", None)
    monkeypatch.delenv(DATA_ROOT_ENV, raising=False)
    candidates = {
        "operator": project / "data" / "coding_cli",   # the default
        "absolute": tmp_path / "own",
        "relative": project / "own",
        "module": tmp_path / "tests",                   # what the plugin's tests set
    }
    wakes = {name: _ended_run_with_wake(root, f"{index:012x}", f"session-{name}")
             for index, (name, root) in enumerate(candidates.items(), start=1)}
    return {"tmp": tmp_path, "wakes": wakes, "module_root": candidates["module"]}


@pytest.fixture
def rings(monkeypatch):
    """wake_session as a recorder; every session may be woken."""
    rung = []

    async def wake(system_config, session_id, user_id, what="", still_needed=None):
        rung.append(session_id)
        return "woke_session"

    monkeypatch.setattr(server_module, "wake_session", wake)
    monkeypatch.setattr(server_module, "wake_blocked", lambda *args: "")
    monkeypatch.setattr(server_module, "wake_depth", lambda: 0)
    return rung


async def _started_and_stopped():
    """An instance's start: its first sweep, the rings it starts -- then its stop."""
    server = CodingCliServer("coding_cli", AgentSystemConfig(), ToolServerConfig())
    await server.start_plugin()
    try:
        await asyncio.wait_for(asyncio.gather(*list(server._rings.values())), 10)
    finally:
        await server.stop_plugin()


@pytest.mark.parametrize("variable, module_root, swept", [
    (None, False, "operator"),
    ("absolute", False, "absolute"),
    ("relative", False, "relative"),
    # The plugin's tests keep their tmp dir even under a shell that exports the variable.
    ("absolute", True, "module"),
])
async def test_an_instance_rings_the_wakes_of_its_own_root_only(roots, rings, monkeypatch,
                                                               variable, module_root, swept):
    if variable == "absolute":
        monkeypatch.setenv(DATA_ROOT_ENV, str(roots["tmp"] / "own"))
    elif variable == "relative":
        monkeypatch.setenv(DATA_ROOT_ENV, "own")
    if module_root:
        monkeypatch.setattr(server_module, "DATA_ROOT", roots["module_root"])

    await _started_and_stopped()

    assert rings == [f"session-{swept}"]
    for name, wake in roots["wakes"].items():
        if name == swept:
            assert not wake.exists(), "the rung wake was not used up"
        else:
            assert wake.exists(), f"the wake in the {name} root was used up"
            assert not wake.with_suffix(".rung").exists(), f"the {name} root's ring was taken"
