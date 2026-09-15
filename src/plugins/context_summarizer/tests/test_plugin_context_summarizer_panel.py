"""The Context Summarizer panel in a real browser, against the real plugin: its router, its hook recording the runs,
its static files. Only the summarizing LLM is stubbed.

Seeded through the hook: session ``s-1`` with an applied run (request ``r-1``, 40 messages), a rejected one (``r-2``,
the summaries longer than what they replace) and a skipped one (``r-3``, too few messages); session ``s-2`` with an
applied run whose summaries are markup (``r-4``); session ``s-3`` with 101 skipped runs. POST /__stub/record runs the
hook once more in ``s-1`` (skipped), POST /__stub/drop/{id} removes a run from the history behind the panel's back.
GET /__stub/asked counts the histories asked for, per session (``all`` without one). With the cookie
``cs_history=fails`` the history fails, with ``cs_history=slow`` it is held for 1.5 s; with ``cs_slow_event=<id>``
that run's detail is held for 1.5 s.
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from agent_system.config.models import AgentSystemConfig, MCPConfig
from agent_system.hooks import HookContext, HookType
from agent_system.llm.models import ChatMessage
from agent_system.plugins.web_adapter import PluginWebRegistry
from agent_system.ui.resources import STATIC_DIR
from tests.ui.browser import find_browser, run_app_test_page

BROWSER = find_browser()
PAGE_TIMEOUT = 120
pytestmark = [pytest.mark.skipif(BROWSER is None, reason="no Chromium-based browser installed"),
              pytest.mark.timeout(PAGE_TIMEOUT + 60)]

TESTS = Path(__file__).resolve().parent
MARKUP = '<img src="x" onerror="window.parent.__xss = 1">'


def messages(count: int) -> list[ChatMessage]:
    return [ChatMessage(role="user" if i % 2 == 0 else "assistant", content=f"Message {i} " + "words " * 100)
            for i in range(count)]


async def run(hooks, session: str, request: str, count: int) -> None:
    await hooks.summarize_context(HookContext(
        hook_type=HookType.PRE_LLM_CALL, request_id=request, session_id=session, messages=messages(count),
        llm=SimpleNamespace(context_window=100000), metadata={"manual_trigger": True}))


def panel_app():
    from plugins.context_summarizer.plugin import PLUGIN_FACTORY

    plugin = PLUGIN_FACTORY("context_summarizer", AgentSystemConfig(), MCPConfig())
    hooks = plugin.server._hooks_impl
    reply = {"text": "The user asked for a plan."}
    hooks._summarizer_llm = AsyncMock(model_name="stub", chat=AsyncMock(side_effect=lambda *a, **k: reply["text"]))

    async def seed():
        await run(hooks, "s-1", "r-1", 40)
        reply["text"] = "a summary far longer than the messages it replaces " * 200
        await run(hooks, "s-1", "r-2", 40)
        await run(hooks, "s-1", "r-3", 5)
        reply["text"] = MARKUP
        await run(hooks, "s-2", "r-4", 40)
        for i in range(101):
            await run(hooks, "s-3", f"r-bulk-{i}", 5)

    asyncio.run(seed())
    assert [event["status"] for event in plugin.server.summarization_history[:4]] == ["success", "rejected", "skipped", "success"]
    app = FastAPI()
    asked: dict[str, int] = {}

    @app.middleware("http")
    async def stub_modes(request: Request, call_next):
        path = request.url.path
        held = False
        if path == "/plugins/context_summarizer/history":
            scope = request.query_params.get("session_id", "all")
            asked[scope] = asked.get(scope, 0) + 1
            held = request.cookies.get("cs_history") == "slow"
            if request.cookies.get("cs_history") == "fails":
                return JSONResponse({"detail": "The history is locked"}, status_code=500)
        slow_event = request.cookies.get("cs_slow_event")
        if slow_event and path == f"/plugins/context_summarizer/events/{slow_event}":
            held = True
        if not held:
            return await call_next(request)
        answer = await call_next(request)
        payload = b"".join([chunk async for chunk in answer.body_iterator])

        async def body():  # headers at once: an identical request must not queue behind this one in the browser
            await asyncio.sleep(1.5)
            yield payload
        return StreamingResponse(body(), status_code=answer.status_code, media_type="application/json",
                                 headers={"Cache-Control": "no-store"})

    @app.get("/__stub/asked")
    async def histories_asked():
        return asked

    @app.post("/__stub/record")
    async def record():
        await run(hooks, "s-1", "r-5", 5)
        return {}

    @app.post("/__stub/drop/{event_id}")
    async def drop(event_id: int):
        history = plugin.server.summarization_history
        history[:] = [event for event in history if event["id"] != event_id]
        return {}

    registry = PluginWebRegistry()  # the plugin's router and static files, mounted as the app mounts them
    registry.register_web_plugin("context_summarizer", plugin)
    registry.apply_to_app(app)
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    app.mount("/tests/context_summarizer", StaticFiles(directory=TESTS), name="panel-tests")
    return app


@pytest.fixture(scope="module")
def results():
    return run_app_test_page(BROWSER, panel_app(), "tests/context_summarizer/panel_tests.html", timeout=PAGE_TIMEOUT)


EXPECTED = [
    'opened on a session, the panel counts its runs and lists them newest first with what each did',
    'all sessions show the session of each run and the newest 100 of every run',
    'a run opens in the drawer with its figures, its summaries and the messages they replaced, markup as text',
    'a run opened after another shows only the one opened last',
    'a run no longer in the history is refused with a notice and opens nothing',
    'a failed load shows the error and none of the runs shown before',
    'a tick of the auto refresh leaves a load still on its way alone',
    'an answer for the session left behind is not drawn',
    'with no session open the panel says so and asks for nothing',
    'rows drawn anew keep the keyboard focus, and an unchanged answer draws nothing',
    'closing the drawer gives the focus back to its row, also when the list was drawn anew behind it',
    'the auto refresh runs from the start and brings a new run',
    'clearing asks once, cancelled keeps every run and confirmed forgets them',
]


@pytest.mark.parametrize("name", EXPECTED)
def test_context_summarizer_panel(results, name):
    assert results.get(name) == "ok", f"{name}: {results.get(name)!r} (all: {results})"


def test_the_page_runs_exactly_the_expected_checks(results):
    assert sorted(results) == sorted(EXPECTED)
