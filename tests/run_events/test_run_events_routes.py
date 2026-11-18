"""The run event routes (api/run_event_routes.py): a panel's run says it started and how it ended without anyone
asking for it, a person reads their own runs only, and the stream reads on where a dropped client was."""
import asyncio
import json
import threading

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from starlette.requests import Request as StarletteRequest

from agent_system.api import run_event_routes
from agent_system.core import turn_runs
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


@pytest.fixture(autouse=True)
def no_sweep_by_the_streams(monkeypatch):
    """The streams have the store end the runs whose process is gone, now and then. The runs of "another process"
    these tests write name one that is not there: ended by whichever stream came first in the half minute."""
    monkeypatch.setattr(run_event_routes, "SWEEP_S", float("inf"))


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


async def test_a_stream_tells_what_another_process_wrote_and_that_its_run_is_alive(app, events, monkeypatch):
    """The bus hands out this process's events only: a turn in an agent-cli says its events into the store, and
    the server's streams never heard of them -- nor that the run was alive, shown silent after half a minute."""
    import os

    from agent_system import run_events

    store, bus = events
    monkeypatch.setattr(run_event_routes, "TAIL_S", 0.05)
    mine = RunReporter("mine", "code_analyst", who="local", store=store, bus=bus)
    await mine.started()
    response = await run_event_routes.agent_runs_stream(request_for(app), after="")
    lines = response.body_iterator

    def elsewhere(type_, **fields):
        with monkeypatch.context() as other:
            other.setattr(run_events, "OWNER", f"{os.getpid()}:1.000:another")
            return store.append("cli-turn", type_, agent="coder", who="local", source="chat", task="turn", **fields)

    try:
        await read(lines, 2)
        elsewhere("run.started")
        (started,) = await read(lines, 1)
        assert (data_of(started)["run_id"], data_of(started)["type"]) == ("cli-turn", "run.started")
        assert started.startswith("id: evt_")
        # this process's own events come once: through the bus, not again from the store
        await mine.step("Read the code")
        elsewhere("run.step", step={"index": 1, "label": "Thinking"})
        said = [data_of(line) for line in await read(lines, 2)]
        assert [(e["run_id"], e["type"]) for e in said] == [("mine", "run.step"), ("cli-turn", "run.step")]
        elsewhere("run.heartbeat")
        (beat,) = await read(lines, 1)
        assert (data_of(beat)["run_id"], data_of(beat)["type"]) == ("cli-turn", "run.heartbeat")
        assert not beat.startswith("id:")
        # another process wrote first, this one after it -- and its bus is the faster: what was written before it
        # is still sent (the bus moving how far the store was read, the earlier event was skipped for good)
        monkeypatch.setattr(run_event_routes, "TAIL_S", 0.4)
        await asyncio.sleep(0.5)      # the round under way reads with the old beat
        elsewhere("run.step", step={"index": 3, "label": "Search"})
        await mine.step("Sum up")
        both = [(data_of(line)["type"], data_of(line)["step"]["label"]) for line in await read(lines, 2)]
        assert both == [("run.step", "Sum up"), ("run.step", "Search")], both   # the event, not a sign of life
        monkeypatch.setattr(run_event_routes, "TAIL_S", 0.05)
        # an event of this process read from the store before the bus brought it is not sent again
        late = store.append("mine", "run.step", step={"index": 2, "label": "Assess"})
        (first,) = await read(lines, 1)
        bus.publish(late)
        elsewhere("run.step", step={"index": 2, "label": "Read"})
        (second,) = await read(lines, 1)
        assert [data_of(first)["step"]["label"], data_of(second)["step"]["label"]] == ["Assess", "Read"]
    finally:
        await lines.aclose()


def test_the_runs_in_the_terminals_words_are_the_asking_persons(app, events):
    """What the chat's /runs shows: the lines ``agent-cli runs`` prints, of the asker's runs only."""
    store, bus = events

    async def two():
        mine = RunReporter("mine", "code_analyst", who="local", title="Assess the issues", source="chat", task="turn",
                           store=store, bus=bus)
        await mine.started()
        await mine.step("Thinking")
        await RunReporter("theirs", "inventory_agent", who="someone", store=store, bus=bus).started()

    asyncio.run(two())
    with TestClient(app) as client:
        text = client.get("/api/agent-runs/text?width=50").json()["text"]
        assert client.get("/api/agent-runs/text?width=20").status_code == 422
    lines = text.splitlines()
    assert lines[0] == "At work (1)" and lines[1].startswith("  ● code_analyst · Assess the issues")
    assert lines[2] == "      Thinking" and len(lines) == 3 and max(map(len, lines)) <= 50


def test_the_runs_are_told_in_the_askers_language_where_an_agent_names_its_steps_so(app, events):
    store, bus = events

    async def thinking():
        mine = RunReporter("mine", "coder", who="local", title="Fix it", source="chat", task="turn", store=store, bus=bus)
        await mine.started()
        await mine.step("Thinking", labels={"de": "Denkt nach"})

    asyncio.run(thinking())
    with TestClient(app) as client:
        assert "      Thinking" in client.get("/api/agent-runs/text").json()["text"].splitlines()
        assert "      Denkt nach" in client.get("/api/agent-runs/text?lang=de-DE").json()["text"].splitlines()


def test_a_stop_that_cannot_be_sent_says_why(app, events, monkeypatch):
    """409 for a run that has ended, for one another process holds and for one of this server's with nothing to
    stop it by: told apart, or the list said "runs in another process" of a run that had just ended, and of one
    this very server held -- and sent its person looking for a process there is not."""
    from agent_system import run_events

    store, bus = events
    ending = RunReporter("ending", "coder", who="local", store=store, bus=bus)

    async def four():
        ended = RunReporter("ended", "coder", who="local", store=store, bus=bus)
        await ended.started()
        await ended.done("ok")
        await RunReporter("here", "coder", who="local", store=store, bus=bus).started()
        await RunReporter("closing", "coder", who="local", store=store, bus=bus).started()
        await ending.started()
        with monkeypatch.context() as other:
            other.setattr(run_events, "OWNER", "999999:1.000:theirs:" + run_events.NAMESPACE)
            await RunReporter("elsewhere", "coder", who="local", store=store, bus=bus).started()

    asyncio.run(four())

    async def too_late(run_id, who):
        # nothing of this process works there any more: the run wrote its end as the stop came
        if run_id == "ending":
            await ending.done("ok")
        return False

    monkeypatch.setattr(run_event_routes, "cancel_run", too_late)
    # a turn that is over and still writes its end (core/turn_runs.py): at work in the store for that moment
    written = threading.Event()
    writing = threading.Thread(target=written.wait, daemon=True)
    writing.start()
    monkeypatch.setitem(turn_runs._ENDING, "closing", writing)
    marks = ("x-run-held-elsewhere", "x-run-takes-no-stop")
    try:
        with TestClient(app) as client:
            said = {run_id: client.post(f"/api/agent-runs/{run_id}/cancel")
                    for run_id in ("ended", "ending", "closing", "here", "elsewhere")}
    finally:
        written.set()
        writing.join()
    assert {run_id: (r.status_code, *(r.headers.get(mark) for mark in marks)) for run_id, r in said.items()} == {
        "ended": (409, None, None), "ending": (409, None, None), "closing": (409, None, None),
        "here": (409, None, "1"), "elsewhere": (409, "1", None)}
    assert {said[run_id].json()["detail"] for run_id in ("ended", "ending", "closing")} == {"This run has ended"}


async def test_a_step_kept_as_its_run_ended_is_not_told_after_the_end(app, events, monkeypatch):
    """A turn's end is not held up for a step still being written (core/turn_runs.py): the step is kept, its
    event never reaches the bus, the end goes out at once -- and the store, read on, brought the step after it. A
    page that had let go of the ended run took it for a new one at work: a row that never ended."""
    from agent_system.plugins.sqlite_guard import connection

    store, bus = events
    monkeypatch.setattr(run_event_routes, "TAIL_S", 0.05)
    run = RunReporter("turn", "coder", who="local", source="chat", task="turn", store=store, bus=bus)
    await run.started()
    response = await run_event_routes.agent_runs_stream(request_for(app), after="")
    lines = response.body_iterator
    try:
        await read(lines, 2)
        store.append("turn", "run.step", step={"index": 1, "label": "Thinking"})      # kept, not said
        await run.done("ok")
        (end,) = await read(lines, 1)
        assert data_of(end)["type"] == "run.done"
        # what the store brings next is the step from before the end, and the end the bus was faster with: neither
        store.append("other", "run.started", agent="coder", who="local", source="chat", task="turn")
        (next_,) = await read(lines, 1)
        assert (data_of(next_)["run_id"], data_of(next_)["type"]) == ("other", "run.started")
        # a run of the id begun anew is told, though it counts from one again (the store had let go of the old one)
        with store._lock, connection(store.path) as db:
            db.execute("DELETE FROM agent_run_events WHERE run_id = 'turn'")
            db.execute("DELETE FROM agent_runs WHERE run_id = 'turn'")
        again = store.append("turn", "run.started", agent="coder", who="local", source="chat", task="turn")
        assert again["seq"] == 1
        told = [data_of(line) for line in await read(lines, 1)]
        assert [(e["run_id"], e["type"], e["seq"]) for e in told] == [("turn", "run.started", 1)]
        store.append("turn", "run.step", step={"index": 1, "label": "Thinking"})
        (step,) = await read(lines, 1)
        assert (data_of(step)["type"], data_of(step)["seq"]) == ("run.step", 2)
    finally:
        await lines.aclose()


async def test_the_streams_end_the_run_of_a_process_that_is_gone(app, events, monkeypatch):
    """An agent-cli closed mid-turn says no end. The store ends such runs when a process opens it -- a server that
    stays up never did again, and the turn stood "at work" above the sessions for as long as it ran."""
    from agent_system import run_events

    store, bus = events
    monkeypatch.setattr(run_event_routes, "TAIL_S", 0.05)
    with monkeypatch.context() as other:
        other.setattr(run_events, "OWNER", "999999:1.000:gone:" + run_events.NAMESPACE)
        store.append("cli-turn", "run.started", agent="coder", who="local", source="chat", task="turn")
    monkeypatch.setattr(run_event_routes, "SWEEP_S", 30.0)
    monkeypatch.setattr(run_event_routes, "_swept", 0.0)
    assert run_event_routes._sweep_due() and not run_event_routes._sweep_due(), "not once in a while: every time"
    monkeypatch.setattr(run_event_routes, "_swept", 0.0)
    response = await run_event_routes.agent_runs_stream(request_for(app), after="")
    lines = response.body_iterator
    try:
        await read(lines, 2)
        (ended,) = await read(lines, 1)
        assert (data_of(ended)["run_id"], data_of(ended)["type"]) == ("cli-turn", "run.failed")
        assert data_of(ended)["payload"] == {"reason": run_events.INTERRUPTED}
    finally:
        await lines.aclose()
