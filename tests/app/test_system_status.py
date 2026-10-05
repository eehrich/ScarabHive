"""The System panel's status: every level has a reason behind it.

/health said "ok" whenever the process answered. The admin status names what
is wrong: servers that did not start, LLMs paused after rate limits, and a
checked-out commit the process does not run yet.
"""
from __future__ import annotations

import subprocess
import zipfile
from pathlib import Path

import pytest

from agent_system import runtime as runtime_module
from agent_system.config.models import ToolServerConfig
from agent_system.llm.model_health import ModelHealth
from agent_system.runtime import Runtime
from agent_system.services import system_status
from bootstrap.test_runtime_lazy_contract import _config, _plugin_dir

COMMIT_A = {"hash": "a" * 40, "date": "2026-09-15T10:00:00+02:00", "subject": "running"}
COMMIT_B = {"hash": "b" * 40, "date": "2026-09-15T11:00:00+02:00", "subject": "pulled later"}


def _checks(**facts):
    base = dict(problems=[], blocked=[], commit_at_start=COMMIT_A, commit_on_disk=COMMIT_A)
    return {c["name"]: c for c in system_status.build_checks(**{**base, **facts})}


def test_nothing_wrong_is_ok():
    checks = _checks()

    assert {c["level"] for c in checks.values()} == {"ok"}
    assert system_status.overall(list(checks.values())) == "ok"


def test_a_server_that_did_not_start_is_an_error():
    checks = _checks(problems=["server 'x': unknown type 'y'"])

    assert checks["servers"]["level"] == "error"
    assert "server 'x'" in checks["servers"]["detail"]
    assert system_status.overall(list(checks.values())) == "error"


def test_a_paused_model_needs_attention():
    checks = _checks(blocked=[{"endpoint": "e", "model": "gpt-x", "seconds_left": 42.0, "pause": 60.0}])

    assert checks["llm"]["level"] == "warn"
    assert "gpt-x" in checks["llm"]["detail"]
    assert system_status.overall(list(checks.values())) == "warn"


def test_newer_code_on_disk_says_a_restart_deploys_it():
    checks = _checks(commit_on_disk=COMMIT_B)

    assert checks["deploy"]["level"] == "warn"
    assert "bbbbbbbb" in checks["deploy"]["detail"] and "aaaaaaaa" in checks["deploy"]["detail"]


def test_the_worst_check_decides():
    checks = _checks(problems=["p"], commit_on_disk=COMMIT_B)

    assert system_status.overall(list(checks.values())) == "error"


def test_the_commit_is_read_from_git():
    root = Path(__file__).resolve().parents[2]
    if not (root / ".git").exists():
        pytest.skip("not a git checkout (an unpacked download)")
    commit = system_status.git_commit(root)

    assert commit is not None, "fixture: the test tree is not a git checkout"
    assert len(commit["hash"]) == 40 and commit["subject"]


def _git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout


def test_a_download_without_git_names_the_commit_it_was_made_from(tmp_path):
    """What GitHub/GitLab do for "Download ZIP": git archive, which fills in
    the repo's own _commit.json through its own .gitattributes."""
    repo_root = Path(__file__).resolve().parents[2]
    repo = tmp_path / "repo"
    (repo / "src/agent_system").mkdir(parents=True)
    (repo / ".gitattributes").write_bytes((repo_root / ".gitattributes").read_bytes())
    (repo / "src/agent_system/_commit.json").write_bytes(
        (repo_root / "src/agent_system/_commit.json").read_bytes())
    _git(repo, "init", "-q")
    _git(repo, "add", ".")
    _git(repo, "-c", "user.name=t", "-c", "user.email=t@t", "-c", "commit.gpgsign=false", "commit", "-q", "-m", "c")
    head = _git(repo, "rev-parse", "HEAD").strip()
    archive = tmp_path / "download.zip"
    _git(repo, "archive", "--format=zip", "-o", str(archive), "HEAD")
    unpacked = tmp_path / "unpacked"
    zipfile.ZipFile(archive).extractall(unpacked)

    # A clone keeps the placeholders: nothing is claimed from them.
    assert system_status.archived_commit(repo / "src/agent_system/_commit.json") is None
    commit = system_status.git_commit(unpacked, unpacked / "src/agent_system/_commit.json")
    assert commit is not None and commit["hash"] == head and commit["date"]


def test_an_unpacked_download_inside_another_repository_does_not_take_its_commit(tmp_path):
    outer = tmp_path / "outer"
    outer.mkdir()
    _git(outer, "init", "-q")
    _git(outer, "-c", "user.name=t", "-c", "user.email=t@t", "-c", "commit.gpgsign=false", "commit", "-q", "--allow-empty", "-m", "outer")
    inside = outer / "unpacked"
    inside.mkdir()
    assert system_status.git_commit(inside, inside / "_commit.json") is None


def test_blocked_names_paused_models_without_the_credential():
    class Client:
        model = "gpt-x"
        base_url = "https://llm.example/v1"
        api_key = "secret-key"

    health = ModelHealth()
    health.block(Client(), max_pause=600, rate_limit=True)

    blocked = health.blocked()

    assert [b["model"] for b in blocked] == ["gpt-x"]
    assert blocked[0]["seconds_left"] > 0
    assert "secret" not in repr(blocked) and len(blocked[0]) == 4


def test_the_runtime_records_what_did_not_start(tmp_path, monkeypatch):
    # not "plugins": that package name would shadow src/plugins for later tests
    root = tmp_path / "status_probe_plugins"
    _plugin_dir(root, "broken_probe", "", "def PLUGIN_FACTORY(*a, **k):\n    raise RuntimeError('boom')\n")
    monkeypatch.setattr(runtime_module, "_in_test_cwd", lambda: False)
    monkeypatch.setattr(Runtime, "last_started", Runtime.last_started)  # restored after the test

    runtime = Runtime(_config({"probe_unknown": ToolServerConfig(type="no_such_type", enabled=True),
                               "probe_broken": ToolServerConfig(type="broken_probe", enabled=True)},
                              plugin_dirs=[str(root)])).start()

    assert any("probe_unknown" in p and "no_such_type" in p for p in runtime.problems), runtime.problems
    assert any("probe_broken" in p and "boom" in p for p in runtime.problems), runtime.problems
    assert Runtime.last_started is runtime


async def test_the_collected_status_reports_the_last_started_runtime(tmp_path, monkeypatch):
    monkeypatch.setattr(runtime_module, "_in_test_cwd", lambda: False)
    monkeypatch.setattr(Runtime, "last_started", Runtime.last_started)  # restored after the test
    runtime = Runtime(_config({"probe_unknown": ToolServerConfig(type="no_such_type", enabled=True)},
                              plugin_dirs=[str(tmp_path)])).start()
    monkeypatch.setattr(system_status, "_started", {"at": 1.0, "commit": COMMIT_A})
    monkeypatch.setattr(system_status, "git_commit", lambda cwd=None: COMMIT_B)

    status = await system_status.collect_system_status()

    assert status["status"] == "error"
    assert status["servers"]["problems"] == runtime.problems
    assert {c["name"]: c["level"] for c in status["checks"]}["deploy"] == "warn"


def test_an_agent_config_error_is_its_own_check_not_a_server_that_did_not_start():
    checks = _checks(config_findings=["agent 'a': LLM config does not resolve"])

    assert checks["servers"]["level"] == "ok"
    assert checks["config"]["level"] == "error" and "agent 'a'" in checks["config"]["detail"]


def test_validate_findings_are_kept_apart_from_problems(monkeypatch):
    monkeypatch.setattr(Runtime, "last_started", Runtime.last_started)  # restored after the test
    runtime = Runtime(_config({}))
    monkeypatch.setattr(runtime, "validate", lambda: ["agent 'a': LLM config does not resolve"])

    runtime.start()

    assert runtime.config_findings == ["agent 'a': LLM config does not resolve"]
    assert runtime.problems == []
