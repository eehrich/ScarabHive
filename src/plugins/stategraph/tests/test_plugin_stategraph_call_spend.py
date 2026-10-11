"""What the LLM calls inside a call activity cost: counted into the activity, through the real hook dispatch.

The writer's book end runs its phases as call functions over the v4 pipeline: their LLM calls reach no run's
events, so without this a run's spend -- and the panel's cost per state -- would leave out everything they cost.
"""
from __future__ import annotations

import pytest

from agent_system.hooks import HookType, get_hook_registry
from agent_system.plugins.discovery import register_plugin_hooks
from plugins.stategraph.tests.test_plugin_stategraph_server import server  # noqa: F401 -- a fixture

ASKS = """\
stategraph: 1
id: asks
python: asks.py
initial: ask
states:
  ask:
    do: {call: ask, args: {}}
    transitions: [{target: done}]
  done: {type: final}
"""

ASKS_PY = """\
import asyncio
from agent_system.llm.hook_notify import notify_response


async def one(cost):
    await notify_response(provider="openai", model="gpt-4o-mini", url="https://x", usage={
        "prompt_tokens": 1000, "completion_tokens": 200, "total_tokens": 1200, "cost": cost})


async def ask(sg):
    await one(0.25)
    await asyncio.gather(one(0.5), one(0.125))   # tasks it starts count too
    await notify_response(provider="openai", model="gpt-4o-mini", url="https://x", error="retry")  # no usage
"""


@pytest.fixture
async def hooked(server):  # noqa: F811
    registry = get_hook_registry()
    names = await register_plugin_hooks("stategraph", server.hooks_plugin, metadata=server.get_schema_data(),
                                        registry=registry)
    assert names, "the hook was not registered from schema.yaml"
    yield server
    for name in names:
        await registry.unregister_hook(HookType.POST_LLM_RESPONSE, name)


async def test_the_llm_calls_of_a_call_activity_are_its_cost(hooked, tmp_path):
    (tmp_path / "machines" / "asks.yaml").write_text(ASKS, encoding="utf-8")
    (tmp_path / "machines" / "asks.py").write_text(ASKS_PY, encoding="utf-8")
    row = await hooked.service.run_to_end("asks", {})
    assert row["status"] == "succeeded", row.get("error")
    spend = hooked.service.get_run(row["id"])["spend"]
    assert spend["cost"] == 0.875 and spend["activities"] == 1 and "by_state" not in spend, spend
    assert spend["tokens"] == {"prompt": 3000, "completion": 600, "cached": 0}


PHASED = """\
stategraph: 1
id: phased
python: phased.py
imports: {sub: ./phased_sub.yaml}
initial: first
states:
  first:
    do: {call: spend, args: {cost: 0.25}}
    transitions: [{target: phase}]
  phase:
    do: {machine: sub, params: {}}
    transitions: [{target: done}]
  done: {type: final}
"""

PHASED_SUB = """\
stategraph: 1
id: phased_sub
python: phased.py
initial: a
states:
  a:
    do: {call: spend, args: {cost: 0.5}}
    transitions: [{target: b}]
  b:
    do:
      parallel:
        x: {call: spend, args: {cost: 0.0625}}
        y: {call: spend, args: {cost: null}}
    transitions: [{target: done}]
  done: {type: final}
"""

PHASED_PY = """\
from agent_system.llm.hook_notify import notify_response


async def spend(cost):
    usage = {"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12}
    if cost is not None:  # billed; else estimated from the price table (deepseek-chat is in it)
        usage["cost"] = cost
    await notify_response(provider="openai", model="gpt-4o-mini" if cost is not None else "deepseek-chat",
                          url="https://x", usage=usage)
"""


async def test_a_submachine_state_holds_what_its_machine_cost(hooked, tmp_path):
    """The cost of a phase: a state that runs a submachine sums the states below it."""
    for name, text in (("phased.yaml", PHASED), ("phased_sub.yaml", PHASED_SUB), ("phased.py", PHASED_PY)):
        (tmp_path / "machines" / name).write_text(text, encoding="utf-8")
    row = await hooked.service.run_to_end("phased", {})
    assert row["status"] == "succeeded", row.get("error")
    spend = hooked.service.get_run(row["id"])["spend"]
    assert [(s["path"], s["cost"], s["activities"], s.get("cost_is_estimate", False)) for s in spend["by_state"]] == [
        ("first", 0.25, 1, False), ("phase", 0.562504, 3, True), ("phase/a", 0.5, 1, False),
        ("phase/b", 0.062504, 2, True)], "a parallel's branches count to their state, an estimate is marked"



async def test_a_second_stategraph_instance_does_not_count_a_call_again(hooked, tmp_path):
    """Each instance registers the hook and gets its own copy of the call's context: one call counts once."""
    from agent_system.config.models import AgentSystemConfig
    from plugins.stategraph.server import StateGraphServer
    from plugins.stategraph.tests.stategraph_testkit import tool_config

    (tmp_path / "two" / "machines").mkdir(parents=True)
    other = StateGraphServer("stategraph_two", AgentSystemConfig(), tool_config(tmp_path / "two"))
    registry = get_hook_registry()
    names = await register_plugin_hooks("stategraph_two", other.hooks_plugin, metadata=other.get_schema_data(),
                                        registry=registry)
    try:
        (tmp_path / "machines" / "asks.yaml").write_text(ASKS, encoding="utf-8")
        (tmp_path / "machines" / "asks.py").write_text(ASKS_PY, encoding="utf-8")
        row = await hooked.service.run_to_end("asks", {})
        assert hooked.service.get_run(row["id"])["spend"]["cost"] == 0.875
    finally:
        for name in names:
            await registry.unregister_hook(HookType.POST_LLM_RESPONSE, name)
        await other.stop_plugin()


def test_a_read_only_store_reads_the_spend_and_writes_nothing(tmp_path):
    # a report on a server's runs.db (a report): it must never migrate, write or create the file
    import sqlite3

    from plugins.stategraph.engine.journal import RunStore

    store = RunStore(tmp_path / "runs.db")
    store.create_run("r", "m", {}, status="succeeded")
    for key, state in (("s1", "a"), ("s2", "b")):
        store.record("r", "activity", key, state=state, status="done",
                     data={"kind": "call", "path": state, "state": state, "meta": {"cost": 0.5}})
    store.close()
    reader = RunStore.read_only(tmp_path / "runs.db")
    try:
        assert reader.spend("r")["cost"] == 1.0
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            reader.update_run("r", status="failed")
    finally:
        reader.close()
    with pytest.raises(sqlite3.OperationalError):
        RunStore.read_only(tmp_path / "elsewhere" / "runs.db")
    assert not (tmp_path / "elsewhere").exists()


def test_a_row_a_resume_set_aside_costs_in_the_state_it_ran_in(tmp_path):
    # a resumable failure's rows move to rewound.<n>/<key> (runner.REWOUND): their cost still counts, in their state
    from plugins.stategraph.engine.journal import RunStore
    from plugins.stategraph.engine.runner import REWOUND

    store = RunStore(tmp_path / "runs.db")
    try:
        store.create_run("r", "m", {}, status="succeeded")
        store.add_frame("r", "s1/m/", "sub", "phase")
        for key, state, cost in (("s0", "first", 0.5), (f"{REWOUND}1/s1/m/s0", "inner", 0.25), ("s1/m/s0", "inner", 1.0)):
            store.record("r", "activity", key, state=state, status="done",
                         data={"kind": "call", "path": state, "state": state, "meta": {"cost": cost}})
        by_state = {s["path"]: s["cost"] for s in store.spend("r")["by_state"]}
        assert by_state == {"first": 0.5, "phase": 1.25, "phase/inner": 1.25}
    finally:
        store.close()
