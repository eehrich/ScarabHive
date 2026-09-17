"""Guards for the gamedev harness configuration.

The gamedev agent is the coder agent plus a handful of swaps, and every one
of those swaps fails silently when it goes wrong: a `!` that removes
nothing, a `+` that lands in a list that was replaced, a hook that still
injects the coder's Python conventions into a Godot task, a sub-agent name
that no registry knows. So each swap is asserted on the RESOLVED config --
``get_tool_server_config`` -- which is where inheritance materialises;
the raw ``config.plugins.servers[...]`` still holds the unmerged child.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from agent_system.config.settings import get_tool_server_config, load_settings
from agent_system.servers.agent.tool_schema_builder import tool_matches_patterns

PLUGIN = Path(__file__).resolve().parent.parent
GODOT_TOOLS = {"status", "setup", "import_assets", "check", "run", "script", "export",
               "scene", "node", "play", "observe", "command"}
EDITOR_TOOLS = {"scene", "node", "play", "observe", "command"}

# "<instance>_<tool>" as prompts and skills spell them. Longest first.
_TOOL_REF = re.compile(r"\b((?:gamedev_okf|gamedev_sam|godot)_[a-z_]+)\b")


@pytest.fixture(scope="module")
def config():
    return load_settings()


def resolved(config, name):
    cfg = get_tool_server_config(name, config)
    assert cfg is not None, f"{name} is not configured"
    return cfg


def allows(agent_cfg, tool: str) -> bool:
    """``"server/tool_name"`` against the agent's resolved allowlist."""
    server, name = tool.split("/", 1)
    return tool_matches_patterns(name, server, agent_cfg.agent_config.tools.allowed)


def hook_field(overrides, hook: str, field: str):
    """Overrides arrive as models or as plain dicts depending on the path."""
    entry = overrides[hook]
    return entry.get(field) if isinstance(entry, dict) else getattr(entry, field, None)


# ── the swaps ─────────────────────────────────────────────────────────────

def test_gamedev_is_the_coder_plus_godot(config):
    gamedev = resolved(config, "gamedev")
    coder = resolved(config, "coder")
    assert gamedev.agent_config.llm_profile == coder.agent_config.llm_profile, \
        "the model chain is inherited, not restated"
    assert gamedev.agent_config.max_steps == coder.agent_config.max_steps
    for tool in ("godot/godot_run", "godot/godot_play", "coder_fs/coder_fs_read_file",
                 "coder_shell/coder_shell_execute", "media_ops/media_ops_load"):
        assert allows(gamedev, tool), f"gamedev cannot reach {tool}"


def test_gamedev_swapped_the_sub_agent_manager_and_the_bundle(config):
    gamedev = resolved(config, "gamedev")
    assert allows(gamedev, "gamedev_sam/gamedev_sam_manage_sub_agent")
    assert allows(gamedev, "gamedev_okf/gamedev_okf_search")
    # The `!` entries must have REMOVED the coder's instances, or the agent
    # has two managers and two bundles and picks one at random.
    assert not allows(gamedev, "coder_sam/coder_sam_manage_sub_agent")
    assert not allows(gamedev, "coder_okf/coder_okf_search")


def test_the_gamedev_bundle_is_injected_and_the_coders_is_not(config):
    overrides = resolved(config, "gamedev").agent_config.hooks.overrides
    assert hook_field(overrides, "coder_okf.okf_context_injection", "enabled") is False
    assert hook_field(overrides, "gamedev_okf.okf_context_injection", "enabled") is True
    bundle = resolved(config, "gamedev").agent_config.template_vars["okf_bundle"]
    assert hook_field(overrides, "gamedev_okf.okf_context_injection", "hook_bundle") == bundle, \
        "the prompt's bundle path and the hook's must be one value"
    root = getattr(config.plugins.servers["gamedev_okf"], "allowed_directories")
    assert bundle in root, "the hook's bundle must lie inside the instance's sandbox, or it injects nothing"


@pytest.mark.parametrize("hook", [
    "todo.inject_todo_tasks",
    "sequential_thinking.inject_active_sessions",
    "coder_sam.inject_sub_agent_context",
    "gamedev_sam.inject_sub_agent_context",
])
def test_no_hook_rewrites_the_cached_prefix_mid_turn(config, hook):
    overrides = resolved(config, "gamedev").agent_config.hooks.overrides
    assert hook in overrides and hook_field(overrides, hook, "enabled") is False, \
        f"{hook} churns the prompt prefix"


def test_gamedev_has_its_own_loop_and_the_coders_on_demand_skills(config):
    skills = resolved(config, "gamedev").agent_config.skills
    assert skills.always == ["gamedev-loop"], "a bare list replaces; coding-harness must be gone"
    assert {"godot-conventions", "asset-pipeline", "adversarial-review"} <= set(skills.on_demand)


def test_every_named_skill_exists(config):
    """skill_dirs are glob patterns (src/plugins*/*/skills), absolute after
    loading -- hence glob.glob, which pathlib refuses for absolute patterns."""
    import glob
    dirs = list(config.skills.skill_dirs)
    for agent in ("gamedev", "godot_agent", "image_agent"):
        skills = resolved(config, agent).agent_config.skills
        for name in [*skills.always, *skills.on_demand]:
            hits = [p for d in dirs for p in glob.glob(str(Path(d) / name / "SKILL.md"))]
            assert hits, f"{agent} names skill {name!r}, no SKILL.md found in {dirs}"


# ── the sub-agents ────────────────────────────────────────────────────────

def test_every_sub_agent_the_manager_allows_is_a_configured_agent(config):
    allowed = getattr(config.plugins.servers["gamedev_sam"], "allowed_agents")
    assert {"coder_explorer", "coder_reviewer", "gamedev_tester", "blender_agent", "image_agent"} == set(allowed)
    for name in allowed:
        server = config.plugins.servers.get(name)
        assert server is not None and server.enabled, f"{name} is allowed but not configured"


def test_the_tester_gets_the_headless_tools_and_none_of_the_editor(config):
    tester = resolved(config, "gamedev_tester")
    for tool in ("godot_check", "godot_run", "godot_script", "godot_status"):
        assert allows(tester, f"godot/{tool}"), f"tester lacks {tool}"
    for tool in EDITOR_TOOLS:
        assert not allows(tester, f"godot/godot_{tool}"), \
            f"tester reaches the editor tool godot_{tool}; two drivers on one editor"
    assert allows(tester, "coder_shell/coder_shell_execute"), "inherited from coder_tester"


def test_the_asset_agents_cannot_touch_the_projects(config):
    """Their deliverable is a path; landing it in a project is the
    orchestrator's job, and their sandboxes say so."""
    blender = resolved(config, "blender_agent")
    image = resolved(config, "image_agent")
    for agent in (blender, image):
        assert not allows(agent, "coder_fs/coder_fs_manage")
        assert not allows(agent, "godot/godot_script")
    images = config.plugins.servers["images"]
    assert getattr(images, "output_directories") == ["data/workspace/images"]
    # Write paths are project-relative (the schema says so, and the read side
    # has always resolved them that way); the sandbox is the only thing that
    # decides where they may land. A second base directory to resolve against
    # is what once doubled the path.
    assert getattr(images, "output_root", None) is None


# ── prompts and skills name only tools that exist ─────────────────────────

def _rendered_godot_tools(config):
    from plugins.godot.plugin import PLUGIN_FACTORY
    server = PLUGIN_FACTORY(name="godot", system_config=config, server_config=config.plugins.servers["godot"])
    return {t["function"]["name"] for t in server.get_tools()}


def test_the_schema_renders_exactly_the_documented_tools(config):
    assert _rendered_godot_tools(config) == {f"godot_{t}" for t in GODOT_TOOLS}


@pytest.mark.parametrize("path", sorted(
    [*PLUGIN.glob("agents/prompts/*.md"), *PLUGIN.glob("skills/*/SKILL.md")]),
    ids=lambda p: p.name if p.name != "SKILL.md" else p.parent.name)
def test_prompts_and_skills_reference_only_tools_that_exist(config, path):
    text = path.read_text(encoding="utf-8")
    rendered = _rendered_godot_tools(config)
    known_prefixes = ("gamedev_okf_", "gamedev_sam_")
    for ref in set(_TOOL_REF.findall(text)):
        if ref.startswith("godot_"):
            assert ref in rendered, f"{path.name} names {ref}, which the godot plugin does not render"
        else:
            assert ref.startswith(known_prefixes)


def test_prompts_carry_no_per_call_template_variables(config):
    """A variable that changes per call would break the cached prefix."""
    for path in PLUGIN.glob("agents/prompts/*.md"):
        found = set(re.findall(r"{{\s*(\w+)\s*}}", path.read_text(encoding="utf-8")))
        assert found <= {"okf_bundle"}, f"{path.name} uses {found}"
