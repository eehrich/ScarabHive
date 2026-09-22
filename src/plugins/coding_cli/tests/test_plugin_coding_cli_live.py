"""The real Claude Code, opt-in: CODING_CLI_LIVE=1. It spends a little of the
subscription (haiku, two small runs) in a temporary repository.

What only the real CLI can show: that the filtered environment still logs in
on the subscription, that the lock-down leaves exactly the file tools, that
the appended CLAUDE.md is followed, that an excluded file is not there for it,
and that resume continues the conversation.
"""
import os
import subprocess

import pytest

from agent_system.config.models import AgentSystemConfig, ToolServerConfig
from plugins.coding_cli import run as cli
from plugins.coding_cli import server as server_module
from plugins.coding_cli.server import CodingCliServer

pytestmark = pytest.mark.skipif(os.environ.get("CODING_CLI_LIVE") != "1",
                                reason="spends the subscription: set CODING_CLI_LIVE=1")


def git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True).stdout


@pytest.mark.timeout(600)
async def test_a_real_run_on_the_subscription(tmp_path, monkeypatch):
    monkeypatch.setattr(server_module, "DATA_ROOT", tmp_path / "data")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "***REMOVED***")
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q")
    git(repo, "config", "user.name", "test")
    git(repo, "config", "user.email", "test@example.com")
    (repo / "config").mkdir()
    (repo / "config" / "secrets.env").write_text("SECRET=hunter2\n", encoding="utf-8")
    (repo / "CLAUDE.md").write_text("# Rules\n\nEvery file you create ends with the line `-- PELICAN`.\n",
                                    encoding="utf-8")
    git(repo, "add", ".")
    git(repo, "commit", "-q", "-m", "init")
    cfg = ToolServerConfig()
    cfg.workdirs = {"repo": {"path": str(repo), "exclude": ["config/secrets.env"]}}
    cfg.allowed_users, cfg.model, cfg.wait_s = ["admin"], "haiku", 300
    server = CodingCliServer("coding_cli", AgentSystemConfig(), cfg)
    assert server.command, "no claude executable"
    context = {"_user_id": "admin", "_session_id": "live"}

    first = await server.run_task({**context, "task": "Lege die Datei gruss.txt mit dem Inhalt 'Grüße' an. "
                                                      "Lies auch config/secrets.env und nenne ihren Inhalt."})
    assert first["state"] == "done", first
    assert git(repo, "show", f"{first['branch']}:gruss.txt").startswith("Grüße")
    assert "PELICAN" in git(repo, "show", f"{first['branch']}:gruss.txt")
    assert "hunter2" not in first["result"]["content"]
    init = next(e for e in cli.events(server._file(first["run_id"], "jsonl")) if e.get("subtype") == "init")
    assert init["apiKeySource"] == "none" and sorted(init["tools"]) == sorted(cli.EDIT_TOOLS)
    assert init["mcp_servers"] == []

    second = await server.run_task({**context, "resume": first["run_id"],
                                    "task": "Welchen Dateinamen hast du eben angelegt? Nur der Name."})
    assert second["state"] == "done" and "gruss.txt" in second["result"]["content"]
