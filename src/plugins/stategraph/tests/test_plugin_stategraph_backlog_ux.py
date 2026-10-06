"""Regressions for the backlog of the stategraph review of 2026-09-27 (docs/backlog.md), phase 4: the panel's server
side -- the lists its fields offer, and the runs page by page with a status filter. The panel itself is tested in
tests/js/panel_cases.js (test_plugin_stategraph_js.py) and in the browser (test_plugin_stategraph_layout_browser.py).
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from plugins.stategraph.engine.journal import RunStore
from plugins.stategraph.service import ServiceError, StateGraphService
from plugins.stategraph.tests.stategraph_testkit import FASTAPI_PY314
from plugins.stategraph.tests.test_plugin_stategraph_web import SCHEMA, RecordingService

pytestmark = pytest.mark.filterwarnings(FASTAPI_PY314)


def web(server: SimpleNamespace):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from plugins.stategraph.wait_questions import WaitBroker
    from plugins.stategraph.web_endpoints import StateGraphWebEndpoints

    vars(server).setdefault("wait_questions", WaitBroker(lambda *sent: {"accepted": True}, lambda run: None))
    vars(server).setdefault("wait_answer_url", "/plugins/stategraph/answer")
    app = FastAPI()
    app.state.config = SimpleNamespace(auth=SimpleNamespace(enabled=False))
    app.include_router(StateGraphWebEndpoints(server).get_web_router())
    return TestClient(app)


# ------------------------------------------------------------------ U2: what the fields offer

def test_the_catalog_route_offers_agents_tools_and_decision_profiles():
    asked = []

    async def runner_tools(pattern, machine_id=None):
        asked.append(machine_id)
        return [{"name": "store_put", "description": "Store a value", "parameters": {}, "required": []}]

    server = SimpleNamespace(name="stategraph", get_schema_data=lambda: SCHEMA, service=RecordingService(),
                             _catalog=lambda pattern, machine_id: {"agents": [{"name": "scene_writer", "description": "Writes"}],
                                               "decision_profiles": ["fast"], "kinds": [], "tools": ["store/*"]},
                             _runner_tools=runner_tools)

    answer = web(server).get("/plugins/stategraph/api/catalog")

    assert answer.status_code == 200 and answer.json() == {
        "agents": [{"name": "scene_writer", "description": "Writes"}], "profiles": ["fast"],
        "tools": [{"name": "store_put", "description": "Store a value"}]}, answer.text
    web(server).get("/plugins/stategraph/api/catalog?machine_id=v6_story")
    assert asked == [None, "v6_story"], "the open machine's runner names the tools"


# ------------------------------------------------------------------ U5: runs page by page, by status

def test_the_runs_route_passes_the_status_and_the_page_it_continues():
    service = RecordingService()
    server = SimpleNamespace(name="stategraph", get_schema_data=lambda: SCHEMA, service=service)

    web(server).get("/plugins/stategraph/api/runs?machine_id=m&status=failed&before=r7&limit=20")
    web(server).get("/plugins/stategraph/api/runs?machine_id=m&nested=true")

    assert service.calls[-2] == ("list_runs", (), {"machine_id": "m", "limit": 20, "status": "failed",
                                                   "before": "r7", "nested": False})
    assert service.calls[-1][2]["nested"] is True, "a submachine's list: the runs it ran in too"


def test_pages_of_runs_follow_each_other_without_a_gap_or_a_repeat_also_at_one_created_at(tmp_path):
    store = RunStore(tmp_path / "runs.db")
    for n in (3, 0, 4, 1, 2, 5, 6):  # not in id order: the table's own order must not be what sorts them
        store.create_run(f"r{n}", "m", {})
        if n < 5:  # five runs of the same millisecond
            store.update_run(f"r{n}", created_at="2020-01-01T10:00:00.000+00:00")

    seen, before = [], None
    for _ in range(10):  # a cursor that does not move would page for ever
        page = store.list_runs("m", limit=2, before=before)
        if not page:
            break
        seen += [row["id"] for row in page]
        before = page[-1]["id"]

    assert seen == ["r6", "r5", "r4", "r3", "r2", "r1", "r0"], seen


def test_an_unknown_status_is_refused_for_every_caller(tmp_path):
    service = StateGraphService(SimpleNamespace(name="stategraph", system_config=None, runner_agent="r",
                                                inject_params={}, machines=None, run_store=RunStore(tmp_path / "r.db"),
                                                run_manager=None))

    with pytest.raises(ServiceError) as refused:
        service.list_runs("m", status="done")

    assert refused.value.status == 422 and "succeeded" in refused.value.message
