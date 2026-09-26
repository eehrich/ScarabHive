"""The stategraph entries as the framework resolves them.

Every link of the activation chain fails silently when it breaks: an allowlist
pattern that matches nothing, a skill another root shadows, a prompt Jinja
cannot parse, an example machine naming an agent that is not configured. So
each is asserted on the RESOLVED config
(``load_settings`` + ``get_tool_server_config``), the way the runtime sees it.

Mutation checks run (each turned the named test red, then was restored from a copy):
- stategraph.yaml: ``enabled: false`` on stategraph_json          -> test_the_instances_are_enabled
- stategraph.yaml: ``runner_agent: stategraph_runnr``              -> test_the_plugin_names_an_enabled_runner_agent
- author yaml: send_event entry removed / ``+stategraph/*`` / ``+skills/*`` removed
                                                                    -> test_the_author_may_call_exactly_the_tools_its_loop_needs
- stategraph.yaml: runner allows ``stategraph/stategraph_run_machine`` -> test_the_runner_may_call_no_stategraph_tool
- stategraph.yaml: runner allows ``no_such_server/*``               -> test_every_runner_pattern_names_an_enabled_server
- author yaml ``always: []``; SKILL.md ``name`` changed; a reference renamed; a same-named skill
  in .claude/skills                                                -> test_the_author_loads_the_skill_from_the_plugin
- author prompt: ``{{ broken``                                     -> test_the_prompt_renders
- author prompt names ``stategraph_delete_machine``                -> test_prompt_and_skill_name_only_tools_the_author_may_call
- patterns.md names ``stategraph_fly``                             -> test_the_references_name_only_real_tools
- hello_agent.yaml: ``agent: coder``                               -> test_every_shipped_machine_validates_against_the_shipped_config
- stategraph.yaml: machine root glob ``src/plugins*/*/machine``    -> test_the_machine_roots_find_the_shipped_machines
- patterns.md review_loop: ``agent: coder``                        -> test_every_machine_in_the_docs_validates
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml
from jinja2 import Environment

from agent_system.config.settings import get_tool_server_config, load_settings
from agent_system.servers.agent.tool_schema_builder import tool_matches_patterns
from agent_system.skills.registry import SkillRegistry
from agent_system.utils.prompt_renderer import render_prompts
from plugins.stategraph.engine.backend import make_config_check
from plugins.stategraph.model.loader import load_tree
from plugins.stategraph.model.validate import validate_tree
from plugins.stategraph.store import FileSources, MachineStore
from plugins.stategraph.tests.stategraph_testkit import FASTAPI_PY314

pytestmark = pytest.mark.filterwarnings(FASTAPI_PY314)

PLUGIN = Path(__file__).resolve().parent.parent
REPO = PLUGIN.parents[2]
SKILL = PLUGIN / "skills" / "stategraph-authoring"
INSTANCE = "stategraph"
# The author's loop (design §9): look up, write, validate, save, test-run, read the
# run, drive a waiting test run with an event, control its own runs.
AUTHOR_TOOLS = {"catalog", "list_machines", "get_machine", "validate_machine", "save_machine",
                "run_machine", "get_run", "control_run", "send_event"}
# Instance names that share the tools' prefix; the docs name them, they are not tools.
NOT_TOOLS = {"author", "runner", "json", "json_manage_json", "design"}
_TOOL_REF = re.compile(r"\bstategraph_([a-z_]+)\b")
_FENCE = re.compile(r"```(\w+)\n(.*?)```", re.S)


@pytest.fixture(scope="module")
def config():
    return load_settings()


def resolved(config, name):
    cfg = get_tool_server_config(name, config)
    assert cfg is not None, f"{name} is not configured"
    return cfg


def schema_tools() -> set[str]:
    text = (PLUGIN / "schema.yaml").read_text(encoding="utf-8")
    return set(re.findall(r'name: "\{\{ name \}\}_([a-z_]+)"', text))


def allowed_stategraph_tools(agent_cfg) -> set[str]:
    allowed = agent_cfg.agent_config.tools.allowed
    return {tool for tool in schema_tools() if tool_matches_patterns(f"{INSTANCE}_{tool}", INSTANCE, allowed)}


def config_check(config):
    plugin = resolved(config, INSTANCE)
    return make_config_check(config, runner=plugin.runner_agent, own_instance=INSTANCE)


def machine_store(config) -> MachineStore:
    plugin = resolved(config, INSTANCE)
    return MachineStore(plugin.machine_dirs, plugin.writable_machine_dirs, base=REPO)


def problems_of(tree) -> list[str]:
    return [f"{p.level} {p.code} {Path(p.file).name}:{p.line} {p.path}: {p.message}" for p in tree.problems]


# ---------------------------------------------------------------- instances

@pytest.mark.parametrize("name, kind", [
    ("stategraph", "stategraph"), ("stategraph_runner", "basic_agent"),
    ("stategraph_json", "json_store"), ("stategraph_author", "basic_agent")])
def test_the_instances_are_enabled(config, name, kind):
    cfg = resolved(config, name)
    assert cfg.enabled, f"{name} is off (enabled defaults to false)"
    assert cfg.type == kind


def test_the_plugin_names_an_enabled_runner_agent(config):
    plugin = resolved(config, INSTANCE)
    runner = resolved(config, plugin.runner_agent)
    assert runner.enabled and runner.type == "basic_agent", f"runner_agent {plugin.runner_agent} is no enabled agent"


# ---------------------------------------------------------------- allowlists

def test_the_author_may_call_exactly_the_tools_its_loop_needs(config):
    author = resolved(config, "stategraph_author")
    assert AUTHOR_TOOLS <= schema_tools(), f"no such tools: {AUTHOR_TOOLS - schema_tools()}"
    assert allowed_stategraph_tools(author) == AUTHOR_TOOLS
    # explicit entries: a stategraph tool added later is not granted by a wildcard
    assert not tool_matches_patterns(f"{INSTANCE}_tool_added_later", INSTANCE, author.agent_config.tools.allowed)
    assert tool_matches_patterns("skills_read", "skills", author.agent_config.tools.allowed), \
        "the author cannot read its skill's references"


def test_the_runner_may_call_no_stategraph_tool(config):
    """Design §8.3: a machine must not save, start or control machines."""
    runner = resolved(config, resolved(config, INSTANCE).runner_agent)
    assert allowed_stategraph_tools(runner) == set()
    assert not tool_matches_patterns(f"{INSTANCE}_tool_added_later", INSTANCE, runner.agent_config.tools.allowed)


def test_every_runner_pattern_names_an_enabled_server(config):
    """A pattern for a server that does not exist is a dead entry nobody notices."""
    runner = resolved(config, resolved(config, INSTANCE).runner_agent)
    patterns = runner.agent_config.tools.allowed
    assert patterns, "the runner may call nothing: tool activities would all be denied"
    for pattern in patterns:
        server = pattern.split("/", 1)[0]
        assert "*" not in server, f"{pattern}: name the server explicitly"
        assert resolved(config, server).enabled, f"{pattern}: {server} is not enabled"
    assert tool_matches_patterns("stategraph_json_manage_json", "stategraph_json", patterns), \
        "the store the docs and examples use is not callable"


# ---------------------------------------------------------------- skill and prompts

def test_the_author_loads_the_skill_from_the_plugin(config):
    """``.claude/skills`` is scanned first: a same-named skill there would shadow the plugin's."""
    author = resolved(config, "stategraph_author")
    assert SKILL.name in author.agent_config.skills.always
    registry = SkillRegistry()
    registry.discover(list(config.skills.skill_dirs))
    found = registry.get(SKILL.name)
    assert found is not None, f"{SKILL.name} is not discovered"
    assert Path(found.path).resolve() == SKILL, f"{SKILL.name} resolves to {found.path}"
    front = yaml.safe_load((SKILL / "SKILL.md").read_text(encoding="utf-8").split("---")[1])
    assert front["name"] == SKILL.name and front["description"]
    named = set(re.findall(r"references/[a-z_]+\.md", (SKILL / "SKILL.md").read_text(encoding="utf-8")))
    assert named, "SKILL.md points to no reference"
    for reference in named:
        assert (SKILL / reference).is_file(), f"SKILL.md names {reference}, which does not exist"


@pytest.mark.parametrize("agent", ["stategraph_author", "stategraph_runner"])
def test_the_prompt_renders(config, agent):
    cfg = resolved(config, agent)
    template = Path(cfg.agent_config.system_template)
    raw = template.read_text(encoding="utf-8")
    Environment().parse(raw)  # a Jinja error makes render_prompts fall back to the raw text, silently
    rendered = render_prompts(str(template), dict(cfg.agent_config.template_vars or {}))["system_prompt"]
    assert rendered.strip() and "{{" not in rendered and "{%" not in rendered


def test_prompt_and_skill_name_only_tools_the_author_may_call(config):
    """What sits in the author's system prompt must not send it to a tool it cannot call."""
    author = resolved(config, "stategraph_author")
    texts = [Path(author.agent_config.system_template).read_text(encoding="utf-8"),
             (SKILL / "SKILL.md").read_text(encoding="utf-8")]
    named = {ref for text in texts for ref in _TOOL_REF.findall(text)} - NOT_TOOLS
    assert named, "the prompt names no stategraph tool -- this test would be vacuous"
    assert named <= allowed_stategraph_tools(author), f"named but not callable: {named - allowed_stategraph_tools(author)}"


def test_the_references_name_only_real_tools():
    texts = [path.read_text(encoding="utf-8") for path in (SKILL / "references").glob("*.md")]
    named = {ref for text in texts for ref in _TOOL_REF.findall(text)} - NOT_TOOLS
    assert named <= schema_tools(), f"the references name tools that do not exist: {named - schema_tools()}"


# ---------------------------------------------------------------- machines

SHIPPED = sorted(path.name for path in (PLUGIN / "machines").glob("*.yaml"))


def test_the_machine_roots_find_the_shipped_machines(config):
    assert SHIPPED, "no shipped machine -- the tests below would be vacuous"
    found = {machine.id: machine.path.resolve() for machine in machine_store(config).list()}
    for name in SHIPPED:
        assert Path(name).stem in found, f"{name} is not in any configured machine root"


@pytest.mark.parametrize("name", SHIPPED)
def test_every_shipped_machine_validates_against_the_shipped_config(config, name):
    """SG007 checks that agents are configured and tools are in the runner's allowlist: config and examples
    must agree."""
    path = PLUGIN / "machines" / name
    tree = validate_tree(load_tree(str(path), FileSources(machine_store(config))), config_check(config))
    assert tree.files[str(path)].spec is not None, problems_of(tree)
    assert not tree.problems, problems_of(tree)


def skill_machines() -> list[tuple[str, str, dict[str, str]]]:
    """``(label, yaml, companions)`` for every complete machine in the skill and the format cheat sheet."""
    found = []
    for doc in [SKILL / "SKILL.md", *sorted((SKILL / "references").glob("*.md")), PLUGIN / "docs" / "format.md"]:
        blocks = _FENCE.findall(doc.read_text(encoding="utf-8"))
        companions = {body.splitlines()[0][2:].strip(): body for lang, body in blocks
                      if lang == "python" and body.startswith("# ") and body.splitlines()[0].endswith(".py")}
        for lang, body in blocks:
            if lang == "yaml" and body.startswith("stategraph: 1"):
                machine_id = re.search(r"^id: (\w+)", body, re.M).group(1)
                found.append((f"{doc.name}:{machine_id}", body, companions))
    return found


SKILL_MACHINES = skill_machines()


def test_the_skill_carries_complete_machines():
    assert len(SKILL_MACHINES) >= 5, "the extraction finds (almost) no machine -- the test below would be vacuous"


@pytest.mark.parametrize("label, text, companions", SKILL_MACHINES, ids=[m[0] for m in SKILL_MACHINES])
def test_every_machine_in_the_docs_validates(config, label, text, companions):
    """The examples the author learns from must be valid machines under the shipped config.

    They are placed next to the shipped machines, so ``./critique_round.yaml`` imports resolve.
    """
    machines = PLUGIN / "machines"
    machine_id = label.split(":", 1)[1]
    path = str(machines / f"{machine_id}.yaml")
    overrides = {path: text, **{str(machines / name): source for name, source in companions.items()}}
    tree = validate_tree(load_tree(path, FileSources(machine_store(config), overrides)), config_check(config))
    assert tree.files[path].spec is not None, problems_of(tree)
    assert not tree.problems, problems_of(tree)
