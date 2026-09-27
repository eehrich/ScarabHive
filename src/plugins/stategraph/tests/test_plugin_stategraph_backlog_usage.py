"""Regressions for the backlog of the stategraph review of 2026-09-27 (docs/backlog.md), phase 3: machines for users
and agents -- runs to find again, answers that say what comes next, a slash command with params, the tools a
machine may call, a wait state answered in the conversation, and a machine offered as an agent.

The real tools of a StateGraphServer on tmp_path (call activities, no LLM) and the real facade (MachineAgent).
"""

from __future__ import annotations

import asyncio

import pytest

from agent_system.config.models import AgentSystemConfig
from plugins.stategraph.server import StateGraphServer
from plugins.stategraph.tests.stategraph_testkit import FASTAPI_PY314, until
from plugins.stategraph.tests.test_plugin_stategraph_facade import Env, final_of
from plugins.stategraph.tests.test_plugin_stategraph_server import run_tool, server  # noqa: F401 -- a fixture

pytestmark = pytest.mark.filterwarnings(FASTAPI_PY314)


# ------------------------------------------------------------------ N1: runs to find again

async def test_list_runs_shows_a_user_their_own_runs_filtered_by_machine_and_status(server, monkeypatch):
    monkeypatch.setattr(StateGraphServer, "_is_admin", staticmethod(lambda user_id: False))
    server.system_config = AgentSystemConfig(auth={"enabled": True})
    for user, machine in (("ops_ann", "hello"), ("ops_bob", "hello"), ("ops_ann", "approval")):
        await run_tool(server, "stategraph_run_machine", {"machine_id": machine, "wait": "background",
                                                          "_user_id": user})
    await until(lambda: sorted(r["status"] for r in server.run_store.list_runs()) == ["succeeded", "succeeded", "waiting"],
                what="three runs, the approval waiting")

    mine, closing = await run_tool(server, "stategraph_list_runs", {"_user_id": "ops_ann"})
    hello, _ = await run_tool(server, "stategraph_list_runs", {"_user_id": "ops_ann", "machine_id": "hello"})
    waiting, _ = await run_tool(server, "stategraph_list_runs", {"_user_id": "ops_ann", "status": "waiting"})

    assert sorted(r["machine_id"] for r in mine["runs"]) == ["approval", "hello"], mine
    assert [r["machine_id"] for r in hello["runs"]] == ["hello"]
    assert [r["machine_id"] for r in waiting["runs"]] == ["approval"]
    assert closing.message == "2 run(s)"
    wrong, _ = await run_tool(server, "stategraph_list_runs", {"status": "done"})
    assert (wrong["status"], wrong.get("error_type")) == ("error", "http_422"), wrong


# ------------------------------------------------------------------ N2: waiting for a run, and what comes next

SLOW = """\
stategraph: 1
id: slow
python: slow.py
initial: nap
states:
  nap:
    do: {call: nap, args: {}}
    transitions: [{target: done}]
  done: {type: final, output: rested}
"""


async def test_get_run_can_wait_for_the_end_and_every_answer_says_what_comes_next(server, tmp_path):
    (tmp_path / "machines" / "slow.yaml").write_text(SLOW, encoding="utf-8")
    (tmp_path / "machines" / "slow.py").write_text("import time\n\n\ndef nap():\n    time.sleep(0.5)\n    return 1\n",
                                                   encoding="utf-8")
    started, _ = await run_tool(server, "stategraph_run_machine", {"machine_id": "slow", "wait": "background"})
    assert started["run_status"] == "running" and "wait='finish'" in started["next"], started

    ended, _ = await run_tool(server, "stategraph_get_run", {"run_id": started["run_id"], "wait": "finish"})

    assert (ended["run_status"], ended["output"], "next" in ended) == ("succeeded", "rested", False), ended
    waiting, _ = await run_tool(server, "stategraph_run_machine", {"machine_id": "approval"})
    assert waiting["run_status"] == "waiting" and "stategraph_send_event" in waiting["next"], waiting


def slow_machine(tmp_path, seconds: float) -> None:
    (tmp_path / "machines" / "slow.yaml").write_text(SLOW, encoding="utf-8")
    (tmp_path / "machines" / "slow.py").write_text(f"import time\n\n\ndef nap():\n    time.sleep({seconds})\n    return 1\n",
                                                   encoding="utf-8")


async def test_a_reader_that_stops_waiting_leaves_the_run_running(server, tmp_path, monkeypatch):
    monkeypatch.setattr(StateGraphServer, "_is_admin", staticmethod(lambda user_id: False))
    server.system_config = AgentSystemConfig(auth={"enabled": True})
    slow_machine(tmp_path, 1.5)
    run_id = (await server.service.start_run("slow", params={}, user_id=None))["run_id"]  # nobody's: every user sees it

    class Token:
        is_cancelled = False

    token = Token()
    reading = asyncio.ensure_future(server.get_run({"run_id": run_id, "wait": "finish", "_user_id": "ops_bob",
                                                    "_cancellation_token": token}))
    await asyncio.sleep(0.3)
    token.is_cancelled = True
    answer = await asyncio.wait_for(reading, 10)

    assert answer["run_status"] == "running", answer
    await until(lambda: server.run_store.get_run(run_id)["status"] == "succeeded", what="the run ends by itself")


async def test_get_run_waits_for_a_run_another_process_holds(server, tmp_path):
    slow_machine(tmp_path, 0.8)
    run_id = (await server.service.start_run("slow", params={}, user_id=None))["run_id"]
    held = server.run_manager.live.pop(run_id)  # to this process, the run is another's
    try:
        answer, _ = await run_tool(server, "stategraph_get_run", {"run_id": run_id, "wait": "finish"})
    finally:
        server.run_manager.live[run_id] = held

    assert answer["run_status"] == "succeeded", answer


# ------------------------------------------------------------------ N4: /stategraph-run <id> {params}

@pytest.mark.parametrize("request_line,greeting", [
    ('hello {"name": "Ann"}', "Hello, Ann!"), ("hello name=Bob", "Hello, Bob!"), ("hello", "Hello, world!"),
])
async def test_run_machine_takes_the_slash_command_s_line(server, request_line, greeting):
    result, _ = await run_tool(server, "stategraph_run_machine", {"request": request_line})

    assert result["output"] == {"greeting": greeting}, result


@pytest.mark.parametrize("request_line,says", [('hello {"name": ', "no JSON object"), ("hello Bob", "key=value")])
async def test_a_slash_command_line_that_does_not_parse_says_how(server, request_line, says):
    result, _ = await run_tool(server, "stategraph_run_machine", {"request": request_line})

    assert (result["status"], says in result.get("error", "")) == ("error", True), result


def test_the_slash_commands_bind_what_they_are_given():
    import yaml
    from pathlib import Path

    schema = yaml.safe_load((Path(__file__).parents[1] / "schema.yaml").read_text(encoding="utf-8"))
    commands = {c["name"]: c for c in schema["commands"]}

    assert (commands["stategraph-run"]["argument"], commands["stategraph-runs"]["tool"],
            commands["stategraph-stop"]["params"]) == ("request", "{{ name }}_list_runs", {"action": "terminate"})


# ------------------------------------------------------------------ N5: the tools a machine may call

async def test_the_catalog_lists_the_runner_s_tools_with_their_parameters(server, monkeypatch):
    from agent_system.config.models import AgentConfig, ToolServerConfig
    from agent_system.plugins import tool_adapter
    from agent_system.tools.base import ToolDef

    class Adapter:
        plugin_server = object()

        async def list_tools(self):
            return [ToolDef("store_put", "Store a value", {"properties": {"key": {"type": "string", "description": "k"},
                                                                          "value": {"type": "object"}},
                                                           "required": ["key"]}),
                    ToolDef("store_drop", "Drop everything", {"properties": {}})]

    class Registry:
        def list_servers(self):
            return ["store"]

        def get_server(self, name):
            return Adapter()

    monkeypatch.setattr(tool_adapter, "plugin_tool_registry", Registry())
    server.system_config = AgentSystemConfig(plugins={"servers": {"stategraph_runner": ToolServerConfig(
        type="basic_agent", enabled=True, agent_config=AgentConfig(llm_profile="normal", tools={
            "allowed": ["store/*"], "blocked": ["store/store_drop"]}))}})

    result, _ = await run_tool(server, "stategraph_catalog", {})

    assert result["tools"] == [{"name": "store_put", "description": "Store a value", "parameters": {
        "key": {"type": "string", "description": "k"}, "value": {"type": "object"}}, "required": ["key"]}], result["tools"]


# ------------------------------------------------------------------ N6: a wait state answered in the conversation

REVIEW = """\
stategraph: 1
id: m
params: {task: {type: string, required: true}}
events:
  approve: {description: the draft is fine}
  reject: {description: send it back, data: {type: object, properties: {why: {type: string}}}}
context: {why: null}
initial: review
states:
  review:
    description: Is the draft fine?
    transitions:
      - trigger: approve
        target: done
      - trigger: reject
        target: rejected
        effect: ctx.why = event.data["why"]
  done: {type: final, output: {verdict: approved}}
  rejected: {type: final, output: {verdict: rejected, why: "{{ ctx.why }}"}}
"""


@pytest.fixture
async def asking(tmp_path):
    made = Env(tmp_path, on_wait="ask")
    (tmp_path / "machines" / "m.yaml").write_text(REVIEW, encoding="utf-8")
    yield made
    await made.close()


async def test_a_waiting_run_asks_in_the_conversation_and_the_reply_is_its_event(asking):
    question = final_of(await asking.ask("the draft", request_id="r1", session_id="s1"))
    assert question["type"] == "final" and question["waiting"] == {"states": ["review"], "events": ["approve", "reject"]}
    for words in ("'review'", "Is the draft fine?", "- approve: the draft is fine", "- reject: send it back (data:"):
        assert words in question["summary"], question["summary"]

    unclear = final_of(await asking.ask("maybe", request_id="r2", session_id="s1"))
    assert unclear["summary"].startswith("Not sent: 'maybe' is none of the events"), unclear["summary"]

    answer = final_of(await asking.ask('{"event": "reject", "data": {"why": "too long"}}', request_id="r3",
                                       session_id="s1"))
    assert answer["summary"] == '{"verdict": "rejected", "why": "too long"}', answer
    assert len(asking.runs()) == 1


async def test_a_bare_event_name_answers_it_too(asking):
    await asking.ask("the draft", request_id="r1", session_id="s1")

    answer = final_of(await asking.ask("Approve", request_id="r2", session_id="s1"))

    assert answer["summary"] == '{"verdict": "approved"}', answer


async def test_without_on_wait_ask_the_request_waits_for_the_run_to_end(tmp_path):
    env = Env(tmp_path)
    (tmp_path / "machines" / "m.yaml").write_text(REVIEW, encoding="utf-8")
    try:
        asked = asyncio.ensure_future(env.ask("the draft", request_id="r1", session_id="s1"))
        await until(lambda: any(r["status"] == "waiting" for r in env.runs()), what="the wait")
        await asyncio.sleep(1.2)  # a tick of the facade's loop: it must not answer while the run waits
        assert not asked.done(), "a blocking facade answered while its run waits"
        run_id = env.runs()[0]["id"]
        env.server.service.send_event(run_id, "approve")
        assert final_of(await asked)["summary"] == '{"verdict": "approved"}'
    finally:
        await env.close()


# ------------------------------------------------------------------ N8: what a machine takes

async def test_a_request_that_does_not_fit_the_params_is_told_what_the_machine_takes(tmp_path):
    env = Env(tmp_path, input="json")
    try:
        answer = final_of(await env.ask("not json", request_id="r1", session_id="s1"))
        assert answer["type"] == "error" and "it takes: task (string, required)" in answer["message"], answer
        missing = final_of(await env.ask("{}", request_id="r2", session_id="s2"))
        assert "it takes: task (string, required)" in missing["message"], missing
    finally:
        await env.close()


# ------------------------------------------------------------------ N7: the machine agents of a machine

async def test_get_machine_names_the_agents_that_run_it_and_what_keeps_one_from_running(env_with_agents):
    env = env_with_agents
    agents = {a["name"]: a for a in env.server.service.get_machine("m")["agents"]}

    assert sorted(agents) == ["asker", "broken"], agents
    assert (agents["asker"]["visibility"], agents["asker"]["on_wait"], agents["asker"]["problems"]) == ("tool", "ask", [])
    assert agents["broken"]["problems"] == ["task_param 'request' is no param of m (it has: task)",
                                            "m requires task: neither in params nor the task_param"]


@pytest.fixture
async def env_with_agents(tmp_path):
    made = Env(tmp_path)

    def agent(**keys):
        return {"type": "stategraph_machine", "enabled": True, "agent_config": {"llm_profile": "normal"}, **keys}

    made.server.system_config = AgentSystemConfig(plugins={"servers": {
        "asker": agent(machine="m", on_wait="ask", metadata={"visibility": "tool"}),
        "broken": agent(machine="m", task_param="request"),
        "other": agent(machine="elsewhere"),
    }})
    yield made
    await made.close()

