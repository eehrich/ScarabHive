"""The model's stand-in (tests/agent_stand_in.py) the browser tests run on: it answers from its script -- by what the
user wrote, by name for the next run, else the default --, streams its answer, ends a wait on a cancel, and stands
in for every client the registry builds, so a real agent runs on it."""
from __future__ import annotations

import asyncio

import pytest

from agent_stand_in import Answer, ModelStandIn, stand_in_for_model

SCRIPT = {"default": {"text": "Done."},
          "answers": {"greeting": {"when": "(?i)hello", "text": "Hi there!", "chunks": 3},
                      "slow": {"when": "(?i)slow", "text": "Finally.", "delay": 30},
                      "down": {"when": "(?i)down", "error": "not reachable"}}}


class Token:
    is_cancelled = False


def stream(client, user, token=None):
    async def collect():
        return [chunk async for chunk in client.chat_tools_streaming([{"role": "user", "content": user}], [],
                                                                     cancellation_token=token)]
    return asyncio.run(collect())


def test_an_answer_is_chosen_by_what_the_user_wrote_by_name_or_by_default():
    stand_in = ModelStandIn.from_data(SCRIPT)
    assert stand_in.choose("Hello you").name == "greeting" and stand_in.choose("anything").name == "default"
    stand_in.use("slow", times=2)
    assert [stand_in.choose("hello").name for _ in range(3)] == ["slow", "slow", "greeting"]
    stand_in.when("(?i)hello", text="Mine.")
    assert stand_in.choose("hello").text == "Mine.", "a test's own answer comes before the script's"
    stand_in.reset()
    assert stand_in.choose("hello").name == "greeting"
    with pytest.raises(KeyError, match="no answer 'nope'"):
        stand_in.use("nope")
    with pytest.raises(ValueError, match="unknown fields"):
        ModelStandIn.from_data({"answers": {"x": {"txt": "typo"}}})


def test_it_streams_its_answer_and_records_the_call():
    stand_in = ModelStandIn.from_data(SCRIPT)
    chunks = stream(stand_in.client(), "hello")
    assert [c["type"] for c in chunks] == ["content_delta"] * 3 + ["final"]
    assert chunks[-2]["accumulated"] == "Hi there!" and chunks[-1]["assistant"]["content"] == "Hi there!"
    assert stand_in.calls == [{"user": "hello", "answer": "greeting", "model": "stand-in", "tools": 0, "messages": 1}]
    with pytest.raises(RuntimeError, match="not reachable"):
        stream(stand_in.client(), "the model is down")


def test_a_cancel_ends_a_waiting_answer(monkeypatch):
    import agent_stand_in

    monkeypatch.setattr(agent_stand_in, "CANCEL_POLL", 0.01)
    stand_in = ModelStandIn(answers={"slow": Answer(name="slow", when="slow", text="late", delay=30)})
    token = Token()
    token.is_cancelled = True
    assert stream(stand_in.client(), "slow", token) == [], "nothing is answered after the cancel"


async def test_a_real_agent_runs_on_it(monkeypatch):
    from agent_system.config.models import AgentConfig, AgentSystemConfig, ToolServerConfig
    from agent_system.servers.agent.server import Agent
    from agent_system.tools.base import ToolServerRegistry
    from agent_system.llm import registry

    stand_in = stand_in_for_model(monkeypatch, ModelStandIn.from_data(SCRIPT))
    client = registry.build_client(type("Cfg", (), {"provider": "openai", "model": "gpt-x",
                                                     "rate_limit_retry_seconds": 0.0})())
    assert client.stand_in is stand_in and client.model == "gpt-x"
    server_config = ToolServerConfig(type="agent", enabled=True, agent_config=AgentConfig(max_steps=3))
    agent = Agent("stand_in_agent", AgentSystemConfig(), server_config, ToolServerRegistry())
    agent.llm = stand_in.client()
    events = [event async for event in agent.run_events("hello agent", session_id="s-stand-in")]
    finals = [event for event in events if event.get("type") == "final"]
    assert finals and "Hi there!" in str(finals[-1]) and stand_in.calls[-1]["user"] == "hello agent"
