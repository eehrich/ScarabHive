"""An Antigravity instance (cli: antigravity) against a fake agy that replays the stream measured on 1.4.8.

Real are the server, the git worktree, the detached process and the files it streams into; only the CLI is
replaced (fake_agy.py). Shared fixtures come from the server tests.
"""
import json
import sys
from pathlib import Path

import pytest

from agent_system.config.models import AgentSystemConfig, ToolServerConfig
from plugins.coding_cli import run as cli
from plugins.coding_cli.server import CodingCliServer

from test_plugin_coding_cli_server import (TEST_SESSION_MARKER, call, data_root, fresh_repo, git, log,  # noqa: F401
                                            plain_git, repo)

FAKE_AGY = Path(__file__).resolve().parent / "fake_agy.py"


def make_agy(repo, **config):
    cfg = ToolServerConfig()
    cfg.cli = "antigravity"
    cfg.workdirs = {"repo": {"path": str(repo), "exclude": ["config/secrets.env"], "web": True}}
    cfg.allowed_users = ["admin"]
    cfg.pass_env = ["FAKE_CLAUDE_LOG", TEST_SESSION_MARKER]
    cfg.wait_s = 30
    for key, value in config.items():
        setattr(cfg, key, value)
    server = CodingCliServer("agy_cli", AgentSystemConfig(), cfg)
    server.command = [sys.executable, str(FAKE_AGY)]
    return server


async def test_an_agy_run_is_read_like_a_claude_code_run(repo, log):
    server = make_agy(repo, model="gemini-3.8-flash", effort="high")
    result, status = await call(server, "run_task", task="WRITE b.txt hello\nDENY")
    assert result["state"] == "done", result
    assert git(repo, "show", f"{result['branch']}:b.txt") == "hello" and result["commit"]
    assert result["result"] == {"untrusted": True, "content": "did: WRITE b.txt hello"}
    assert result["denials"] == {"untrusted": True, "content": ["RunCommand"]}
    record = server._load(result["run_id"])
    assert record["model"] == "gemini-3.8-flash" and record["claude_session"] and record["cli"] == "antigravity"
    sent = log()
    # The task went in on stdin as agy's one stream message, whole; --print= is the last word.
    assert sent["task"] == "WRITE b.txt hello\nDENY"
    argv = sent["argv"]
    assert argv[-1] == "--print=" and argv[argv.index("--mode") + 1] == "accept-edits"
    assert argv[argv.index("--effort") + 1] == "high" and "--disable-slash-commands" in argv
    assert status.closing[0][0] == "end"


async def test_an_agy_run_has_a_home_of_its_own_and_no_claude_login(repo, log, data_root, monkeypatch):  # noqa: F811
    """The user's agy settings (an auto-approval, MCP servers) and Claude Code's token stay out of the run."""
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "claude-subscription-token")
    monkeypatch.setenv("HOMEDRIVE", "C:")
    result, _ = await call(make_agy(repo), "run_task", task="x")
    sent = log()
    home = data_root / "agy_home" / "admin"
    assert result["state"] == "done" and Path(sent["home"]) == home
    assert sys.platform != "win32" or Path(sent["userprofile"]) == home
    assert json.loads((home / ".gemini" / "antigravity-cli" / "settings.json").read_text(encoding="utf-8")) \
        == cli.AGY_SETTINGS
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in sent["env"] and "HOMEDRIVE" not in sent["env"]
    assert "CLAUDE_CODE_MCP_TOOL_IDLE_TIMEOUT" not in sent["env"]


async def test_a_run_of_the_other_cli_is_not_resumed(repo):
    first, _ = await call(make_agy(repo), "run_task", task="x")
    claude = make_agy(repo, cli="claude")
    claude.command = [sys.executable, str(FAKE_AGY)]
    result, _ = await call(claude, "run_task", task="y", resume=first["run_id"])
    assert "ran on antigravity, this instance runs claude" in result["error"]


async def test_an_agy_instance_has_no_quota_to_show(repo):
    result, _ = await call(make_agy(repo), "quota")
    assert result["status"] == "error" and "no subscription windows" in result["error"]


async def test_run_task_on_an_unknown_cli_says_so(repo):
    server = make_agy(repo, cli="gemini")
    server.command = None
    result, _ = await call(server, "run_task", task="x")
    assert "cli 'gemini' is none of claude, antigravity" in result["error"]


def test_a_cmd_wrapper_is_no_agy(monkeypatch):
    """cmd.exe would parse the model's json_schema by its own rules."""
    monkeypatch.setattr(cli.shutil, "which", lambda command: rf"C:\tools\{command}.cmd")
    monkeypatch.setattr(cli.os, "name", "nt")
    assert cli.find_agy("agy") is None
    monkeypatch.setattr(cli.shutil, "which", lambda command: rf"C:\tools\{command}.exe")
    assert cli.find_agy("agy") == [r"C:\tools\agy.exe"]


async def test_plan_mode_asks_agy_to_only_read(repo, log):
    server = make_agy(repo)
    result, _ = await call(server, "run_task", task="x", mode="plan")
    argv = log()["argv"]
    assert result["state"] == "done" and argv[argv.index("--mode") + 1] == "plan"


async def test_an_agy_error_fails_the_run_with_its_words(repo):
    server = make_agy(repo)
    result, _ = await call(server, "run_task", task="ERROR")
    assert result["state"] == "failed"
    assert result["result"] == {"untrusted": True, "content": "the model request failed with an unspecified error"}


async def test_structured_output_comes_back_as_from_claude_code(repo):
    server = make_agy(repo)
    result, _ = await call(server, "run_task", task='OUTPUT {"n": 3}', json_schema={"type": "object"})
    assert result["output"] == {"untrusted": True, "content": {"n": 3}}


async def test_resume_continues_the_agy_conversation(repo, log):
    server = make_agy(repo)
    first, _ = await call(server, "run_task", task="WRITE b.txt one")
    conversation = server._load(first["run_id"])["claude_session"]
    second, _ = await call(server, "run_task", task="WRITE c.txt two", resume=first["run_id"])
    argv = log()["argv"]
    assert argv[argv.index("--conversation") + 1] == conversation
    assert {"A\tb.txt", "A\tc.txt"} <= set(second["changes"]["content"])


async def test_claude_codes_full_window_does_not_hold_an_agy_run(repo, data_root):  # noqa: F811
    """quota.json is Claude Code's: an Antigravity instance on the same data root must not read it."""
    data_root.mkdir(parents=True)
    (data_root / "quota.json").write_text(json.dumps({"status": "allowed", "unifiedWindows": {
        "five_hour": {"utilization": 0.99, "resetsAt": 4_000_000_000}}}))
    result, _ = await call(make_agy(repo), "run_task", task="x")
    assert result["state"] == "done", result


def test_an_agy_workdir_must_say_web_and_name_no_mcp_server(repo, caplog):
    server = make_agy(repo, workdirs={"plain": {"path": str(repo)}, "web": {"path": str(repo), "web": True},
                                      "servers": {"path": str(repo), "web": True, "mcp_servers": ["x"]}})
    assert set(server.workdirs) == {"web"}
    assert caplog.text.count("an Antigravity run searches the web") == 2


def test_an_unknown_cli_offers_nothing(repo, caplog):
    cfg = ToolServerConfig()
    cfg.cli = "gemini"
    cfg.workdirs = {"repo": {"path": str(repo), "web": True}}
    server = CodingCliServer("x", AgentSystemConfig(), cfg)
    assert server.command is None and "cli 'gemini' is none of" in caplog.text


def test_the_measured_agy_stream_reads_as_claude_codes(tmp_path):
    """Lines as agy 1.4.8 wrote them: the tool call becomes an action line, the text deltas nothing."""
    lines = [
        {"event": "init", "conversation_id": "c1", "init": {"model": "gemini-3.8-flash-medium"}},
        {"event": "step_update", "step_update": {"conversation_id": "c1", "step_index": 2, "state": "ACTIVE",
                                                 "step_type": "tool", "tool_name": "read_file", "tool_info": {
                                                     "name": "read_file", "parameters": {"Path": str(tmp_path / "notes.md")}}}},
        {"event": "step_update", "step_update": {"conversation_id": "c1", "step_index": 2, "state": "DONE",
                                                 "step_type": "tool", "tool_name": "read_file", "tool_info": {
                                                     "name": "read_file", "output": "1 lines, 25 bytes"}}},
        {"event": "step_update", "step_update": {"conversation_id": "c1", "step_index": 3, "state": "ERROR",
                                                 "step_type": "tool", "tool_name": "run_command", "tool_info": {
                                                     "name": "run_command", "parameters": {"CommandLine": "echo x"},
                                                     "error": {"type": "TOOL_ERROR", "message": "user denied"}}}},
        {"event": "step_update", "step_update": {"conversation_id": "c1", "step_index": 4, "state": "ACTIVE",
                                                 "step_type": "agent_response", "text_delta": "Der kalte"}},
        {"event": "result", "result": {"conversation_id": "c1", "status": "SUCCESS", "response": "Der kalte Wind.\n",
                                       "duration_seconds": 7.47, "num_turns": 1}},
    ]
    stream = tmp_path / "s.jsonl"
    stream.write_text("".join(json.dumps(line) + "\n" for line in lines), encoding="utf-8")
    events = cli.events(stream)
    assert [e["type"] for e in events] == ["system", "assistant", "user", "user", "result"]
    assert [a for e in events for a in cli.actions(e, tmp_path)] == ["read_file notes.md"]
    found = cli.outcome(events)
    assert found["session"] == "c1" and found["model"] == "gemini-3.8-flash-medium"
    assert found["result"]["result"] == "Der kalte Wind.\n" and found["result"]["subtype"] == "success"
    assert events[2]["message"]["content"][0]["tool_use_id"] == events[1]["message"]["content"][0]["id"]
    failed = events[3]["message"]["content"][0]
    assert failed["is_error"] is True and failed["content"] == "user denied"


def test_an_agy_instance_names_itself_to_the_model_and_offers_no_quota(repo):
    tools = {tool["function"]["name"]: tool["function"] for tool in make_agy(repo).get_tools()}
    assert set(tools) == {"agy_cli_run_task", "agy_cli_get_run", "agy_cli_cancel_run"}
    assert "Give a programming task to Antigravity." in tools["agy_cli_run_task"]["description"]
    assert "max_window_utilization" not in tools["agy_cli_run_task"]["parameters"]["properties"]


async def test_each_user_has_an_agy_home_of_their_own(repo, log, data_root):  # noqa: F811
    server = make_agy(repo, allowed_users=["admin", "other"])
    await call(server, "run_task", task="x")
    await call(server, "run_task", user="other", task="x")
    assert Path(log()["home"]) == data_root / "agy_home" / "other"


def test_the_pinned_settings_win_over_others_and_leave_agys_own_alone(tmp_path):
    settings = tmp_path / ".gemini" / "antigravity-cli" / "settings.json"
    settings.parent.mkdir(parents=True)
    settings.write_text('{"toolPermission": "always-proceed"}', encoding="utf-8")
    cli.agy_home(tmp_path, {})
    assert json.loads(settings.read_text(encoding="utf-8")) == cli.AGY_SETTINGS
    agys_own = json.dumps(cli.AGY_SETTINGS, indent=2)   # agy writes the settings back in its own layout
    settings.write_text(agys_own, encoding="utf-8")
    cli.agy_home(tmp_path, {})
    assert settings.read_text(encoding="utf-8") == agys_own


@pytest.mark.parametrize("root", cli.AGY_ROOTS)
def test_an_agy_run_may_not_write_a_folder_agy_would_run_from(root):
    """measured: agy ran a command hook from hooks.json in each of its four roots without asking; the deny rule
    kept a run from writing any of them, in any case."""
    assert f"write_file({root}/)" in cli.AGY_SETTINGS["permissions"]["deny"]


@pytest.mark.parametrize("planted", [".agents/hooks.json", "_agents/plugins/x/plugin.json", ".agent/mcp_config.json",
                                     "_Agent/Hooks.json"])
async def test_a_worktree_carrying_what_agy_would_run_is_not_started(tmp_path, planted, log):
    repo = fresh_repo(tmp_path / "planted")
    (repo / planted).parent.mkdir(parents=True)
    (repo / planted).write_text("{}", encoding="utf-8")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "plant")
    result, _ = await call(make_agy(repo), "run_task", task="x")
    assert "not started" in result["error"] and "/".join(planted.split("/")[:2]) in result["error"]
    assert git(repo, "branch", "--list", "coding_cli/*").strip() == ""


async def test_a_run_whose_worktree_got_a_hook_is_not_resumed(repo, log):
    server = make_agy(repo)
    first, _ = await call(server, "run_task", task="WRITE b.txt one")
    worktree = Path(server._load(first["run_id"])["worktree"])
    (worktree / "_agents").mkdir()
    (worktree / "_agents" / "hooks.json").write_text("{}", encoding="utf-8")
    result, _ = await call(server, "run_task", task="WRITE c.txt two", resume=first["run_id"])
    assert "not started" in result["error"] and "_agents/hooks.json" in result["error"]
    assert worktree.is_dir(), "a refused resume keeps the earlier run's worktree"


def test_effort_beside_a_model_naming_its_level_is_dropped(repo, caplog):
    assert make_agy(repo, model="gemini-3.8-flash-medium", effort="high").effort == ""
    assert "names its level already" in caplog.text
    assert make_agy(repo, model="gemini-3.8-flash", effort="high").effort == "high"


def test_a_failed_step_whose_error_is_no_object_still_reads(tmp_path):
    stream = tmp_path / "s.jsonl"
    stream.write_text(json.dumps({"event": "step_update", "step_update": {
        "conversation_id": "c", "step_index": 1, "state": "ERROR", "step_type": "tool", "tool_name": "x",
        "tool_info": {"name": "x", "error": "plain words"}}}) + "\n", encoding="utf-8")
    assert cli.events(stream)[0]["message"]["content"][0]["content"] == "plain words"
