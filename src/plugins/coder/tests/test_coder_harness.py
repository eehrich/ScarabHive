"""Guards for the coder harness configuration.

Everything here is a property of *this plugin* that no other test covers and
that a plausible edit would break silently:

* the reviewer and the explorer cannot write -- the point of the design
* both sandboxes see the same tree -- a reviewer that cannot read what the
  coder wrote reviews nothing, and says nothing about it
* every tool a prompt names actually exists and is visible to that agent
* ``semantic_search`` is reachable AND an index is configured to back it
* the knowledge bundle is reachable by the injection hook AND isolated from
  every other agent's bundle -- both failure modes are silent
* the prompts stay free of per-call template variables, so the cached system
  prompt prefix holds

Deliberately NOT re-tested here: that ``coder_sam.allowed_agents`` resolves and
that the LLM profiles exist. ``tests/config/test_config_no_silent_drift.py``
and ``tests/llm/test_llm_factory_integration.py`` already fail on those, and a
second assertion of the same thing is noise that hides the line that matters.

Scope note: the prompt/tool consistency check below would be worth running over
every agent in the repo, not just these four. It is scoped to this plugin
because widening it is a change to other people's prompts, not to this one.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from agent_system.config.settings import get_tool_server_config, load_settings
from agent_system.servers.agent.tool_schema_builder import tool_matches_patterns

HARNESS_AGENTS = ["coder", "coder_explorer", "coder_reviewer", "coder_tester"]
TOOL_INSTANCES = ["coder_fs_ro", "coder_fs", "coder_shell", "coder_sam", "coder_okf"]

# Tool references in a prompt look like "<instance>_<tool>". Longest instance
# name first, so "coder_fs_ro_read_file" is not read as instance "coder_fs".
_TOOL_REF = re.compile(r"\b(coder_(?:fs_ro|fs|shell|sam|okf)_[a-z_]+)\b")


@pytest.fixture(scope="module")
def config():
    return load_settings()


def _agent_config(config, name):
    """The config the agent really runs with -- inheritance applied.

    NOT ``config.plugins.servers[name]``: that raw entry still carries the merge
    prefixes ("+coder_fs/*"), and every pattern here is matched against a tool
    name. "+coder_fs/*" matches nothing, so each check below silently read as
    "the agent may not use this tool" and the whole file failed on tools the
    agent has all along.
    """
    server = get_tool_server_config(name, config)
    assert server is not None, f"agent {name} is not configured"
    return server.agent_config


def _rendered_tool_names(config, instance_name):
    """The tools an instance actually exposes, straight from the plugin."""
    from plugins.file_ops.plugin import PLUGIN_FACTORY as FILE_OPS
    from plugins.okf.plugin import PLUGIN_FACTORY as OKF
    from plugins.terminal.plugin import PLUGIN_FACTORY as TERMINAL

    factories = {"file_ops": FILE_OPS, "terminal": TERMINAL, "okf": OKF}
    server_config = config.plugins.servers[instance_name]
    factory = factories[server_config.type]
    server = factory(
        name=instance_name, system_config=config, server_config=server_config
    )
    return {t["function"]["name"] for t in server.get_tools()}


class TestReadOnlyIsEnforced:
    """The reviewer must not be able to become a second author."""

    def test_the_read_only_instance_renders_no_write_tools(self, config):
        tools = _rendered_tool_names(config, "coder_fs_ro")
        writers = {"coder_fs_ro_manage", "coder_fs_ro_replace_string_in_file"}
        assert not (tools & writers), (
            "read_only file_ops still offers write tools -- the reviewer could "
            f"edit the change it is reviewing: {sorted(tools & writers)}"
        )
        # Counter-check: it must still be able to READ, or it is not a
        # reviewer, it is a disabled plugin.
        assert "coder_fs_ro_read_file" in tools

    def test_the_writable_instance_does_offer_them(self, config):
        """Counter-check for the test above: if write tools were missing
        everywhere, the assertion there would pass for the wrong reason."""
        tools = _rendered_tool_names(config, "coder_fs")
        assert "coder_fs_manage" in tools
        assert "coder_fs_replace_string_in_file" in tools

    def test_read_only_flag_is_actually_set(self, config):
        assert getattr(config.plugins.servers["coder_fs_ro"], "read_only") is True
        assert not getattr(config.plugins.servers["coder_fs"], "read_only", False)

    @pytest.mark.parametrize("agent", ["coder_explorer", "coder_reviewer"])
    def test_the_read_only_agents_have_no_writable_file_tools(self, config, agent):
        allowed = _agent_config(config, agent).tools.allowed
        # The instance is the part before the slash, minus any merge prefix.
        reached = {p.lstrip("+!").split("/")[0] for p in allowed}
        assert "coder_fs" not in reached, (
            f"{agent} reaches the writable file_ops instance: {sorted(reached)}"
        )


def test_both_sandboxes_cover_the_same_tree(config):
    """A reviewer that cannot reach what the coder just wrote does not report
    a gap -- it reviews whatever it can reach, which looks like a clean run."""
    rw = getattr(config.plugins.servers["coder_fs"], "allowed_directories")
    ro = getattr(config.plugins.servers["coder_fs_ro"], "allowed_directories")
    assert sorted(rw) == sorted(ro), (
        "coder_fs and coder_fs_ro must see the same directories; "
        f"read/write={sorted(rw)} read-only={sorted(ro)}"
    )


#: Hooks that insert a system message behind the first one -- inside the
#: cached prompt prefix -- and whose content changes MID-TURN. Enabling one
#: costs the cache from that call on, and nothing about the run looks wrong.
_PREFIX_CHURNING_HOOKS = [
    "todo.inject_todo_tasks",
    "sequential_thinking.inject_active_sessions",
    "coder_sam.inject_sub_agent_context",
]


@pytest.mark.parametrize("agent", HARNESS_AGENTS)
@pytest.mark.parametrize("hook", _PREFIX_CHURNING_HOOKS)
def test_no_hook_rewrites_the_cached_prefix_mid_turn(config, agent, hook):
    """Every one of these injects what the agent already has: its own tool
    calls, still in the transcript, with the tool in its allowlist to re-read
    on demand. The trade is a broken prefix for a duplicate, so they stay off.

    The OKF injection is deliberately NOT in this list -- it keys on the last
    user message, so it is byte-identical across the calls within a turn."""
    hooks = getattr(_agent_config(config, agent), "hooks", None)
    override = (getattr(hooks, "overrides", None) or {}).get(hook)
    if override is None:
        return  # not configured, and every one of these defaults to off
    enabled = (
        override.get("enabled")
        if isinstance(override, dict)
        else getattr(override, "enabled", None)
    )
    assert enabled is not True, (
        f"{agent} enables {hook}, which rewrites the cached system-prompt "
        "prefix whenever a task, a thought or a sub-agent changes -- so every "
        "later call in the turn pays full price for context the agent already "
        "has in its transcript"
    )


class TestKnowledgeBundleIsIsolatedAndReachable:
    """Both failure modes here are silent: a bundle path outside the sandbox
    makes the injection hook return without doing anything, and a sandbox that
    is too wide lets this agent write into another agent's bundle."""

    def test_the_bundle_sandbox_is_not_the_shared_one(self, config):
        roots = getattr(config.plugins.servers["coder_okf"], "allowed_directories")
        shared = getattr(config.plugins.servers["okf"], "allowed_directories")
        assert sorted(roots) != sorted(shared), (
            "coder_okf is sandboxed as widely as the shared okf server, so the "
            "coder can write into every other agent's bundle -- the sysadmin's "
            f"infra runbooks included: {sorted(roots)}"
        )
        assert len(roots) == 1, (
            f"the harness bundle should be exactly one root, got {sorted(roots)}"
        )

    def test_the_injected_bundle_resolves_inside_that_sandbox(self, config):
        """The hook logs at debug and returns quietly when the bundle is
        outside its sandbox, so a typo here looks exactly like 'nothing
        learned yet'."""
        override = _agent_config(config, "coder").hooks.overrides[
            "coder_okf.okf_context_injection"
        ]
        hook_bundle = (
            override.get("hook_bundle")
            if isinstance(override, dict)
            else getattr(override, "hook_bundle", None)
        )
        assert hook_bundle, "coder_okf.okf_context_injection has no hook_bundle"

        root = Path(
            getattr(config.plugins.servers["coder_okf"], "allowed_directories")[0]
        ).resolve()
        target = Path(hook_bundle).resolve()
        assert target == root or root in target.parents, (
            f"hook_bundle {hook_bundle!r} is outside coder_okf's sandbox "
            f"({root}); the injection hook would silently inject nothing"
        )

    def test_the_prompt_tells_the_agent_which_bundle_to_pass(self, config):
        """Every okf tool call needs an explicit `bundle`; there is no default,
        so a prompt that never names it buys an error on the first call."""
        agent_config = _agent_config(config, "coder")
        prompt = Path(agent_config.system_template).read_text(encoding="utf-8")
        assert "okf_bundle" in prompt, (
            "the coder prompt does not render {{ okf_bundle }}, so the agent is "
            "never told the bundle path it must pass to every okf call"
        )
        assert "okf_bundle" in (agent_config.template_vars or {}), (
            "the prompt renders {{ okf_bundle }} but no template_vars supplies "
            "it -- it would render empty"
        )


class TestPromptsMatchTheToolset:
    """A prompt that names a tool the agent does not have spends a turn on a
    call that cannot succeed, every single run."""

    @pytest.mark.parametrize("agent", HARNESS_AGENTS)
    def test_every_tool_named_in_the_prompt_exists_and_is_visible(self, config, agent):
        agent_config = _agent_config(config, agent)
        prompt = Path(agent_config.system_template).read_text(encoding="utf-8")

        problems = []
        for ref in sorted(set(_TOOL_REF.findall(prompt))):
            instance = next(
                (i for i in TOOL_INSTANCES if ref.startswith(f"{i}_")), None
            )
            if instance is None:
                problems.append(f"{ref}: no such tool instance")
                continue
            if config.plugins.servers[instance].type in ("file_ops", "terminal", "okf"):
                if ref not in _rendered_tool_names(config, instance):
                    problems.append(f"{ref}: {instance} exposes no such tool")
                    continue
            visible = tool_matches_patterns(
                ref, instance, agent_config.tools.allowed
            ) and not tool_matches_patterns(
                ref, instance, agent_config.tools.blocked or []
            )
            if not visible:
                problems.append(f"{ref}: not in {agent}'s allowlist")

        assert not problems, f"{agent} prompt names unusable tools: {problems}"

    @pytest.mark.parametrize("agent", HARNESS_AGENTS)
    def test_the_prompt_names_at_least_one_tool(self, config, agent):
        """Counter-check: the test above passes trivially on a prompt that
        mentions no tools at all, which is how it would go quiet if the regex
        ever stopped matching."""
        prompt = Path(
            _agent_config(config, agent).system_template
        ).read_text(encoding="utf-8")
        assert _TOOL_REF.findall(prompt), (
            f"{agent}: no tool reference found in the prompt -- either the "
            "prompt stopped naming its tools or _TOOL_REF no longer matches"
        )


class TestSemanticSearchIsReachableAndBacked:
    """A search tool without an index answers SemanticSearchDisabled every
    time, which costs the agent a turn and teaches it the tool is broken.

    Both halves therefore belong in one test: the tool must be visible AND the
    instance behind it must be configured to have an index. Until 18.09.2026
    this file asserted the opposite -- the tool was blocked in all four agents
    because the index was off.
    """

    @pytest.mark.parametrize(
        "agent,instance",
        [
            ("coder", "coder_fs"),
            ("coder_explorer", "coder_fs_ro"),
            ("coder_reviewer", "coder_fs_ro"),
            ("coder_tester", "coder_fs_ro"),
        ],
    )
    def test_semantic_search_is_offered_and_grep_survives(
        self, config, agent, instance
    ):
        tools = _agent_config(config, agent).tools

        def visible(tool_name):
            return tool_matches_patterns(
                tool_name, instance, tools.allowed
            ) and not tool_matches_patterns(tool_name, instance, tools.blocked or [])

        assert visible(f"{instance}_semantic_search"), (
            f"{agent}: semantic_search is not reachable. A block pattern reads "
            "'<instance>/<instance>_semantic_search' -- the tool name already "
            "carries the instance prefix, so the intuitive "
            "'<instance>/semantic_search' matches nothing, silently."
        )
        assert visible(f"{instance}_grep_search")
        assert visible(f"{instance}_read_file")

    def test_the_two_sandboxes_share_one_index_and_one_builder(self, config):
        """Same tree, same collection -- and exactly one instance building it.

        Two builders over the same tree is the same seven minutes twice, and
        because a full rebuild clears the collection first, the two would also
        take turns emptying each other's index.

        The builder is coder_fs because tool servers are built on first use:
        coder_fs_ro only exists once a reviewer or explorer is spawned, so an
        index owned by it is one the main agent may never get.
        """
        rw = get_tool_server_config("coder_fs", config).search
        ro = get_tool_server_config("coder_fs_ro", config).search

        assert rw["enable_semantic_search"] and ro["enable_semantic_search"]
        assert rw["collection_name"] == ro["collection_name"], \
            "the twins must read the same collection, or one of them searches an empty index"
        assert rw.get("index_on_startup", True),             "coder_fs has enable_indexing but index_on_startup off -- then nothing builds"
        builders = [name for name, cfg in (("coder_fs", rw), ("coder_fs_ro", ro))
                    if cfg.get("enable_indexing", True)]
        assert builders == ["coder_fs"], \
            f"exactly the always-present instance builds the index, not {builders}"


@pytest.mark.parametrize("agent", HARNESS_AGENTS)
def test_prompts_carry_no_per_call_template_variables(config, agent):
    """The system prompt is the cached prefix. A variable that changes every
    call (a step counter, a clock) invalidates the cache for everything after
    it, on every turn."""
    prompt = Path(
        _agent_config(config, agent).system_template
    ).read_text(encoding="utf-8")
    volatile = [
        var
        for var in ("current_step", "current_time", "current_datetime")
        if var in prompt
    ]
    assert not volatile, (
        f"{agent} prompt renders per-call variables {volatile}; put anything "
        "that changes per turn into an injection hook, not the template"
    )
