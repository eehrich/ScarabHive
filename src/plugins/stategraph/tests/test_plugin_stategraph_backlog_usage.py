"""Regressions for the backlog of the stategraph review of 2026-09-27 (docs/backlog.md), phase 3: machines for users
and agents -- runs to find again, answers that say what comes next, a slash command with params, the tools a
machine may call, a wait state answered in the conversation, and a machine offered as an agent.

The real tools of a StateGraphServer on tmp_path (call activities, no LLM) and the real facade (MachineAgent).
"""

from __future__ import annotations

import asyncio
import json

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


COUNT = """\
stategraph: 1
id: count
params: {n: {type: integer, required: true}, label: {type: string}}
initial: go
states:
  go:
    transitions: [{target: done}]
  done: {type: final, output: {label: "{{ params.label }}"}}
"""


@pytest.mark.parametrize("words,label", [("label='two words'", "two words"), (r"label=C:\data\x.txt", r"C:\data\x.txt")])
async def test_key_value_words_read_as_the_types_the_machine_declares(server, tmp_path, words, label):
    (tmp_path / "machines" / "count.yaml").write_text(COUNT, encoding="utf-8")

    result, _ = await run_tool(server, "stategraph_run_machine", {"request": f"count n=3 {words}"})

    assert (result.get("run_status"), result.get("output")) == ("succeeded", {"label": label}), result
    assert server.run_store.get_run(result["run_id"])["params"] == {"n": 3, "label": label}


@pytest.mark.parametrize("request_line,says", [('hello {"name": ', "no JSON object"), ("hello Bob", "key=value")])
async def test_a_slash_command_line_that_does_not_parse_says_how(server, request_line, says):
    result, _ = await run_tool(server, "stategraph_run_machine", {"request": request_line})

    assert (result["status"], says in result.get("error", "")) == ("error", True), result


def test_the_slash_commands_bind_what_they_are_given():
    import yaml
    from pathlib import Path

    import plugins.stategraph.server as plugin  # the plugin under test: its schema, not this file's neighbour

    schema = yaml.safe_load((Path(plugin.__file__).parent / "schema.yaml").read_text(encoding="utf-8"))
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


@pytest.mark.parametrize("reply,data", [
    ('reject {"why": "too long: by far"}', {"why": "too long: by far"}),
    ('reject: {"why": "a: b"}', {"why": "a: b"}),
    ('reject\n{"why": "x"}', {"why": "x"}),
    ("Approve", None),
])
def test_a_reply_names_the_event_and_the_data_follows_it_whole(reply, data):
    from plugins.stategraph.facade import _event_of

    name, sent, frame = _event_of(reply, ["approve", "reject"])

    assert (name, sent, frame) == (reply.split()[0].rstrip(":").lower(), data, None)


@pytest.mark.parametrize("crashed", [False, True])
async def test_a_reply_after_a_restart_answers_the_wait_the_run_resumes_into(asking, crashed):
    await asking.ask("the draft", request_id="r1", session_id="s1")
    await asking.server.run_manager.shutdown()  # the process ends: the run is interrupted in its wait
    asking.server.run_manager._stopping = False
    if crashed:  # it died instead: its row still says waiting, and nobody holds it (the sweep has not come yet)
        asking.server.run_store.update_run(asking.runs()[0]["id"], status="waiting")
    assert asking.runs()[0]["status"] == ("waiting" if crashed else "interrupted")

    answer = final_of([event async for event in asking.fresh_agent(on_wait="ask").run_events(
        "approve", request_id="r2", session_id="s1")])

    assert answer["summary"] == '{"verdict": "approved"}', answer


async def test_as_another_agent_s_tool_a_waiting_run_blocks_instead_of_asking(asking):
    called = asyncio.ensure_future(asking.agent.call(asking.agent.name, {"task": "the draft", "request_id": "c1"}))
    await until(lambda: any(r["status"] == "waiting" for r in asking.runs()), what="the wait")
    await asyncio.sleep(1.2)  # a tick of the facade's loop: a question would have ended the call by now
    assert not called.done(), called.result()

    asking.server.service.send_event(asking.runs()[0]["id"], "approve")

    assert (await asyncio.wait_for(called, 10))["summary"] == '{"verdict": "approved"}'
    assert len(asking.runs()) == 1


async def test_the_answered_wait_is_not_asked_again_before_the_run_took_the_reply(asking, monkeypatch):
    await asking.ask("the draft", request_id="r1", session_id="s1")
    service, loop = asking.server.service, asyncio.get_running_loop()
    send = service.send_event

    def later(run_id, name, data=None, frame=None, *, user_id=None):  # taken now, dispatched in 1.5 s
        loop.call_later(1.5, lambda: send(run_id, name, data, frame, user_id=user_id))
        return {"accepted": True}

    monkeypatch.setattr(service, "send_event", later)
    answer = final_of(await asking.ask("approve", request_id="r2", session_id="s1"))

    assert answer["summary"] == '{"verdict": "approved"}', answer


GUARDED = """\
stategraph: 1
id: m
params: {task: {type: string, required: true}}
events:
  approve: {description: fine}
  note: {description: count}
context: {n: 0}
initial: review
states:
  review:
    transitions:
      - trigger: approve
        target: done
        guard: "ctx.n > 0"
      - trigger: note
        effect: ctx.n = ctx.n + 1
  done: {type: final, output: {verdict: approved}}
"""


async def test_a_reply_a_guard_discards_is_said_with_the_guards_it_met(asking, tmp_path):
    (tmp_path / "machines" / "m.yaml").write_text(GUARDED, encoding="utf-8")
    await asking.ask("the draft", request_id="r1", session_id="s1")

    again = final_of(await asking.ask("approve", request_id="r2", session_id="s1"))

    assert again["summary"].startswith("Not taken: 'approve' in 'review' -- no transition took it (guards: ctx.n > 0 "
                                       "-> False)."), again["summary"]
    assert "It takes:" in again["summary"]


SUB = """\
stategraph: 1
id: sub
params: {label: {type: string}}
events: {approve: {description: ok}}
initial: w
states:
  w:
    transitions: [{trigger: approve, target: ok}]
  ok: {type: final, output: "{{ params.label }}"}
"""
PARALLEL = """\
stategraph: 1
id: m
params: {task: {type: string, required: true}}
imports: {sub: ./sub.yaml}
initial: work
states:
  work:
    do:
      parallel:
        left: {machine: sub, params: {label: left}}
        right: {machine: sub, params: {label: right}}
    transitions: [{target: done}]
  done: {type: final, output: both}
"""


async def test_an_event_several_frames_take_is_answered_with_its_frame(asking, tmp_path):
    (tmp_path / "machines" / "m.yaml").write_text(PARALLEL, encoding="utf-8")
    (tmp_path / "machines" / "sub.yaml").write_text(SUB, encoding="utf-8")
    question = final_of(await asking.ask("go", request_id="r1", session_id="s1"))
    frames = [frame["prefix"] for frame in asking.server.run_store.get_run(question["run_id"])["view"]["frames"]
              if frame.get("accepts")]
    assert len(frames) == 2 and all(f"{prefix!r} in 'w'" in question["summary"] for prefix in frames), question
    assert '"frame": "<frame>"' in question["summary"]

    left = final_of(await asking.ask(json.dumps({"event": "approve", "frame": frames[0]}), request_id="r2",
                                     session_id="s1"))
    assert left["waiting"]["events"] == ["approve"] and "in frames" not in left["summary"], left
    done = final_of(await asking.ask("approve", request_id="r3", session_id="s1"))

    assert done["summary"] == '{"output": "both"}', done


async def test_the_question_does_not_run_the_machine_s_python(asking, tmp_path):
    marks = tmp_path / "imports.txt"
    (tmp_path / "machines" / "m.yaml").write_text(REVIEW.replace("id: m\n", "id: m\npython: m.py\n"), encoding="utf-8")
    (tmp_path / "machines" / "m.py").write_text(f"with open({str(marks)!r}, 'a') as f:\n    f.write('x')\n",
                                                encoding="utf-8")
    await asking.ask("the draft", request_id="r1", session_id="s1")
    before = marks.read_text()

    unclear = final_of(await asking.ask("maybe", request_id="r2", session_id="s1"))

    assert unclear["summary"].startswith("Not sent:") and marks.read_text() == before


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
        mistyped = final_of(await env.ask('{"task": 3}', request_id="r3", session_id="s3"))
        assert "it takes: task (string, required)" in mistyped["message"], mistyped
    finally:
        await env.close()


# ------------------------------------------------------------------ N7: the machine agents of a machine

async def test_get_machine_names_the_agents_that_run_it_and_what_keeps_one_from_running(env_with_agents):
    env = env_with_agents
    agents = {a["name"]: a for a in env.server.service.get_machine("m")["agents"]}

    assert sorted(agents) == ["asker", "broken", "odd"], agents
    assert (agents["asker"]["visibility"], agents["asker"]["on_wait"], agents["asker"]["problems"]) == ("tool", "ask", [])
    assert agents["odd"]["problems"] == ["on_wait must be ask or block, not 'sometimes'"]
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
        "odd": agent(machine="m", on_wait="sometimes"),
        "other": agent(machine="elsewhere"),
    }})
    yield made
    await made.close()

