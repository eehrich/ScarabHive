"""The n8n entries as the framework resolves them.

Every link of the activation chain fails silently when it breaks: an
allowlist pattern that matches nothing, a skill name no directory carries, a
tool the prompt names that the schema does not have. So each is asserted on
the RESOLVED config (``get_tool_server_config``), the way an agent sees it.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path

import pytest
import yaml

from agent_system.config.settings import get_tool_server_config, load_settings
from agent_system.servers.agent.tool_schema_builder import tool_matches_patterns
from agent_system.skills.registry import SkillRegistry
from agent_system.utils.prompt_renderer import render_prompts
from plugins.n8n import server as server_module

PLUGIN = Path(__file__).resolve().parent.parent
BUILDER_TOOLS = {"search_nodes", "get_node_types", "explore_node_resources", "get_best_practices",
                 "get_sdk_reference", "list_credentials", "validate_node_config", "validate_workflow",
                 "create_workflow", "update_workflow", "get_workflow", "list_workflows",
                 "test_workflow", "get_execution", "list_executions",
                 "publish_workflow", "unpublish_workflow", "archive_workflow", "trigger_workflow"}
# n8n offers these, the plugin never forwards them (design §3.4, §13).
NEVER = {"execute_workflow", "delete_workflow"}
_TOOL_REF = re.compile(r"\bn8n_([a-z_]+)\b")


@pytest.fixture(scope="module")
def config():
    return load_settings()


def resolved(config, name):
    cfg = get_tool_server_config(name, config)
    assert cfg is not None, f"{name} is not configured"
    return cfg


def allows(agent_cfg, tool: str) -> bool:
    return tool_matches_patterns(f"n8n_{tool}", "n8n", agent_cfg.agent_config.tools.allowed)


def schema_tools(plugin: Path = PLUGIN) -> set[str]:
    text = (plugin / "schema.yaml").read_text(encoding="utf-8")
    return set(re.findall(r'name: "\{\{ name \}\}_([a-z_]+)"', text))


OKF_PLUGIN = PLUGIN.parent / "okf"


def test_the_tool_instance_is_on_and_needs_no_variable_in_yaml(config):
    """Keys come from the environment, so an installation without n8n gets no
    warning about unset variables on every start."""
    n8n = resolved(config, "n8n")
    assert n8n.enabled and n8n.type == "n8n"
    raw = (PLUGIN / "agents" / "n8n.yaml").read_text(encoding="utf-8")
    assert "${" not in raw


def test_the_builder_gets_exactly_the_building_tools(config):
    agent = resolved(config, "n8n_agent")
    assert {t for t in BUILDER_TOOLS if allows(agent, t)} == BUILDER_TOOLS
    assert not {t for t in NEVER if allows(agent, t)}
    assert not NEVER & schema_tools()


def test_publishing_is_on_as_the_operator_decided(config):
    """Design E4: the builder publishes when the user asks. The code default is
    off, so the switch has to be in the shipped config."""
    n8n = resolved(config, "n8n")
    assert getattr(n8n, "allow_publish", None) is True


def test_missing_keys_are_warned_about_because_the_builder_wants_the_tools(config):
    assert "n8n_agent" in server_module._agents_wanting(config, "n8n")
    assert server_module._agents_wanting(config, "no_such_instance") == []


def test_every_builder_tool_exists_in_the_schema():
    assert BUILDER_TOOLS <= schema_tools()


def test_prompt_and_skills_exist_and_name_only_real_tools(config):
    agent = resolved(config, "n8n_agent")
    prompt = PLUGIN / "agents" / "prompts" / "n8n_agent.md"
    assert prompt.is_file()
    skills = agent.agent_config.skills.on_demand
    texts = [prompt.read_text(encoding="utf-8")]
    for skill in skills:
        path = PLUGIN / "skills" / skill / "SKILL.md"
        assert path.is_file(), f"skill {skill} has no SKILL.md"
        text = path.read_text(encoding="utf-8")
        front = yaml.safe_load(text.split("---")[1])
        assert front["name"] == skill and front["description"]
        texts.append(text)
    named = {ref for text in texts for ref in _TOOL_REF.findall(text)} - {"agent", "okf"}   # instances
    memory = {ref.removeprefix("okf_") for ref in named if ref.startswith("okf_")}
    named -= {f"okf_{ref}" for ref in memory}
    assert named <= schema_tools(), f"prompt/skills name tools that do not exist: {named - schema_tools()}"
    assert memory and memory <= schema_tools(OKF_PLUGIN), f"no such n8n_okf tools: {memory - schema_tools(OKF_PLUGIN)}"


def test_the_prompt_renders_the_bundle_and_todays_date(config):
    """n8n_okf_append_log takes its date from the caller: without today's date
    in the prompt the model made one up (2026-02-13 on 22.09.)."""
    agent = resolved(config, "n8n_agent")
    prompt = str(PLUGIN / "agents" / "prompts" / "n8n_agent.md")
    rendered = render_prompts(prompt, dict(agent.agent_config.template_vars))["system_prompt"]
    today = datetime.now(timezone.utc).date().isoformat()
    assert "`data/okf/n8n` is your memory" in rendered and f"Today is {today}." in rendered


def test_the_builder_can_search_the_web(config):
    allowed = resolved(config, "n8n_agent").agent_config.tools.allowed
    assert all(tool_matches_patterns(f"tavily_search_{t}", "tavily_search", allowed) for t in ("web_search", "extract"))


def test_the_builder_remembers_in_a_bundle_of_its_own(config):
    """The hook returns silently when hook_bundle is missing or outside the
    sandbox -- which looks exactly like 'nothing remembered yet'."""
    agent = resolved(config, "n8n_agent")
    assert tool_matches_patterns("n8n_okf_write_concept", "n8n_okf", agent.agent_config.tools.allowed)
    roots = resolved(config, "n8n_okf").allowed_directories
    assert len(roots) == 1 and sorted(roots) != sorted(resolved(config, "okf").allowed_directories)
    override = agent.agent_config.hooks.overrides["n8n_okf.okf_context_injection"]
    hook = override if isinstance(override, dict) else vars(override)
    bundle = Path(hook["hook_bundle"]).resolve()
    assert hook.get("enabled") is True and bundle == Path(agent.agent_config.template_vars["okf_bundle"]).resolve()
    root = Path(roots[0]).resolve()
    assert bundle == root or root in bundle.parents


def test_the_builder_is_reachable_as_a_tool(config):
    agent = resolved(config, "n8n_agent")
    assert agent.metadata.visibility == "both"
    assert "n8n_agent_execute_task" in (agent.self_tool_descriptions or {})
    assert tool_matches_patterns("n8n_agent_execute_task", "n8n_agent", ["n8n_agent/*"]), \
        "the allowlist entry the README tells a caller to add"


def test_the_skills_are_discovered_from_the_plugin(config):
    """A skill no directory carries is silently absent from the agent."""
    registry = SkillRegistry()
    registry.discover(list(getattr(getattr(config, "skills", None), "skill_dirs", None) or []))
    for skill in resolved(config, "n8n_agent").agent_config.skills.on_demand:
        found = registry.get(skill)
        assert found is not None, f"{skill} is not discovered"
        assert PLUGIN in Path(found.path).parents, f"{skill} resolves to {found.path}"
