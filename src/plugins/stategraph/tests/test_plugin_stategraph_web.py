"""The panel's REST layer against a recording service: routes from schema.yaml, JSON in and out, refusals as HTTP."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import yaml
from fastapi import FastAPI
from fastapi.testclient import TestClient

from agent_system.auth.models import UserRole
from http_routes import http_routes
from plugins.stategraph import web_endpoints
from plugins.stategraph.service import ServiceError
from plugins.stategraph.tests.stategraph_testkit import FASTAPI_PY314
from plugins.stategraph.web_endpoints import StateGraphWebEndpoints

pytestmark = pytest.mark.filterwarnings(FASTAPI_PY314)

PLUGIN = Path(web_endpoints.__file__).parent
SCHEMA = yaml.safe_load((PLUGIN / "schema.yaml").read_text(encoding="utf-8").replace("{{ name }}", "stategraph"))


class RecordingService:
    """Answers every call with what ``answers`` holds for it (an exception is raised), and remembers the call."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple, dict]] = []
        self.answers: dict[str, Any] = {}

    def __getattr__(self, name: str):
        if name.startswith("_"):
            raise AttributeError(name)

        def call(*args: Any, **kwargs: Any) -> Any:
            self.calls.append((name, args, kwargs))
            answer = self.answers.get(name, {"called": name})
            if isinstance(answer, Exception):
                raise answer
            return answer

        if name in ("start_run", "control_run"):  # the service's coroutines
            async def coroutine(*args: Any, **kwargs: Any) -> Any:
                return call(*args, **kwargs)
            return coroutine
        return call


def client(*, auth: bool = False) -> tuple[TestClient, RecordingService]:
    service = RecordingService()
    server = SimpleNamespace(name="stategraph", get_schema_data=lambda: SCHEMA, service=service)
    app = FastAPI()
    app.state.config = SimpleNamespace(auth=SimpleNamespace(enabled=auth))
    app.include_router(StateGraphWebEndpoints(server).get_web_router())
    return TestClient(app), service


def test_every_endpoint_in_the_schema_has_its_route():
    test_client, _ = client()
    paths = {(path, method) for method, path in http_routes(test_client.app)}

    for endpoint in SCHEMA["web_ui"]["endpoints"]:
        assert (f"/plugins/stategraph{endpoint['path']}".rstrip("/") or "/", endpoint["method"]) in \
            {(path.rstrip("/") or "/", method) for path, method in paths}, endpoint


def test_reads_answer_with_what_the_service_returns():
    test_client, service = client()
    service.answers["list_machines"] = [{"id": "review", "valid": True}]

    answer = test_client.get("/plugins/stategraph/api/machines")
    journal = test_client.get("/plugins/stategraph/api/runs/r1/journal?after=5&kinds=activity,trace")

    assert answer.status_code == 200 and answer.json() == [{"id": "review", "valid": True}]
    assert journal.status_code == 200
    assert service.calls[-1] == ("journal", ("r1",), {"after": 5, "limit": 200, "kinds": ["activity", "trace"]})


def test_an_edit_carries_the_operation_and_the_version_read():
    test_client, service = client()
    op = {"op": "add_state", "name": "judge"}

    answer = test_client.post("/plugins/stategraph/api/machines/review/edit", json={"op": op, "expected_version": "abc"})

    assert answer.status_code == 200
    assert service.calls == [("edit_machine", ("review", op, "abc"), {"drafts": None})]


def test_an_edit_onto_drafts_needs_no_version():
    test_client, service = client()
    op = {"op": "add_state", "name": "judge"}

    answer = test_client.post("/plugins/stategraph/api/machines/review/edit", json={"op": op, "drafts": {"review.yaml": "x"}})
    without = test_client.post("/plugins/stategraph/api/machines/review/edit", json={"op": op})

    assert answer.status_code == 200
    assert service.calls == [("edit_machine", ("review", op, None), {"drafts": {"review.yaml": "x"}})]
    assert without.status_code == 422 and without.json()["detail"] == "expected_version is required"


def test_a_service_refusal_keeps_its_status_and_message():
    test_client, service = client()
    service.answers["edit_machine"] = ServiceError(409, "the file changed since you read it")

    answer = test_client.post("/plugins/stategraph/api/machines/review/edit", json={"op": {"op": "x"}, "expected_version": "a"})

    assert answer.status_code == 409 and answer.json()["detail"] == "the file changed since you read it"


def test_a_write_must_come_as_a_json_object():
    test_client, service = client()

    form = test_client.post("/plugins/stategraph/api/machines/review/edit", content="op=x",
                            headers={"content-type": "application/x-www-form-urlencoded"})
    listed = test_client.post("/plugins/stategraph/api/machines/review/edit", json=[1, 2])
    missing = test_client.post("/plugins/stategraph/api/machines/review/edit", json={"op": {"op": "x"}})

    assert (form.status_code, listed.status_code, missing.status_code) == (415, 422, 422)
    assert service.calls == []


def test_start_run_is_awaited_and_runs_as_the_admin_asking(monkeypatch):
    test_client, service = client(auth=True)
    admin = SimpleNamespace(username="ada", is_active=True, role=UserRole.ADMIN)

    async def found(*args: Any) -> Any:
        return admin

    monkeypatch.setattr(web_endpoints, "get_optional_user", found)
    monkeypatch.setattr(web_endpoints, "get_db", lambda: None)
    service.answers["start_run"] = {"run_id": "r7"}

    answer = test_client.post("/plugins/stategraph/api/runs", json={"machine_id": "review", "mock_only": True,
                                                                     "breakpoints": [{"state": "judge", "at": "exit"}]})

    assert answer.status_code == 200 and answer.json() == {"run_id": "r7"}
    name, args, kwargs = service.calls[-1]
    assert (name, args, kwargs["user_id"], kwargs["mock_only"]) == ("start_run", ("review",), "ada", True)
    assert kwargs["breakpoints"] == [{"state": "judge", "at": "exit"}]


@pytest.mark.parametrize("user, status", [
    (None, 401),
    (SimpleNamespace(username="bob", is_active=True, role=UserRole.USER), 403),
    (SimpleNamespace(username="old", is_active=False, role=UserRole.ADMIN), 401),
])
def test_only_active_admins_reach_the_api_when_auth_is_on(monkeypatch, user, status):
    test_client, service = client(auth=True)

    async def found(*args: Any) -> Any:
        return user

    monkeypatch.setattr(web_endpoints, "get_optional_user", found)
    monkeypatch.setattr(web_endpoints, "get_db", lambda: None)

    answer = test_client.get("/plugins/stategraph/api/machines")
    page = test_client.get("/plugins/stategraph/")

    assert (answer.status_code, page.status_code) == (status, status)
    assert service.calls == []


def test_control_run_passes_known_arguments_and_the_user_and_refuses_others():
    test_client, service = client()

    answer = test_client.post("/plugins/stategraph/api/runs/r1/control", json={"action": "run_to", "state": "judge"})
    refused = test_client.post("/plugins/stategraph/api/runs/r1/control", json={"action": "set", "value": 1})

    assert answer.status_code == 200
    assert service.calls[0] == ("control_run", ("r1", "run_to"), {"state": "judge", "user_id": None})
    assert refused.status_code == 422 and len(service.calls) == 1


def test_a_run_is_read_evented_and_controlled_as_the_admin_asking(monkeypatch):
    """The service checks who may see a run (server.sees_run) and fails closed: a route that names nobody would find
    every run of a user missing. The control answer carries as many journal rows as the panel's poll."""
    test_client, service = client(auth=True)
    admin = SimpleNamespace(username="ada", is_active=True, role=UserRole.ADMIN)

    async def found(*args: Any) -> Any:
        return admin

    monkeypatch.setattr(web_endpoints, "get_optional_user", found)
    monkeypatch.setattr(web_endpoints, "get_db", lambda: None)

    run = test_client.get("/plugins/stategraph/api/runs/r1?steps=200")
    event = test_client.post("/plugins/stategraph/api/runs/r1/events", json={"name": "approve"})
    control = test_client.post("/plugins/stategraph/api/runs/r1/control", json={"action": "pause", "steps": 200})

    assert (run.status_code, event.status_code, control.status_code) == (200, 200, 200)
    assert service.calls == [("get_run", ("r1",), {"steps": 200, "user_id": "ada", "frames": True}),  # the frame picker
                             ("send_event", ("r1", "approve"), {"data": None, "frame": None, "user_id": "ada"}),
                             ("control_run", ("r1", "pause"), {"steps": 200, "user_id": "ada"})]


def test_the_page_names_its_viewer_whose_sessions_the_chat_can_open(monkeypatch):
    test_client, _ = client(auth=True)
    admin = SimpleNamespace(username="ada", is_active=True, role=UserRole.ADMIN)

    async def found(*args: Any) -> Any:
        return admin

    monkeypatch.setattr(web_endpoints, "get_optional_user", found)
    monkeypatch.setattr(web_endpoints, "get_db", lambda: None)

    page = test_client.get("/plugins/stategraph/")

    assert page.status_code == 200 and 'data-viewer="ada"' in page.text


def test_a_machine_is_deleted_as_json_naming_the_version_seen():
    test_client, service = client()

    form = test_client.request("DELETE", "/plugins/stategraph/api/machines/review", content="expected_version=v1",
                               headers={"content-type": "application/x-www-form-urlencoded"})
    blind = test_client.request("DELETE", "/plugins/stategraph/api/machines/review", json={})
    deleted = test_client.request("DELETE", "/plugins/stategraph/api/machines/review", json={"expected_version": "v1"})

    assert (form.status_code, blind.status_code, deleted.status_code) == (415, 422, 200)
    assert service.calls == [("delete_machine", ("review", "v1"), {})]


def test_the_panel_page_loads_elk_before_the_module():
    test_client, _ = client()

    page = test_client.get("/plugins/stategraph/")

    assert page.status_code == 200
    text = page.text
    assert "/plugins/stategraph/static/vendor/elkjs/elk.bundled.js" in text
    assert text.index("elk.bundled.js") < text.index("/plugins/stategraph/static/panel.js")
    assert 'id="canvas"' in text and "/static/kit/panel-kit.js" in text
