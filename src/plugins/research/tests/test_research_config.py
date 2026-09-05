"""Guards for the research plugin's configuration.

Config only: a YAML, a prompt file and a skill that the core finds by
convention -- and each of those conventions fails quietly. So every guard
here asserts on the RESOLVED configuration, on real skill discovery, and on
the prompt as it actually renders.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from agent_system.config.settings import get_mcp_config_by_name, load_settings
from agent_system.servers.agent.prompt_strategies import PromptContext, PromptRenderer
from agent_system.servers.agent.tool_schema_builder import tool_matches_patterns
from agent_system.skills.registry import SkillRegistry

PLUGIN = Path(__file__).resolve().parent.parent
SKILL = "web-research"
# The shared tool instances the agent is built on. All in config/plugins.yaml.
INSTANCES = ("tavily_search", "duckduckgo_search", "web_scraper", "datetime")


@pytest.fixture(scope="module")
def config():
    return load_settings()


@pytest.fixture(scope="module")
def agent(config):
    cfg = get_mcp_config_by_name("research_agent", config)
    assert cfg is not None, "research_agent is not configured"
    return cfg


@pytest.fixture(scope="module")
def skills(config):
    registry = SkillRegistry()
    registry.discover(list(getattr(getattr(config, "skills", None), "skill_dirs", None) or []))
    return registry


def allows(agent_cfg, tool: str) -> bool:
    server, name = tool.split("/", 1)
    return tool_matches_patterns(name, server, agent_cfg.agent_config.tools.allowed)


# ── the agent ─────────────────────────────────────────────────────────────

def test_the_prompt_that_actually_renders_is_this_agents_own(agent, config):
    """Rendered for real: a raw system_prompt inherited from anywhere would
    beat the template file without an error (see the amiga plugin's history),
    and a skill that is not discovered is simply absent."""
    ctx = PromptContext(agent_name="research_agent", agent_config=agent.agent_config,
                        system_config=config, available_tools=[], max_steps=40,
                        current_step=0, agent_instance=object())
    rendered, _ = PromptRenderer().render(ctx)
    assert "You are a web research agent" in rendered
    assert "You are a skills agent" not in rendered
    assert "# Web research" in rendered, "the always-skill is not in the prompt"
    assert "Rank sources before you read" in rendered


def test_the_prompt_is_a_file_next_to_the_agent(agent):
    template = Path(agent.agent_config.system_template)
    assert template.is_file(), template
    assert template.parent == PLUGIN / "agents" / "prompts"


def test_every_tool_the_prompt_names_is_one_the_agent_may_call(agent, config):
    """The prompt tells the agent which tool to search and read with. A name
    that does not render is an instruction to do the impossible."""
    body = Path(agent.agent_config.system_template).read_text(encoding="utf-8")
    names = set(re.findall(r"\b([a-z_]+_(?:web_search|extract|page|download|manage_sub_agent))\b", body))
    assert names, "the prompt names no tool at all"
    servers = config.plugins.servers
    for name in names:
        owner = next((s for s in servers if name.startswith(s + "_")), None)
        assert owner is not None, f"{name}: no configured server owns this tool name"
        assert allows(agent, f"{owner}/{name}"), f"the prompt names {name}, but it does not render"


def test_it_has_search_reading_and_nothing_that_touches_the_machine(agent):
    for tool in ("tavily_search/tavily_search_web_search", "duckduckgo_search/duckduckgo_search_web_search",
                 "web_scraper/web_scraper_page", "web_scraper/web_scraper_download", "datetime/datetime_operations"):
        assert allows(agent, tool), tool
    for tool in ("terminal/terminal_execute", "file_ops/file_ops_manage", "coder_fs/coder_fs_manage"):
        assert not allows(agent, tool), f"{tool} has no business in a research agent"


def test_the_tool_instances_it_is_built_on_exist_and_are_on(config):
    for name in INSTANCES:
        cfg = get_mcp_config_by_name(name, config)
        assert cfg is not None and cfg.enabled, f"{name} is not a configured, enabled server"
    scraper = get_mcp_config_by_name("web_scraper", config)
    dirs = [Path(d) for d in getattr(scraper, "allowed_directories", None) or []]
    assert Path("data/workspace") in dirs, "download must land where the harnesses read"


def test_the_model_chain_falls_over_to_another_route(agent, config):
    chain = agent.agent_config.llm_profile
    assert chain == ["or-deepseek-flash", "deepseek-chat"]
    profiles = set(getattr(config.llm_system, "profiles", None) or {})
    assert not set(chain) - profiles, "a chain member that is in no profile file is silently skipped"


def test_context_engineering_is_on_for_forty_steps_of_pages(agent):
    overrides = getattr(agent.agent_config.hooks, "overrides", None) or {}
    entry = overrides.get("context_engineer.engineer_context")
    assert entry is not None
    enabled = entry.get("enabled") if isinstance(entry, dict) else getattr(entry, "enabled", None)
    assert agent.agent_config.hooks.enabled is True and enabled is True


def test_it_is_usable_from_the_ui_and_by_other_agents(agent):
    assert agent.metadata.visibility == "both"


def test_it_can_fork_itself_for_a_wide_question_but_only_one_level_deep(agent, config):
    """Parallel sub-researchers are for wide questions. The manager spawns
    only this agent, and a fork cannot fork again -- otherwise one wide
    question becomes a tree of searches."""
    sam = get_mcp_config_by_name("research_sam", config)
    assert sam is not None and sam.enabled
    assert getattr(sam, "allowed_agents") == ["research_agent"]
    assert getattr(sam, "max_nesting_depth") == 2
    assert getattr(sam, "max_sub_agents_per_type") <= 4, "four parallel forks is the width cap"
    assert allows(agent, "research_sam/research_sam_manage_sub_agent")


# ── where it is spawned ───────────────────────────────────────────────────

@pytest.mark.parametrize("manager", ["sub_agent_manager", "sysadmin_agent_manager"])
def test_it_is_registered_with_the_managers_that_used_to_spawn_its_predecessor(config, manager):
    cfg = get_mcp_config_by_name(manager, config)
    assert cfg is not None, manager
    assert "research_agent" in getattr(cfg, "allowed_agents", [])


def test_the_predecessors_are_gone_from_every_config_and_agent_prompt():
    """web_research_agent and meta_web_research_agent were deleted. A stale
    name in an allowlist or a prompt is not an error anywhere -- the spawn
    just fails at runtime."""
    stale = re.compile(r"\b(meta_)?web_research_agent\b")

    def live_lines(path: Path):
        """What the runtime reads: YAML without its comments, prompt files,
        code. READMEs and comments may tell the history."""
        text = path.read_text(encoding="utf-8", errors="ignore")
        if path.suffix in (".yaml", ".yml"):
            return [line.split("#", 1)[0] for line in text.splitlines()]
        if path.suffix == ".md":
            return text.splitlines() if "prompts" in path.parts else []
        return text.splitlines()

    hits = []
    for root in (Path("config"), Path("src/plugins"), Path("src/plugins_writer"), Path("src/agent_system")):
        for path in root.rglob("*"):
            if path.suffix in (".yaml", ".yml", ".md", ".py") and path.is_file() and "tests" not in path.parts:
                if any(stale.search(line) for line in live_lines(path)):
                    hits.append(str(path))
    assert not hits, hits


# ── the skill ─────────────────────────────────────────────────────────────

def test_the_skill_is_discovered_from_the_plugin(skills):
    found = skills.get(SKILL)
    assert found is not None, f"{SKILL} is not discovered"
    assert PLUGIN in Path(found.path).parents, f"{SKILL} resolves to {found.path}"


def test_the_agent_asks_for_exactly_the_skill_that_exists(agent, skills):
    assert set(agent.agent_config.skills.always) == {SKILL}
    assert not agent.agent_config.skills.on_demand
    assert skills.get(SKILL) is not None
