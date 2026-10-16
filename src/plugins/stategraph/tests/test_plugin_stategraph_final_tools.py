"""The tools of the review before the commit (review_2026-10-15 fix round): no server path for a reader, a root
that is not UTF-8, one validation of every machine for the tool and the panel, bounded list_runs and control_run
answers, the panel's journal pages off the event loop.

Each test drives the real StateGraphServer (run_tool, as an agent's call) or the real web endpoints; each was
checked with a mutation of the production line it is about.
"""

from __future__ import annotations

import asyncio
import json
import threading

import pytest

from plugins.stategraph import server as server_module
from plugins.stategraph.tests.stategraph_testkit import FASTAPI_PY314
from plugins.stategraph.tests.test_plugin_stategraph_r15_api_tools import server  # noqa: F401  -- the fixture
from plugins.stategraph.tests.test_plugin_stategraph_server import _enable_auth, run_tool
from plugins.stategraph.tests.test_plugin_stategraph_web import client
from plugins.stategraph.web_endpoints import StateGraphWebEndpoints

pytestmark = pytest.mark.filterwarnings(FASTAPI_PY314)


def size(value) -> int:
    return len(json.dumps(value, default=str))


async def test_a_reader_gets_no_machine_root_as_a_server_path(server, monkeypatch, tmp_path):  # noqa: F811
    """T1: list_machines handed a non-admin reader every machine's root folder as an absolute path."""
    _enable_auth(server, monkeypatch, {"alice": "USER", "root": "ADMIN"})
    where = json.dumps(str(tmp_path))[1:-1]  # as the answer writes it, backslashes doubled

    listed, _ = await run_tool(server, "stategraph_list_machines", {"_user_id": "alice"})
    machine, _ = await run_tool(server, "stategraph_get_machine", {"machine_id": "hello", "_user_id": "alice"})

    assert listed["status"] == "success" and machine["status"] == "success", (listed, machine)
    assert where not in json.dumps(listed) and where not in json.dumps(machine), json.dumps(listed)[:600]
    assert {entry["root"] for entry in listed["machines"]} == {"machines"}, listed["machines"]
    by_admin, _ = await run_tool(server, "stategraph_list_machines", {"_user_id": "root"})
    assert where in json.dumps(by_admin), "the admin keeps the folder"


async def test_get_machine_of_a_root_that_is_not_utf8_is_a_422(server, tmp_path):  # noqa: F811
    """T2: the loader leaves such a root out of the tree; get_machine raised KeyError(<absolute path>)."""
    (tmp_path / "machines" / "latin.yaml").write_bytes(
        "stategraph: 1\nid: latin\ntitle: caf\xe9\ninitial: a\nstates:\n  a: {type: final}\n".encode("latin-1"))

    got, _ = await run_tool(server, "stategraph_get_machine", {"machine_id": "latin"})

    assert got["error_type"] == "http_422" and "not UTF-8" in got["error"], got
    assert "latin.yaml" in got["error"] and json.dumps(str(tmp_path))[1:-1] not in json.dumps(got), got["error"]


async def test_the_tool_and_the_panel_share_one_validation_read_after_they_asked(server, monkeypatch):  # noqa: F811
    """T3: the panel's route had its own computation (each validated every machine), and the tool joined one that had
    started before it asked -- a list right after a save could carry the counts from before it."""
    gates = [threading.Event() for _ in range(3)]
    runs: list[int] = []

    def machine_checks():
        runs.append(len(runs) + 1)
        number = len(runs)
        assert gates[number - 1].wait(10), "never let go"
        return {"hello": {"valid": True, "errors": 0, "warnings": number}}

    async def until(condition) -> None:
        for _ in range(500):
            if condition():
                return
            await asyncio.sleep(0.01)
        raise AssertionError("never came")

    monkeypatch.setattr(server.service, "machine_checks", machine_checks)
    web = StateGraphWebEndpoints(server)

    async def no_user(request):
        return None

    monkeypatch.setattr(web, "_user", no_user)
    listed = lambda: asyncio.ensure_future(server.call_with_status("stategraph_list_machines", {}))  # noqa: E731
    first = listed()
    await until(lambda: len(runs) == 1)
    panel = asyncio.ensure_future(web.api_machine_checks(None))
    tool = listed()
    await asyncio.sleep(0.2)
    assert runs == [1], "the panel's request started a validation of its own"
    gates[0].set()
    await until(lambda: len(runs) == 2)
    gates[1].set()

    warnings = lambda answer: {m["id"]: m.get("warnings") for m in answer["machines"]}["hello"]  # noqa: E731
    assert warnings(await first) == 1
    assert (await panel)["hello"]["warnings"] == 2, "asked while the first ran: the next one answers"
    assert warnings(await tool) == 2, "the tool too: not the computation that started before it asked"
    assert runs == [1, 2]


async def test_list_runs_keeps_the_answer_bound(server):  # noqa: F811
    """T4: tags are up to some 5,000 characters a run; 60 runs answered 339,300 characters."""
    tags = {f"t{i:02d}" + "n" * 60: "v" * 200 for i in range(20)}
    for _ in range(60):
        await server.service.start_run("hello", tags=tags)

    got, _ = await run_tool(server, "stategraph_list_runs", {"limit": 200})

    assert got["status"] == "success", str(got)[:300]
    assert size(got) <= server_module.ANSWER_CHARS, size(got)
    kept = len(got["runs"])
    assert 0 < kept < 60 and got["runs_cut"].startswith(f"{60 - kept} older run(s)"), (kept, got.get("runs_cut"))
    newest = server.service.list_runs(None, 1)[0]["id"]
    assert got["runs"][0]["id"] == newest, "the newest runs stay"


async def test_control_run_keeps_the_bound(server, monkeypatch):  # noqa: F811
    """T5: control_run's answers went out unbounded -- a value it evaluates, and a run row with its journal rows."""
    started, _ = await run_tool(server, "stategraph_run_machine", {"machine_id": "big", "breakpoints": ["make@exit"]})
    assert started["run_status"] == "paused", started
    run_id = started["run_id"]
    try:
        value, _ = await run_tool(server, "stategraph_control_run", {"run_id": run_id, "action": "evaluate",
                                                                     "expr": "out"})
        assert value["status"] == "success", str(value)[:300]
        assert size(value) <= server_module.ANSWER_CHARS and "cut" in value, (size(value), str(value)[:300])

        get_run = server.service.get_run

        def long_run(*args, **kwargs):  # 200 rows of 1,900 characters: a book run's last steps
            row = get_run(*args, **kwargs)
            return {**row, "journal": [{"seq": seq, "kind": "activity", "data": {"out": "z" * 1900}}
                                       for seq in range(1, 201)]}

        monkeypatch.setattr(server.service, "get_run", long_run)
        row, _ = await run_tool(server, "stategraph_control_run", {"run_id": run_id, "action": "set_breakpoints",
                                                                   "breakpoints": [], "steps": 200})
        assert row["status"] == "success" and row["run_id"] == run_id, str(row)[:300]
        assert size(row) <= server_module.ANSWER_CHARS and "journal_cut" in row, (size(row), str(row)[:300])
        assert row["journal"][-1]["seq"] == 200, "the newest rows stay"
    finally:
        await run_tool(server, "stategraph_control_run", {"run_id": run_id, "action": "terminate"})


def test_the_panels_journal_pages_are_read_off_the_event_loop():
    """T6: a history page (200 whole rows of a book run: MBs) was read and decoded on the API's event loop."""
    test_client, service = client()

    def journal(*args, **kwargs):
        try:
            asyncio.get_running_loop()
        except RuntimeError:  # a worker thread
            return [{"seq": 1}]
        raise AssertionError("the journal was read on the event loop")

    service.journal = journal

    answer = test_client.get("/plugins/stategraph/api/runs/r1/journal?before=300")

    assert answer.status_code == 200, answer.text
    assert answer.json() == [{"seq": 1}]

