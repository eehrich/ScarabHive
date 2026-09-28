"""The Memory panel in a real browser, against the real plugin: its router, its memory store on disk, its static files.
Only the vector store is stubbed: it ranks by the words a memory shares with the query, without embeddings.

Seeded (keywords as given): session ``s-1`` with mem_001 "Python type hints" (importance 9, keywords ``python`` and
``typing``, accessed once), mem_002 "Deploy checklist" (importance 6, ``deploy``, accessed three times) and mem_003 "Style
guide" (importance 3, no keywords, never accessed), most recently accessed first in that order; session ``s-2`` with
mem_001, title and content markup, and mem_002 "Second session memory"; session ``s-3`` with 101 memories "Bulk memory
1" to "Bulk memory 101", mem_100 accessed 12,000 times and mem_101 900 times; session ``s-8`` with a metadata file that is
no JSON. Behind the panel's back: POST /__stub/store stores "Stored by an agent" in ``s-1`` (or the ``session`` given),
/__stub/forget deletes mem_003 of ``s-1``, /__stub/retitle retitles it "Style guide, revised" without new content,
/__stub/ghost drops mem_002 of ``s-2`` from the metadata but not from the vector store; /__stub/vectors/off makes the
vector store fail, /__stub/vectors/on mends it. GET /__stub/asked counts the calls, per kind (``list``, ``stats``, ``search``) and session. With the cookie
``mm=fails`` the list and the figures fail, with ``mm=slow`` they and the search are held for 1.5 s.
"""
from __future__ import annotations

import asyncio
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from agent_system.auth.dependencies import get_optional_user
from agent_system.plugins.web_adapter import PluginWebRegistry
from agent_system.ui.resources import STATIC_DIR
from tests.ui.browser import find_browser, run_app_test_page

BROWSER = find_browser()
PAGE_TIMEOUT = 150
pytestmark = [pytest.mark.skipif(BROWSER is None, reason="no Chromium-based browser installed"),
              pytest.mark.timeout(PAGE_TIMEOUT + 60)]

TESTS = Path(__file__).resolve().parent
MARKUP = '<img src="x" onerror="window.parent.__xss = 1">'
KINDS = {"/plugins/memory/memories": "list", "/plugins/memory/stats": "stats", "/plugins/memory/memories/search": "search"}


class WordVectors:
    """The vector store's add/query/delete, ranked by the share of the query's words a document holds.

    Unlike an embedding search it leaves out what shares no word, so a search can find nothing."""

    def __init__(self):
        self.collections: dict[str, dict[str, tuple[str, dict]]] = {}
        self.broken = False

    def add(self, collection, ids, documents, metadatas):
        self.collections.setdefault(collection, {}).update(zip(ids, zip(documents, metadatas)))

    def delete(self, collection, ids):
        for item in ids:
            self.collections.get(collection, {}).pop(item, None)

    def query(self, collection, query_text, n_results, include):
        if self.broken:
            raise RuntimeError("the embedding model is not loaded")
        words = set(re.findall(r"\w+", query_text.lower()))
        scored = sorted(((len(words & set(re.findall(r"\w+", document.lower()))) / len(words), item, document, metadata)
                         for item, (document, metadata) in self.collections.get(collection, {}).items()),
                        key=lambda hit: -hit[0])
        hits = [hit for hit in scored if hit[0] > 0][:n_results]
        return {"ids": [[hit[1] for hit in hits]], "documents": [[hit[2] for hit in hits]],
                "metadatas": [[hit[3] for hit in hits]], "distances": [[2 * (1 - hit[0]) for hit in hits]]}


async def seed(server) -> None:
    async def store(session, title, content, importance, keywords=None):
        await server._operation_store(session_id=session, title=title, content=content, importance=importance, keywords=keywords)

    await store("s-1", "Python type hints", "Always annotate function parameters in Python code", 9, ["python", "typing"])
    await store("s-1", "Deploy checklist", "Run the migrations before restarting the service", 6, ["deploy"])
    await store("s-1", "Style guide", "Follow PEP 8 in Python", 3)
    for memory_id in ("mem_002", "mem_002", "mem_002", "mem_001"):
        await server._operation_recall(session_id="s-1", memory_id=memory_id)
    await store("s-2", MARKUP, MARKUP, 5)
    await store("s-2", "Second session memory", "Kept for the second session", 5)
    for number in range(1, 102):
        await store("s-3", f"Bulk memory {number}", f"Bulk content {number}", 1)
    # stamped a minute apart in the order given, oldest first: the clock may give memories stored in a row one time
    base = datetime.now(timezone.utc) - timedelta(days=1)
    for session, order in (("s-1", ["mem_003", "mem_002", "mem_001"]), ("s-2", ["mem_002", "mem_001"]),
                           ("s-3", [f"mem_{number:03d}" for number in range(1, 102)])):
        collection = await server._load_collection(session)
        for minutes, memory_id in enumerate(order):
            collection.memories[memory_id].accessed_at = base + timedelta(minutes=minutes)
        if session == "s-3":  # counts whose text does not sort as they do: "900" before "12,000"
            collection.memories["mem_100"].access_count = 12000
            collection.memories["mem_101"].access_count = 900
        await server._save_collection(collection)


def panel_app(storage: Path):
    from plugins.memory.plugin import PLUGIN_FACTORY

    plugin = PLUGIN_FACTORY("memory", {}, SimpleNamespace(storage_path=str(storage), auto_extract_keywords=False))
    server = plugin.server
    server.vector_store = vectors = WordVectors()
    asyncio.run(seed(server))
    (storage / "s-8.json").write_text("{", encoding="utf-8")  # a metadata file cut short
    app = FastAPI()
    # One person's instance (auth off): the panel sees every session (agent_system/auth/session_access.py), and
    # nobody signs in -- the user database, data/users.db by default, is not asked.
    app.state.config = SimpleNamespace(auth=SimpleNamespace(enabled=False))
    app.dependency_overrides[get_optional_user] = lambda: None
    asked: dict[str, int] = {}

    @app.middleware("http")
    async def modes(request: Request, call_next):
        kind = KINDS.get(request.url.path)
        if not kind:
            return await call_next(request)
        key = f"{kind}:{request.query_params.get('session_id')}"
        asked[key] = asked.get(key, 0) + 1
        mode = request.cookies.get("mm")
        if mode == "fails" and kind != "search":
            return JSONResponse({"detail": "The memory store is locked"}, status_code=500)
        if mode != "slow":
            return await call_next(request)
        answer = await call_next(request)
        payload = b"".join([chunk async for chunk in answer.body_iterator])

        async def body():  # headers at once, not cacheable: an identical request must not queue behind this one
            await asyncio.sleep(1.5)
            yield payload
        return StreamingResponse(body(), status_code=answer.status_code, media_type="application/json",
                                 headers={"Cache-Control": "no-store"})

    @app.get("/__stub/asked")
    async def calls_asked():
        return asked

    @app.post("/__stub/store")
    async def store(session: str = "s-1"):
        await server._operation_store(session_id=session, title="Stored by an agent", content="A memory stored meanwhile", importance=7)
        return {}

    @app.post("/__stub/forget")
    async def forget():
        await server._operation_delete(session_id="s-1", memory_id="mem_003")
        return {}

    @app.post("/__stub/retitle")
    async def retitle():
        await server._operation_update(session_id="s-1", memory_id="mem_003", title="Style guide, revised")
        return {}

    @app.post("/__stub/ghost")
    async def ghost():
        collection = await server._load_collection("s-2")
        del collection.memories["mem_002"]
        await server._save_collection(collection)
        return {}

    @app.post("/__stub/vectors/{state}")
    async def vector_state(state: str):
        vectors.broken = state == "off"
        return {}

    registry = PluginWebRegistry()  # the plugin's router and static files, mounted as the app mounts them
    registry.register_web_plugin("memory", plugin)
    registry.apply_to_app(app)
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    app.mount("/tests/memory", StaticFiles(directory=TESTS), name="panel-tests")
    return app


@pytest.fixture(scope="module")
def results(tmp_path_factory):
    app = panel_app(tmp_path_factory.mktemp("memory_panel"))
    return run_app_test_page(BROWSER, app, "tests/memory/panel_tests.html", timeout=PAGE_TIMEOUT)


EXPECTED = [
    'opened on a session, the panel counts its memories and lists them most recently accessed first',
    'of more than a hundred memories the hundred most recently accessed are listed, and the total said',
    'a session without memories says so',
    'a memory file that cannot be read is answered 503 with the reason, and shown',
    'a memory opens in the drawer with its figures and content, markup as text',
    'a search lists the closest memories with their match, and Clear brings the list back',
    'a search shows what the memory says now, and nothing of a memory that is gone',
    'a search that finds nothing says so, and a failed one says why',
    'a tick of the auto refresh keeps the results of a search and brings the figures',
    'deleting asks once: cancelled the memory stays, confirmed it goes and the row in its place takes the focus',
    'a memory already gone is refused with a notice and the list loaded anew',
    'a failed load shows the error and none of the memories shown before',
    'a tick of the auto refresh leaves a load still on its way alone',
    'the panel follows the session the chat switches to, drops its search and draws no late answer of the one before',
    'a link to a session keeps to it and to its search, whatever session the chat opens',
    'the way back from a link: the toolbar names the session it was sent to, and one click follows the chat again',
    'with no session open the panel says so and asks for nothing',
    'the server refuses a session id that is no session id',
    'rows drawn anew keep the keyboard focus, and an unchanged answer draws nothing',
    'closing the drawer gives the focus back to its row, also when the list was drawn anew behind it',
    'the auto refresh runs from the start and brings a memory an agent stores',
    'a click on a head sorts the memories by value, and the order holds when they are drawn anew',
]


@pytest.mark.parametrize("name", EXPECTED)
def test_memory_panel(results, name):
    assert results.get(name) == "ok", f"{name}: {results.get(name)!r} (all: {results})"


def test_the_page_runs_exactly_the_expected_checks(results):
    assert sorted(results) == sorted(EXPECTED)
