"""ask_user in a run a panel started (plugins/agent_tasks.py): nobody reads a chat row, but a person follows the run
in the panel through its run events. The question goes out as ``run.needs_input`` -- question, options, the answer
route -- the run's view carries it while it waits, the answer comes through the plugin's route as in the chat, and
``run.answered`` puts the run back to work. A run without a reporter (no panel follows it) is still told at once that
nobody can answer."""
from __future__ import annotations

import asyncio
from typing import Any, Dict

import pytest

from agent_system.run_events import RunEventBus, RunEventStore, RunReporter
from plugins.ask_user.tests.test_plugin_ask_user_tool import URL, _agent, _ask, _client, _last, _Model, _run, _server

pytestmark = pytest.mark.asyncio


@pytest.fixture
def store(tmp_path):
    return RunEventStore(tmp_path / "run_events.db")


async def _panel_run(store: RunEventStore, session_id: str) -> RunReporter:
    """The reporter launch() holds for a panel run working in ``session_id``."""
    reporter = RunReporter(session_id, "asking_agent", who="alice", title="Check the gateway", source="classifier",
                           store=store, bus=RunEventBus())
    await reporter.started()
    return reporter


async def _until(test, seconds: float = 5.0) -> Any:
    end = asyncio.get_running_loop().time() + seconds
    while asyncio.get_running_loop().time() < end:
        found = test()
        if found:
            return found
        await asyncio.sleep(0.02)
    raise AssertionError("did not happen in time")


def _types(store: RunEventStore, run_id: str) -> list[str]:
    return [event["type"] for event in store.events(run_id)]


async def test_the_question_waits_in_the_panel_and_the_answer_from_there_goes_back_to_the_model(store):
    server = _server(panel_ask_timeout=20)
    model = _Model([_ask("c1", "Is the gateway reachable from the internet?", ["Yes", "No", "I don't know"])])
    agent = _agent(server)
    agent.llm = model
    reporter = await _panel_run(store, "panel-s1")
    seen: Dict[str, Any] = {}

    async def person():
        run = await _until(lambda: (store.run("panel-s1") or {}).get("question") and store.run("panel-s1"))
        seen.update(run)
        question = run["question"]
        async with _client(server) as client:
            answer = await client.post(question["answer_url"], json={"question_id": question["question_id"],
                                                                     "choices": ["No"], "text": "only in the test network"})
        seen["status"] = answer.status_code

    events, _ = await asyncio.wait_for(asyncio.gather(_run(agent, "panelreq1", session_id="panel-s1"), person()), 15)

    assert seen["state"] == "needs_input" and seen["needs_you"] is True
    assert seen["question"]["question"] == "Is the gateway reachable from the internet?"
    assert seen["question"]["options"] == ["Yes", "No", "I don't know"] and seen["question"]["answer_url"] == URL
    assert seen["question"]["wait_s"] == 20 and seen["status"] == 200
    [result] = model.results()
    assert result == {"status": "success", "choices": ["No"], "text": "only in the test network"}
    assert _types(store, "panel-s1") == ["run.started", "run.needs_input", "run.answered"]
    answered = store.events("panel-s1")[-1]
    assert answered["state"] == "working" and answered["payload"]["outcome"] == "answered" and answered["needs_you"] is False
    run = store.run("panel-s1")
    assert run["state"] == "working" and "question" not in run and server.broker.pending() == []
    assert "answered by" in _last(events)["message"]
    assert not reporter.ended


async def test_unanswered_the_run_goes_on_and_the_model_decides_itself(store):
    server = _server(panel_ask_timeout=0.3)
    model = _Model([_ask("c1", "Which log level?", ["info", "debug"])])
    agent = _agent(server)
    agent.llm = model
    await _panel_run(store, "panel-s2")

    await asyncio.wait_for(_run(agent, "panelreq2", session_id="panel-s2"), 10)

    [result] = model.results()
    assert result["status"] == "error" and result["reason"] == "timeout"
    assert "0.3 seconds" in result["error"] and "decide yourself" in result["error"]
    assert _types(store, "panel-s2")[-2:] == ["run.needs_input", "run.answered"]
    assert store.events("panel-s2")[-1]["payload"] == {"outcome": "timeout"}
    assert store.run("panel-s2")["state"] == "working" and server.broker.pending() == []


async def test_a_run_stopped_while_it_waits_ends_the_question(store):
    server = _server(panel_ask_timeout=20)
    model = _Model([_ask("c1", "Go on?", ["Yes", "No"])])
    agent = _agent(server)
    agent.llm = model
    reporter = await _panel_run(store, "panel-s3")

    async def stop():
        await _until(lambda: store.run("panel-s3")["state"] == "needs_input")
        await reporter.cancelled("alice")

    started = asyncio.get_running_loop().time()
    await asyncio.wait_for(asyncio.gather(_run(agent, "panelreq3", session_id="panel-s3"), stop()), 10)

    assert asyncio.get_running_loop().time() - started < 5, "the question waited on for a run that had ended"
    [result] = model.results()
    assert result["status"] == "error" and result["reason"] == "gone"
    assert store.run("panel-s3")["state"] == "cancelled" and server.broker.pending() == []


async def test_a_run_no_panel_follows_is_told_at_once_that_nobody_can_answer(store):
    server = _server(panel_ask_timeout=60)
    model = _Model([_ask("c1", "Which database?", ["Postgres", "SQLite"])])
    agent = _agent(server)
    agent.llm = model
    await _panel_run(store, "panel-other")   # a panel run elsewhere does not make this one followed

    await asyncio.wait_for(_run(agent, "headless9", session_id="not-a-panel-run"), 10)

    [result] = model.results()
    assert result["reason"] == "unattended"
    assert _types(store, "panel-other") == ["run.started"]
