"""The Message Debugger panel in a real browser, against the real plugin: its router, its database, its static files.

Seeded: request ``r-1`` (session ``s-1``, agent ``writer``) with three turns -- the oldest carries a long system
message, a user message with markup, an assistant message with a tool call, and a JSON tool result with further
fields -- and three LLM request log entries, a retry among them; under it a tool call ``r-1_001`` (in ``s-1``)
and a sub-agent ``r-1_sub_ab12`` (in ``s-sub``, with an entry); ``r-10``, which only starts like it;
``r-3``, a later request of ``s-1``, with a turn; ``r-2`` (session ``s-2``) with a turn and an entry; ``r-slow`` in
session ``s-slow``, whose lists answer after 1.5 s; and 120 turns of agent ``bulk``. The oldest turn of ``r-1``
answers after 1.5 s too; the oldest ``bulk`` turn is gone once the list is drawn. POST /__stub/turn adds a newer
``bulk`` turn, or with ``?session_id=s-slow`` one in ``s-slow``; GET /__stub/answered counts the turn lists answered.
"""
from __future__ import annotations

import asyncio
import time
from pathlib import Path

import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from agent_system.config.models import AgentSystemConfig, ToolServerConfig
from agent_system.plugins.web_adapter import PluginWebRegistry
from agent_system.ui.resources import STATIC_DIR
from tests.ui.browser import find_browser, run_app_test_page

BROWSER = find_browser()
PAGE_TIMEOUT = 120
pytestmark = [pytest.mark.skipif(BROWSER is None, reason="no Chromium-based browser installed"),
              pytest.mark.timeout(PAGE_TIMEOUT + 60)]

TESTS = Path(__file__).resolve().parent


def seed(db) -> tuple[int, int]:
    """Fill the database; returns the id of the oldest turn of ``r-1``, whose detail answers after 1.5 s, and of
    the oldest ``bulk`` turn, whose detail is gone (404)."""
    now = time.time() * 1000 - 60_000
    messages = [
        {"index": 0, "role": "system", "content": "You write chapters.\n" * 200},  # longer than the drawer
        {"index": 1, "role": "user", "content": "Write <b>chapter</b> 3.", "content_length": 23},
        {"index": 2, "role": "assistant", "content": "", "tool_calls": [
            {"id": "call-1", "function": {"name": "writer_outline", "arguments": '{"chapter": 3}'}}]},
        {"index": 3, "role": "tool", "content": '{"beats": ["arrival", "storm"]}', "is_tool_result": True,
         "tool_call_id": "call-1", "served_by": "gpt", "reasoning_details": [{"type": "reasoning.text"}]},
    ]
    slow_turn = db.insert_turn(now, "pre_llm", agent_name="writer", request_id="r-1", session_id="s-1", step=1,
                               message_count=4, total_tokens=900, messages=messages)
    db.insert_turn(now + 1000, "post_llm", agent_name="writer", request_id="r-1", session_id="s-1", step=1,
                   message_count=5, total_tokens=12000, llm_response={"usage": {"prompt_tokens": 1000, "cost": 0.0021,
                                                            "prompt_tokens_details": {"cached_tokens": 800}}})
    db.insert_turn(now + 1500, "pre_llm", agent_name="tooler", request_id="r-1_001", session_id="s-1", step=1)
    db.insert_turn(now + 1700, "pre_llm", agent_name="helper", request_id="r-1_sub_ab12", session_id="s-sub", step=1)
    db.insert_llm_request(now + 1600, "response", agent_name="helper", request_id="r-1_sub_ab12", session_id="s-sub",
                          provider="openrouter", model="m-3", finish_reason="stop")
    db.insert_turn(now + 1800, "pre_llm", agent_name="writer", request_id="r-10", session_id="s-10", step=1)
    db.insert_turn(now + 2000, "pre_llm", agent_name="writer", request_id="r-1", session_id="s-1", step=2,
                   message_count=6)
    db.insert_llm_request(now + 100, "request", agent_name="writer", request_id="r-1", session_id="s-1",
                          provider="openrouter", model="m-1", payload={"messages": []})
    db.insert_llm_request(now + 200, "response", agent_name="writer", request_id="r-1", session_id="s-1",
                          provider="openrouter", model="m-1", error="[RETRY 1/3] rate limited", finish_reason="retry")
    db.insert_llm_request(now + 900, "response", agent_name="writer", request_id="r-1", session_id="s-1",
                          provider="openrouter", model="m-1", finish_reason="stop", duration_ms=800,
                          usage={"total_tokens": 1200, "cost": 0.0021}, served_by="Google AI Studio")
    db.insert_turn(now + 2500, "pre_llm", agent_name="writer", request_id="r-3", session_id="s-1", step=1)
    db.insert_turn(now + 3000, "pre_llm", agent_name="coder", request_id="r-2", session_id="s-2", step=1)
    db.insert_llm_request(now + 3000, "request", agent_name="coder", request_id="r-2", session_id="s-2",
                          provider="openrouter", model="m-2")
    db.insert_turn(now + 4000, "pre_llm", agent_name="slow", request_id="r-slow", session_id="s-slow", step=1)
    bulk = [db.insert_turn(now + 5000 + i, "pre_llm", agent_name="bulk", request_id=f"r-bulk-{i}", session_id="s-bulk")
            for i in range(120)]  # more than two pages
    return slow_turn, bulk[0]


def panel_app(tmp_path: Path):
    from plugins.message_debugger.plugin import MessageDebuggerHybridPlugin

    server_config = ToolServerConfig()
    server_config.config = {"db_path": str(tmp_path / "debugger.db")}
    plugin = MessageDebuggerHybridPlugin("message_debugger", AgentSystemConfig(), server_config)
    slow_turn, gone_turn = seed(plugin._db)

    app = FastAPI()
    answered = {"turns": 0}

    @app.middleware("http")
    async def slow_or_gone(request: Request, call_next):
        if request.url.path == f"/plugins/message_debugger/turns/{gone_turn}":  # pruned since the list was drawn
            return JSONResponse({"detail": f"Turn {gone_turn} not found"}, status_code=404)
        if (request.query_params.get("session_id") == "s-slow"
                or request.url.path == f"/plugins/message_debugger/turns/{slow_turn}"):
            await asyncio.sleep(1.5)
        response = await call_next(request)
        if request.url.path == "/plugins/message_debugger/turns":
            answered["turns"] += 1
        return response

    @app.get("/__stub/answered")
    async def answered_lists():
        return answered

    @app.post("/__stub/turn")
    async def add_turn(session_id: str = "s-bulk"):
        agent, request_id = ("slow", "r-slow-2") if session_id == "s-slow" else ("bulk", "r-bulk-new")
        plugin._db.insert_turn(time.time() * 1000, "pre_llm", agent_name=agent, request_id=request_id, session_id=session_id)
        return {}

    registry = PluginWebRegistry()  # the plugin's router and static files, mounted as the app mounts them
    registry.register_web_plugin("message_debugger", plugin)
    registry.apply_to_app(app)
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    app.mount("/tests/message_debugger", StaticFiles(directory=TESTS), name="panel-tests")
    return app, plugin


@pytest.fixture(scope="module")
def results(tmp_path_factory):
    app, plugin = panel_app(tmp_path_factory.mktemp("message_debugger_panel"))
    try:
        return run_app_test_page(BROWSER, app, "tests/message_debugger/panel_tests.html", timeout=PAGE_TIMEOUT)
    finally:
        plugin._db.close()


EXPECTED = [
    'opened for a request, both lists show that request and the calls under it, and their tabs count them',
    'the lists sort by a column, by value and not by the text shown, and keep that order when drawn anew',
    'a turn opens in the drawer with its messages: text as text, JSON tool results and tool arguments as trees, other fields by name; the drawer scrolls below its head',
    'the drawer steps to newer and older entries and stops at the ends of the list',
    'an entry that answers after one asked for later does not replace it in the drawer',
    "an entry's session filter shows every request of that session",
    'a slower answer for filters changed since does not replace the list',
    'a list asked for new filters shows nothing of the old ones while it loads',
    'a tick of the auto refresh leaves a list that is still loading alone, and the ticks after it bring what is new',
    'more entries load a page at a time, also when a refresh comes in between, and a refresh keeps as many as are wanted; an entry gone meanwhile is named in the drawer',
    'with the refresh paused, an entry captured since shows only once the viewer refreshes -- not on a tab switch or a filter change, and a list asked for while a refresh is on its way does not undo it',
    'the retry of a request shows its attempt and reason, and its entry opens with its error',
    'a response names the backend that served it, in the list and in the drawer',
    'clearing asks first and, confirmed, empties the lists',
    'pruning asks first and, confirmed, tells what it did, a compacted file included, and shows the lists as they are now',
]


@pytest.mark.parametrize("name", EXPECTED)
def test_message_debugger_panel(results, name):
    assert results.get(name) == "ok", f"{name}: {results.get(name)!r} (all: {results})"


def test_the_page_runs_exactly_the_expected_checks(results):
    assert sorted(results) == sorted(EXPECTED)
