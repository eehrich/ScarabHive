"""The api cluster of the review of 2026-10-15, its panel routes and helpers: the run read for a poll (UI-1), the
journal paged backwards (UI-16), the run list off the loop (PROD-5), inject_params (API-11), a wait question's
discards (DBG-11), run_machine's description (API-12). Each test was checked with a mutation (the report)."""

from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from agent_system.config.models import AgentSystemConfig
from plugins.stategraph.engine.backend import _redact
from plugins.stategraph.engine.journal import RunStore
from plugins.stategraph.server import StateGraphServer
from plugins.stategraph.service import usable_inject
from plugins.stategraph.tests.stategraph_testkit import FASTAPI_PY314, tool_config
from plugins.stategraph.tests.test_plugin_stategraph_server import HELLO, HELLO_PY
from plugins.stategraph.tests.test_plugin_stategraph_web import SCHEMA, client
from plugins.stategraph.wait_questions import discarded

pytestmark = pytest.mark.filterwarnings(FASTAPI_PY314)


@pytest.fixture
async def served(tmp_path):
    server = StateGraphServer("stategraph", AgentSystemConfig(), tool_config(tmp_path))
    (tmp_path / "machines" / "hello.yaml").write_text(HELLO, encoding="utf-8")
    (tmp_path / "machines" / "hello.py").write_text(HELLO_PY, encoding="utf-8")
    app = FastAPI()
    app.state.config = SimpleNamespace(auth=SimpleNamespace(enabled=False))
    app.include_router(server.get_web_router())
    yield server, app
    await server.stop_plugin()


async def _get(app, path: str):
    import httpx

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as http:
        return await http.get(path)


async def test_a_poll_reads_only_new_rows_cut_and_no_ctx(served):
    server, app = served
    started = await server.service.start_run("hello", params={"name": "w" * 500})
    await server.run_manager.wait(started["run_id"], timeout=5.0)
    base = f"/plugins/stategraph/api/runs/{started['run_id']}"

    whole = (await _get(app, base)).json()
    last = whole["journal"][-1]["seq"]
    assert whole["view"]["frames"][0]["ctx"], "without the new parameters: the whole answer as before"
    polled = (await _get(app, f"{base}?after={last - 2}&row_chars=50&ctx=none")).json()

    assert [row["seq"] for row in polled["journal"]] == [last - 1, last], polled["journal"]
    assert all(frame["ctx"] is None for frame in polled["view"]["frames"])
    assert "w" * 500 in str(whole["journal"]), "fixture: a row holds the long name"
    cut = (await _get(app, f"{base}?row_chars=50")).json()
    assert len(cut["journal"]) == len(whole["journal"]) and "w" * 51 not in str(cut["journal"]), cut["journal"]
    root = (await _get(app, f'{base}?ctx=[""]')).json()
    assert root["view"]["frames"][0]["ctx"], "a frame asked for keeps its ctx"


async def test_the_journal_pages_backwards_by_state_and_one_row_whole(served):
    server, app = served
    started = await server.service.start_run("hello")
    await server.run_manager.wait(started["run_id"], timeout=5.0)
    base = f"/plugins/stategraph/api/runs/{started['run_id']}/journal"
    rows = (await _get(app, f"{base}?limit=1000")).json()
    last = rows[-1]["seq"]

    older = (await _get(app, f"{base}?before={last}&limit=2")).json()
    assert [row["seq"] for row in older] == [last - 2, last - 1], older
    greet = (await _get(app, f"{base}?state=greet")).json()
    assert greet and {row["state"] for row in greet} == {"greet"}, greet
    one = (await _get(app, f"{base}?seq={rows[0]['seq']}")).json()
    assert one == [rows[0]], one


def test_the_run_list_is_read_off_the_event_loop():
    test_client, service = client()

    def list_runs(**kwargs):
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return [{"id": "r1", "tags": kwargs.get("tags")}]
        raise AssertionError("the run list was read on the event loop")

    service.list_runs = list_runs
    answer = test_client.get("/plugins/stategraph/api/runs?tags=book:95,path:main")

    assert answer.status_code == 200, answer.text
    assert answer.json() == [{"id": "r1", "tags": {"book": "95", "path": "main"}}]


def test_an_empty_or_short_injected_value_garbles_no_text(caplog):
    result = {"error": "HTTP 401: missing api key", "text": "found 15 tickets"}

    assert _redact(result, "ticket_store_get", {"ticket_store_*": {"api_key": "", "page_size": 5}}) == result
    assert _redact(result, "ticket_store_get", {"ticket_store_*": {"api_key": "missing api"}})["error"] == \
        "HTTP 401: [redacted] key"
    with caplog.at_level(logging.ERROR):
        usable = usable_inject({"ticket_store_*": {"api_key": "", "page_size": 5}}, "instance test_api11")
    assert usable == {"ticket_store_*": {"page_size": 5}}
    assert "ticket_store_*.api_key is empty" in caplog.text


def test_a_discard_past_two_hundred_trace_rows_is_found(tmp_path):
    store = RunStore(tmp_path / "runs.db")
    store.create_run("r", "m", {"root": "m.yaml", "files": {}})
    for n in range(250):
        store.record("r", "trace", f"other:{n}", status="enter", state="busy")
    store.record("r", "trace", "discard", status="event_discarded", state="review", data={"event": "approve"})

    lines = discarded(store, "r", 0)

    store.close()
    assert lines == ["Not taken: 'approve' in 'review' -- no transition took it."], lines


def test_run_machine_says_once_what_its_parameters_say():
    tool = next(t["function"] for t in SCHEMA["tools"] if t["function"]["name"] == "stategraph_run_machine")
    for repeated in ("$visits", "max_wait", "run_key", "mock_only refuses"):
        assert repeated not in tool["description"], repeated
    assert len(tool["description"]) <= 200, len(tool["description"])
