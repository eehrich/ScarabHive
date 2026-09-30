"""The shipped configuration: who gets ask_user, and that the instance works.

Reads the real configuration (load_settings + get_tool_server_config, the
inheritance included). Only agents a person chats with in the web UI may ask;
a writer agent or a sub-agent that runs unattended must not get the tool --
through its own allowlist or through the agent it inherits from
(research_worker from research_agent, gamedev from coder).
"""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from agent_system.config.settings import get_tool_server_config, load_settings
from agent_system.servers.agent.tool_schema_builder import tool_matches_patterns
from plugins.ask_user.plugin import PLUGIN_FACTORY
from plugins.tool_approval.plugin import PLUGIN_FACTORY as APPROVAL_FACTORY
from plugins.tool_approval.rules import first_match

ROOT = Path(__file__).resolve().parents[4]

#: The agents that ask, chosen: general agents a person works with in the chat.
#: Not stategraph_author: its files belong to the stategraph owners, who decide.
ASKING = {"coder", "gamedev", "research_agent", "sysadmin_agent"}


@pytest.fixture(scope="module")
def config():
    return load_settings()


def _writer_agents():
    """Every server the writer declares in its agent files."""
    names = set()
    for path in (ROOT / "src" / "plugins_writer").rglob("agents/*.yaml"):
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        names.update(((data.get("plugins") or {}).get("servers") or {}).keys())
    return names


def _may_ask(cfg) -> bool:
    tools = cfg.agent_config.tools
    return (tool_matches_patterns("ask_user", "ask_user", list(tools.allowed or []))
            and not tool_matches_patterns("ask_user", "ask_user", list(tools.blocked or [])))


def test_the_instance_is_on_and_reads_its_config(config):
    cfg = get_tool_server_config("ask_user", config)
    assert cfg is not None and cfg.enabled, "ask_user is not configured"

    server = PLUGIN_FACTORY("ask_user", config, cfg)

    assert server.ask_timeout == float(cfg.config["ask_timeout"])
    assert [t["function"]["name"] for t in server.get_tools()] == ["ask_user"]
    assert server.answer_url == "/plugins/ask_user/answer"


def test_only_the_chosen_chat_agents_may_ask(config):
    writer = _writer_agents()
    if (ROOT / "src" / "plugins_writer").is_dir():  # not in the open-source checkout
        assert len(writer) > 20, "fixture: the writer's agents were not found"
    asking = {}
    for name in config.plugins.servers:
        cfg = get_tool_server_config(name, config)
        if cfg is not None and getattr(cfg, "agent_config", None) is not None and _may_ask(cfg):
            asking[name] = cfg

    assert set(asking) == ASKING
    for name, cfg in asking.items():
        assert name not in writer, f"writer agent {name} may ask"
        visibility = getattr(cfg.metadata, "visibility", None) if cfg.metadata else None
        assert visibility in ("ui", "both"), f"{name} is no agent a person chats with ({visibility})"


def test_tool_approval_lets_the_question_through_without_asking_first(config):
    """Approving a question before asking it would ask the same person twice, and a
    run nobody watches would get the approval's block instead of "decide yourself"."""
    plugin = APPROVAL_FACTORY("tool_approval", None, get_tool_server_config("tool_approval", config))

    settings = plugin.settings_for({"mode": "ask"})

    assert first_match(settings.allow, "ask_user", "ask_user", {}) is not None
