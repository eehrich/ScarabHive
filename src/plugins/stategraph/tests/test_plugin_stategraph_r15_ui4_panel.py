"""The panel side of the run data the review of 2026-10-15 asked for (docs/review_2026-10-15.md: UI-1/PROD-4,
UI-16/DBG-9, DBG-1, DBG-3, DBG-6, PROD-6, FEAT-1, FEAT-2, FEAT-6, FEAT-7, FEAT-8, R9): the panel against the kit's timing
(tests/js/panel_cases.js, its ui4 cases) and the run read's ``seqs`` (the open activities a poll reads again)."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import FastAPI

from agent_system.config.models import AgentSystemConfig
from plugins.stategraph.server import StateGraphServer
from plugins.stategraph.tests.stategraph_testkit import FASTAPI_PY314, tool_config
from plugins.stategraph.tests.test_plugin_stategraph_js import JSC, NODE, prepare_panel, run_case
from plugins.stategraph.tests.test_plugin_stategraph_server import HELLO, HELLO_PY

needs_js = pytest.mark.skipif(JSC is None and NODE is None, reason="neither JavaScriptCore (jsc) nor node is installed")

UI4_CASES = [
    "ui4_a_poll_asks_only_what_is_new_reads_open_activities_again_and_keeps_its_rows",
    "ui4_a_queued_event_is_read_again_until_a_frame_takes_it",
    "ui4_a_burst_of_rows_starts_the_older_pages_over",
    "ui4_the_state_filter_keeps_its_rows_that_leave_the_last_rows",
    "ui4_a_preview_never_scans_a_long_answer_whole",
    "ui4_a_folded_frames_ctx_is_read_when_it_is_unfolded",
    "ui4_history_pages_back_filters_by_state_and_shows_a_row_whole",
    "ui4_a_step_shows_what_it_changed_and_an_error_where_it_came_from",
    "ui4_an_activity_lists_every_request_and_the_runs_it_started",
    "ui4_an_activity_ended_without_outcome_is_not_in_flight_nor_read_again",
    "ui4_a_resumed_run_and_a_burst_of_rows_are_read_whole_again",
    "ui4_runs_of_every_machine_by_status_and_tags_open_with_their_machine",
    "ui4_a_run_that_failed_resumably_can_be_closed",
    "ui4_a_slot_wait_and_the_cost_against_the_limit_are_shown",
    "ui4_max_visits_takes_a_number_or_an_expression",
]


@needs_js
def test_every_ui4_panel_case_is_run(tmp_path):
    prepare_panel(tmp_path, "fake_kit_held.js", ("fake_kit_held.js", "panel_cases.js"))

    listed = next(line for line in run_case(tmp_path, "ui4:*") if line.startswith("CASES ")).split()[1:]

    assert sorted(listed) == sorted(UI4_CASES)


@needs_js
@pytest.mark.parametrize("case", UI4_CASES)
def test_ui4_panel_case(tmp_path, case):
    prepare_panel(tmp_path, "fake_kit_held.js", ("fake_kit_held.js", "panel_cases.js"))

    lines = run_case(tmp_path, case)

    assert f"PASS {case}" in lines and "ERRORS []" in lines, "\n".join(lines)


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


@pytest.mark.filterwarnings(FASTAPI_PY314)
async def test_a_poll_reads_the_rows_it_names_again_cut_like_the_others(served):
    server, app = served
    started = await server.service.start_run("hello", params={"name": "w" * 500})
    await server.run_manager.wait(started["run_id"], timeout=5.0)
    base = f"/plugins/stategraph/api/runs/{started['run_id']}"
    rows = (await _get(app, base)).json()["journal"]
    last, first = rows[-1]["seq"], rows[0]["seq"]

    polled = (await _get(app, f"{base}?after={last}&seqs={first},{last - 1}&row_chars=50")).json()

    assert [row["seq"] for row in polled["journal"]] == [first, last - 1], polled["journal"]
    assert polled["journal"][0] == {**rows[0], "data": polled["journal"][0]["data"]}
    assert "w" * 51 not in str(polled["journal"])
    assert (await _get(app, f"{base}?seqs=0")).status_code == 422
    assert (await _get(app, f"{base}?seqs={','.join(str(n) for n in range(1, 52))}")).status_code == 422
    gone = (await _get(app, f"{base}?after={last}&seqs={last + 50}")).json()
    assert gone["journal"] == [], "a row that is not there is left out, not an error"


@pytest.mark.filterwarnings(FASTAPI_PY314)
async def test_rows_read_again_cost_no_run_read_each(served, monkeypatch):
    server, app = served
    started = await server.service.start_run("hello", params={"name": "w"})
    await server.run_manager.wait(started["run_id"], timeout=5.0)
    base = f"/plugins/stategraph/api/runs/{started['run_id']}"
    last = (await _get(app, base)).json()["journal"][-1]["seq"]
    store = server.service.run_store
    reads = []
    get_run = store.get_run
    monkeypatch.setattr(store, "get_run", lambda *a, **k: reads.append(1) or get_run(*a, **k))

    await _get(app, f"{base}?after={last}")
    alone = len(reads)
    polled = (await _get(app, f"{base}?after={last}&seqs=1,2,3")).json()

    assert [row["seq"] for row in polled["journal"]] == [1, 2, 3]
    assert len(reads) - alone == alone, "each row read again reads the run again"
