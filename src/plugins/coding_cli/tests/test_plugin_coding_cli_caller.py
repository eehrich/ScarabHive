"""What a call brings to its run: the caller's own tools (a workdir whose gateway says ``caller``), a model and
effort of its own, and -- refused -- a code a caller decides by.

A CLI that answers as an agent's model (llm_coding_cli) calls that agent's tools: the run's grant names the agent
that made the call as its runner, and exactly the tools that agent may call. Real are the gateway plugin (its
names, its grants), the calling Agent with a tool server, the coding_cli server and the detached process; only the
CLI is replaced.
"""
import asyncio
import json
import time
from pathlib import Path

import psutil
import pytest

from agent_system.config.models import (AgentConfig, AgentSystemConfig, LLMModelConfig, LLMProfile, LLMSystemConfig,
                                        ToolConfig, ToolServerConfig)
from agent_system.core.request_context import current_run_agent, current_tool_bindings
from agent_system.core.session_presence import alive
from agent_system.servers.agent.server import Agent
from agent_system.tools.base import ToolServer, ToolServerRegistry
from plugins.coding_cli import run as cli
from plugins.coding_cli import server as server_module
from plugins.mcp_gateway import grants
from plugins.mcp_gateway.plugin import McpGatewayPlugin

from test_plugin_coding_cli_agy import make_agy
from test_plugin_coding_cli_server import (call, data_root, fresh_repo, git, gone, log, make_server,  # noqa: F401
                                            plain_git, repo)

ADDRESS = "http://127.0.0.9:8123/plugins/mcp_gateway/mcp"
CALLER = {"caller": True}


class _Probe(ToolServer):
    def __init__(self):
        super().__init__("probe", AgentSystemConfig(), ToolServerConfig(type="probe", enabled=True))

    def get_tools(self):
        return [{"type": "function", "function": {"name": name, "description": name, "parameters": {
            "type": "object", "properties": {"text": {"type": "string"}}}}}
            for name in ("probe_read", "probe_edit", "probe_secret")]


def agent_with(allowed, coding_cli=None):
    """An agent of its own, as a sub-agent's run builds one: the probe's tools and, where given, a coding_cli
    instance's, allowed as given."""
    tools = ToolServerRegistry()
    tools.register("probe", _Probe())
    if coding_cli is not None:
        tools.register(coding_cli.name, coding_cli)
    llm = LLMSystemConfig(models={"m": LLMModelConfig(provider="openai", model="m", api_key="fake")},
                          profiles={"normal": LLMProfile(model_ref="m")}, default_profile="normal")
    return Agent("writer_agent", AgentSystemConfig(llm_system=llm), ToolServerConfig(
        type="agent", enabled=True, agent_config=AgentConfig(max_steps=1, llm_profile="normal",
                                                            tools=ToolConfig(allowed=allowed))), tools)


@pytest.fixture
async def gateway():
    """The gateway plugin of this process: it tells an agent's tool names and serves at ADDRESS."""
    plugin = McpGatewayPlugin("mcp_gateway", AgentSystemConfig(), ToolServerConfig(type="mcp_gateway", enabled=True))
    grants.serve("mcp_gateway", True, ADDRESS)
    yield plugin
    await plugin.stop_plugin()


def caller_workdir(repo, **gateway):
    return {"repo": {"path": str(repo), "exclude": ["config/secrets.env"], "gateway": {**CALLER, **gateway}}}


async def grant_of(server, run_id):
    """The grant of a run going now, or None where it got no gateway."""
    env = psutil.Process(server._load(run_id)["pid"]).environ()
    token = env.get("CODING_CLI_MCP_0")
    return grants.lookup("mcp_gateway", token.removeprefix("Bearer ")) if token else None


@pytest.mark.parametrize("named, tools", [
    (["probe_read", "probe_secret", "coding_cli_run_task", "no_such_tool"], ("probe_read",)),
    (None, ("probe_edit", "probe_read")),       # every tool of the caller's, this instance's own left out
])
async def test_a_run_in_a_caller_workdir_reaches_the_tools_of_the_agent_that_made_the_call(repo, gateway, named,
                                                                                         tools):
    server = make_server(repo, workdirs=caller_workdir(repo), wait_s=0.2)
    agent = agent_with(["probe/probe_read", "probe/probe_edit", "coding_cli/*"], server)
    extra = {} if named is None else {"_tools": named}
    started, _ = await call(server, "run_task", task="SLEEP 30", _agent=agent, **extra)
    try:
        grant = await grant_of(server, started["run_id"])
        assert grant.tools == tools and grant.runner is agent
        assert grant.session_vars == {} and (grant.user_id, grant.session_id) == ("admin", "s1")
    finally:
        await call(server, "cancel_run", run_id=started["run_id"])


async def test_the_agent_of_a_model_call_is_the_runs(repo, gateway):
    """llm_coding_cli's call is no tool call: it brings no _agent, the agent run around it is the caller."""
    server = make_server(repo, workdirs=caller_workdir(repo), wait_s=0.2)
    agent = agent_with(["probe/probe_read"])
    reset = current_run_agent.set(agent)
    try:
        started, _ = await call(server, "run_task", task="SLEEP 30", _tools=["probe_read"])
    finally:
        current_run_agent.reset(reset)
    try:
        assert (await grant_of(server, started["run_id"])).runner is agent
    finally:
        await call(server, "cancel_run", run_id=started["run_id"])


async def test_the_workdirs_patterns_narrow_the_callers_tools(repo, gateway):
    server = make_server(repo, workdirs=caller_workdir(repo, tools=["probe_r*"]), wait_s=0.2)
    started, _ = await call(server, "run_task", task="SLEEP 30", _agent=agent_with(["probe/*"]))
    try:
        assert (await grant_of(server, started["run_id"])).tools == ("probe_read",)
    finally:
        await call(server, "cancel_run", run_id=started["run_id"])


@pytest.mark.parametrize("case", ["the call names no tool", "the agent has none"])
async def test_a_caller_without_tools_gets_a_run_without_a_gateway(repo, gateway, log, case):
    server = make_server(repo, workdirs=caller_workdir(repo))
    extra = ({"_tools": []} if case == "the call names no tool" else {"_agent": agent_with([])})
    result, _ = await call(server, "run_task", task="x", **extra)
    assert result["state"] == "done", result
    argv = log()["argv"]
    assert json.loads(Path(argv[argv.index("--mcp-config") + 1]).read_text(encoding="utf-8")) == {"mcpServers": {}}
    assert server._load(result["run_id"])["gateway"] is None


async def test_a_caller_workdir_without_a_calling_agent_starts_nothing(repo, gateway, data_root):
    result, _ = await call(make_server(repo, workdirs=caller_workdir(repo)), "run_task", task="x")
    assert "no agent made this call" in result["error"], result
    assert not (data_root / "worktrees").exists()


@pytest.mark.parametrize("gateway_entry, web, usable", [
    ({"caller": True}, False, True),
    ({"caller": True}, True, False),                    # a page could steer the caller's tools
    ({"caller": True, "with_web": True}, True, True),   # the operator accepts it
    ({"tools": ["probe_*"], "with_web": True}, True, False),   # with_web is the caller's: others say read_only
    # nobody can vouch that whichever agent calls has only reading tools: beside web, with_web alone counts
    ({"caller": True, "read_only": True}, True, False),
    ({"caller": True, "confined": True, "pin": {"probe_*": ["text"]}}, True, False),
    ({"caller": True, "read_only": True}, False, False),
    ({"caller": "yes"}, False, False),
    ({"caller": 1}, False, False),      # equal to True and none: no fixed gateway of every tool by a slip
    ({"caller": True, "with_web": 1}, True, False),
])
def test_a_caller_gateway_meets_the_web_only_when_the_operator_accepts_it(repo, gateway_entry, web, usable):
    workdirs = {"repo": {"path": str(repo), "web": web, "gateway": gateway_entry}}
    assert bool(make_server(repo, workdirs=workdirs).workdirs) is usable


async def test_an_agy_run_registers_its_callers_tools_up_front(repo, gateway, log):
    """agy searches the web in every run: its caller workdir says with_web."""
    server = make_agy(repo, workdirs={"repo": {"path": str(repo), "web": True,
                                               "gateway": {"caller": True, "with_web": True}}})
    result, _ = await call(server, "run_task", task="WRITE b.txt one", _agent=agent_with(["probe/probe_read"]))
    assert result["state"] == "done", result
    entry = json.loads(log()["mcp_config"])["mcpServers"][cli.GATEWAY_SERVER]
    assert entry["tools"] == {"probe_read": {"eager": True}}
    assert log()["task"] == cli.agy_note(["probe_read"], ["probe_read"]) + "WRITE b.txt one"


@pytest.mark.parametrize("pin, pinned", [
    (None, {"probe_*": {"text": "held"}}),
    ({"probe_read": {"text": "held"}}, {"probe_*": {"text": "held"}, "probe_read": {"text": "held"}}),
])
async def test_the_calling_runs_bindings_hold_its_tools_in_the_run_it_starts(repo, gateway, pin, pinned):
    """A bound agent that calls run_task itself: the CLI reaches its tools under the values its own calls are held
    to -- unbound, the run was a way around them. A pin of the call only adds to them."""
    server = make_server(repo, workdirs=caller_workdir(repo), wait_s=0.2)
    reset = current_tool_bindings.set({"probe_*": {"text": "held"}})
    try:
        started, _ = await call(server, "run_task", task="SLEEP 30", _agent=agent_with(["probe/*"]),
                                **({"pin": pin} if pin else {}))
        try:
            assert (await grant_of(server, started["run_id"])).pinned == pinned
        finally:
            await call(server, "cancel_run", run_id=started["run_id"])
        refused, _ = await call(server, "run_task", task="x", _agent=agent_with(["probe/*"]),
                                pin={"probe_*": {"text": "other"}})
        assert refused["error"].startswith("pin: tool_bindings['probe_*']['text']: this run holds it"), refused
    finally:
        current_tool_bindings.reset(reset)


async def test_a_model_calls_bindings_are_dropped_where_the_workdir_hands_on_no_tool(repo, gateway, log):
    """llm_coding_cli sends its agent run's bindings as _pin: a workdir without a gateway gives the CLI no tool of
    the agent's, so they hold nothing -- as pin it refused every bound run. With a gateway they are the run's."""
    bindings = {"probe_*": {"text": "held"}}
    plain, _ = await call(make_server(repo), "run_task", task="x", _pin=bindings)
    assert plain["state"] == "done", plain
    refused, _ = await call(make_server(repo), "run_task", task="x", pin=bindings)
    assert "has no gateway" in refused["error"], "a caller that names a pin itself is still told"
    server = make_server(repo, workdirs=caller_workdir(repo), wait_s=0.2)
    started, _ = await call(server, "run_task", task="SLEEP 30", _agent=agent_with(["probe/*"]), _pin=bindings)
    try:
        assert (await grant_of(server, started["run_id"])).pinned == bindings
    finally:
        await call(server, "cancel_run", run_id=started["run_id"])
    # A gateway with tools of its own that requires values (not a caller's): they are what the run must bring
    fixed = make_server(repo, wait_s=0.2, workdirs={"repo": {"path": str(repo), "gateway": {
        "tools": ["probe_*"], "pin": {"probe_*": ["text"]}}}})
    started, _ = await call(fixed, "run_task", task="SLEEP 30", _pin=bindings)
    try:
        assert (await grant_of(fixed, started["run_id"])).pinned == bindings
    finally:
        await call(fixed, "cancel_run", run_id=started["run_id"])
    missing, _ = await call(fixed, "run_task", task="x")
    assert "must fix probe_*: text" in missing["error"], missing


async def test_a_run_resumed_under_other_bindings_is_refused_for_a_fresh_one(repo, gateway):
    """The conversation was held to other values: by its code the model call starts a fresh run under the new."""
    server = make_server(repo, workdirs=caller_workdir(repo))
    agent = agent_with(["probe/*"])
    first, _ = await call(server, "run_task", task="x", _agent=agent, _pin={"probe_*": {"text": "a"}})
    assert first["state"] == "done", first
    for changed in ({"_pin": {"probe_*": {"text": "b"}}}, {}):
        reset = current_tool_bindings.set({"probe_*": {"text": "b"}})   # the calling run's own, inherited
        try:
            again, _ = await call(server, "run_task", task="x", _agent=agent, resume=first["run_id"], **changed)
        finally:
            current_tool_bindings.reset(reset)
        assert again.get("code") == server_module.CODE_RESUME and "pin: a resume keeps" in again["error"], again


DEAD = {"pid": 2 ** 22 + 4711, "started": 1.0, "instance": "0dead0beef00"}


@pytest.mark.parametrize("case", ["the caller's process died", "the caller lives, the owner let the run go",
                                  "the caller lives, a later owner died", "it dies once the run is taken over"])
async def test_a_run_that_is_a_model_call_goes_with_the_process_that_made_the_call(repo, case):
    """The call that waited for its answer died with its process (a restart), its grant with it: left going, the
    run worked on without its tools up to its time limit. Judged by the process that made the call, not by who
    owns the run: a plugin stopped and started again, or another process that took the run over and ended, leaves
    it to the call that still waits; one that dies after the run was taken over stops it then."""
    first = make_server(repo, wait_s=0.2)
    started, _ = await call(first, "run_task", task="SLEEP 30", wake=False, _background=False)
    run_id = started["run_id"]
    await first.stop_plugin()
    record = first._load(run_id)
    assert record["with_caller"] == first._me and first._file(run_id, "answered").exists(), "fixture"

    def rewrite(**fields):
        first._save({**first._load(run_id), **fields})
        for released in (first._root() / "runs").glob(f"{run_id}.released-*"):
            released.unlink()

    if case == "the caller's process died":
        rewrite(with_caller=DEAD, owner=DEAD)
    elif case == "the caller lives, a later owner died":
        rewrite(owner=DEAD)
    other = make_server(repo)
    try:
        await other._sweep()
        if case == "it dies once the run is taken over":
            assert run_id in other._monitors, "fixture: the run was not taken over"
            # the watch reads the record it holds: the caller named there has ended meanwhile
            other._save({**other._load(run_id), "with_caller": DEAD})
            other._monitors[run_id].cancel()
            other._watch(other._load(run_id), None)
        if case in ("the caller's process died", "it dies once the run is taken over"):
            await gone(record["pid"])
            for _ in range(100):
                final = other._load(run_id)
                if final["state"] != "running":
                    break
                await asyncio.sleep(0.1)
            assert final["state"] == "cancelled" and "ended with its process" in final["note"], final
        else:
            assert run_id in other._monitors and alive(record["pid"], record["pid_started"])
    finally:
        await call(other, "cancel_run", run_id=run_id)
        await other.stop_plugin()


# ── a model and effort of the call's own ──


async def test_a_call_names_a_model_the_instance_lists(repo, log):
    server = make_server(repo, model="opus", models=["sonnet", "haiku"], effort="low")
    first, _ = await call(server, "run_task", task="x", model="sonnet", effort="high")
    argv = log()["argv"]
    assert (argv[argv.index("--model") + 1], argv[argv.index("--effort") + 1]) == ("sonnet", "high")
    # a resume without one goes on with it; one that names another switches
    await call(server, "run_task", task="x", resume=first["run_id"])
    argv = log()["argv"]
    assert (argv[argv.index("--model") + 1], argv[argv.index("--effort") + 1]) == ("sonnet", "high")
    await call(server, "run_task", task="x", resume=first["run_id"], model="opus")
    assert log()["argv"][log()["argv"].index("--model") + 1] == "opus"
    await call(server, "run_task", task="x")
    argv = log()["argv"]
    assert (argv[argv.index("--model") + 1], argv[argv.index("--effort") + 1]) == ("opus", "low")


@pytest.mark.parametrize("config, asked, said", [
    ({"model": "opus", "models": ["sonnet"]}, {"model": "haiku"}, "model: one of opus, sonnet"),
    ({"model": "opus"}, {"model": "sonnet"}, "model: this instance runs opus only"),
    ({"model": "opus"}, {"effort": "extreme"}, "effort: one of low, medium, high, xhigh, max"),
])
async def test_a_model_the_instance_does_not_list_is_refused(repo, data_root, config, asked, said):
    result, _ = await call(make_server(repo, **config), "run_task", task="x", **asked)
    assert result["error"].startswith(said), result
    assert not (data_root / "worktrees").exists()


async def test_an_agy_model_that_names_its_level_takes_no_effort_beside_it(repo, log):
    server = make_agy(repo, model="gemini-3.8-flash", models=["gemini-4-argon-high"])
    await call(server, "run_task", task="x", model="gemini-4-argon-high", effort="low")
    argv = log()["argv"]
    assert argv[argv.index("--model") + 1] == "gemini-4-argon-high" and "--effort" not in argv
    await call(server, "run_task", task="x", effort="low")
    argv = log()["argv"]
    assert (argv[argv.index("--model") + 1], argv[argv.index("--effort") + 1]) == ("gemini-3.8-flash", "low")


def test_the_schema_offers_a_model_only_where_the_instance_lists_some(repo):
    assert make_server(repo, model="opus").get_template_vars()["models"] == []
    assert make_server(repo, model="opus", models=["sonnet"]).get_template_vars()["models"] == ["opus", "sonnet"]


# ── a refusal says what it is ──


async def test_a_refusal_carries_the_code_a_caller_decides_by(repo, monkeypatch):
    server = make_server(repo, max_task_chars=1_500)
    too_long, _ = await call(server, "run_task", task="x" * 1_501)
    assert too_long["code"] == server_module.CODE_TASK
    assert too_long["error"] == "task: 1501 characters, at most 1500 (coding_cli max_task_chars)"
    unknown, _ = await call(server, "run_task", task="x", resume="0123456789ab")
    assert unknown["code"] == server_module.CODE_RESUME and unknown["error"].startswith("resume:")
    plain, _ = await call(server, "run_task", task="x", mode="fast")
    assert "code" not in plain, "a refusal no caller decides by needs none"

    async def busy():
        return [{"run_id": "abc", "instance_name": server.name, "owner": server._me}]
    monkeypatch.setattr(server, "_running", busy)
    monkeypatch.setattr(server, "_mine", lambda record: True)
    full, _ = await call(server, "run_task", task="x")
    assert full["code"] == server_module.CODE_BUSY and "limit" not in full


async def test_a_resume_its_worktree_refuses_says_so_by_its_code(repo, tmp_path):
    """What _start finds in the run's own worktree (agy: a file it would run) is a resume refused -- a fresh run
    of the same task starts. A fresh run that meets it is not one: started fresh again, it would meet it again."""
    server = make_agy(repo)
    first, _ = await call(server, "run_task", task="x")
    hooks = Path(first["worktree"]) / cli.AGY_ROOTS[0]
    hooks.mkdir()
    (hooks / "hooks.json").write_text("{}", encoding="utf-8")
    refused, _ = await call(server, "run_task", task="x", resume=first["run_id"])
    assert refused["code"] == server_module.CODE_RESUME and "the worktree carries" in refused["error"], refused

    carrying = fresh_repo(tmp_path / "carrying")
    (carrying / cli.AGY_ROOTS[0]).mkdir()
    (carrying / cli.AGY_ROOTS[0] / "hooks.json").write_text("{}", encoding="utf-8")
    git(carrying, "add", ".")
    git(carrying, "commit", "-q", "-m", "hooks")
    fresh, _ = await call(make_agy(carrying, workdirs={"repo": {"path": str(carrying), "web": True}}),
                          "run_task", task="x")
    assert "the worktree carries" in fresh["error"] and "code" not in fresh, fresh


async def test_every_way_a_resume_is_refused_carries_the_code(repo, gateway):
    """llm_coding_cli starts a fresh run by the code alone: a path that loses it moves the agent's chain on
    instead."""
    def refused(answer, words):
        assert answer.get("code") == server_module.CODE_RESUME and words in answer["error"], answer

    server, patient = make_server(repo, wait_s=0.2, max_parallel=3), make_server(repo, max_parallel=3)
    going, _ = await call(server, "run_task", task="SLEEP 30")
    try:
        refused((await call(server, "run_task", task="x", resume=going["run_id"]))[0], "has not ended yet")
    finally:
        await call(server, "cancel_run", run_id=going["run_id"])
    first, _ = await call(patient, "run_task", task="x")
    assert first["state"] == "done", first
    busy, _ = await call(server, "run_task", task="SLEEP 30", resume=first["run_id"])
    assert busy["state"] == "running", busy
    try:
        refused((await call(server, "run_task", task="x", resume=first["run_id"]))[0],
                "another run works in that worktree")
    finally:
        await call(server, "cancel_run", run_id=busy["run_id"])
    other = make_agy(repo)
    other.name = server.name        # the same instance, its cli changed since the run
    refused((await call(other, "run_task", task="x", resume=first["run_id"]))[0], "this instance runs antigravity")

    pinned = make_server(repo, workdirs={"repo": {"path": str(repo), "gateway": {
        "tools": ["probe_*"], "pin": {"probe_*": ["text"]}}}})
    held, _ = await call(pinned, "run_task", task="x", pin={"probe_*": {"text": "a"}})
    assert held["state"] == "done", held
    refused((await call(pinned, "run_task", task="x", resume=held["run_id"], pin={"probe_*": {"text": "b"}}))[0],
            "pin: a resume keeps")


async def test_the_budget_and_an_agy_task_too_long_carry_their_codes(repo, monkeypatch):
    server = make_server(repo)
    state = {"unifiedWindows": {"five_hour": {"utilization": 0.95, "resetsAt": time.time() + 3600}}}

    async def quota(threshold, user_id, ask_when_old=False):
        return state
    monkeypatch.setattr(server, "_quota_now", quota)
    blocked, _ = await call(server, "run_task", task="x")
    assert blocked["code"] == server_module.CODE_LIMIT and blocked["limit"]["window"] == "five_hour", blocked
    long, _ = await call(make_agy(repo, max_task_tokens=1_000), "run_task", task="word " * 3_000)
    assert long["code"] == server_module.CODE_TASK and "agy takes at most 1000" in long["error"], long
