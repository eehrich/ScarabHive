"""Deferred tools (tools.deferred): held back from the tool list until loaded.

Driven through a real Agent run -- discovery, schema build, the step loop and
tool execution are production code; only the LLM is scripted and the tool
server is a stub. What is measured is what the MODEL is sent and what the
SERVER runs, the two things the feature is about.
"""
import json

import pytest

from agent_system.config.models import (
    AgentConfig,
    AgentSystemConfig,
    LLMModelConfig,
    LLMProfile,
    LLMSystemConfig,
    ToolConfig,
    ToolServerConfig,
)
from agent_system.llm.models import ChatMessage
from agent_system.servers.agent.components.tool_execution import tool_message_never_ran
from agent_system.servers.agent.deferred_tools import MAX_RESULTS, TOOL_SEARCH, DeferredTools
from agent_system.servers.agent.server import Agent
from agent_system.tools.base import ToolServerRegistry


class _ToolDef:
    def __init__(self, name, description):
        self.name = name
        self.description = description
        self.input_schema = {"type": "object", "properties": {"text": {"type": "string"}}}


class _Server:
    """One plugin with an everyday tool and a rare one."""

    def __init__(self):
        self.ran = []

    async def list_tools(self):
        return [_ToolDef("kit_read", "Read a file. Returns its text."),
                _ToolDef("kit_deploy", "Deploy the build to the staging host. Takes a while.")]

    async def call(self, name, params):
        self.ran.append((name, params.get("text")))
        return {"ok": True}


def _call(n, name, args):
    return {"id": f"call_{n}", "type": "function",
            "function": {"name": name, "arguments": json.dumps(args)}}


class _ScriptedLLM:
    """Answers call n with script[n-1] (a list of tool calls, or text) and
    records the tool names each call was SENT."""

    model = "test/model"

    def __init__(self, script):
        self.script = script
        self.sent_tools = []

    def supports_streaming(self):
        return True

    async def chat_tools_streaming(self, messages, tools, cancellation_token=None, status_scope=None):
        self.sent_tools.append([t["function"]["name"] for t in tools or []])
        n = len(self.sent_tools)
        step = self.script[n - 1] if n <= len(self.script) else "done"
        if isinstance(step, str):
            answer = {"role": "assistant", "content": step}
        else:
            answer = {"role": "assistant", "content": None,
                      "tool_calls": [_call(f"{n}_{i}", name, args) for i, (name, args) in enumerate(step)]}
        yield {"type": "final", "assistant": answer}


def _agent(server, deferred):
    llm_system = LLMSystemConfig(
        models={"m": LLMModelConfig(provider="openai", model="m", api_key="k")},
        profiles={"normal": LLMProfile(model_ref="m")},
        default_profile="normal",
    )
    registry = ToolServerRegistry()
    registry.register("kit", server)
    agent_config = AgentConfig(max_steps=5, llm_profile="normal",
                               tools=ToolConfig(allowed=["kit/*"], deferred=deferred))
    return Agent("test_agent", AgentSystemConfig(llm_system=llm_system),
                 ToolServerConfig(type="agent", enabled=True, agent_config=agent_config), registry)


async def _run(agent, llm, session, task="go"):
    agent.llm = llm
    return [event async for event in agent.run_events(task, request_id=f"req_{session}_{len(llm.script)}",
                                                      session_id=session)]


@pytest.mark.asyncio
async def test_a_deferred_tool_is_sent_only_after_tool_search_loads_it():
    server = _Server()
    llm = _ScriptedLLM([[(TOOL_SEARCH, {"query": "select:kit_deploy"})],
                        [("kit_deploy", {"text": "v1"})],
                        "deployed"])
    await _run(_agent(server, ["kit/kit_deploy"]), llm, "s_search")

    first, second, _third = llm.sent_tools
    assert "kit_deploy" not in first and {"kit_read", TOOL_SEARCH} <= set(first)
    assert "kit_deploy" in second, "tool_search did not put the schema into the next call"
    assert server.ran == [("kit_deploy", "v1")]


@pytest.mark.asyncio
async def test_calling_an_unloaded_tool_does_not_run_it_but_loads_it():
    server = _Server()
    llm = _ScriptedLLM([[("kit_deploy", {"text": "guessed"}), ("kit_deploy", {"text": "guessed too"})],
                        [("kit_deploy", {"text": "v2"})],
                        "deployed"])
    await _run(_agent(server, ["kit/kit_deploy"]), llm, "s_blind")

    assert server.ran == [("kit_deploy", "v2")], "a call with guessed arguments was run"
    assert "kit_deploy" in llm.sent_tools[1]


@pytest.mark.asyncio
async def test_a_call_behind_the_tool_search_that_loads_it_does_not_run():
    server = _Server()
    llm = _ScriptedLLM([[(TOOL_SEARCH, {"query": "select:kit_deploy"}), ("kit_deploy", {"text": "guessed"})],
                        [("kit_deploy", {"text": "v3"})],
                        "deployed"])
    await _run(_agent(server, ["kit/kit_deploy"]), llm, "s_same_step")
    assert server.ran == [("kit_deploy", "v3")], "a call written without the schema was run"


def test_a_refused_unloaded_call_counts_as_a_call_that_never_ran():
    """The error streak (auto-escalation) skips it, as it skips a blocked call."""
    tools = _schemas("a_read", "b_deploy")
    deferred = DeferredTools.split(tools, {"a_read": "a", "b_deploy": "b"}, ["b/*"])
    answer = deferred.intercept("b_deploy", {}, 0, tools)
    message = ChatMessage(role="tool", tool_call_id="c1", name="b_deploy", content=json.dumps(answer))
    assert tool_message_never_ran(message)
    ran = ChatMessage(role="tool", tool_call_id="c2", name="a_read", content=json.dumps({"error": "no file"}))
    assert not tool_message_never_ran(ran)


@pytest.mark.asyncio
async def test_a_new_run_of_the_session_starts_with_what_the_history_loaded():
    server = _Server()
    agent = _agent(server, ["kit/kit_deploy"])
    await _run(agent, _ScriptedLLM([[(TOOL_SEARCH, {"query": "deploy"})], "loaded"]), "s_resume")

    second_run = _ScriptedLLM(["nothing to do"])
    await _run(agent, second_run, "s_resume", task="again")
    assert "kit_deploy" in second_run.sent_tools[0]


@pytest.mark.asyncio
async def test_without_deferred_patterns_every_schema_is_sent_and_no_tool_search():
    llm = _ScriptedLLM(["hi"])
    await _run(_agent(_Server(), []), llm, "s_plain")
    assert set(llm.sent_tools[0]) == {"kit_read", "kit_deploy"}


@pytest.mark.asyncio
async def test_context_counts_what_a_run_of_the_session_sends():
    server = _Server()
    agent = _agent(server, ["kit/kit_deploy"])
    _prompt, fresh = await agent.describe_context_inputs(None)
    assert "kit_deploy" not in [t["function"]["name"] for t in fresh]

    await _run(agent, _ScriptedLLM([[(TOOL_SEARCH, {"query": "select:kit_deploy"})], "ok"]), "s_ctx")
    _prompt, resumed = await agent.describe_context_inputs("s_ctx")
    assert "kit_deploy" in [t["function"]["name"] for t in resumed]

    # The API hands the stored messages over: another process's agent, whose
    # tracker never saw the session, counts the loaded tool as well.
    stored = [m.model_dump() for m in agent._session_tracker.get_session_messages("s_ctx")]
    elsewhere = _agent(_Server(), ["kit/kit_deploy"])
    _prompt, from_record = await elsewhere.describe_context_inputs("s_ctx", stored)
    assert "kit_deploy" in [t["function"]["name"] for t in from_record]


def _schemas(*names):
    return [{"type": "function", "function": {"name": n, "description": f"{n} does things. More.",
                                              "parameters": {}}} for n in names]


def test_keyword_search_ranks_the_name_above_the_description():
    # a_mention sorts first by name: only the score puts b_deploy ahead of it.
    tools = _schemas("a_read", "b_deploy", "a_mention")
    tools[2]["function"]["description"] = "Mentions deploy only here."
    deferred = DeferredTools.split(tools, {"a_read": "a", "b_deploy": "b", "a_mention": "c"},
                                   ["b/*", "c/*"])
    assert deferred.search("deploy") == ["b_deploy", "a_mention"]
    assert deferred.search("select:a_mention, nope") == ["a_mention"]
    assert deferred.search("zzz") == []


def test_keywords_match_the_server_name_and_the_pattern_form():
    tools = _schemas("a_read", "deploy")
    deferred = DeferredTools.split(tools, {"a_read": "a", "deploy": "kit"}, ["kit/*"])
    assert deferred.search("kit") == ["deploy"]
    assert deferred.search("kit/deploy") == ["deploy"]


def test_a_query_that_is_not_text_finds_nothing_and_breaks_nothing():
    tools = _schemas("a_read", "b_deploy")
    deferred = DeferredTools.split(tools, {"a_read": "a", "b_deploy": "b"}, ["b/*"])
    assert deferred.intercept(TOOL_SEARCH, {"query": 5}, 0, tools)["loaded"] == []


def test_the_index_line_is_capped():
    tools = _schemas("a_read", "b_deploy")
    tools[1]["function"]["description"] = "x" * 500
    deferred = DeferredTools.split(tools, {"a_read": "a", "b_deploy": "b"}, ["b/*"])
    line = next(l for l in deferred.search_schema()["function"]["description"].splitlines()
                if l.startswith("- b_deploy"))
    assert len(line) < 140


def test_a_deferred_pattern_that_matches_nothing_is_reported_per_agent(caplog):
    with caplog.at_level("WARNING"):
        for agent in ("agent_one", "agent_two"):
            DeferredTools.split(_schemas("a_read", "b_deploy"), {"a_read": "a", "b_deploy": "b"},
                                ["b/*", "typo_server_xyz/*"], agent)
    assert "Agent agent_one: tools.deferred pattern 'typo_server_xyz/*'" in caplog.text
    assert "Agent agent_two: tools.deferred pattern 'typo_server_xyz/*'" in caplog.text
    assert "'b/*'" not in caplog.text


@pytest.mark.asyncio
async def test_an_unloaded_call_with_broken_arguments_is_loaded_as_well():
    """Refused and loaded like any unloaded call: a later run's restore counts
    every call, so the run itself must have loaded it too."""
    server = _Server()

    class _BrokenArgsLLM(_ScriptedLLM):
        async def chat_tools_streaming(self, messages, tools, cancellation_token=None, status_scope=None):
            async for event in super().chat_tools_streaming(messages, tools, cancellation_token, status_scope):
                if len(self.sent_tools) == 1:
                    event["assistant"]["tool_calls"][0]["function"]["arguments"] = '{"text": "cut'
                yield event

    llm = _BrokenArgsLLM([[("kit_deploy", {})], "stop"])
    await _run(_agent(server, ["kit/kit_deploy"]), llm, "s_broken")
    assert "kit_deploy" in llm.sent_tools[1] and server.ran == []


def test_a_tool_search_with_broken_arguments_gets_the_parse_error():
    tools = _schemas("a_read", "b_deploy")
    deferred = DeferredTools.split(tools, {"a_read": "a", "b_deploy": "b"}, ["b/*"])
    assert deferred.intercept(TOOL_SEARCH, None, 0, tools) is None


def test_a_keyword_search_loads_a_bounded_number_of_tools():
    names = [f"x_tool{i}" for i in range(MAX_RESULTS + 2)]
    tools = _schemas(*names)
    deferred = DeferredTools.split(tools, dict.fromkeys(names, "x"), ["x/*"])
    assert len(deferred.search("tool")) == MAX_RESULTS


def test_a_real_tool_named_tool_search_turns_deferring_off():
    tools = _schemas("a_read", TOOL_SEARCH)
    assert DeferredTools.split(tools, {"a_read": "a", TOOL_SEARCH: "s"}, ["a/*"]) is None
    assert [t["function"]["name"] for t in tools] == ["a_read", TOOL_SEARCH]


def test_restore_reads_stored_message_dicts():
    tools = _schemas("a_read", "b_deploy", "b_undo")
    deferred = DeferredTools.split(tools, {"a_read": "a", "b_deploy": "b", "b_undo": "b"}, ["b/*"])
    history = [{"role": "tool", "name": TOOL_SEARCH,
                "content": json.dumps({"loaded": ["b_deploy", {"not": "a name"}]})},
               {"role": "assistant", "tool_calls": [{"function": {"name": "b_undo", "arguments": "{}"}}]}]
    assert deferred.restore(history, tools) == ["b_deploy", "b_undo"]
    assert [t["function"]["name"] for t in tools] == ["a_read", TOOL_SEARCH, "b_deploy", "b_undo"]


def test_restore_keeps_the_order_the_run_loaded_in():
    """[tool_search -> b_undo, b_deploy unloaded] loaded b_undo first: the
    rebuilt list must be the one the last run ended with."""
    tools = _schemas("a_read", "b_deploy", "b_undo")
    deferred = DeferredTools.split(tools, {"a_read": "a", "b_deploy": "b", "b_undo": "b"}, ["b/*"])
    history = [{"role": "assistant", "tool_calls": [
                   {"id": "s1", "function": {"name": TOOL_SEARCH, "arguments": "{}"}},
                   {"id": "c1", "function": {"name": "b_deploy", "arguments": "{}"}}]},
               {"role": "tool", "name": TOOL_SEARCH, "tool_call_id": "s1",
                "content": json.dumps({"loaded": ["b_undo"]})},
               {"role": "tool", "name": TOOL_SEARCH, "tool_call_id": "s0", "content": "not json"}]
    assert deferred.restore(history, tools) == ["b_undo", "b_deploy"]


def test_restore_keeps_answers_that_share_an_id_or_have_none():
    names = ("a_read", "b_one", "b_two", "b_three", "b_four")
    tools = _schemas(*names)
    deferred = DeferredTools.split(tools, {n: n[0] for n in names}, ["b/*"])

    def search_turn(loaded):  # a client that numbers the calls of each turn from 0
        return [{"role": "assistant", "tool_calls": [{"id": "call_0", "function": {"name": TOOL_SEARCH}}]},
                {"role": "tool", "name": TOOL_SEARCH, "tool_call_id": "call_0",
                 "content": json.dumps({"loaded": [loaded]})}]

    history = [{"role": "tool", "name": TOOL_SEARCH, "content": json.dumps({"loaded": ["b_one"]})},
               {"role": "tool", "name": TOOL_SEARCH, "content": json.dumps({"loaded": ["b_two"]})},
               *search_turn("b_three"), *search_turn("b_four"),
               {"role": "tool", "name": TOOL_SEARCH, "tool_call_id": "x", "content": json.dumps({"loaded": 5})}]
    assert deferred.restore(history, tools) == ["b_one", "b_two", "b_three", "b_four"]
