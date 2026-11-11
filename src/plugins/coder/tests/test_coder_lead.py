"""Guards for coder_lead: the coder that hands a change to a dynamic workflow.

* it may write and start workflows, and their results come by themselves -- both delivery hooks, or the
  first hands a result in that is never marked delivered
* the machines its skill ships validate with coder_lead as their author: a workflow may name only what its
  author could reach, and a machine that names more is refused when the model runs it
* coder and gamedev stay usable in machines: saved machines name `coder` as their builder, and an agent that
  may start workflows may appear in none
* the prompt's workflow section is there exactly when the tool is
"""
from __future__ import annotations

import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_system.config.settings import get_tool_server_config, load_settings
from agent_system.servers.agent.tool_schema_builder import tool_matches_patterns

MACHINES = Path(__file__).resolve().parents[1] / "skills" / "coder-workflows" / "machines"


@pytest.fixture(scope="module")
def config():
    return load_settings()


def _agent_config(config, name):
    server = get_tool_server_config(name, config)  # inheritance applied, as the agent runs
    assert server is not None, f"agent {name} is not configured"
    return server.agent_config


def _machine_check(config, caller=None):
    from plugins.stategraph.engine.backend import make_config_check

    return make_config_check(config, runner="stategraph_runner", own_instance="stategraph",
                             default_runner="stategraph_runner", caller=caller)


def test_the_lead_may_write_and_start_workflows_and_gets_their_results(config):
    from plugins.stategraph.server import StateGraphServer

    lead = _agent_config(config, "coder_lead")
    for tool in ("stategraph_catalog", "stategraph_validate_machine", "stategraph_run_workflow",
                 "stategraph_get_run"):
        assert tool_matches_patterns(tool, "stategraph", lead.tools.allowed), tool
    assert "coder-workflows" in lead.skills.on_demand and "stategraph-workflows" in lead.skills.on_demand
    delivers = StateGraphServer.delivers_results(SimpleNamespace(name="stategraph"), SimpleNamespace(agent_config=lead))
    assert delivers is True, "coder_lead does not switch on both delivery hooks"


def test_the_shipped_workflows_validate_with_the_lead_as_their_author(config):
    from plugins.stategraph.model.loader import parse_yaml
    from plugins.stategraph.model.validate import validate_tree
    from plugins.stategraph.store import MachineStore
    from plugins.stategraph.workflows import inline_problems

    paths = sorted(MACHINES.glob("*.yaml"))
    assert len(paths) >= 2, f"fixture: the skill ships no machines in {MACHINES}"
    check = _machine_check(config, caller="coder_lead")
    named = set()
    for path in paths:
        text = path.read_text(encoding="utf-8")
        data = parse_yaml(text)
        assert inline_problems(data) == [], path.name
        with tempfile.TemporaryDirectory() as tmp:
            tree = validate_tree(MachineStore([], [tmp]).load_files({path.name: text}, machine_id=data["id"]), check)
        errors = [f"{p.code} {p.path} {p.message}" for p in tree.problems if p.level == "error"]
        assert errors == [], f"{path.name}: {errors}"
        named |= set(_agents_named(data))
    assert {"coder", "coder_tester", "coder_reviewer"} <= named, f"fixture: the machines name {sorted(named)}"


def _agents_named(node):
    if isinstance(node, dict):
        if isinstance(node.get("agent"), str):
            yield node["agent"]
        for value in node.values():
            yield from _agents_named(value)
    elif isinstance(node, list):
        for value in node:
            yield from _agents_named(value)


def test_the_coder_and_gamedev_stay_usable_in_machines(config):
    check = _machine_check(config)
    assert check("agent", "coder", {}) is None
    assert check("agent", "gamedev", {}) is None
    assert check("agent", "coder_lead", {}), "coder_lead may start workflows, so no machine may name it"


def _rendered_prompt(config, tools):
    from agent_system.servers.agent.prompt_strategies import PromptContext, PromptRenderer

    ctx = PromptContext(agent_name="coder_lead", agent_config=_agent_config(config, "coder_lead"),
                        system_config=config, available_tools=tools, max_steps=300,
                        current_step=0, agent_instance=object())
    rendered, _ = PromptRenderer().render(ctx)
    return rendered


def test_the_workflow_section_is_there_exactly_when_the_tool_is(config):
    """Without the tool the coder is told who has it: asked for a workflow, it searched the checkout for the
    tools instead."""
    with_tool = _rendered_prompt(config, ["stategraph", "stategraph_run_workflow", "stategraph_get_run"])
    without = _rendered_prompt(config, ["coder_fs", "coder_fs_read_file"])

    assert "coder-workflows" in with_tool and "You cannot start a workflow" not in with_tool
    assert "coder-workflows" not in without and "You cannot start a workflow" in without
