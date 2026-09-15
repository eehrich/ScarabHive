"""The Context Engineer panel in a real browser, against the real plugin: its router, its session stores on disk under
tmp_path, its static files. The compaction events are seeded in the shape the hook records them; the real history file
is neither read nor written.

Seeded: session ``s-1`` with three compactions (``writer`` 50,000 → 30,000 tokens with L1 and 3 tool results stored;
an agent named in markup 40,000 → 36,000 with P, L1 and L2, 2 media in the window, 1 duplicate and 3 MB; ``blender``
10,000 → 9,000 with no layer and 2 duplicates), two stored tool results, three archived messages (and one tagged
``default``) and four core memory facts, one of them markup; ``s-2`` with one Pre-Layer T compaction; ``s-3`` with 101
compactions and no stores; ``s-8`` with a core memory file that is no JSON. POST /__stub/record records a compaction in
``s-1``; GET /__stub/live answers what the hook's own open stores of ``s-1`` count; GET /__stub/disk lists the session directories and the
sessions the hook holds open. GET /__stub/asked counts the calls per kind (``history``, ``session``) and session
(``all`` for none). With the cookie ``ce=fails`` both calls fail, with ``ce=slow`` they are held for 1.5 s, with
``ce=slower`` for 3 s.
"""
from __future__ import annotations

import asyncio
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from agent_system.plugins.web_adapter import PluginWebRegistry
from agent_system.ui.resources import STATIC_DIR
from tests.ui.browser import find_browser, run_app_test_page

BROWSER = find_browser()
PAGE_TIMEOUT = 120
in_browser = pytest.mark.skipif(BROWSER is None, reason="no Chromium-based browser installed")

TESTS = Path(__file__).resolve().parent
MARKUP = '<img src="x" onerror="window.parent.__xss = 1">'
KINDS = {"/plugins/context_engineer/history": "history", "/plugins/context_engineer/session": "session"}
FACTS = 300
HOLD_SECONDS = {"slow": 1.5, "slower": 3.0}


def event(session, agent, stamp, original, final, layers, stored=0, window=0, duplicates=0, megabytes=0):
    return {"timestamp": stamp, "session_id": session, "agent_name": agent, "original_tokens": original,
            "final_tokens": final, "tokens_saved": original - final, "reduction_percent": (original - final) / original * 100,
            "layers_applied": layers, "tool_results_stored": stored, "messages_archived": 0, "messages_dropped": 0,
            "messages_pruned": 0, "media_deduplicated": duplicates, "media_compacted_after_event": 0,
            "media_always_compacted": window, "media_bytes_saved": megabytes * 1024 * 1024}


def panel_app(storage: Path, monkeypatch):
    from plugins.context_engineer.plugin import PLUGIN_FACTORY
    from plugins.context_engineer.server import ContextEngineerServer

    monkeypatch.setattr(ContextEngineerServer, "_load_history", lambda self: None)  # data/context_engineer stays unread
    plugin = PLUGIN_FACTORY("context_engineer", SimpleNamespace(),
                            SimpleNamespace(config={"storage_path": str(storage)}, hook_config={}, name="context_engineer"))
    hooks = plugin.server._hooks_impl
    hooks.history_callback = None  # and unwritten
    history = plugin.server.stats_history
    base = time.time() - 3600
    history += [event("s-1", "writer", base, 50000, 30000, [1], stored=3),
                event("s-1", MARKUP, base + 60, 40000, 36000, ["P", 1, 2], window=2, duplicates=1, megabytes=3),
                event("s-1", "blender", base + 120, 10000, 9000, [], duplicates=2),
                event("s-2", "coder", base + 180, 4000, 3000, ["T"])]
    history += [event("s-3", f"bulk-{i}", base + 240 + i, 2000, 1900, [1]) for i in range(101)]

    live = hooks._get_session_components("s-1")  # the hook's own path: its files, its stores held open
    for i in range(2):
        live["tool_store"].store_and_reference(f"call_{i}", "read_file", f"line {i}\n" * 400)
    for i in range(3):
        live["archival_memory"].store({"role": "user", "content": f"Archived message {i} " + "words " * 50})
    # tagged as an archive did before it knew its session: in the file, but out of list's reach
    live["archival_memory"].store({"role": "user", "content": "An untagged message"}, session_id="default")

    async def seed():
        for fact, category, importance in (("Use SQLite for the cache", "decisions", 0.9), ("Prefers Python", "preferences", 0.8),
                                           (MARKUP, "facts", 0.3), ("Never deploy on Fridays", "decisions", 0.95)):
            await hooks._handle_store_fact(fact=fact, category=category, importance=importance, session_id="s-1")

    asyncio.run(seed())
    (storage / "s-8").mkdir()
    (storage / "s-8" / "core_memory.json").write_text("{", encoding="utf-8")
    app = FastAPI()
    asked: dict[str, int] = {}

    @app.middleware("http")
    async def modes(request: Request, call_next):
        kind = KINDS.get(request.url.path)
        if not kind:
            return await call_next(request)
        key = f"{kind}:{request.query_params.get('session_id', 'all')}"
        asked[key] = asked.get(key, 0) + 1
        mode = request.cookies.get("ce")
        if mode == "fails":
            return JSONResponse({"detail": "The stores are locked"}, status_code=500)
        if mode not in HOLD_SECONDS:
            return await call_next(request)
        answer = await call_next(request)
        payload = b"".join([chunk async for chunk in answer.body_iterator])

        async def body():  # headers at once, not cacheable: an identical request must not queue behind this one
            await asyncio.sleep(HOLD_SECONDS[mode])
            yield payload
        return StreamingResponse(body(), status_code=answer.status_code, media_type="application/json",
                                 headers={"Cache-Control": "no-store"})

    @app.get("/__stub/asked")
    async def calls_asked():
        return asked

    @app.post("/__stub/record")
    async def record():
        history.append(event("s-1", "recorded", time.time(), 8000, 6000, [1]))
        return {}

    @app.get("/__stub/live")
    async def live_counts():
        return {"tool_results": live["tool_store"].get_stats()["total_tokens_stored"],
                "archived": live["archival_memory"].get_stats("s-1")["total_tokens"],
                "facts": live["core_memory"].get_token_usage()}

    @app.get("/__stub/disk")
    async def disk():
        return {"directories": sorted(path.name for path in storage.iterdir()), "open": sorted(hooks._session_components)}

    registry = PluginWebRegistry()  # the plugin's router and static files, mounted as the app mounts them
    registry.register_web_plugin("context_engineer", plugin)
    registry.apply_to_app(app)
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    app.mount("/tests/context_engineer", StaticFiles(directory=TESTS), name="panel-tests")
    return app


@pytest.fixture(scope="module")
def results(tmp_path_factory):
    with pytest.MonkeyPatch.context() as monkeypatch:
        app = panel_app(tmp_path_factory.mktemp("context_engineer_panel"), monkeypatch)
        return run_app_test_page(BROWSER, app, "tests/context_engineer/panel_tests.html", timeout=PAGE_TIMEOUT)


EXPECTED = [
    'opened on a session, the panel shows its compactions newest first, what its stores hold and its facts, markup as text',
    'all sessions show the session of each compaction, the newest 100 of all, and no stores',
    'a session without data says so, and looking at it writes nothing',
    'a store that cannot be read is answered 503 with the reason, and shown',
    'a failed load shows the error and nothing shown before',
    'a tick of the auto refresh leaves a load still on its way alone',
    'an overtaken load, when its answer comes, does not let the ticks overtake the load that overtook it',
    'the panel follows the session the chat switches to and draws no late answer of the one before',
    'a link to a session keeps to it, whatever session the chat opens',
    'with no session open the panel says so and asks for nothing',
    'the server refuses a session id that is no session id',
    'an unchanged answer draws nothing, and a click on the scope shown asks nothing',
    'the auto refresh runs from the start and brings a new compaction',
]


@in_browser
@pytest.mark.timeout(PAGE_TIMEOUT + 60)
@pytest.mark.parametrize("name", EXPECTED)
def test_context_engineer_panel(results, name):
    assert results.get(name) == "ok", f"{name}: {results.get(name)!r} (all: {results})"


@in_browser
@pytest.mark.timeout(PAGE_TIMEOUT + 60)
def test_the_page_runs_exactly_the_expected_checks(results):
    assert sorted(results) == sorted(EXPECTED)


def test_a_core_memory_being_saved_reads_whole(tmp_path):
    """The hook's save on its thread against the panel's reads of the same file, for a second."""
    import threading

    from plugins.context_engineer.core_memory import CoreMemory, Fact
    from plugins.context_engineer.web_endpoints import _core_memory

    path = tmp_path / "core_memory.json"
    memory = CoreMemory(path, max_tokens=100_000)  # a large file: a long write, a wide window for a torn read
    memory.facts = [Fact(content=f"Fact {number} " + "about the project " * 20, category="facts", importance=0.5)
                    for number in range(FACTS)]
    memory._save_sync()
    failures: list[str] = []

    def save():
        end = time.monotonic() + 1.2
        while time.monotonic() < end:
            try:
                memory._save_sync()
            except Exception as error:  # noqa: BLE001 -- collected
                failures.append(f"save: {error!r}")

    saver = threading.Thread(target=save)
    saver.start()
    reads = 0
    end = time.monotonic() + 1
    while time.monotonic() < end:
        try:
            if len(_core_memory(path, 100_000)["facts"]) != FACTS:
                failures.append("read: not all the facts")
        except Exception as error:  # noqa: BLE001 -- collected
            failures.append(f"read: {error!r}")
        reads += 1
    saver.join()
    assert reads > 50, f"fixture: only {reads} reads"
    assert not failures, failures[:5]


def test_a_read_refused_while_the_file_is_replaced_is_tried_again(tmp_path, monkeypatch):
    """What Windows answers an open during os.replace, twice, then the file."""
    from plugins.context_engineer import web_endpoints

    path = tmp_path / "core_memory.json"
    path.write_text('{"facts": []}', encoding="utf-8")
    refusals = iter([PermissionError(13, "Permission denied"), PermissionError(13, "Permission denied")])
    read_text = Path.read_text

    def refused_twice(self, *args, **kwargs):
        refusal = next(refusals, None)
        if refusal:
            raise refusal
        return read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", refused_twice)
    assert web_endpoints._read_saved(path) == '{"facts": []}'


def test_the_session_is_read_off_the_event_loop(tmp_path, monkeypatch):
    """The read may wait out a save: never on the loop every agent runs on."""
    import threading

    from plugins.context_engineer import web_endpoints

    (tmp_path / "s-1").mkdir()
    (tmp_path / "s-1" / "core_memory.json").write_text('{"facts": []}', encoding="utf-8")
    read_on = []
    read = web_endpoints._read_saved
    monkeypatch.setattr(web_endpoints, "_read_saved", lambda path: (read_on.append(threading.get_ident()), read(path))[1])
    hooks = SimpleNamespace(_storage_base=tmp_path, core_memory_max_tokens=2000)
    factory = web_endpoints.ContextEngineerWebFactory(SimpleNamespace(_hooks_impl=hooks, name="context_engineer"), [])

    async def ask():
        return threading.get_ident(), await factory.get_session(None, session_id="s-1")

    loop_thread, answer = asyncio.run(ask())
    assert answer["core_memory"]["facts"] == [] and read_on, "fixture: the file was not read"
    assert loop_thread not in read_on, "the session was read on the event loop"


def test_a_store_created_but_without_its_table_yet_holds_nothing(tmp_path):
    """The hook's sqlite3.connect creates the file, its CREATE TABLE comes a moment later."""
    from plugins.context_engineer.web_endpoints import _table_totals

    (tmp_path / "tool_results.db").write_bytes(b"")
    assert _table_totals(tmp_path / "tool_results.db", "tool_results") == {"count": 0, "tokens": 0}


def test_a_store_on_a_network_share_is_read(tmp_path):
    """Storage on a share (or a mapped drive, which resolves to one): the store is opened by its UNC path."""
    import sqlite3

    from plugins.context_engineer.web_endpoints import _table_totals

    database = tmp_path / "archive.db"
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE archived_messages (session_id TEXT, token_count INTEGER)")
        connection.executemany("INSERT INTO archived_messages VALUES (?, ?)", [("s-1", 5), ("s-1", 7), ("default", 11)])
    connection.close()
    share = Path(f"\\\\localhost\\{database.drive[0]}$" + str(database)[2:])
    if not share.exists():
        pytest.skip("no administrative share to reach the test directory by a UNC path")
    assert _table_totals(share, "archived_messages", "s-1") == {"count": 2, "tokens": 12}
    assert _table_totals(share, "archived_messages") == {"count": 3, "tokens": 23}
