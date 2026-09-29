"""Guards for the wake harness (`config/agents/wake_test_agent.yaml`).

It is config only, and every link in it fails QUIETLY: a tool pattern that
matches nothing leaves the coordinator without its manager, a hook override
key that matches no hook is simply not found, a sub-agent missing from
`allowed_agents` is refused at runtime and nowhere else. The harness is what
the wake mechanism is measured with, so a harness that silently stopped
measuring is worse than none.

Asserted on the RESOLVED configuration, not on the file's text.
"""
from __future__ import annotations

import pytest

from agent_system.config.settings import get_tool_server_config, load_settings
from agent_system.servers.agent.tool_schema_builder import tool_matches_patterns


@pytest.fixture(scope="module")
def config():
    return load_settings()


def entry(config, name):
    found = get_tool_server_config(name, config)
    assert found is not None, f"{name} is not configured"
    assert getattr(found, "enabled", False), f"{name} is configured but not enabled"
    return found


def test_the_coordinator_can_reach_its_manager_and_a_clock(config):
    """`wake_test_sam/*` matches on the server, `datetime/*` likewise -- a pattern
    that names the tool instead (`wake_test_sam/manage_sub_agent`) matches nothing
    and the coordinator would have no way to spawn anything."""
    allowed = entry(config, "wake_test_agent").agent_config.tools.allowed

    assert tool_matches_patterns("wake_test_sam_manage_sub_agent", "wake_test_sam", allowed)
    assert tool_matches_patterns("datetime_operations", "datetime", allowed)
    assert not tool_matches_patterns("terminal_run", "terminal", allowed), \
        "the harness runs one thing; anything else it could reach is noise in the test"


def test_the_worker_is_spawnable_and_has_nothing_to_work_with(config):
    """Registration is the link that only shows at runtime: an agent missing from
    `allowed_agents` cannot be spawned. And the prompt tells the worker it has no
    tools, which has to be true, or a task will wait on one that never answers."""
    assert "wake_test_worker" in (entry(config, "wake_test_sam").allowed_agents or [])
    assert entry(config, "wake_test_worker").agent_config.tools.allowed == []


def test_the_hook_override_names_a_hook_that_exists(config):
    """`<instance>.<hook>` is what the registry builds. A key that matches nothing
    is not an error anywhere -- the hook just stays off, and the harness quietly
    loses the half of itself that works without a wake."""
    overrides = entry(config, "wake_test_agent").agent_config.hooks.overrides

    assert "wake_test_sam.inject_sub_agent_context" in overrides
    override = overrides["wake_test_sam.inject_sub_agent_context"]
    enabled = override.get("enabled") if isinstance(override, dict) else override.enabled
    assert enabled is True


def test_the_coordinator_carries_its_own_prompt(config):
    """It inherits a `system_template` from the plugin defaults, and a raw
    `system_prompt` wins over that. The harness depends on its own text: the
    whole test is whether the model ends its turn and reports how it came back."""
    agent_config = entry(config, "wake_test_agent").agent_config

    assert agent_config.system_prompt, "no prompt of its own -- it would run on the default one"
    for expected in ("wake_when_done=true", "poll", "woken"):
        assert expected in agent_config.system_prompt, expected
