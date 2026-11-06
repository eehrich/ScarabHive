"""The demo server (``demo_server.py``) through the real client: started as ``python``, asked field by field.

What is pinned: a stdio ``command: python`` runs on the app's own interpreter, not on whatever PATH
finds; each demo tool's question reaches the person and the answer the server.
"""
from __future__ import annotations

import asyncio
import os
import types
from pathlib import Path

import pytest

from agent_system.core import run_questions
from agent_system.core.request_context import ExternalCall, current_external_call
from agent_system.core.run_questions import AnswerRejected
from plugins.mcp_client import elicitation
from plugins.mcp_client.elicitation import ElicitBroker, Elicitation
from plugins.mcp_client.manager import ExternalServerPool

DEMO = str(Path(__file__).parent.parent / "demo_server.py")
MARKER = "AGENT_SYSTEM_TEST_SESSION"


def _config():
    return types.SimpleNamespace(
        url=None, transport="stdio", enabled=True, auth=None, initialization_options=None, description=None,
        tools=types.SimpleNamespace(blocked=[], allowed=[]), resources_and_prompts=False,
        command="python", args=[DEMO], env={MARKER: os.environ[MARKER]})


async def _ask_through(tool: str, arguments: dict, person) -> str:
    broker = ElicitBroker()
    pool = ExternalServerPool(timeout=20, elicit=Elicitation("mcp_client", broker, "/x/answer", timeout=30).ask)
    pool.configure({"demo": _config()})
    await pool.connect("demo")
    call = ExternalCall(request_id="run1_001", session_id="s1", user_id="alice", agent_name="feature_demo_agent",
                        token=types.SimpleNamespace(is_cancelled=False))
    token = current_external_call.set(call)
    try:
        # gather: a failed check of the person's fails the test at once, not behind the call's timeout
        _, result = await asyncio.gather(person(broker), pool.call_tool("demo", tool, arguments))
        return result
    finally:
        current_external_call.reset(token)
        await pool.close_all()


async def _next(broker: ElicitBroker, seen: set):
    for _ in range(400):
        fresh = [q for q in broker.pending() if q.id not in seen]
        if fresh:
            seen.add(fresh[0].id)
            return fresh[0]
        await asyncio.sleep(0.05)
    raise AssertionError("no question was asked")


@pytest.fixture(autouse=True)
def _watched(monkeypatch):
    # No PATH, the way a service starts the app without its venv: a bare "python" now finds the
    # interpreter with the mcp package only when the client resolves it itself.
    monkeypatch.setenv("PATH", "")
    monkeypatch.setattr(elicitation, "is_read", lambda request_id, gone: True)
    monkeypatch.setattr(run_questions, "is_read", lambda request_id, gone: True)


async def test_a_table_is_booked_field_by_field():
    async def person(broker):
        seen: set = set()
        day = await _next(broker, seen)
        assert day.form()["prompt"] == "MCP server 'demo' asks: Booking a table at Lupo.", day.form()
        with pytest.raises(AnswerRejected, match="date"):
            broker.take(day.id, [], "06.11.2026")
        broker.take(day.id, [], "2026-11-07")
        guests = await _next(broker, seen)
        with pytest.raises(AnswerRejected):
            broker.take(guests.id, [], "20")
        broker.take(guests.id, [], "4")
        seating = await _next(broker, seen)
        assert [c["label"] for c in seating.form()["choices"]][:3] == ["window", "terrace", "inside"]
        broker.take(seating.id, ["#1"], "")
        note = await _next(broker, seen)
        broker.take(note.id, ["skip"], "")

    result = await _ask_through("book_table", {"restaurant": "Lupo"}, person)
    assert result == "Booked (pretend) at Lupo: 2026-11-07, 4 guest(s), terrace.", result


async def test_a_pizza_takes_one_to_three_toppings():
    async def person(broker):
        toppings = await _next(broker, set())
        with pytest.raises(AnswerRejected, match="at most 3"):
            broker.take(toppings.id, ["#0", "#1", "#2", "#3"], "")
        broker.take(toppings.id, ["#3", "#0"], "")

    assert await _ask_through("order_pizza", {}, person) == "Pizza ordered (pretend) with cheese, basil."


async def test_an_account_link_is_shown_and_declined():
    async def person(broker):
        link = await _next(broker, set())
        assert "https://example.com/connect-demo" in link.form()["detail"], link.form()
        broker.take(link.id, ["decline"], "")

    assert await _ask_through("link_account", {}, person) == "The person answered decline."
