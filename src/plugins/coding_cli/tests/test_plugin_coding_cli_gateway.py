"""A workdir with a gateway: each run gets a grant of the MCP gateway (plugins/mcp_gateway) for its time, and
its CLI is pointed at it -- Claude Code by its MCP file and environment, agy by the workspace's MCP file.

Real are the grants (the gateway plugin's own module, serving in this process), the server, the worktree and the
detached process; only the CLI is replaced.
"""
import json
from pathlib import Path

import psutil
import pytest

from agent_system.config.models import AgentSystemConfig, MCPServersConfig, RemoteMCPConfig
from plugins.coding_cli import run as cli
from test_plugin_coding_cli_config import PLUGIN, load_schema_from_dir
from plugins.mcp_gateway import grants

from test_plugin_coding_cli_agy import make_agy
from test_plugin_coding_cli_server import (call, data_root, fresh_repo, git, log, make_server,  # noqa: F401
                                            plain_git, repo)

GATEWAY = {"tools": ["probe_*"], "pin": {"probe_*": ["scope_id"]}, "allowed": {"probe_edit": {"op": ["get"]}}}
PIN = {"probe_*": {"scope_id": 7}}


@pytest.fixture
def gateway():
    grants.serve("mcp_gateway", True)
    yield
    grants.serve("mcp_gateway", False)


def with_gateway(repo, **gateway):
    return {"repo": {"path": str(repo), "exclude": ["config/secrets.env"], "gateway": {**GATEWAY, **gateway}}}


async def test_a_claude_code_run_reaches_its_grant_and_only_while_it_runs(repo, data_root, log, gateway, monkeypatch):
    monkeypatch.setenv("HOST", "127.0.0.9")     # the address the API listens on
    server = make_server(repo, workdirs=with_gateway(repo), wait_s=0.2)
    # More than the workdir requires only narrows the run further: the grant gets it whole.
    pin = {**PIN, "probe_edit": {"op": "get"}}
    started, _ = await call(server, "run_task", task="SLEEP 30", pin=pin, _request_id="req42")
    try:
        proc = psutil.Process(server._load(started["run_id"])["pid"])
        argv, env = proc.cmdline(), proc.environ()
        config = json.loads(Path(argv[argv.index("--mcp-config") + 1]).read_text(encoding="utf-8"))
        token = env["CODING_CLI_MCP_0"].removeprefix("Bearer ")
        grant = grants.lookup("mcp_gateway", token)
    finally:
        await call(server, "cancel_run", run_id=started["run_id"])
    assert config == {"mcpServers": {cli.GATEWAY_SERVER: {
        "type": "http", "url": f"http://127.0.0.9:{server.gateway_port}/plugins/mcp_gateway/mcp",
        "headers": {"Authorization": "${CODING_CLI_MCP_0}"}}}}
    assert (grant.user_id, grant.session_id, grant.pinned) == ("admin", "s1", pin)
    # Under the caller's id, a prefix of its own per run.
    assert grant.request_id == f"req42_{started['run_id']}"
    assert grant.tools == ("probe_*",) and grant.allowed == GATEWAY["allowed"]
    assert argv[argv.index("--allowedTools") + 1] == f"mcp__{cli.GATEWAY_SERVER}"
    # Revoked at the run's end; never on disk.
    assert grants.lookup("mcp_gateway", token) is None
    for path in (data_root / "runs").iterdir():
        assert token not in path.read_text(encoding="utf-8", errors="replace"), path


@pytest.mark.parametrize("workdir_gateway, pin, said", [
    (True, None, "must fix probe_*: scope_id"),
    (True, {"probe_edit": {"scope_id": 7}}, "must fix probe_*: scope_id"),
    (True, {"scope_id": 7}, "pin: {tool pattern: {parameter: value}}"),
    (True, {"probe_*": {"scope_id": [7]}}, "pin: {tool pattern: {parameter: value}}"),
    (False, PIN, "has no gateway")])
async def test_a_run_brings_at_least_the_bindings_its_gateway_requires(repo, gateway, workdir_gateway, pin, said):
    workdirs = with_gateway(repo) if workdir_gateway else {"repo": {"path": str(repo)}}
    result, _ = await call(make_server(repo, workdirs=workdirs), "run_task", task="x", pin=pin)
    assert said in result["error"], result


async def test_a_gateway_that_does_not_serve_starts_no_run(tmp_path, log):
    repo = fresh_repo(tmp_path / "r")
    result, _ = await call(make_server(repo, workdirs=with_gateway(repo)), "run_task", task="x", pin=PIN)
    assert "not started: mcp gateway" in result["error"] and "does not serve" in result["error"]
    assert git(repo, "branch", "--list", "coding_cli/*").strip() == ""


async def test_plan_mode_gets_no_grant(repo, log, gateway, monkeypatch):
    issued = []
    monkeypatch.setattr(grants, "issue", lambda *a, **k: issued.append(k) or "t")
    result, _ = await call(make_server(repo, workdirs=with_gateway(repo)), "run_task", task="look", mode="plan",
                           pin=PIN)
    assert result["state"] == "done" and issued == []
    assert f"mcp__{cli.GATEWAY_SERVER}" not in log()["argv"]


@pytest.mark.parametrize("entry, kept", [
    ({"web": True, "gateway": GATEWAY}, False),
    ({"web": True, "gateway": {**GATEWAY, "read_only": True}}, True),
    ({"web": True, "gateway": {**GATEWAY, "confined": True}}, True),
    ({"gateway": {**GATEWAY, "pin": {}, "confined": True}}, False),
    ({"gateway": {**GATEWAY, "pin": ["scope_id"]}}, False),
    ({"gateway": {**GATEWAY, "tools": []}}, False),
    ({"gateway": {**GATEWAY, "pin": {"probe_*": ["a b"]}}}, False),
    ({"gateway": GATEWAY, "mcp_servers": [cli.GATEWAY_SERVER]}, False),
    ({"gateway": {**GATEWAY, "alowed": {"probe_edit": {"op": ["get"]}}}}, False),
    ({"gateway": {**GATEWAY, "pin": {"probe_*": ["request_id"]}}}, False),
    ({"gateway": {**GATEWAY, "allowed": {"probe_edit": {"op": "get"}}}}, False),
    ({"gateway": {**GATEWAY, "allowed": {"probe_edit": {"a..b": ["get"]}}}}, False)])
def test_a_workdirs_gateway_is_checked_at_the_start(repo, entry, kept):
    # An operator's server of the gateway's name would be usable but for the name.
    usable = {cli.GATEWAY_SERVER: RemoteMCPConfig(url="http://h/mcp", enabled=True)}
    system = AgentSystemConfig(external_servers=MCPServersConfig(remote_servers=usable))
    server = make_server(repo, system, workdirs={"w": {"path": str(repo), **entry}})
    assert ("w" in server.workdirs) is kept


def agy_with_gateway(repo):
    return make_agy(repo, workdirs={"repo": {**with_gateway(repo, read_only=True)["repo"], "web": True}})


async def test_an_agy_run_finds_its_grant_in_the_workspace_and_commits_none(repo, log, gateway):
    server = agy_with_gateway(repo)
    first, _ = await call(server, "run_task", task="WRITE b.txt one", pin=PIN)
    config = json.loads(log()["mcp_config"])
    token = config["mcpServers"][cli.GATEWAY_SERVER]["headers"]["Authorization"].removeprefix("Bearer ")
    assert config["mcpServers"][cli.GATEWAY_SERVER]["serverUrl"].endswith("/plugins/mcp_gateway/mcp")
    assert f"mcp({cli.GATEWAY_SERVER}/*)" in cli.AGY_SETTINGS["permissions"]["allow"]
    assert first["state"] == "done" and grants.lookup("mcp_gateway", token) is None
    worktree = Path(server._load(first["run_id"])["worktree"])
    assert not (worktree / ".agents").exists()
    assert ".agents" not in git(repo, "ls-tree", "-r", "--name-only", first["branch"])
    # The file is gone, so the resume starts -- with a grant of its own, on the same values.
    second, _ = await call(server, "run_task", task="WRITE c.txt two", resume=first["run_id"])
    again = json.loads(log()["mcp_config"])["mcpServers"][cli.GATEWAY_SERVER]["headers"]["Authorization"]
    assert second["state"] == "done" and again != f"Bearer {token}"
    assert server._load(second["run_id"])["pin"] == PIN
    refused, _ = await call(server, "run_task", task="x", resume=second["run_id"], pin={"probe_*": {"scope_id": 8}})
    assert "a resume keeps run" in refused["error"]


async def test_a_refused_resume_leaves_the_workspace_file_it_did_not_write(repo, log, gateway):
    server = agy_with_gateway(repo)
    first, _ = await call(server, "run_task", task="WRITE b.txt one", pin=PIN)
    planted = Path(server._load(first["run_id"])["worktree"]) / ".agents" / "mcp_config.json"
    planted.parent.mkdir()
    planted.write_text("{}", encoding="utf-8")
    refused, _ = await call(server, "run_task", task="x", resume=first["run_id"])
    assert "not started" in refused["error"] and planted.read_text(encoding="utf-8") == "{}"


def test_the_gateway_is_dialled_where_the_api_listens(repo, monkeypatch):
    monkeypatch.setenv("HOST", "10.0.0.5")
    assert make_server(repo).gateway_host == "10.0.0.5"
    monkeypatch.setenv("HOST", "0.0.0.0")
    assert make_server(repo).gateway_host == "127.0.0.1"


def spy_tokens(monkeypatch):
    tokens, issue = [], grants.issue
    monkeypatch.setattr(grants, "issue", lambda *a, **k: tokens.append(issue(*a, **k)) or tokens[-1])
    return tokens


async def test_a_start_failing_after_its_grant_revokes_it(repo, log, gateway, monkeypatch):
    tokens = spy_tokens(monkeypatch)
    server = make_server(repo, workdirs=with_gateway(repo))
    server.command = [str(Path(repo) / "no_such_cli.exe")]
    result, _ = await call(server, "run_task", task="x", pin=PIN)
    assert "not started" in result["error"] and len(tokens) == 1
    assert grants.lookup("mcp_gateway", tokens[0]) is None


async def test_an_agy_resume_failing_after_its_grant_leaves_no_file(repo, log, gateway, monkeypatch):
    server = agy_with_gateway(repo)
    first, _ = await call(server, "run_task", task="WRITE b.txt one", pin=PIN)
    tokens = spy_tokens(monkeypatch)
    server.command = [str(Path(repo) / "no_such_cli.exe")]
    refused, _ = await call(server, "run_task", task="x", resume=first["run_id"])
    worktree = Path(server._load(first["run_id"])["worktree"])
    assert "not started" in refused["error"] and len(tokens) == 1
    assert grants.lookup("mcp_gateway", tokens[0]) is None and not (worktree / ".agents").exists()


async def test_an_agy_file_that_stays_is_not_committed(repo, log, gateway, monkeypatch):
    server = agy_with_gateway(repo)
    write = cli.agy_gateway
    monkeypatch.setattr(cli, "agy_gateway", lambda w, u, t: write(w, u, t) if t else None)   # removal fails
    result, _ = await call(server, "run_task", task="WRITE b.txt one", pin=PIN)
    assert not result.get("commit") and "could not be removed" in json.dumps(result)
    assert git(repo, "rev-parse", result["branch"]) == git(repo, "rev-parse", "HEAD")


async def test_a_resume_after_its_workdirs_gateway_changed_is_refused(repo, log, gateway):
    first, _ = await call(make_server(repo), "run_task", task="x")
    changed = make_server(repo, workdirs=with_gateway(repo))
    refused, _ = await call(changed, "run_task", task="y", resume=first["run_id"])
    assert "start a new run" in refused["error"]


def test_run_task_offers_pin_only_where_a_workdir_fixes_values(repo):
    def has_pin(server):
        schema = load_schema_from_dir(str(PLUGIN), template_vars=server.get_template_vars())
        run_task = next(t for t in schema["tools"] if t["function"]["name"].endswith("_run_task"))
        return "pin" in run_task["function"]["parameters"]["properties"]
    assert has_pin(make_server(repo, workdirs=with_gateway(repo)))
    assert not has_pin(make_server(repo, workdirs=with_gateway(repo, pin={})))
    assert not has_pin(make_server(repo))
