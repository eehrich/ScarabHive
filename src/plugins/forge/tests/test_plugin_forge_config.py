"""The activation chain from the forge plugin to a tool the coder sees, on the
RESOLVED configuration: every link of it fails silently when it breaks."""
from __future__ import annotations

from pathlib import Path

import pytest

from agent_system.config.settings import get_tool_server_config, load_settings
from agent_system.servers.agent.prompt_strategies import PromptContext, PromptRenderer
from agent_system.servers.agent.tool_schema_builder import tool_matches_patterns
from agent_system.skills.registry import SkillRegistry

PLUGIN = Path(__file__).resolve().parent.parent


@pytest.fixture(scope="module")
def config():
    return load_settings()


@pytest.fixture(scope="module")
def coder(config):
    cfg = get_tool_server_config("coder", config)
    assert cfg is not None
    return cfg


def test_the_forge_instance_is_enabled(config):
    cfg = get_tool_server_config("forge", config)
    assert cfg is not None and cfg.enabled and cfg.type == "forge"


def test_the_plugin_yaml_sets_nothing_an_operator_tunes():
    """It is merged after config/plugins.yaml: any value here would silently
    override the operator's."""
    import yaml
    entry = yaml.safe_load((PLUGIN / "agents" / "forge.yaml").read_text(encoding="utf-8"))["plugins"]["servers"]["forge"]
    assert set(entry) == {"type", "enabled", "description"}


@pytest.mark.parametrize("tool", ["forge_checkout", "forge_push", "forge_pr_merge", "forge_ci_job_log"])
def test_the_coder_may_call_the_forge_tools(coder, tool):
    assert tool_matches_patterns(tool, "forge", coder.agent_config.tools.allowed)


def test_the_workflow_skill_is_found_and_offered_to_the_coder(config, coder):
    registry = SkillRegistry()
    registry.discover(list(getattr(getattr(config, "skills", None), "skill_dirs", None) or []))
    assert registry.get("forge-workflow") is not None
    assert "forge-workflow" in coder.agent_config.skills.on_demand


def render(coder, config, tools):
    ctx = PromptContext(agent_name="coder", agent_config=coder.agent_config, system_config=config,
                        available_tools=tools, max_steps=300, current_step=0, agent_instance=object())
    return PromptRenderer().render(ctx)[0]


def test_the_prompt_speaks_of_forge_only_when_its_tools_are_there(coder, config):
    assert "## GitLab / GitHub" not in render(coder, config, [])
    shown = render(coder, config, ["forge_checkout", "forge_push"])
    assert "## GitLab / GitHub" in shown and "forge-workflow" in shown


def test_the_claude_code_section_points_to_forge_only_when_forge_is_there(coder, config):
    """Its "never commit it" has a forge exception -- named only where the
    forge section it points to is rendered."""
    alone = render(coder, config, ["coding_cli_run_task"])
    both = render(coder, config, ["coding_cli_run_task", "forge_checkout"])
    assert "Never merge it, never commit it." in alone and "forge section below" not in alone
    assert "commit it on the ticket branch, as the forge section below" in both
