"""The n8n entries as the framework resolves them.

Every link of the activation chain fails silently when it breaks: an
allowlist pattern that matches nothing, a skill name no directory carries, a
tool the prompt names that the schema does not have. So each is asserted on
the RESOLVED config (``get_tool_server_config``), the way an agent sees it.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

from agent_system.config.settings import get_tool_server_config, load_settings
from agent_system.servers.agent.tool_schema_builder import tool_matches_patterns
from agent_system.skills.registry import SkillRegistry

PLUGIN = Path(__file__).resolve().parent.parent
BUILDER_TOOLS = {"search_nodes", "get_node_types", "explore_node_resources", "get_best_practices",
                 "get_sdk_reference", "list_credentials", "validate_node_config", "validate_workflow",
                 "create_workflow", "update_workflow", "get_workflow", "list_workflows",
                 "test_workflow", "get_execution", "list_executions"}
HUMAN_ONLY = {"publish_workflow", "unpublish_workflow", "archive_workflow", "trigger_workflow",
              "execute_workflow", "delete_workflow"}
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


def schema_tools() -> set[str]:
    text = (PLUGIN / "schema.yaml").read_text(encoding="utf-8")
    return set(re.findall(r'name: "\{\{ name \}\}_([a-z_]+)"', text))


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
    assert not {t for t in HUMAN_ONLY if allows(agent, t)}, "publishing and triggering are for a human"


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
    named = {ref for text in texts for ref in _TOOL_REF.findall(text)} - {"agent"}
    assert named <= schema_tools(), f"prompt/skills name tools that do not exist: {named - schema_tools()}"


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
