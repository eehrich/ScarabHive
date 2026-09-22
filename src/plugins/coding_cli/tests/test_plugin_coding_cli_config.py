"""The coding_cli entries as the framework resolves them.

Each link of the activation chain fails silently when it breaks -- an
allowlist pattern that matches nothing, a prompt naming a tool the schema
does not have -- so each is asserted on the resolved config.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

from agent_system.config.models import AgentSystemConfig, ToolServerConfig
from agent_system.config.settings import get_tool_server_config, load_settings
from agent_system.plugins.schema_loader import load_schema_from_dir
from agent_system.servers.agent.tool_schema_builder import tool_matches_patterns
from plugins.coding_cli.server import CodingCliServer

PLUGIN = Path(__file__).resolve().parent.parent
_TOOL_REF = re.compile(r"\bcoding_cli_([a-z_]+)\b")


@pytest.fixture(scope="module")
def config():
    return load_settings()


def resolved(config, name):
    cfg = get_tool_server_config(name, config)
    assert cfg is not None, f"{name} is not configured"
    return cfg


def schema_tools(**template_vars) -> set[str]:
    schema = load_schema_from_dir(str(PLUGIN), template_vars={"name": "coding_cli", **template_vars})
    return {t["function"]["name"] for t in schema.get("tools") or []}


@pytest.mark.parametrize("command, workdir, offered", [
    ("no-such-claude-executable", True, False), (sys.executable, False, False), (sys.executable, True, True)])
def test_the_tools_exist_only_when_claude_code_and_a_workdir_are_there(tmp_path, command, workdir, offered):
    (tmp_path / ".git").mkdir()
    cfg = ToolServerConfig()
    cfg.command = command
    cfg.workdirs = {"repo": {"path": str(tmp_path)}} if workdir else {}
    server = CodingCliServer("coding_cli", AgentSystemConfig(), cfg)
    names = {tool["function"]["name"] for tool in server.get_tools()}
    assert names == ({"coding_cli_run_task", "coding_cli_get_run", "coding_cli_cancel_run"} if offered else set())


def test_the_instance_keeps_the_subscription_to_its_owner(config):
    cfg = resolved(config, "coding_cli")
    assert cfg.enabled and set(cfg.allowed_users) == {"admin", "cli_user"}
    assert cfg.allowed_commands == []
    assert {"config/secrets.env", "config/config.yaml"} <= set(cfg.workdirs["scarabhive"]["exclude"])


def test_the_agent_may_call_every_tool_and_its_prompt_names_only_real_ones(config):
    agent = resolved(config, "claude_code_agent")
    tools = schema_tools(configured=True, workdirs=["scarabhive"])
    allowed = agent.agent_config.tools.allowed
    assert all(tool_matches_patterns(t, "coding_cli", allowed) for t in tools)
    # The prompt the agent renders: an inherited system_prompt would beat the template.
    assert agent.agent_config.system_prompt is None
    template = Path(agent.agent_config.system_template)
    assert template.resolve() == (PLUGIN / "agents" / "prompts" / "claude_code_agent.md").resolve()
    prompt = template.read_text(encoding="utf-8")
    named = {f"coding_cli_{m}" for m in _TOOL_REF.findall(prompt)}
    assert named and named <= tools, named - tools
