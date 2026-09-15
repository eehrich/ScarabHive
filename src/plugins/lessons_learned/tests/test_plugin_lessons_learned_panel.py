"""The Lessons Learned panel in a real browser, against the real plugin: its router, its store, its static files.

Seeded in a temporary directory, never the real data/ (priority order): writer_les_001 "Check every date against the
timeline" (priority 8, active, manual, tags ``continuity`` and ``dates``, one confirming evidence), coder_les_001 "Run
the targeted tests before a commit" (7, active, reflection), writer_les_002 "Verify the dates against the timeline" (6,
draft, auto), bulk_les_001 to bulk_les_055 "Bulk lesson NN" (5, active, general), writer_les_003 "Keep dialogue short"
(4, draft, auto, confidence 0.3, style), coder_les_002 "Escape <b>markup</b> in titles" (3, inactive, quality) and
coder_les_003 "Old build step" (2, archived). The LLM that judges a consolidation is a stub: it merges a cluster into
its first lesson, titled "Merged: <that title>", after 0.6 s; after POST /__stub/llm-fails it raises.

Behind the panel's back: POST /__stub/delete/{lesson_id} deletes a lesson, POST /__stub/update/{lesson_id} sets the
fields posted, POST /__stub/draft adds a writer draft, POST /__stub/add adds a coder lesson, POST /__stub/shrink deletes
bulk lessons 41 to 55, POST /__stub/raw/{lesson_id} writes fields as they are, past the store, POST /__stub/fill adds
``count`` lessons of filler agents. GET /__stub/asked counts the lesson lists asked for. With
the cookie ``ll_lessons=fails`` the lesson list fails, with ``slowfails`` after 1.5 s; with ``slow`` it and the cleanup
take 1.5 s. The agents may keep 55 lessons each: bulk is full.
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from agent_system.plugins.web_adapter import PluginWebRegistry
from agent_system.ui.resources import STATIC_DIR
from tests.ui.browser import find_browser, run_app_test_page

BROWSER = find_browser()
PAGE_TIMEOUT = 180
pytestmark = [pytest.mark.skipif(BROWSER is None, reason="no Chromium-based browser installed"),
              pytest.mark.timeout(PAGE_TIMEOUT + 90)]

TESTS = Path(__file__).resolve().parent


async def seed(server) -> None:
    await server.store_lesson("writer", "Check every date against the timeline",
                              "Dates drift between chapters; check each one against the timeline.",
                              category="domain_knowledge", priority=8, status="active", tags=["continuity", "dates"])
    await server.add_evidence("writer_les_001", session_id="s-1", agent_name="writer")
    await server.store_lesson("coder", "Run the targeted tests before a commit", "The whole suite takes twenty minutes.",
                              category="workflow", priority=7, status="active", source_type="reflection")
    await server.store_lesson("writer", "Verify the dates against the timeline",
                              "Verify every date of a chapter against the timeline.",
                              category="domain_knowledge", priority=6, source_type="auto")
    for number in range(1, 56):
        await server.store_lesson("bulk", f"Bulk lesson {number:02d}", f"Filler {number:02d} for the pages.",
                                  status="active")
    await server.store_lesson("writer", "Keep dialogue short", "Long speeches stall a scene.", category="style",
                              priority=4, source_type="auto", confidence=0.3)
    await server.store_lesson("coder", "Escape <b>markup</b> in titles", "Titles come from models.", category="quality",
                              priority=3, status="inactive")
    await server.store_lesson("coder", "Old build step", "Superseded.", priority=2, status="archived")


def panel_app(tmp_path: Path):
    from plugins.lessons_learned.plugin import PLUGIN_FACTORY

    plugin = PLUGIN_FACTORY("lessons_learned", SimpleNamespace(),
                            SimpleNamespace(database_path=str(tmp_path / "lessons.db"), max_lessons_per_agent=55))
    server = plugin.server
    asyncio.run(seed(server))
    llm = {"fails": False}

    async def judge(lessons):
        await asyncio.sleep(0.6)
        if llm["fails"]:
            raise RuntimeError("The judge is unreachable")
        ids = [lesson["lesson_id"] for lesson in lessons]
        return {"groups": [{"merge_ids": ids, "primary_id": ids[0], "title": f"Merged: {lessons[0]['title']}",
                            "content": lessons[0]["content"]}], "keep_separate": []}

    server._llm_evaluate_cluster = judge
    app = FastAPI()
    asked = {"lists": 0}

    @app.middleware("http")
    async def lessons(request: Request, call_next):
        mode = request.cookies.get("ll_lessons")
        path = request.url.path
        if request.method == "GET" and path == "/plugins/lessons_learned/lessons":
            asked["lists"] += 1
            if mode == "slowfails":
                await asyncio.sleep(1.5)
            if mode in ("fails", "slowfails"):
                return JSONResponse({"detail": "The lesson store is locked"}, status_code=500)
        if mode == "slow" and path in ("/plugins/lessons_learned/lessons", "/plugins/lessons_learned/cleanup"):
            await asyncio.sleep(1.5)
        return await call_next(request)

    @app.get("/__stub/asked")
    async def lists_asked():
        return asked

    @app.post("/__stub/llm-fails")
    async def llm_fails():
        llm["fails"] = True
        return {}

    @app.post("/__stub/delete/{lesson_id}")
    async def delete(lesson_id: str):
        return await server.delete_lesson(lesson_id)

    @app.post("/__stub/update/{lesson_id}")
    async def update(lesson_id: str, request: Request):
        return await server.update_lesson(lesson_id, **await request.json())

    @app.post("/__stub/draft")
    async def draft():
        return await server.store_lesson("writer", "A draft that came later", "Unseen by the preview.")

    @app.post("/__stub/add")
    async def add():
        return await server.store_lesson("coder", "Added by an agent", "Brought by a refresh.", status="active")

    @app.post("/__stub/raw/{lesson_id}")
    async def raw(lesson_id: str, request: Request):
        fields = await request.json()
        conn = server._get_connection()
        try:
            conn.execute(f"UPDATE lessons SET {', '.join(f'{name} = ?' for name in fields)} WHERE lesson_id = ?",
                         [*fields.values(), lesson_id])
            conn.commit()
        finally:
            conn.close()
        return {}

    @app.post("/__stub/fill")
    async def fill(request: Request):
        count = (await request.json())["count"]
        for number in range(count):
            await server.store_lesson(f"filler_{number // 50}", f"Filler lesson {number:03d}", "For the pages.",
                                      status="active")
        return {}

    @app.post("/__stub/shrink")
    async def shrink():
        return await server.cleanup_lessons(agent_name="bulk", lesson_ids=[f"bulk_les_{n:03d}" for n in range(41, 56)],
                                            dry_run=False)

    registry = PluginWebRegistry()  # the plugin's router and static files, mounted as the app mounts them
    registry.register_web_plugin("lessons_learned", plugin)
    registry.apply_to_app(app)
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    app.mount("/tests/lessons_learned", StaticFiles(directory=TESTS), name="panel-tests")
    return app


@pytest.fixture(scope="module")
def results(tmp_path_factory):
    app = panel_app(tmp_path_factory.mktemp("lessons_panel"))
    return run_app_test_page(BROWSER, app, "tests/lessons_learned/panel_tests.html", timeout=PAGE_TIMEOUT)


EXPECTED = [
    'the panel counts the lessons by status and draws each with its figures and what can be done to it',
    'the pages are walked, and a filter, a sort or a search starts on the first',
    'the filters narrow the lessons and the figures, and what is filtered for stays offered',
    'a search finds lessons by meaning in every status, narrowed by the filters, and Clear goes back to the list',
    'a new lesson is created from the editor, which sends nothing the form does not accept',
    'the server refuses a lesson the form would not have sent',
    'editing loads the lesson afresh and saves every field',
    'activating and deleting change the server, and deleting asks first',
    'a refusal of the server is shown and the list loaded anew, and a failed save keeps the editor open',
    'a consolidation shows its progress, and only one that is not a dry run changes the lessons',
    'a failed consolidation says so, and the server refuses a threshold out of range',
    'the cleanup deletes only what its preview showed, and a changed filter drops the preview',
    'the server refuses a cleanup without a filter',
    'a page emptied behind the panel falls back to the last page left',
    'a lesson an agent wrote beyond the form is drawn and saved back unchanged',
    'the pager turns one page a click and stays within the pages',
    'a failed load shows the error and none of the lessons shown before',
    'a tick of the auto refresh leaves a load still on its way alone',
    'a tick of the auto refresh brings a lesson an agent added',
]


@pytest.mark.parametrize("name", EXPECTED)
def test_lessons_panel(results, name):
    assert results.get(name) == "ok", f"{name}: {results.get(name)!r} (all: {results})"


def test_the_page_runs_exactly_the_expected_checks(results):
    assert sorted(results) == sorted(EXPECTED)


async def test_a_merge_runs_on_when_its_client_leaves(tmp_path):
    """Cut short, a merge could leave a lesson retitled with its duplicates still there; a second one meanwhile is refused."""
    from plugins.lessons_learned.plugin import PLUGIN_FACTORY
    from plugins.lessons_learned.web_endpoints import ConsolidateForm

    plugin = PLUGIN_FACTORY("lessons_learned", SimpleNamespace(), SimpleNamespace(database_path=str(tmp_path / "lessons.db")))
    server = plugin.server
    await server.store_lesson("writer", "Check every date against the timeline",
                              "Dates drift between chapters; check each one against the timeline.", status="active")
    await server.store_lesson("writer", "Verify the dates against the timeline",
                              "Verify every date of a chapter against the timeline.", status="active")

    async def judge(lessons):
        await asyncio.sleep(0.5)
        ids = [lesson["lesson_id"] for lesson in lessons]
        return {"groups": [{"merge_ids": ids, "primary_id": ids[0], "title": "Merged", "content": "Merged."}]}

    server._llm_evaluate_cluster = judge
    response = await plugin.web_factory.consolidate(None, ConsolidateForm(agent_name="writer", dry_run=False))
    lines = response.body_iterator
    await lines.__anext__()  # the first progress line; then the client is gone
    await lines.aclose()
    with pytest.raises(HTTPException) as refused:  # a second merge while the first runs
        await plugin.web_factory.consolidate(None, ConsolidateForm(agent_name="writer", dry_run=False))
    assert refused.value.status_code == 409
    await (await plugin.web_factory.consolidate(None, ConsolidateForm(agent_name="writer", dry_run=True))).body_iterator.aclose()
    await asyncio.sleep(2)
    listed = (await server.list_lessons(agent_name="writer"))["lessons"]
    assert [lesson["title"] for lesson in listed] == ["Merged"]
    after = await plugin.web_factory.consolidate(None, ConsolidateForm(agent_name="writer", dry_run=False))
    assert [line async for line in after.body_iterator][-1].startswith('{"type": "result"')
