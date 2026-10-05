"""The Debate Forum panel in a real browser, against the real plugin: its router, its database (under tmp_path), its
static files. Nothing is stubbed; what agents do happens behind the panel's back through the plugin's database.

Seeded: group ``Schema review`` (id 1) with ``schema`` (active; Mira the critic, Sven the author and a moderator over
rounds 1 and 2 -- a moderator post in round 1 after round 2 began, a pinned post, a JSON block with a raw line break in
a string, a Python block, a block holding the JSON number 42, Markdown) and
``titles`` (concluded: a Markdown summary, a verdict with winner, score, summary and remaining_differences); group
``<b>Markup group</b>`` (id 2) with ``<img src=x onerror=parent.__xss=1>`` (active, a post whose name, role and text are
markup); ``old-debate`` (archived, no group, no messages).

Behind the panel's back: POST /__stub/post?channel=&round= posts as Mira, POST /__stub/append?message= appends a chunk,
POST /__stub/conclude?channel=&summary= concludes, POST /__stub/delete?channel= deletes, POST /__stub/many adds 55 channels to
group 1, POST /__stub/create?name= creates a channel, POST /__stub/clear deletes every channel. GET /__stub/message?id= reads a message, GET /__stub/channel?id= a
channel, GET /__stub/asked counts the channel lists asked for. Cookies: ``df_list=fails|slow|slowfail`` for the channel
list, ``df_channel=<id>`` holds that channel's detail 1.5 s, ``df_channel_fails=<id>`` fails it (after the hold, if both),
``df_messages=<id>`` holds its messages 1.5 s (``all``: every channel's detail), ``df_post=slow`` holds a post from the panel
1.5 s, ``df_action=slow`` holds a pin, an archiving and a reopening 0.5 s,
``df_create=slow|slowfail`` answers a new channel after 1.5 s (the second with a failure). GET /__stub/asked also counts
the message lists, pins and channel creations asked for.
"""
from __future__ import annotations

import asyncio
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
PAGE_TIMEOUT = 150
pytestmark = [pytest.mark.skipif(BROWSER is None, reason="no Chromium-based browser installed"),
              pytest.mark.timeout(PAGE_TIMEOUT + 60)]

TESTS = Path(__file__).resolve().parent
PREFIX = "/plugins/debate_forum/api/"

VERDICT = {"winner": "Schema B", "score": 8, "summary": "ignored", "remaining_differences": ["pace"]}
JSON_POST = 'Scores:\n\n```json\n{"winner": "B", "note": "a line\nbroken raw", "reasons": ["tension", "voice"]}\n```'


def seed(db) -> None:
    story = db.create_group("Schema review")["group_id"]
    markup = db.create_group("<b>Markup group</b>")["group_id"]
    schema = db.create_channel("schema", "Which schema is best?", group_id=story)["channel_id"]
    db.post_message(schema, "Mira", "critic", 1, "Schema A nests too **deep**.")
    pinned = db.post_message(schema, "Sven", "author", 1, "The task: pick one of three.")["message_id"]
    db.pin_message(pinned)
    db.post_message(schema, "Mira", "critic", 2, JSON_POST)
    db.post_message(schema, "Moderator", "moderator", 1, "A late note on round 1:\n\n```python\nprint('x')\n```\n\n```\n42\n```")
    titles = db.create_channel("titles", "Which title?", group_id=story)["channel_id"]
    db.post_message(titles, "Sven", "author", 1, "Title one.")
    db.conclude_channel(titles, VERDICT, "**B** wins")
    evil = db.create_channel("<img src=x onerror=parent.__xss=1>", "<i>topic</i>", group_id=markup)["channel_id"]
    db.post_message(evil, "<u>name</u>", "<s>role</s>", 1, "<script>parent.__xss=2</script><img src=x onerror=parent.__xss=3> text")
    old = db.create_channel("old-debate", "Long gone")["channel_id"]
    db.archive_channel(old)


def panel_app(tmp_path: Path) -> FastAPI:
    from plugins.debate_forum.plugin import PLUGIN_FACTORY

    plugin = PLUGIN_FACTORY("debate_forum", SimpleNamespace(), SimpleNamespace(config={"db_path": str(tmp_path / "forum.db")}))
    db = plugin._db
    seed(db)
    app = FastAPI()
    asked = {"channels": 0, "messages": 0, "pins": 0, "creates": 0}

    def held(payload: bytes, status: int, seconds: float) -> StreamingResponse:
        # headers at once, body held, not cacheable: the browser's cache lock would otherwise hold back the next
        # request for the same URL until this answer is complete
        async def body():
            await asyncio.sleep(seconds)
            yield payload
        return StreamingResponse(body(), status_code=status, media_type="application/json", headers={"Cache-Control": "no-store"})

    @app.middleware("http")
    async def modes(request: Request, call_next):
        path, cookies = request.url.path, request.cookies
        if request.method == "GET" and path == f"{PREFIX}channels":
            asked["channels"] += 1
            mode = cookies.get("df_list")
            if mode == "fails":
                return JSONResponse({"detail": "The forum is locked"}, status_code=500)
            if mode == "slowfail":
                return held(b'{"detail": "The forum is locked"}', 500, 1.5)
            if mode == "slow":
                answer = await call_next(request)
                return held(b"".join([chunk async for chunk in answer.body_iterator]), answer.status_code, 1.5)
        if path.endswith("/messages"):
            if request.method == "GET":
                asked["messages"] += 1
            if request.method == "GET" and cookies.get("df_messages") == path.split("/")[-2]:
                answer = await call_next(request)
                return held(b"".join([chunk async for chunk in answer.body_iterator]), answer.status_code, 1.5)
            if request.method == "POST" and cookies.get("df_post") == "slow":
                await asyncio.sleep(1.5)
        if request.method == "POST" and path.endswith(("/pin", "/archive", "/reopen")) and cookies.get("df_action") == "slow":
            await asyncio.sleep(0.5)
        if request.method == "POST" and path.endswith("/pin"):
            asked["pins"] += 1
        if request.method == "POST" and path == f"{PREFIX}channels":
            asked["creates"] += 1
            if cookies.get("df_create") == "slowfail":
                await asyncio.sleep(1.5)
                return JSONResponse({"detail": "The forum is locked"}, status_code=500)
            if cookies.get("df_create") == "slow":
                await asyncio.sleep(1.5)
        if request.method == "GET" and path.startswith(f"{PREFIX}channels/") and path.count("/") == 5:
            channel = path.rsplit("/", 1)[1]
            fails, slow = cookies.get("df_channel_fails") == channel, cookies.get("df_channel") in (channel, "all")
            if fails and not slow:
                return JSONResponse({"detail": "The channel is locked"}, status_code=500)
            if slow:
                if fails:
                    return held(b'{"detail": "The channel is locked"}', 500, 1.5)
                answer = await call_next(request)
                return held(b"".join([chunk async for chunk in answer.body_iterator]), answer.status_code, 1.5)
        return await call_next(request)

    @app.get("/__stub/asked")
    async def lists_asked():
        return asked

    @app.get("/__stub/message")
    async def message(id: int):
        return db.get_message(id)

    @app.get("/__stub/channel")
    async def channel(id: int):
        found = db.get_channel(id)
        return {**found, "messages": db.get_messages(id)} if found else None

    @app.post("/__stub/post")
    async def post(channel: int, round: int = 2):
        return db.post_message(channel, "Mira", "critic", round, "An agent's new post.")

    @app.post("/__stub/append")
    async def append(message: int):
        return db.append_message(message, " And a chunk appended.")

    @app.post("/__stub/conclude")
    async def conclude(channel: int, summary: str = "Done"):
        return db.conclude_channel(channel, {"winner": "A", "summary": "*From the verdict*"}, summary)

    @app.post("/__stub/delete")
    async def delete(channel: int):
        return db.delete_channel(channel)

    @app.post("/__stub/many")
    async def many():
        for index in range(55):
            db.create_channel(f"bulk-{index}", "", group_id=1)
        return {}

    @app.post("/__stub/create")
    async def create(name: str):
        return db.create_channel(name, "")

    @app.post("/__stub/clear")
    async def clear():
        for found in db.list_channels(limit=1000):
            db.delete_channel(found["id"])
        return {}

    registry = PluginWebRegistry()  # the plugin's router and static files, mounted as the app mounts them
    registry.register_web_plugin("debate_forum", plugin)
    registry.apply_to_app(app)
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    app.mount("/tests/debate_forum", StaticFiles(directory=TESTS), name="panel-tests")
    return app


@pytest.fixture(scope="module")
def results(tmp_path_factory):
    yield run_app_test_page(BROWSER, panel_app(tmp_path_factory.mktemp("debate_panel")), "tests/debate_forum/panel_tests.html",
                            timeout=PAGE_TIMEOUT)


EXPECTED = [
    'opened, the panel counts the channels, lists the groups newest first and closed, and shows no channel',
    'a group opens and closes, stays so across a tick and a reload, and lists its channels oldest first',
    'the channel list keeps the width it was resized to across a reload, and a narrow panel still stacks it full width',
    'a channel shows its thread: a round opens only where a later one begins, participants, pins, Markdown, code and the line to post',
    'a JSON block switches to a readable tree, other code does not, and an unchanged thread is not drawn again',
    'posts, names and topics are drawn as text, and a post’s markup is removed',
    'a post goes once into the latest round, clears its text and is drawn; Shift+Enter and a blank text send nothing',
    'a post the channel refuses keeps its text and shows why; reopening lets it post again, once',
    'a pin toggles once, a double click’s second click on the redrawn button does nothing, a refused pin shows why',
    'a concluded channel shows its verdict and is reloaded only when it changed; archive and reopen',
    'deleting asks once, keeps the channel on cancel and deletes the one it named when asked later',
    'a new channel is created once and shown; a refusal stays in the dialog, a late answer leaves a new dialog alone',
    'status, group and search filter the list, and the search asks once while typed',
    'the focus stays on a channel and a group when the list is drawn anew',
    'copying puts the debate as text on the clipboard, and says when it cannot',
    'a failed load shows the error and nothing of the list shown before; a failed channel says so',
    'an answer overtaken by a later load is dropped',
    'a tick of the auto refresh leaves a load still on its way alone',
    'the auto refresh runs from the start, brings posts and appended chunks, and follows only a thread read to its end',
    'the list says when it shows only the most recently active channels',
    'with no channels the panel says so',
]


@pytest.mark.parametrize("name", EXPECTED)
def test_debate_forum_panel(results, name):
    assert results.get(name) == "ok", f"{name}: {results.get(name)!r} (all: {results})"


def test_the_page_runs_exactly_the_expected_checks(results):
    assert sorted(results) == sorted(EXPECTED)
