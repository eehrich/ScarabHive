"""llm_coding_cli against a real coding_cli instance: the contract the client reads -- the answer's fields, the
codes of its refusals, what a run is started with -- is the instance's own, not a stand-in's.

Real are the client, the coding_cli server (called by name through call_with_status), its git worktrees, the
detached process, the gateway plugin and the calling agent; only the CLI is replaced (coding_cli's fake_claude).
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from agent_system import app_state
from agent_system.config.models import (AgentConfig, AgentSystemConfig, LLMModelConfig, LLMProfile, LLMSystemConfig,
                                        ToolConfig, ToolServerConfig)
from agent_system.core.request_context import current_run_agent, current_run_session, current_run_user
from agent_system.llm.models import LLMServerError
from agent_system.servers.agent.server import Agent
from agent_system.tools.base import ToolServer, ToolServerRegistry
from agent_system.tools.status import current_request_id
from plugins.coding_cli import server as server_module
from plugins.coding_cli.server import CodingCliServer
from plugins.llm_coding_cli import client as client_module
from plugins.llm_coding_cli.client import CodingCliClient, render
from plugins.mcp_gateway import grants
from plugins.mcp_gateway.plugin import McpGatewayPlugin

FAKE = Path(server_module.__file__).resolve().parent / "tests" / "fake_claude.py"
# The root conftest ends what a test session leaves running by this variable.
TEST_SESSION_MARKER = "AGENT_SYSTEM_TEST_SESSION"
MESSAGES = [{"role": "system", "content": "You write."}, {"role": "user", "content": "WRITE b.txt one"}]
MORE = [*MESSAGES, {"role": "assistant", "content": "did: # Instructions"}, {"role": "user", "content": "Shorter."}]


def git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True).stdout


@pytest.fixture
def repo(tmp_path, monkeypatch):
    empty = tmp_path / "gitconfig"
    empty.write_text("", encoding="utf-8")      # no global filter (git-lfs: five seconds a checkout)
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(empty))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    path = tmp_path / "repo"
    path.mkdir()
    git(path, "init", "-q")
    git(path, "config", "user.name", "test")
    git(path, "config", "user.email", "test@example.com")
    (path / "a.txt").write_text("a\n", encoding="utf-8")
    git(path, "add", ".")
    git(path, "commit", "-q", "-m", "init")
    return path


@pytest.fixture
def log(tmp_path, monkeypatch):
    path = tmp_path / "fake_log.json"
    monkeypatch.setenv("FAKE_CLAUDE_LOG", str(path))
    return lambda: json.loads(path.read_text(encoding="utf-8"))


@pytest.fixture
def instance(repo, tmp_path, monkeypatch):
    """Builds the coding_cli instance "cc" on the repo, in this process's registry, inside an agent run's context."""
    monkeypatch.setattr(server_module, "DATA_ROOT", tmp_path / "data")
    monkeypatch.setattr(server_module, "POLL_S", 0.05)
    monkeypatch.setattr(client_module, "_conversations", type(client_module._conversations)())

    def make(**config):
        cfg = ToolServerConfig(type="coding_cli", enabled=True)
        cfg.workdirs = {"repo": {"path": str(repo), **config.pop("workdir", {})}}
        cfg.allowed_users = ["ada"]
        cfg.pass_env = ["FAKE_CLAUDE_LOG", TEST_SESSION_MARKER]
        cfg.wait_s = 30
        for key, value in config.items():
            setattr(cfg, key, value)
        server = CodingCliServer("cc", AgentSystemConfig(), cfg)
        server.command = [sys.executable, str(FAKE)]
        registry = ToolServerRegistry()
        registry.register("cc", server)
        monkeypatch.setattr(app_state, "registry", registry)
        return server

    tokens = (current_request_id.set("req-1"), current_run_user.set("ada"), current_run_session.set("sess-1"))
    yield make
    current_request_id.reset(tokens[0])
    current_run_user.reset(tokens[1])
    current_run_session.reset(tokens[2])


def client(model="cc", **kwargs):
    return CodingCliClient("claude_code", model, context_window=200000, **kwargs)


async def test_a_call_is_a_run_of_the_instance_and_the_next_one_resumes_it(instance, log):
    server = instance()
    first = await client().chat_tools(MESSAGES, [])
    assert first["assistant"]["content"] == "did: # Instructions"
    # Claude Code's counts as the usage store reads them: the cache's reads and writes inside the prompt tokens
    assert first["usage"]["prompt_tokens"] == 18 + 661 + 5963 and first["usage"]["completion_tokens"] == 291
    assert log()["task"] == render(MESSAGES)
    await client().chat_tools(MORE, [])
    argv = log()["argv"]
    runs = sorted(server._records(), key=lambda r: r["started_at"])
    assert argv[argv.index("--resume") + 1] == runs[0]["claude_session"] and runs[1]["prior"] == runs[0]["run_id"]
    assert log()["task"] == render(MORE[3:])


async def test_a_resume_the_instance_refuses_starts_a_fresh_run(instance, log):
    """Its worktree is gone: the instance's own refusal, read by its code."""
    server = instance()
    await client().chat_tools(MESSAGES, [])
    record, = server._records()
    cli_run = server_module.cli
    cli_run.remove_worktree(Path(record["repo"]), Path(record["worktree"]), record["branch"])
    assert not Path(record["worktree"]).exists(), "fixture: the worktree is still there"
    answer = await client().chat_tools(MORE, [])
    assert answer["assistant"]["content"] == "did: # Instructions"
    assert "--resume" not in log()["argv"] and log()["task"] == render(MORE)
    assert len(server._records()) == 2


async def test_a_conversation_longer_than_the_instance_takes_moves_the_chain_on(instance):
    instance(max_task_chars=1_000)
    long = [*MESSAGES, {"role": "user", "content": "x" * 2_000}]
    with pytest.raises(LLMServerError, match="max_task_chars"):
        await client().chat_tools(long, [])


async def test_the_entrys_model_and_thinking_level_are_the_runs(instance, log):
    instance(model="opus", models=["sonnet"])
    await client("cc/sonnet", thinking_level="high").chat_tools(MESSAGES, [])
    argv = log()["argv"]
    assert (argv[argv.index("--model") + 1], argv[argv.index("--effort") + 1]) == ("sonnet", "high")
    with pytest.raises(LLMServerError, match="model: one of opus, sonnet"):
        await client("cc/haiku").chat_tools([*MESSAGES, {"role": "user", "content": "another"}], [])


class _Files(ToolServer):
    def __init__(self):
        super().__init__("files", AgentSystemConfig(), ToolServerConfig(type="files", enabled=True))

    def get_tools(self):
        return [{"type": "function", "function": {"name": name, "description": name, "parameters": {
            "type": "object", "properties": {"path": {"type": "string"}}}}} for name in ("files_read", "files_write")]


async def test_a_caller_workdir_gives_the_cli_the_tools_of_the_agents_call(instance, log, monkeypatch):
    """The agent sends its model files_read; it may call files_write too, and the call does not name it."""
    plugin = McpGatewayPlugin("mcp_gateway", AgentSystemConfig(), ToolServerConfig(type="mcp_gateway", enabled=True))
    grants.serve("mcp_gateway", True, "http://127.0.0.9:8123/plugins/mcp_gateway/mcp")
    tools = ToolServerRegistry()
    tools.register("files", _Files())
    llm = LLMSystemConfig(models={"m": LLMModelConfig(provider="openai", model="m", api_key="fake")},
                          profiles={"normal": LLMProfile(model_ref="m")}, default_profile="normal")
    agent = Agent("writer_agent", AgentSystemConfig(llm_system=llm), ToolServerConfig(
        type="agent", enabled=True, agent_config=AgentConfig(max_steps=1, llm_profile="normal",
                                                            tools=ToolConfig(allowed=["files/*"]))), tools)
    issued = []
    issue = grants.issue
    monkeypatch.setattr(grants, "issue", lambda *args, **kwargs: issued.append((args, kwargs)) or issue(*args, **kwargs))
    instance(workdir={"gateway": {"caller": True}})
    reset = current_run_agent.set(agent)
    try:
        sent = [{"type": "function", "function": {"name": "files_read", "parameters": {"type": "object"}}}]
        await client().chat_tools(MESSAGES, sent)
    finally:
        current_run_agent.reset(reset)
        await plugin.stop_plugin()
    (args, kwargs), = issued
    assert args[1] == ["files_read"] and kwargs["runner"] is agent and kwargs["session_id"] == "sess-1"
    argv = log()["argv"]
    assert argv[argv.index("--allowedTools") + 1] == "mcp__gateway"


def test_the_fake_cli_is_where_the_instance_tests_keep_it():
    assert FAKE.is_file() and shutil.which("git"), "fixture: coding_cli's fake_claude.py or git is missing"
