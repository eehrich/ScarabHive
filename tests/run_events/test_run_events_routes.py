"""The run event routes (api/run_event_routes.py): a panel's run says it started and how it ended without anyone
asking for it, a person reads their own runs only, and the stream reads on where a dropped client was."""
import asyncio
import json

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from starlette.requests import Request as StarletteRequest

from agent_system.api import run_event_routes
from agent_system.plugins import agent_tasks
from agent_system.plugins.agent_tasks import AgentTask, AiSettings, AiTaskRoutes
from agent_system.run_events import RunEventBus, RunEventStore, RunReporter, use_run_events


class Server:
    PERSON_ONLY = ()
    name = "code_audit"

    def __init__(self, path):
        self.settings = AiSettings(path, (AgentTask("analyse", "Code analysis", "Analyses.", "code_analyst"),))
        self.settings.set("analyse", "ai", None, "2026-10-31T12:00:00+00:00", "local")

    def ai(self):
        return self.settings

    @staticmethod
    def now():
        return "2026-10-31T12:00:00+00:00"

    @staticmethod
    def ai_prompt(task, target, agent):
        return "Code analysis", "Analyse the project"


class Routes(AiTaskRoutes):
    def __init__(self, server):
        self.server = server


@pytest.fixture
def events(tmp_path):
    store, bus = RunEventStore(tmp_path / "run_events.db"), RunEventBus()
    use_run_events(store, bus)
    yield store, bus
    use_run_events(None, RunEventBus())


@pytest.fixture
def app(tmp_path, monkeypatch, events):
    monkeypatch.setattr(agent_tasks, "START_WAIT", 0.5)
    routes = Routes(Server(tmp_path / "plugin.db"))
    app = FastAPI()
    app.post("/p/ai/{task}/run")(routes.post_ai_run)
    app.include_router(run_event_routes.router)

    @app.post("/run")
    async def run(request: Request):
        return {"status": "success", "summary": "4 attack vectors"}

    return app


def request_for(app, headers=()):
    scope = {"type": "http", "method": "GET", "path": "/api/agent-runs/stream", "query_string": b"",
             "headers": [(k.lower().encode(), v.encode()) for k, v in headers], "app": app}
    return StarletteRequest(scope)


async def read(iterator, count):
    out = []
    while len(out) < count:
        out.append(await asyncio.wait_for(iterator.__anext__(), timeout=2))
    return out


def data_of(line):
    return json.loads(line.split("data: ", 1)[1])


def test_a_panel_run_says_it_started_and_how_it_ended(app, events):
    store, _ = events
    with TestClient(app) as client:
        started = client.post("/p/ai/analyse/run", json={}).json()
        timeline = client.get(f"/api/agent-runs/{started['session_id']}/events").json()
        runs = client.get("/api/agent-runs").json()["runs"]
    assert [e["type"] for e in timeline["events"]] == ["run.started", "run.done"]
    assert timeline["events"][0]["agent"] == "code_analyst" and timeline["events"][0]["source"] == "code_audit"
    assert timeline["events"][-1]["payload"] == {"summary": "4 attack vectors"}
    assert [(r["run_id"], r["state"]) for r in runs] == [(started["session_id"], "done")]


def test_a_person_reads_only_their_own_runs(app, events):
    store, bus = events
    asyncio.run(RunReporter("theirs", "inventory_agent", who="someone", store=store, bus=bus).started())
    with TestClient(app) as client:
        assert client.get("/api/agent-runs").json()["runs"] == []
        assert client.get("/api/agent-runs/theirs/events").status_code == 404
        assert client.get("/api/agent-runs/nothing/events").status_code == 404


def test_the_timeline_reads_on_after_a_gap(app, events):
    store, bus = events

    async def three():
        run = RunReporter("mine", "code_analyst", who="local", store=store, bus=bus)
        await run.started()
        await run.step("Read the code")
        await run.step("Write the report")

    asyncio.run(three())
    with TestClient(app) as client:
        after = client.get("/api/agent-runs/mine/events?after_seq=1").json()["events"]
    assert [e["seq"] for e in after] == [2, 3]


async def test_a_new_stream_starts_with_the_runs_and_then_follows_them_live(app, events):
    store, bus = events
    mine = RunReporter("mine", "code_analyst", who="local", store=store, bus=bus)
    await mine.started()
    response = await run_event_routes.agent_runs_stream(request_for(app), after="")
    lines = response.body_iterator
    try:
        retry, snapshot = await read(lines, 2)
        assert retry.startswith("retry:")
        assert data_of(snapshot)["type"] == "runs.snapshot"
        assert [r["run_id"] for r in data_of(snapshot)["runs"]] == ["mine"]
        await RunReporter("theirs", "inventory_agent", who="someone", store=store, bus=bus).started()
        await mine.step("Read the code")
        (line,) = await read(lines, 1)
        assert line.startswith("id: evt_")
        event = data_of(line)
        assert (event["run_id"], event["type"], event["step"]["label"]) == ("mine", "run.step", "Read the code")
        assert "who" not in event and "cursor" not in event
        await mine.heartbeat()
        (beat,) = await read(lines, 1)
        # not kept, so no id: the browser's Last-Event-ID stays at the step, where a reconnect reads on
        assert not beat.startswith("id:") and data_of(beat)["type"] == "run.heartbeat"
    finally:
        await lines.aclose()
    assert bus.subscribers == 0


async def test_a_stream_that_reconnects_reads_on_from_its_last_event(app, events):
    store, bus = events
    mine = RunReporter("mine", "code_analyst", who="local", store=store, bus=bus)
    first = await mine.started()
    await mine.step("Read the code")
    await mine.done("4 attack vectors")
    response = await run_event_routes.agent_runs_stream(request_for(app, [("Last-Event-ID", first["event_id"])]), after="")
    lines = response.body_iterator
    try:
        _retry, step, done = await read(lines, 3)
    finally:
        await lines.aclose()
    assert [data_of(step)["type"], data_of(done)["type"]] == ["run.step", "run.done"]


async def test_a_quiet_stream_keeps_its_connection_open(app, events, monkeypatch):
    monkeypatch.setattr(run_event_routes, "KEEPALIVE_S", 0.05)
    response = await run_event_routes.agent_runs_stream(request_for(app), after="")
    lines = response.body_iterator
    try:
        _retry, _snapshot, quiet = await read(lines, 3)
    finally:
        await lines.aclose()
    assert quiet == ": keepalive\n\n"


def test_a_person_checks_a_result_and_sees_a_failure(app, events):
    store, bus = events

    async def two():
        review = RunReporter("checkme", "code_analyst", who="local", store=store, bus=bus)
        await review.started()
        await review.review_ready("2 assessed", "Assess the issues")
        failed = RunReporter("broke", "code_analyst", who="local", store=store, bus=bus)
        await failed.started()
        await failed.failed("no model")
        theirs = RunReporter("theirs", "code_analyst", who="someone", store=store, bus=bus)
        await theirs.started()
        await theirs.failed("no model")

    asyncio.run(two())
    with TestClient(app) as client:
        checked = client.post("/api/agent-runs/checkme/ack").json()["run"]
        seen = client.post("/api/agent-runs/broke/ack").json()["run"]
        again = client.post("/api/agent-runs/broke/ack")
        foreign = client.post("/api/agent-runs/theirs/ack")
    assert (checked["state"], checked["needs_you"]) == ("done", False)
    assert (seen["state"], seen["needs_you"]) == ("failed", False)
    assert again.status_code == 409 and foreign.status_code == 404


def test_a_review_task_from_the_panel_ends_waiting_for_its_person(tmp_path, monkeypatch, events):
    store, _ = events
    monkeypatch.setattr(agent_tasks, "START_WAIT", 0.5)
    server = Server(tmp_path / "plugin.db")
    server.settings = AiSettings(tmp_path / "p2.db", (AgentTask("assess", "Assess the issues", "x", "code_analyst",
                                                                review=True),))
    server.settings.set("assess", "ai", None, "2026-10-31T12:00:00+00:00", "local")
    routes = Routes(server)
    review_app = FastAPI()
    review_app.post("/p/ai/{task}/run")(routes.post_ai_run)
    review_app.include_router(run_event_routes.router)
    sent = []

    @review_app.post("/run")
    async def run(request: Request):
        sent.append(await request.json())
        return {"status": "success", "summary": "2 assessed"}

    with TestClient(review_app) as client:
        started = client.post("/p/ai/assess/run", json={}, headers={"Accept-Language": "de"}).json()
        timeline = client.get(f"/api/agent-runs/{started['session_id']}/events").json()
    last = timeline["events"][-1]
    assert (last["type"], last["needs_you"], last["payload"]["check_in"]) == ("run.review_ready", True, "Assess the issues")
    assert sent[0]["request_id"] == f"panel-{started['session_id']}-1"
    # the person checks it without reading code: the agent is asked for three parts in plain words, in German here
    assert all(head in sent[0]["task"] for head in ("### Changed", "### Could go wrong", "### Checked"))
    assert "in German" in sent[0]["task"]


def test_the_three_parts_are_asked_for_in_the_panels_language():
    from agent_system.plugins.agent_tasks import review_note
    assert "in English" in review_note("en-GB,en;q=0.9") and "in German" in review_note("de-DE")
    assert "in English" in review_note("")


def test_a_task_a_person_does_not_check_asks_for_no_parts(tmp_path, monkeypatch, events):
    monkeypatch.setattr(agent_tasks, "START_WAIT", 0.5)
    routes = Routes(Server(tmp_path / "plugin.db"))
    plain = FastAPI()
    plain.post("/p/ai/{task}/run")(routes.post_ai_run)
    sent = []

    @plain.post("/run")
    async def run(request: Request):
        sent.append(await request.json())
        return {"status": "success"}

    with TestClient(plain) as client:
        client.post("/p/ai/analyse/run", json={}, headers={"Accept-Language": "de"})
    assert len(sent) == 1 and "### Changed" not in sent[0]["task"]


def test_a_run_at_work_is_cancelled_and_says_who_cancelled(tmp_path, monkeypatch, events):
    store, _ = events
    monkeypatch.setattr(agent_tasks, "START_WAIT", 0.1)
    routes = Routes(Server(tmp_path / "plugin.db"))
    slow_app = FastAPI()
    slow_app.post("/p/ai/{task}/run")(routes.post_ai_run)
    slow_app.include_router(run_event_routes.router)

    @slow_app.post("/run")
    async def run(request: Request):
        await asyncio.sleep(30)
        return {"status": "success"}

    with TestClient(slow_app) as client:
        started = client.post("/p/ai/analyse/run", json={}).json()
        answer = client.post(f"/api/agent-runs/{started['session_id']}/cancel")
        for _ in range(100):
            events_now = client.get(f"/api/agent-runs/{started['session_id']}/events").json()["events"]
            if events_now[-1]["type"] == "run.cancelled":
                break
            client.get("/api/agent-runs")   # let the loop run the end
            import time as _time
            _time.sleep(0.05)
        again = client.post(f"/api/agent-runs/{started['session_id']}/cancel")
    assert answer.json() == {"status": "cancelling", "run_id": started["session_id"]}
    assert events_now[-1]["type"] == "run.cancelled" and events_now[-1]["payload"] == {"by": "local"}
    assert again.status_code == 409


def test_a_run_waiting_for_its_persons_answer_can_be_cancelled(tmp_path, monkeypatch, events):
    store, _ = events
    monkeypatch.setattr(agent_tasks, "START_WAIT", 0.1)
    routes = Routes(Server(tmp_path / "plugin.db"))
    slow_app = FastAPI()
    slow_app.post("/p/ai/{task}/run")(routes.post_ai_run)
    slow_app.include_router(run_event_routes.router)

    @slow_app.post("/run")
    async def run(request: Request):
        await asyncio.sleep(30)   # the agent waits in ask_user
        return {"status": "success"}

    with TestClient(slow_app) as client:
        started = client.post("/p/ai/analyse/run", json={}).json()
        store.append(started["session_id"], "run.needs_input", payload={"question": "Which one?"})
        answer = client.post(f"/api/agent-runs/{started['session_id']}/cancel")
    assert answer.status_code == 200, answer.text
    assert answer.json() == {"status": "cancelling", "run_id": started["session_id"]}


async def test_a_second_start_in_a_session_still_at_work_is_refused(app, monkeypatch):
    monkeypatch.setattr(agent_tasks, "_LIVE", {"busy"})
    with pytest.raises(agent_tasks.HTTPException) as refused:
        await agent_tasks.launch(request_for(app), "code_analyst", "go on", "Code analysis", session_id="busy")
    assert refused.value.status_code == 409
