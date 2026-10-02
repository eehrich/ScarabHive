"""A machine's runner is chosen by its folder (runners.py): the runner whose runs_machines_in holds the machine's
file, else the instance's runner_agent. Validation, the run's backend, the catalog and get_machine all ask it."""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from plugins.stategraph import runners
from plugins.stategraph.engine.backend import inject
from plugins.stategraph.runners import runner_folders, runner_of

MACHINE = """\
stategraph: 1
id: {id}
initial: put
states:
  put:
    do: {{tool: vault_put, args: {{}}}}
    transitions: [{{target: done}}]
  done: {{type: final}}
"""


def servers(**entries):
    return SimpleNamespace(plugins=SimpleNamespace(servers={name: SimpleNamespace(**entry)
                                                            for name, entry in entries.items()}))


def test_the_deepest_claimed_folder_decides_and_an_unclaimed_one_takes_the_default(tmp_path):
    (tmp_path / "shipped" / "inner").mkdir(parents=True)
    config = servers(outer={"enabled": True, "runs_machines_in": [str(tmp_path / "shipped")]},
                     inner={"enabled": True, "runs_machines_in": str(tmp_path / "shipped" / "inner")},
                     off={"enabled": False, "runs_machines_in": [str(tmp_path / "data")]},
                     plain={"enabled": True})

    assert runner_of(config, str(tmp_path / "shipped" / "m.yaml"), "default") == ("outer", None)
    assert runner_of(config, str(tmp_path / "shipped" / "inner" / "m.yaml"), "default") == ("inner", None)
    assert runner_of(config, str(tmp_path / "data" / "m.yaml"), "default") == ("default", None), "a disabled one claims nothing"
    assert runner_of(config, None, "default") == ("default", None)
    assert sorted(runner_folders(config)) == ["inner", "outer"]


def test_a_sibling_folder_is_not_claimed_and_a_relative_one_hangs_off_the_project(tmp_path, monkeypatch):
    for folder in ("machines", "machines2"):
        (tmp_path / folder).mkdir()
    monkeypatch.setattr(runners, "_project_root", lambda: tmp_path)
    monkeypatch.chdir(tmp_path.parent)  # not the cwd: the project root
    config = servers(shipped={"enabled": True, "runs_machines_in": ["machines"]},
                     typo={"enabled": True, "runs_machines_in": True})

    assert runner_of(config, str(tmp_path / "machines" / "m.yaml"), "default") == ("shipped", None)
    assert runner_of(config, str(tmp_path / "machines2" / "m.yaml"), "default") == ("default", None)
    assert list(runner_folders(config)) == ["shipped"], "a key that is no folder is left out, not raised"


def test_a_folder_two_runners_claim_has_no_runner_and_says_so(tmp_path):
    (tmp_path / "shipped").mkdir()
    config = servers(a={"enabled": True, "runs_machines_in": [str(tmp_path / "shipped")]},
                     b={"enabled": True, "runs_machines_in": [str(tmp_path / "*")]})

    name, problem = runner_of(config, str(tmp_path / "shipped" / "m.yaml"), "default")

    assert name == "default" and "runners a, b both claim" in problem, problem


# ------------------------------------------------------------------ through the server

@pytest.fixture
async def server(tmp_path):
    from agent_system.config.models import AgentSystemConfig

    from plugins.stategraph.server import StateGraphServer
    from plugins.stategraph.tests.stategraph_testkit import tool_config

    own, shipped = tmp_path / "own", tmp_path / "shipped"
    for folder, machine_id in ((own, "mine"), (shipped, "kept")):
        folder.mkdir()
        (folder / f"{machine_id}.yaml").write_text(MACHINE.format(id=machine_id), encoding="utf-8")
    config = AgentSystemConfig.model_validate({"plugins": {"servers": {
        "stategraph_runner": {"type": "basic_agent", "enabled": True,
                              "agent_config": {"tools": {"allowed": ["store/*"]}}},
        "shipped_runner": {"type": "stategraph_runner", "enabled": True, "runs_machines_in": [str(shipped)],
                           "inject_params": {"vault_*": {"key": "from-the-runner"}},
                           "agent_config": {"tools": {"allowed": ["+vault/*"]}}},
        "store": {"type": "json_store", "enabled": True},
        "vault": {"type": "json_store", "enabled": True},
    }}})
    srv = StateGraphServer("stategraph", config, tool_config(tmp_path, machine_dirs=[str(own), str(shipped)],
                                                             writable_machine_dirs=[str(own)],
                                                             inject_params={"vault_put": {"key": "instance", "tenant": "t"},
                                                                            "vault_*": {"key": "instance", "zone": "z"},
                                                                            "store_*": {"ns": "x"}}))
    yield srv
    await srv.stop_plugin()


def sg007(answer):
    return [p["message"] for p in answer["problems"] if p["code"] == "SG007"]


async def test_a_machine_validates_against_the_runner_of_its_folder(server):
    assert server.runner_for("kept") == ("shipped_runner", None)
    assert server.runner_for("mine") == ("stategraph_runner", None)
    assert server.runner_for(None) == ("stategraph_runner", None), "a new machine: the folder it is saved in"

    kept, mine = server.service.get_machine("kept"), server.service.get_machine("mine")
    assert sg007(kept) == [] and kept["runner"] == "shipped_runner"
    assert sg007(mine) == ["tool 'vault_put' is not in stategraph_runner's tool allowlist (the runner is the boundary)"]
    # a draft is checked where it will be saved: the folder of the machine it belongs to
    draft = server.service.validate(files={"kept.yaml": MACHINE.format(id="kept")}, machine_id="kept")
    assert sg007(draft) == []


async def test_a_run_gets_its_machine_s_runner_its_tools_and_its_params(server, monkeypatch):
    monkeypatch.setattr(server, "resolve_runner", lambda name=None: SimpleNamespace(name=name))
    rows = {"r1": {"machine_id": "kept"}, "r2": {"machine_id": "mine"},
            "r3": {"machine_id": "mine", "runner": "shipped_runner"}}  # started with it: a resume keeps it
    monkeypatch.setattr(server.service.run_store, "get_run", lambda run_id: rows[run_id])
    make = server.service.backend_factory()

    kept, mine, kept_since = make("r1"), make("r2"), make("r3")

    assert kept.runner.name == "shipped_runner" and mine.runner.name == "stategraph_runner"
    assert kept_since.runner.name == "shipped_runner", "the row's runner, not where the machine lies now"
    assert kept.config_check("tool", "vault_put", {}) is None, "at run time the backend's own runner answers"
    assert "not in stategraph_runner's tool allowlist" in mine.config_check("tool", "vault_put", {})
    assert "is a runner" in kept.config_check("agent", "stategraph_runner", {}), "the default is a runner too"
    # the runner's params over the instance's, param by param, whichever pattern brings them
    assert inject("vault_put", {}, kept.inject_params) == {"key": "from-the-runner", "tenant": "t", "zone": "z"}
    assert inject("vault_put", {}, mine.inject_params) == {"key": "instance", "tenant": "t", "zone": "z"}


async def test_a_run_keeps_the_runner_it_started_with_and_a_fork_of_its_snapshot_too(server, monkeypatch):
    started = {}

    async def start(tree, **kwargs):
        started.update(kwargs)
        return "r9"

    monkeypatch.setattr(server.service.runs, "start", start)
    await server.service.start_run("kept")
    assert started["runner"] == "shipped_runner"

    server.run_store.create_run("r1", "kept", {}, runner="shipped_runner")
    assert server.run_store.get_run("r1")["runner"] == "shipped_runner", "kept in the row"
    forked = {}

    async def fork(run_id, **kwargs):
        forked.update(kwargs)
        return "r10"

    monkeypatch.setattr(server.service.runs, "fork", fork)
    await server.service._fork("r1", {}, None)
    assert forked["runner"] is None, "a snapshot fork: the source's runner (RunManager.fork reads the row)"
    await server.service._fork("r1", {"definition": "current"}, None)
    assert forked["runner"] == "shipped_runner", "onto the current file: the runner of its folder now"


async def test_a_fork_of_the_snapshot_takes_its_source_s_runner(server, monkeypatch):
    server.run_store.create_run("r1", "kept", server.machines.load("kept").snapshot(), runner="shipped_runner")
    started = {}

    async def start(tree, **kwargs):
        started.update(kwargs)
        return "r2"

    monkeypatch.setattr(server.run_manager, "start", start)
    await server.run_manager.fork("r1")
    assert started["runner"] == "shipped_runner"
    await server.run_manager.fork("r1", runner="stategraph_runner")
    assert started["runner"] == "stategraph_runner", "a runner given wins: the current file's folder"


async def test_a_folder_two_runners_claim_runs_no_machine(server):
    shipped = Path(server.machines.find("kept").path).parent
    # no tool activity: no tool check names the conflict, the machine's own problem has to
    (shipped / "quiet.yaml").write_text("stategraph: 1\nid: quiet\ninitial: done\nstates:\n  done: {type: final}\n",
                                        encoding="utf-8")
    servers = server.system_config.plugins.servers
    servers["rival_runner"] = servers["shipped_runner"].model_copy(update={"runs_machines_in": [str(shipped)]})

    machine, quiet = server.service.get_machine("kept"), server.service.get_machine("quiet")

    assert any(p["code"] == "SG007" and "both claim" in p["message"] for p in machine["problems"]), machine["problems"]
    assert [m for m in sg007(quiet) if m.startswith("runners rival_runner, shipped_runner both claim")], quiet["problems"]
    assert "both claim" in machine["runner_problem"] and machine["runner"] == "stategraph_runner"
    for machine_id in ("kept", "quiet"):
        with pytest.raises(Exception, match="both claim"):
            await server.service.start_run(machine_id)


async def test_the_catalog_names_the_open_machine_s_runner_and_no_runner_is_an_agent_to_call(server):
    assert server._catalog("*", "kept")["runner"] == "shipped_runner"
    assert server._catalog("*", "kept")["tools"] == ["store/*", "vault/*"], "inherited, then its own"
    assert server._catalog()["tools"] == ["store/*"]
    check = server.service.config_check()
    assert "is a runner" in check("agent", "shipped_runner", {})


def test_the_runner_s_params_win_whichever_instance_pattern_sets_them_too():
    merged = runners.merged_inject_params({"vault_*": {"key": "i1", "zone": "z"}, "vault_put": {"key": "i2"}},
                                          {"vault_*": {"key": "runner"}})

    assert inject("vault_put", {}, merged) == {"key": "runner", "zone": "z"}
