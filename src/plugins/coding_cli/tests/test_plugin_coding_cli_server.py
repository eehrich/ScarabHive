"""The server against a fake Claude Code that replays the measured stream.

Real are the git repository, the worktree, the detached process and the files
it streams into; only the CLI is replaced (fake_claude.py). What these pin
down is the plugin's side: what it starts, what it refuses, what a run leaves.
"""
import asyncio
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import psutil
import pytest

from agent_system.config.models import AgentSystemConfig, ToolServerConfig
from agent_system.core.session_presence import alive
from plugins.coding_cli import run as cli
from plugins.coding_cli import server as server_module
from plugins.coding_cli.server import CodingCliServer

FAKE = Path(__file__).resolve().parent / "fake_claude.py"
# The root conftest ends what a test session leaves running by this variable.
TEST_SESSION_MARKER = "AGENT_SYSTEM_TEST_SESSION"


class Status:
    def __init__(self):
        self.progress_lines, self.closing = [], []

    async def progress(self, message, meta=None):
        self.progress_lines.append(message)

    async def end(self, message, meta=None):
        self.closing.append(("end", message))

    async def error(self, message, meta=None):
        self.closing.append(("error", message))


def git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True).stdout


@pytest.fixture(scope="module", autouse=True)
def plain_git(tmp_path_factory):
    """Git reads no global or system config: a git-lfs filter there made
    every checkout of a worktree take five seconds."""
    empty = tmp_path_factory.mktemp("git") / "gitconfig"
    empty.write_text("", encoding="utf-8")
    with pytest.MonkeyPatch.context() as patch:
        patch.setenv("GIT_CONFIG_GLOBAL", str(empty))
        patch.setenv("GIT_CONFIG_NOSYSTEM", "1")
        yield


@pytest.fixture(autouse=True)
def data_root(tmp_path, monkeypatch):
    """Runs, streams and worktrees never reach data/coding_cli."""
    monkeypatch.setattr(server_module, "DATA_ROOT", tmp_path / "data")
    monkeypatch.setattr(server_module, "POLL_S", 0.05)
    return tmp_path / "data"


@pytest.fixture(scope="module")
def repo(tmp_path_factory, plain_git):
    """One repository for the module: every run works on a branch of its own."""
    path = tmp_path_factory.mktemp("repo")
    git(path, "init", "-q")
    git(path, "config", "user.name", "test")
    git(path, "config", "user.email", "test@example.com")
    (path / "config").mkdir()
    (path / "config" / "secrets.env").write_text("SECRET=1\n", encoding="utf-8")
    (path / "a.txt").write_text("a\n", encoding="utf-8")
    (path / "CLAUDE.md").write_text("# rules\n", encoding="utf-8")
    git(path, "add", ".")
    git(path, "commit", "-q", "-m", "init")
    return path


@pytest.fixture
def log(tmp_path, monkeypatch):
    path = tmp_path / "fake_log.json"
    monkeypatch.setenv("FAKE_CLAUDE_LOG", str(path))
    return lambda: json.loads(path.read_text(encoding="utf-8"))


def make_server(repo, **config):
    cfg = ToolServerConfig()
    cfg.workdirs = {"repo": {"path": str(repo), "exclude": ["config/secrets.env"]}}
    cfg.allowed_users = ["admin"]
    # The run gets an allowlisted environment; without the test session's
    # marker the root conftest would never reap a run a test leaves behind.
    cfg.pass_env = ["FAKE_CLAUDE_LOG", TEST_SESSION_MARKER]
    cfg.wait_s = 30
    for key, value in config.items():
        setattr(cfg, key, value)
    server = CodingCliServer("coding_cli", AgentSystemConfig(), cfg)
    server.command = [sys.executable, str(FAKE)]
    return server


async def call(server, tool, user="admin", session="s1", **params):
    status = Status()
    result = await getattr(server, tool)({**params, "_status": status, "_user_id": user, "_session_id": session})
    assert len(status.closing) == 1, status.closing
    return result, status


async def ended(server, run_id, timeout=20):
    monitor = server._monitors.get(run_id)
    if monitor is not None:
        await asyncio.wait_for(asyncio.shield(monitor), timeout)
    if server._rings:
        await asyncio.wait_for(asyncio.gather(*server._rings.values()), timeout)


async def gone(pid):
    for _ in range(100):
        if not alive(pid):
            return
        await asyncio.sleep(0.1)
    raise AssertionError(f"process {pid} still alive")


def ring_recorder(monkeypatch):
    """wake_session as a recorder; every session may be woken."""
    rings = []

    async def wake(system_config, session_id, user_id, what="", still_needed=None):
        rings.append((session_id, user_id, still_needed))
        return "woke_session"

    monkeypatch.setattr(server_module, "wake_blocked", lambda *a: "")
    monkeypatch.setattr(server_module, "wake_session", wake)
    return rings


async def unwatched(repo, task, **config):
    """A run whose starting process is gone (an API restart): started, its
    watch stopped, the process left to itself."""
    first = make_server(repo, wait_s=0.2, **config)
    started, _ = await call(first, "run_task", task=task)
    await first.stop_plugin()
    return first._load(started["run_id"])


async def test_a_run_leaves_its_changes_committed_on_its_branch(repo):
    server = make_server(repo)
    head = git(repo, "rev-parse", "HEAD")
    result, status = await call(server, "run_task", task="WRITE b.txt hello")
    assert result["state"] == "done", result
    assert git(repo, "show", f"{result['branch']}:b.txt") == "hello"
    assert "A\tb.txt" in result["changes"]["content"] and result["commit"]
    assert git(repo, "rev-parse", "HEAD") == head and not (repo / "b.txt").exists()
    assert result["result"] == {"untrusted": True, "content": "did: WRITE b.txt hello"}
    assert result["denials"] == {"untrusted": True, "content": ["Write /elsewhere/outside.txt"]} and result["abo"]["five_hour"] == 0.34
    assert status.closing[0][0] == "end" and "1 file(s)" in status.closing[0][1]


async def test_an_excluded_file_is_neither_readable_nor_deleted_on_the_branch(repo):
    server = make_server(repo)
    result, _ = await call(server, "run_task", task="WRITE b.txt hello")
    assert not (Path(result["worktree"]) / "config" / "secrets.env").exists()
    assert git(repo, "ls-tree", "--name-only", result["branch"], "config/secrets.env").strip() == "config/secrets.env"
    assert not any("secrets" in line for line in result["changes"]["content"])
    assert result["hidden"] == ["config/secrets.env"]


def fresh_repo(path):
    path.mkdir()
    git(path, "init", "-q")
    git(path, "config", "user.name", "test")
    git(path, "config", "user.email", "test@example.com")
    (path / "a.txt").write_text("a\n", encoding="utf-8")
    git(path, "add", ".")
    git(path, "commit", "-q", "-m", "init")
    return path


def test_every_file_holding_an_excluded_secret_is_hidden_too(tmp_path):
    """A key copied into a doc or a test: the exclude alone would leave it readable."""
    repo = fresh_repo(tmp_path / "repo")
    (repo / "config").mkdir()
    (repo / "config" / "secrets.env").write_text('API_KEY="k3y-value-123"\nSHORT=abc\n', encoding="utf-8")
    (repo / "config" / "config.yaml").write_text(
        "auth:\n  secret_key: jwt-signing-key-42\n  expire_minutes: 12345678\n  title: public-name-value\n",
        encoding="utf-8")
    (repo / "docs").mkdir()
    (repo / "docs" / "review.md").write_text("the key was k3y-value-123\n", encoding="utf-8")
    (repo / "check.py").write_text("KEY = 'jwt-signing-key-42'\n", encoding="utf-8")
    (repo / "plain.md").write_text("public-name-value abc 12345678\n", encoding="utf-8")
    git(repo, "add", ".")
    git(repo, "commit", "-q", "-m", "leaks")
    worktree = tmp_path / "wt"
    made = cli.make_worktree(repo, worktree, "b1", ["config/secrets.env", "config/config.yaml"])
    assert made.hidden == ["check.py", "config/config.yaml", "config/secrets.env", "docs/review.md"]
    assert not any((worktree / rel).exists() for rel in made.hidden) and (worktree / "plain.md").exists()
    assert git(worktree, "status", "--porcelain") == ""


async def test_a_rewritten_git_file_does_not_move_the_commit(repo):
    """The .git file in the worktree is the run's to rewrite: the plugin's git
    keeps to the git directory it was made with."""
    server = make_server(repo)
    result, _ = await call(server, "run_task", task="WRITE b.txt hello\nWRITE .git gitdir: elsewhere")
    assert result["state"] == "done" and result["commit"], result.get("note")
    assert git(repo, "show", f"{result['branch']}:b.txt") == "hello"


async def test_no_hook_runs_when_the_plugin_commits(tmp_path):
    """A hook would run a program in ScarabHive's process, with its keys."""
    repo = fresh_repo(tmp_path / "repo")
    hooks, marker = tmp_path / "hooks", tmp_path / "hook_ran"
    hooks.mkdir()
    hook = hooks / "pre-commit"
    hook.write_text(f"#!/bin/sh\necho ran > '{marker.as_posix()}'\n", encoding="utf-8", newline="\n")
    os.chmod(hook, 0o755)
    git(repo, "config", "core.hooksPath", str(hooks))
    (repo / "c.txt").write_text("c\n", encoding="utf-8")
    git(repo, "add", "c.txt")
    git(repo, "commit", "-q", "-m", "the hook runs here")
    assert marker.exists()
    marker.unlink()
    result, _ = await call(make_server(repo), "run_task", task="WRITE b.txt hello")
    assert result["commit"] and not marker.exists()


async def test_the_command_line_locks_claude_code_down(repo, log):
    server = make_server(repo)
    await call(server, "run_task", task="WRITE b.txt x")
    argv = log()["argv"]
    assert {"--restricted", "--strict-mcp-config", "-p"} <= set(argv)
    assert argv[argv.index("--tools") + 1] == "Read,Edit,Write,Glob,Grep"
    assert argv[argv.index("--permission-mode") + 1] == "acceptEdits"
    assert "--allowedTools" not in argv and "--resume" not in argv
    assert json.loads(Path(argv[argv.index("--mcp-config") + 1]).read_text()) == {"mcpServers": {}}
    assert Path(argv[argv.index("--append-system-prompt-file") + 1]).name == "CLAUDE.md"
    assert log()["task"] == "WRITE b.txt x" and "WRITE" not in " ".join(argv)


async def test_plan_mode_only_reads(repo, log):
    server = make_server(repo, allowed_commands=["pytest:*"])
    result, _ = await call(server, "run_task", task="look", mode="plan")
    argv = log()["argv"]
    assert argv[argv.index("--tools") + 1] == "Read,Glob,Grep"
    assert argv[argv.index("--permission-mode") + 1] == "plan" and "--allowedTools" not in argv
    assert result["state"] == "done" and result["commit"] is None


async def test_allowed_commands_bring_the_shell_and_git_push_stays_refused(repo, log):
    server = make_server(repo, allowed_commands=["pytest:*"])
    await call(server, "run_task", task="test it")
    argv = log()["argv"]
    shell = cli.shell_tool()
    assert argv[argv.index("--tools") + 1].endswith(f",{shell}")
    assert argv[argv.index("--allowedTools") + 1] == f"{shell}(pytest:*)"
    denied = argv[argv.index("--disallowedTools") + 1:argv.index("--tools")]
    assert f"{shell}(git push:*)" in denied and f"{shell}(git remote:*)" in denied


async def test_the_child_gets_no_key_from_this_process(repo, log, monkeypatch):
    """An ANTHROPIC_API_KEY would switch Claude Code from the subscription to the API."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    server = make_server(repo)
    await call(server, "run_task", task="x")
    env = {name.upper() for name in log()["env"]}
    assert "ANTHROPIC_API_KEY" not in env and "OPENAI_API_KEY" not in env and "PATH" in env


async def test_a_run_a_test_starts_carries_the_test_session_marker(repo):
    """A run is detached and outlives its test when the test fails early; the
    root conftest ends it at the end of the session only if it carries the
    marker, and the allowlist in child_env leaves it out unless passed on."""
    server = make_server(repo, wait_s=0.2)
    started, _ = await call(server, "run_task", task="SLEEP 30")
    try:
        assert started["state"] == "running", started
        run = psutil.Process(server._load(started["run_id"])["pid"])
        assert run.environ()[TEST_SESSION_MARKER] == os.environ[TEST_SESSION_MARKER]
    finally:
        await call(server, "cancel_run", run_id=started["run_id"])


async def test_only_listed_users_start_runs(repo, data_root):
    server = make_server(repo)
    result, status = await call(server, "run_task", user="bob", task="x")
    assert "allowed_users" in result["error"] and status.closing[0][0] == "error"
    assert not (data_root / "runs").exists()


@pytest.mark.parametrize("resets_in, refused", [(3600, True), (-60, False)])
async def test_a_full_window_refuses_new_runs_until_it_resets(repo, data_root, resets_in, refused):
    data_root.mkdir(parents=True)
    (data_root / "quota.json").write_text(json.dumps({"status": "allowed", "unifiedWindows": {
        "five_hour": {"utilization": 0.85, "resetsAt": time.time() + resets_in}}}))
    server = make_server(repo)
    result, _ = await call(server, "run_task", task="x")
    assert ("five_hour window is at 85%" in result.get("error", "")) is refused
    assert (result.get("state") == "done") is not refused


async def test_what_a_run_reports_of_the_subscription_guards_the_next(repo):
    server = make_server(repo)
    first, _ = await call(server, "run_task", task="RATE 0.9")
    assert first["state"] == "done" and first["abo"]["five_hour"] == 0.9
    second, _ = await call(server, "run_task", task="x")
    assert "90%" in second["error"]


async def test_resume_continues_the_conversation_in_the_same_worktree(repo, log):
    server = make_server(repo)
    first, _ = await call(server, "run_task", task="WRITE b.txt one")
    session = server._load(first["run_id"])["claude_session"]
    second, _ = await call(server, "run_task", task="WRITE c.txt two", resume=first["run_id"])
    argv = log()["argv"]
    assert argv[argv.index("--resume") + 1] == session
    assert second["worktree"] == first["worktree"] and second["branch"] == first["branch"]
    assert {"A\tb.txt", "A\tc.txt"} <= set(second["changes"]["content"])


async def test_resume_takes_only_an_ended_run_of_ones_own(repo, data_root):
    server = make_server(repo)
    first, _ = await call(server, "run_task", task="x")
    other, _ = await call(server, "run_task", user="admin2", task="x", resume=first["run_id"])
    assert "allowed_users" in other["error"]
    server.allowed_users = frozenset({"admin", "admin2"})
    other, _ = await call(server, "run_task", user="admin2", task="x", resume=first["run_id"])
    assert "no run" in other["error"]
    # A record outside runs/ is never reached: the id is no path.
    (data_root / "evil.json").write_text(json.dumps({**server._load(first["run_id"]), "run_id": "../evil"}))
    for bogus_id in ("../evil", "../../etc"):
        bogus, _ = await call(server, "run_task", task="x", resume=bogus_id)
        assert "no run" in bogus["error"], bogus_id


async def test_two_runs_never_share_a_worktree(repo):
    server = make_server(repo, max_parallel=2, wait_s=0.2)
    first, _ = await call(server, "run_task", task="x")
    await call(server, "get_run", run_id=first["run_id"], wait_s=20)
    second, _ = await call(server, "run_task", task="SLEEP 30", resume=first["run_id"])
    assert second["state"] == "running"
    third, _ = await call(server, "run_task", task="x", resume=first["run_id"])
    assert "another run works in that worktree" in third["error"]
    await call(server, "cancel_run", run_id=second["run_id"])


async def test_a_long_run_answers_with_its_id_and_wakes_the_session(repo, monkeypatch):
    rings = ring_recorder(monkeypatch)
    server = make_server(repo, wait_s=0.3)
    result, status = await call(server, "run_task", task="SLEEP 1.5\nWRITE b.txt x")
    assert result["state"] == "running" and result["wake"] is True and "end your turn" in result["wake_note"]
    assert "one-shot agent-cli run is never woken" in result["wake_note"]
    assert status.closing[0] == ("end", f"run {result['run_id']} still going after 0 s, wake armed")
    await ended(server, result["run_id"])
    assert [r[:2] for r in rings] == [("s1", "admin")] and rings[0][2]() is True
    final, _ = await call(server, "get_run", run_id=result["run_id"])
    assert final["state"] == "done" and rings[0][2]() is False


class SubAgentPresence:
    def get(self, session_id, user_id):
        return {"sub_agent": True}


@pytest.mark.parametrize("woken, sub_agent, session, reason", [
    (1, False, "s1", "itself woken"), (0, True, "s1", "sub-agent's session is never woken"),
    (0, False, "../s1", "cannot be watched")])
async def test_a_woken_run_or_a_sub_agent_is_not_promised_a_wake(repo, monkeypatch, woken, sub_agent, session, reason):
    """wake_blocked checks neither (core/session_presence.py)."""
    monkeypatch.setattr(server_module, "wake_blocked", lambda *a: "")
    monkeypatch.setattr(server_module, "wake_depth", lambda: woken)
    monkeypatch.setattr(server_module, "presence_for", lambda cfg: SubAgentPresence() if sub_agent else None)
    server = make_server(repo, wait_s=0.2)
    result, _ = await call(server, "run_task", session=session, task="SLEEP 30")
    assert result["wake"] is False and reason in result["wake_note"]
    assert not server._file(result["run_id"], "wake").exists()
    await call(server, "cancel_run", run_id=result["run_id"])


async def test_without_a_wake_the_note_says_how_to_wait(repo):
    """No session presence in a bare config: nobody can be woken."""
    server = make_server(repo, wait_s=0.2)
    result, _ = await call(server, "run_task", task="SLEEP 1\nWRITE b.txt x")
    assert result["wake"] is False and "wait_s" in result["wake_note"] and result["run_id"] in result["wake_note"]
    final, status = await call(server, "get_run", run_id=result["run_id"], wait_s=20)
    assert final["state"] == "done" and status.closing[0][0] == "end"


async def test_progress_names_what_claude_code_does(repo):
    server = make_server(repo)
    server.wait_s = 20
    _, status = await call(server, "run_task", task="WRITE b.txt x\nSLEEP 0.5")
    assert any(line.startswith("Write b.txt") for line in status.progress_lines), status.progress_lines


def test_a_tool_line_names_the_file_relative_to_the_worktree(tmp_path):
    event = {"type": "assistant", "message": {"content": [
        {"type": "tool_use", "name": "Edit", "input": {"file_path": str(tmp_path / "src" / "x.py")}},
        {"type": "tool_use", "name": "PowerShell", "input": {"command": "pytest  -q\n tests"}},
        {"type": "text", "text": "\n  Done now.\nmore"}]}}
    assert cli.actions(event, tmp_path) == ["Edit src/x.py", "PowerShell pytest -q tests", "Done now."]


async def test_a_run_that_dies_fails_with_its_stderr(repo):
    server = make_server(repo)
    result, status = await call(server, "run_task", task="WRITE b.txt x\nDIE")
    assert result["state"] == "failed" and "without a result" in result["note"]
    assert result["details"] == {"untrusted": True, "content": ["boom"]}
    assert "A\tb.txt" in result["changes"]["content"] and status.closing[0][0] == "error"


async def test_an_error_result_fails(repo):
    server = make_server(repo)
    result, _ = await call(server, "run_task", task="ERROR")
    assert result["state"] == "failed" and result["result"]["content"] == "failed"


async def test_a_foreign_line_in_the_stream_is_skipped(repo):
    server = make_server(repo)
    result, _ = await call(server, "run_task", task="GARBAGE\nWRITE b.txt x")
    assert result["state"] == "done"


async def test_cancel_stops_the_run_and_keeps_what_it_changed(repo):
    server = make_server(repo, wait_s=0.5)
    started, _ = await call(server, "run_task", task="WRITE b.txt x\nSLEEP 30")
    record = server._load(started["run_id"])
    for _ in range(100):
        if (Path(record["worktree"]) / "b.txt").exists():
            break
        await asyncio.sleep(0.1)
    pid = record["pid"]
    _, looked = await call(server, "get_run", run_id=started["run_id"])
    assert looked.closing[0][1].endswith("running, 1 recent action(s)"), looked.closing
    result, status = await call(server, "cancel_run", run_id=started["run_id"])
    assert result["state"] == "cancelled" and "cancelled on request" in result["note"]
    assert "A\tb.txt" in result["changes"]["content"] and not alive(pid) and status.closing[0][0] == "error"


async def test_the_time_limit_stops_a_run(repo):
    server = make_server(repo)
    server.max_run_s = 0.5
    result, _ = await call(server, "run_task", task="SLEEP 30")
    assert result["state"] == "cancelled" and "time limit" in result["note"]


async def test_a_run_nobody_watches_is_ended_by_the_next_look(repo):
    """The API restarted while the run went on: the process is detached, and
    the next get_run in any process finds its end on disk."""
    first = make_server(repo, wait_s=0.2)
    started, _ = await call(first, "run_task", task="SLEEP 1\nWRITE b.txt x")
    await first.stop_plugin()
    pid = first._load(started["run_id"])["pid"]
    for _ in range(100):
        if not alive(pid):
            break
        await asyncio.sleep(0.1)
    result, _ = await call(make_server(repo), "get_run", run_id=started["run_id"])
    assert result["state"] == "done" and "A\tb.txt" in result["changes"]["content"]


async def test_another_users_run_is_no_run(repo):
    server = make_server(repo, allowed_users=["admin", "eve"])
    started, _ = await call(server, "run_task", task="x")
    for tool in ("get_run", "cancel_run"):
        result, _ = await call(server, tool, user="eve", run_id=started["run_id"])
        assert "no run" in result["error"]


async def test_one_run_at_a_time(repo):
    server = make_server(repo, wait_s=0.2)
    first, _ = await call(server, "run_task", task="SLEEP 30")
    second, _ = await call(server, "run_task", task="x")
    assert first["run_id"] in second["error"]
    await call(server, "cancel_run", run_id=first["run_id"])


async def test_a_stopped_turn_stops_the_run_it_waits_for(repo):
    server = make_server(repo)
    call_task = asyncio.create_task(call(server, "run_task", task="SLEEP 30"))
    record = None
    for _ in range(300):        # the worktree takes seconds under load
        records = server._records()
        record = records[0] if records else None
        if record and record.get("pid"):
            break
        await asyncio.sleep(0.1)
    assert record and record.get("pid"), "the run never got its process"
    call_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await call_task
    result, _ = await call(server, "get_run", run_id=record["run_id"], wait_s=20)
    assert result["state"] == "cancelled" and "stopped with the turn" in result["note"]
    assert not alive(record["pid"])


@pytest.mark.parametrize("age_s, taken_over", [(3600, True), (0, False)])
async def test_a_claim_left_by_a_dead_process_is_taken_over(repo, age_s, taken_over):
    """An API restart while a run was being finalized left its claim behind."""
    first = make_server(repo, wait_s=0.2)
    started, _ = await call(first, "run_task", task="SLEEP 1\nWRITE b.txt x")
    await first.stop_plugin()
    run_id = started["run_id"]
    pid = first._load(run_id)["pid"]
    for _ in range(100):
        if not alive(pid):
            break
        await asyncio.sleep(0.1)
    claim = first._file(run_id, "final")
    claim.touch()
    os.utime(claim, (time.time() - age_s, time.time() - age_s))
    result, _ = await call(make_server(repo), "get_run", run_id=run_id)
    assert (result["state"] == "done") is taken_over, result


async def test_a_line_torn_in_two_still_counts(repo):
    """The stream is read while it is written: a line half there is left for the next read."""
    server = make_server(repo)
    _, status = await call(server, "run_task", task="TORN\nSLEEP 0.3")
    assert any(line.startswith("Write torn.txt") for line in status.progress_lines), status.progress_lines


async def test_a_subscription_that_refuses_stops_new_runs(repo):
    server = make_server(repo)
    await call(server, "run_task", task="REJECT")
    second, _ = await call(server, "run_task", task="x")
    assert "refuses requests (rejected)" in second["error"]


@pytest.mark.parametrize("cancels", [1, 2, "every task"])
async def test_a_stop_while_the_worktree_is_made_stops_the_run(repo, monkeypatch, cancels):
    """The worktree takes seconds (git-lfs); a turn stopped in that time must
    not leave a run behind that nobody watches -- nor when it is cancelled
    again while it waits for the start (a second Ctrl+C, a loop's teardown)."""
    made = cli.make_worktree

    def slow(*args):
        time.sleep(1.0)
        return made(*args)

    monkeypatch.setattr(cli, "make_worktree", slow)
    server = make_server(repo)
    call_task = asyncio.create_task(call(server, "run_task", task="SLEEP 30"))
    await asyncio.sleep(0.3)
    if cancels == "every task":       # as a loop's teardown does
        for other in asyncio.all_tasks():
            if other is not asyncio.current_task():
                other.cancel()
    else:
        for _ in range(cancels):
            call_task.cancel()
            await asyncio.sleep(0.1)
    with pytest.raises(asyncio.CancelledError):
        await call_task
    record = server._records()[0]
    await gone(record["pid"])
    result, _ = await call(server, "get_run", run_id=record["run_id"], wait_s=20)
    assert result["state"] == "cancelled" and "stopped with the turn" in result["note"]


@pytest.mark.parametrize("how", ["start_plugin", "run_task"])
async def test_a_run_nobody_watches_is_stopped_at_its_time_limit(repo, how):
    """Its process gone, the run is taken over by the next process that starts
    the plugin, and settled by the next run_task."""
    record = await unwatched(repo, "SLEEP 30")
    server = make_server(repo)
    server._save({**record, "started_at": time.time() - 2 * server.max_run_s})
    if how == "start_plugin":
        await server.start_plugin()
        await ended(server, record["run_id"])
    else:
        started, _ = await call(server, "run_task", task="x")
        assert started["state"] == "done", started
    await gone(record["pid"])
    assert server._load(record["run_id"])["state"] == "cancelled"


async def test_cancelling_a_run_that_ended_unseen_keeps_its_outcome(repo):
    record = await unwatched(repo, "SLEEP 0.5\nWRITE b.txt x")
    await gone(record["pid"])
    result, status = await call(make_server(repo), "cancel_run", run_id=record["run_id"])
    assert result["state"] == "done" and status.closing[0][0] == "end"


@pytest.mark.parametrize("reader, rung", [("s2", True), ("s1", False)])
async def test_the_session_that_asked_is_rung_whoever_ends_the_run(repo, monkeypatch, reader, rung):
    """The end is found by another process and read by another session: the
    session told "you are woken" is rung all the same -- unless it read the
    end itself."""
    rings = ring_recorder(monkeypatch)
    record = await unwatched(repo, "SLEEP 1")
    other = make_server(repo)
    assert json.loads(other._file(record["run_id"], "wake").read_text())["session_id"] == "s1"
    result, _ = await call(other, "get_run", session=reader, run_id=record["run_id"], wait_s=20)
    await ended(other, record["run_id"])
    # Rung or read, the wake is done with: no sweep looks at it again.
    wake = other._file(record["run_id"], "wake")
    assert not wake.exists()
    # A second look, in any process, rings no more -- also once the ringer is
    # gone, and also when the wake could not be removed.
    wake.write_text(json.dumps({"session_id": "s1", "user_id": "admin"}))
    lease = other._file(record["run_id"], "rung")
    if lease.exists():
        lease.write_text(json.dumps({**dead_process(), "done": True}))
    third = make_server(repo)
    await call(third, "get_run", session=reader, run_id=record["run_id"])
    await ended(third, record["run_id"])
    assert result["state"] == "done" and not wake.exists()
    assert [r[:2] for r in rings] == ([("s1", "admin")] if rung else [])


async def test_resume_settles_a_run_that_ended_unseen(repo):
    record = await unwatched(repo, "SLEEP 0.5\nWRITE b.txt x")
    await gone(record["pid"])
    result, _ = await call(make_server(repo), "run_task", task="WRITE c.txt y", resume=record["run_id"])
    assert result["state"] == "done" and {"A\tb.txt", "A\tc.txt"} <= set(result["changes"]["content"])


def test_kill_tree_returns_once_the_processes_are_gone():
    """On Windows the kill is asynchronous: finalizing right after it found the run alive."""
    procs = [subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"]) for _ in range(5)]
    try:
        for proc in procs:
            cli.kill_tree(proc.pid, cli.process_start(proc.pid))
            assert not alive(proc.pid)
    finally:
        for proc in procs:
            proc.kill()
            proc.wait()


async def test_cancelling_a_run_that_died_unseen_keeps_its_failure(repo):
    """A process already gone is not cancelled: its own end is the answer."""
    record = await unwatched(repo, "SLEEP 0.5\nDIE")
    await gone(record["pid"])
    result, _ = await call(make_server(repo), "cancel_run", run_id=record["run_id"])
    assert result["state"] == "failed" and result["details"]["content"] == ["boom"]


async def test_a_success_result_wins_over_a_stop_that_came_too_late(repo):
    """The stop can land between the process's end and the finalize."""
    record = await unwatched(repo, "SLEEP 0.5\nWRITE b.txt x")
    await gone(record["pid"])
    server = make_server(repo)
    server._stop(record["run_id"], "cancelled on request")
    assert server._finalize(record["run_id"])["state"] == "done"


def dead_process():
    """{pid, started} of a process that has ended."""
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    started = cli.process_start(proc.pid)
    proc.wait()
    return {"pid": proc.pid, "started": started, "instance": "gone"}


async def test_a_run_being_started_by_a_live_owner_is_left_alone(repo, data_root):
    """Between its first save and the launch a run has no pid yet: only its
    owner may end it, or a look from elsewhere records it as failed."""
    owner = make_server(repo)
    (data_root / "runs").mkdir(parents=True)
    owner._save({"run_id": "a1b2c3d4e5f6", "user_id": "admin", "workdir": "repo", "mode": "edit",
                 "state": "running", "started_at": time.time(), "owner": owner._me})
    owner._starting.add("a1b2c3d4e5f6")
    await owner._sweep()
    other = make_server(repo)
    await other._sweep()
    refused, _ = await call(other, "run_task", task="x")
    assert "a1b2c3d4e5f6" in refused["error"]
    result, _ = await call(other, "get_run", run_id="a1b2c3d4e5f6")
    assert result["state"] == "running" and other._load("a1b2c3d4e5f6")["state"] == "running"
    assert "a1b2c3d4e5f6" not in other._monitors


async def test_a_run_whose_owner_lives_is_not_taken_over(repo):
    owner = make_server(repo, wait_s=0.2)
    started, _ = await call(owner, "run_task", task="SLEEP 30")
    other = make_server(repo)
    await other.start_plugin()
    try:
        assert started["run_id"] not in other._monitors and started["run_id"] in owner._monitors
    finally:
        await other.stop_plugin()
        await call(owner, "cancel_run", run_id=started["run_id"])


async def test_the_sweep_takes_over_a_run_whose_owner_left_later(repo, monkeypatch):
    """The API started before the one-shot process that started the run."""
    monkeypatch.setattr(server_module, "SWEEP_S", 0.2)
    api = make_server(repo)
    api.max_run_s = 1
    await api.start_plugin()
    try:
        record = await unwatched(repo, "SLEEP 30")
        for _ in range(600):        # a sweep, a kill and a commit take seconds under load
            if api._load(record["run_id"])["state"] != "running":
                break
            await asyncio.sleep(0.1)
        assert api._load(record["run_id"])["state"] == "cancelled"
        await gone(record["pid"])
    finally:
        await api.stop_plugin()


async def test_a_ring_cut_short_is_rung_again(repo, monkeypatch):
    """A ringer stopped before the end (a restart while the session was held)
    leaves no lease; one that died leaves a lease of a dead holder."""
    rings = []

    async def slow_wake(system_config, session_id, user_id, what="", still_needed=None):
        rings.append(session_id)
        await asyncio.sleep(30)

    monkeypatch.setattr(server_module, "wake_blocked", lambda *a: "")
    monkeypatch.setattr(server_module, "wake_session", slow_wake)
    first = make_server(repo, wait_s=0.2)
    started, _ = await call(first, "run_task", task="SLEEP 0.5")
    run_id = started["run_id"]
    for _ in range(600):        # run, commit and finalize take seconds under load
        if rings:
            break
        await asyncio.sleep(0.1)
    await first.stop_plugin()
    assert rings == ["s1"] and not first._file(run_id, "rung").exists()
    second = make_server(repo)
    second._ring(run_id)
    await asyncio.sleep(0.2)
    assert rings == ["s1", "s1"]
    await second.stop_plugin()
    # A ringer that died leaves its lease behind.
    first._file(run_id, "rung").write_text(json.dumps({**dead_process(), "done": False}))
    third = make_server(repo)
    third._ring(run_id)
    await asyncio.sleep(0.2)
    assert rings == ["s1", "s1", "s1"]
    await third.stop_plugin()


async def test_a_save_refused_after_the_launch_kills_the_run(repo, monkeypatch):
    """Answered "not started", the run must not go on."""
    launched = []
    launch = cli.launch

    def recording_launch(*args):
        launched.append(launch(*args))
        return launched[-1]

    monkeypatch.setattr(cli, "launch", recording_launch)
    server = make_server(repo)
    saves = []
    save = server._save

    def refusing_save(record):
        saves.append(record.get("pid"))
        if record.get("pid"):
            raise PermissionError(13, "sharing violation")
        save(record)

    server._save = refusing_save
    result, _ = await call(server, "run_task", task="SLEEP 30")
    assert "not started" in result["error"]
    await gone(launched[0].pid)


def test_a_save_waits_out_a_sharing_violation(repo, data_root, monkeypatch):
    server = make_server(repo)
    (data_root / "runs").mkdir(parents=True)
    replace, refused = os.replace, []

    def flaky(src, dst):
        if len(refused) < 3:
            refused.append(dst)
            raise PermissionError(13, "sharing violation")
        replace(src, dst)

    monkeypatch.setattr(server_module.os, "replace", flaky)
    server._save({"run_id": "a1b2c3d4e5f6", "state": "done"})
    assert server._load("a1b2c3d4e5f6")["state"] == "done" and len(refused) == 3


async def test_an_orphan_is_taken_over_by_one_instance_only(repo):
    """Both instances read the orphan before either has taken it over."""
    record = await unwatched(repo, "SLEEP 30")
    first, second = make_server(repo), make_server(repo)
    await first._adopt(record)
    await second._adopt(record)
    try:
        assert [record["run_id"] in s._monitors for s in (first, second)] == [True, False]
    finally:
        await call(first, "cancel_run", run_id=record["run_id"])
        await first.stop_plugin()


async def test_a_cancel_from_elsewhere_waits_for_the_owner_to_end_it(repo):
    owner = make_server(repo, wait_s=0.2)
    started, _ = await call(owner, "run_task", task="SLEEP 30")
    result, _ = await call(make_server(repo), "cancel_run", run_id=started["run_id"])
    assert result["state"] == "cancelled" and "cancelled on request" in result["note"]


def test_a_read_waits_out_a_replace_under_way(repo, data_root, monkeypatch):
    """Windows refuses a read while another task replaces the record."""
    server = make_server(repo)
    (data_root / "runs").mkdir(parents=True)
    server._save({"run_id": "a1b2c3d4e5f6", "state": "done"})
    read, refused = Path.read_text, []

    def flaky(self, *args, **kwargs):
        if self.suffix == ".json" and len(refused) < 3:
            refused.append(self)
            raise PermissionError(13, "sharing violation")
        return read(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", flaky)
    assert server._load("a1b2c3d4e5f6")["state"] == "done" and len(refused) == 3


async def test_a_look_at_a_run_whose_record_is_gone_says_so(repo, data_root):
    server = make_server(repo)
    (data_root / "runs").mkdir(parents=True)
    server._save({"run_id": "a1b2c3d4e5f6", "user_id": "admin", "state": "running"})
    server._file("a1b2c3d4e5f6", "json").unlink()
    assert (await server._settle("a1b2c3d4e5f6"))["note"] == "the record is gone"


async def test_a_run_whose_finalize_died_is_ended_once_the_claim_is_stale(repo, data_root):
    """Its owner died while finalizing: the fresh claim holds every sweep off
    until it is stale -- the sweeps before must not use up the takeover."""
    server = make_server(repo)
    (data_root / "runs").mkdir(parents=True)
    run_id = "a1b2c3d4e5f6"
    dead = dead_process()
    server._save({"run_id": run_id, "user_id": "admin", "workdir": "repo", "mode": "plan", "state": "running",
                  "started_at": time.time(), "owner": dead, "pid": dead["pid"], "pid_started": dead["started"]})
    server._file(run_id, "final").touch()
    await server._sweep()
    await server._sweep()
    assert server._load(run_id)["state"] == "running"
    old = time.time() - 2 * server_module.STALE_CLAIM_S
    os.utime(server._file(run_id, "final"), (old, old))
    await server._sweep()
    assert server._load(run_id)["state"] == "failed"


async def test_a_failing_sweep_leaves_the_sweeping_going(repo, monkeypatch):
    """The first sweep in start_plugin and every later one in the loop."""
    monkeypatch.setattr(server_module, "SWEEP_S", 0.02)
    server = make_server(repo)
    failed = []

    async def broken():
        failed.append(1)
        raise PermissionError(13, "sharing violation")

    monkeypatch.setattr(server, "_sweep", broken)
    await server.start_plugin()
    try:
        for _ in range(250):
            if len(failed) >= 4:
                break
            await asyncio.sleep(0.02)
        assert len(failed) >= 4 and not server._sweeper.done()
    finally:
        await server.stop_plugin()


@pytest.mark.parametrize("why", ["woken", "blocked", "lease unreadable"])
async def test_a_process_that_cannot_ring_leaves_the_ring_to_one_that_can(repo, monkeypatch, why):
    rings = ring_recorder(monkeypatch)
    record = await unwatched(repo, "SLEEP 0.3")
    await gone(record["pid"])
    server = make_server(repo)
    server._file(record["run_id"], "wake").write_text(json.dumps({"session_id": "s1", "user_id": "admin"}))
    await server._settle(record["run_id"])
    await ended(server, record["run_id"])
    assert len(rings) == 1
    # As if the ring had not got through yet.
    server._file(record["run_id"], "wake").write_text(json.dumps({"session_id": "s1", "user_id": "admin"}))
    if why == "woken":
        monkeypatch.setattr(server_module, "wake_depth", lambda: 1)
    elif why == "blocked":
        monkeypatch.setattr(server_module, "wake_blocked", lambda *a: "waking is switched off")
    else:
        def refused(*args):
            raise PermissionError(13, "sharing violation")
        monkeypatch.setattr(server_module, "_claim", refused)
    server._file(record["run_id"], "rung").unlink(missing_ok=True)
    server._ring(record["run_id"])
    await ended(server, record["run_id"])
    assert len(rings) == 1 and not server._file(record["run_id"], "rung").exists()


def test_a_claim_whose_write_fails_leaves_nothing_behind(tmp_path):
    """An empty lease would hold every later ring off."""
    class Unwritable(str):
        def encode(self, *args, **kwargs):
            raise OSError(28, "no space left on device")

    with pytest.raises(OSError):
        server_module._claim(tmp_path / "x.rung", Unwritable("{}"))
    assert not (tmp_path / "x.rung").exists()


@pytest.mark.parametrize("age_s, rung", [(1, False), (60, True)])
async def test_an_empty_lease_is_taken_over_once_it_is_stale(repo, monkeypatch, age_s, rung):
    """A ringer that died between creating its lease and writing it."""
    rings = ring_recorder(monkeypatch)
    record = await unwatched(repo, "SLEEP 0.3")
    await gone(record["pid"])
    server = make_server(repo)
    # Ended first: finalizing takes seconds under load, the lease's age must be the one it rings with.
    assert server._finalize(record["run_id"])["state"] == "done"
    lease = server._file(record["run_id"], "rung")
    lease.write_text("")
    old = time.time() - age_s
    os.utime(lease, (old, old))
    server._ring(record["run_id"])
    await ended(server, record["run_id"])
    assert len(rings) == (1 if rung else 0)


async def test_a_cancel_before_the_launch_stops_the_run_once_it_is_there(repo, monkeypatch):
    """Between the first save and the launch a run has no process to kill:
    the stop waits on disk, and the owner's monitor carries it out."""
    release = threading.Event()
    launch = cli.launch

    def held_launch(*args):
        release.wait(20)
        return launch(*args)

    monkeypatch.setattr(cli, "launch", held_launch)
    # The cancel waits for the owner's end; a worktree and a commit take long under load.
    monkeypatch.setattr(server_module, "CLAIM_WAIT_S", 120)
    owner = make_server(repo, wait_s=0.2)
    starting = asyncio.create_task(call(owner, "run_task", task="SLEEP 30"))
    try:
        records = []
        for _ in range(1200):
            records = owner._records()
            if records:
                break
            await asyncio.sleep(0.05)
        run_id = records[0]["run_id"]
        assert not records[0].get("pid")
        cancelling = asyncio.create_task(call(make_server(repo), "cancel_run", run_id=run_id))
        for _ in range(100):
            if owner._file(run_id, "cancel").exists():
                break
            await asyncio.sleep(0.05)
    finally:
        release.set()
    await starting
    result, _ = await cancelling
    assert result["state"] == "cancelled" and "cancelled on request" in result["note"]


def test_a_record_without_a_worktree_commits_nothing_where_the_process_runs(repo, data_root, tmp_path, monkeypatch):
    """A hand-made or broken record: git must not run in the process's own
    directory -- a test run's is the developer's checkout."""
    here = tmp_path / "checkout"
    here.mkdir()
    git(here, "init", "-q")
    (here / "loose.txt").write_text("x\n", encoding="utf-8")
    monkeypatch.chdir(here)
    server = make_server(repo)
    (data_root / "runs").mkdir(parents=True)
    server._save({"run_id": "a1b2c3d4e5f6", "user_id": "admin", "mode": "edit", "base": "HEAD", "state": "running",
                  "git_dir": str(here / ".git")})
    assert server._finalize("a1b2c3d4e5f6")["state"] == "failed"
    assert git(here, "status", "--porcelain") == "?? loose.txt\n"


async def test_a_task_that_is_no_valid_text_is_refused_before_anything_is_made(repo, data_root):
    """A lone surrogate (a broken escape in the tool call) would fail only
    after the worktree and the branch exist."""
    result, status = await call(make_server(repo), "run_task", task=json.loads('"WRITE b.txt x \\ud83d"'))
    assert "not valid text" in result["error"] and status.closing[0][0] == "error"
    assert not (data_root / "worktrees").exists()


async def test_a_git_error_is_quoted_in_details_not_in_the_note(repo):
    """git's message can name a file the run created: it is the run's words."""
    record = await unwatched(repo, "SLEEP 0.3\nWRITE b.txt x")
    await gone(record["pid"])
    (Path(record["git_dir"]) / "index.lock").write_text("", encoding="utf-8")
    result, _ = await call(make_server(repo), "get_run", run_id=record["run_id"])
    assert result["note"].startswith("not committed, the changes are in the worktree")
    assert "index.lock" not in result["note"]
    assert result["details"]["untrusted"] is True and any("index.lock" in d for d in result["details"]["content"])


async def test_a_ring_whose_lease_cannot_be_marked_done_is_not_rung_again(repo, monkeypatch):
    """The wake goes first: a lease left "not done" by a failed write rings no
    second time once its holder is gone."""
    rings = ring_recorder(monkeypatch)
    record = await unwatched(repo, "SLEEP 0.3")
    await gone(record["pid"])
    run_id, server = record["run_id"], make_server(repo)
    write = Path.write_text

    def refusing(self, *args, **kwargs):
        if self.suffix == ".rung":
            raise PermissionError(13, "sharing violation")
        return write(self, *args, **kwargs)

    monkeypatch.setattr(Path, "write_text", refusing)
    await server._settle(run_id)
    await ended(server, run_id)
    monkeypatch.setattr(Path, "write_text", write)
    assert len(rings) == 1 and not server._file(run_id, "wake").exists()
    server._file(run_id, "rung").write_text(json.dumps({**dead_process(), "done": False}))
    other = make_server(repo)
    other._ring(run_id)
    await ended(other, run_id)
    assert len(rings) == 1


async def test_no_fsmonitor_runs_when_the_plugin_commits(tmp_path):
    """A configured fsmonitor would run a program in ScarabHive's process."""
    repo = fresh_repo(tmp_path / "repo")
    marker, monitor = tmp_path / "fsmonitor_ran", tmp_path / "fsmonitor.sh"
    # Only once the run has written its file: the worktree is made with the operator's own config.
    monitor.write_text(f"#!/bin/sh\n[ -f trigger ] && echo ran > '{marker.as_posix()}'\nexit 1\n",
                       encoding="utf-8", newline="\n")
    os.chmod(monitor, 0o755)
    git(repo, "config", "core.fsmonitor", monitor.as_posix())
    (repo / "trigger").write_text("x\n", encoding="utf-8")
    git(repo, "status", "--porcelain")
    assert marker.exists()
    marker.unlink()
    (repo / "trigger").unlink()
    result, _ = await call(make_server(repo), "run_task", task="WRITE trigger x")
    assert result["commit"] and not marker.exists()


async def test_a_long_hidden_list_is_capped(repo, data_root):
    server = make_server(repo)
    (data_root / "runs").mkdir(parents=True)
    server._save({"run_id": "a1b2c3d4e5f6", "user_id": "admin", "state": "done",
                  "hidden": [f"f{i}.txt" for i in range(60)]})
    result, _ = await call(server, "get_run", run_id="a1b2c3d4e5f6")
    assert len(result["hidden"]) == 51 and result["hidden"][-1] == "... 10 more"

