"""The mail assistant as the framework resolves it from the real configuration: every link of the activation chain
fails silently when it breaks -- an allowlist pattern that matches nothing, a prompt naming a tool that is not there."""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from agent_system.config.models import AgentSystemConfig, ToolServerConfig
from agent_system.config.settings import get_tool_server_config, load_settings
from agent_system.servers.agent.tool_schema_builder import tool_matches_patterns
from agent_system.utils.prompt_renderer import render_prompts
from plugins.mail import server as mail

PLUGIN = Path(__file__).resolve().parent.parent


@pytest.fixture(scope="module")
def config():
    return load_settings()


def mail_tools() -> list[str]:
    """The tool names of a mail entry with an IMAP server, as the schema renders them."""
    entry = mail.MailServer("mail", AgentSystemConfig(), ToolServerConfig(type="mail", imap_host="imap.example.org"))
    return [t["function"]["name"] for t in entry.get_tools()]


def test_the_assistant_resolves_with_the_mail_tools_only(config):
    agent = get_tool_server_config("mail_assistant", config)
    assert agent is not None, "mail_assistant is not in the loaded configuration"
    assert get_tool_server_config("mail", config).type == "mail"
    allowed = agent.agent_config.tools.allowed
    tools = mail_tools()
    assert len(tools) == 6 and all(tool_matches_patterns(t, "mail", allowed) for t in tools), (allowed, tools)
    for server_name, tool in (("file_ops", "file_ops_read_file"), ("terminal", "terminal_execute"),
                              ("web_scraper", "web_scraper_scrape")):
        assert not tool_matches_patterns(tool, server_name, allowed), tool
    assert agent.metadata.category == "Assistant" and agent.metadata.min_role == "admin"
    assert agent.metadata.visibility in ("ui", "both")


def test_the_prompt_renders_and_names_only_real_tools(config):
    agent = get_tool_server_config("mail_assistant", config)
    template = Path(agent.agent_config.system_template)
    assert template.is_file() and PLUGIN in template.resolve().parents, template
    rendered = render_prompts(str(template), dict(agent.agent_config.template_vars or {}))["system_prompt"]
    named = set(re.findall(r"\bmail_[a-z]+\b", rendered))
    assert named and named <= set(mail_tools()), named - set(mail_tools())
