"""What the model learns about the sandbox: one sentence in the tool description.

Rendered once with the schema, so it is the same text on every request and
the prompt cache survives. Absent without a sandbox, so existing deployments
see the description they always saw. Decided by platform without probing, like
the backend choice itself.
"""
from __future__ import annotations

import os

import pytest

from agent_system.config.models import AgentSystemConfig, ToolServerConfig
from agent_system.utils import process_sandbox as ps
from plugins.terminal.server import TerminalServer

PLAIN = "Run a shell command, in the foreground or in the background."


def description(tmp_path, mode: str, cwd=None) -> str:
    config = ToolServerConfig(
        type="terminal", enabled=True,
        security={"whitelist": None, "blacklist": [], "allow_command_chains": True},
        limits={}, platform={"bash_path": "auto", "initial_cwd": str(cwd or tmp_path)},
        sandbox={"mode": mode})
    server = TerminalServer("terminal", AgentSystemConfig(), config)
    return server.get_tools()[0]["function"]["description"]


def test_without_a_sandbox_the_description_is_unchanged(tmp_path):
    assert description(tmp_path, "danger-full-access") == PLAIN


def test_a_partial_sandbox_says_so(tmp_path, monkeypatch):
    monkeypatch.setattr(ps, "_host", lambda: "macos")
    text = description(tmp_path, "workspace-write")
    assert text.startswith(PLAIN + " Commands run sandboxed (workspace-write)")
    assert str(tmp_path.resolve()) in text
    assert "partial confinement" in text
    assert "git hooks and config" in text


def test_a_full_sandbox_does_not_claim_partial(tmp_path, monkeypatch):
    monkeypatch.setattr(ps, "_host", lambda: "linux")
    text = description(tmp_path, "read-only")
    assert "Commands run sandboxed (read-only)" in text
    assert "private /tmp" in text and "partial" not in text


def test_a_host_without_a_backend_announces_the_refusal(tmp_path, monkeypatch):
    monkeypatch.setattr(ps, "_host", lambda: "windows")
    assert "Commands are refused here" in description(tmp_path, "workspace-write")


@pytest.mark.skipif(os.name == "nt", reason="Windows forbids a double quote in a file name")
def test_a_workspace_path_with_quotes_survives_the_yaml(tmp_path, monkeypatch):
    """The note lands inside a double-quoted YAML string."""
    monkeypatch.setattr(ps, "_host", lambda: "macos")
    odd = tmp_path / 'we"ird\\ws'
    odd.mkdir()
    assert str(odd.resolve()) in description(tmp_path, "workspace-write", cwd=odd)
