"""AI tasks of a plugin panel (agent_system/plugins/agent_tasks.py): the person's choice per task, and the start of
the chosen agent on /run as the person who asked.

The app's /run is a stub here: it records what it was sent and answers at once, or refuses as the real one does;
an agent named "slow" runs until the test lets it end. /api/sessions/{id} is a stub as well, answering the messages
the test put there -- what the panel shows of a run.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

import pytest
from fastapi import FastAPI, HTTPException, Request
from fastapi.testclient import TestClient

from agent_system.llm.tls import httpx_verify
from agent_system.plugins import agent_tasks
from agent_system.plugins.agent_tasks import AgentTask, AiSettings, AiTaskRoutes, PersonOnly, steps

TASKS = (AgentTask("assess", "Assessing", "Proposes an assessment.", "assessor", target="finding", batch=True),
         AgentTask("plan", "Planning", "Plans one item.", "planner", target="item"))
PERSON = (PersonOnly("decide", "Deciding", "A person decides."),)


class Server:
    PERSON_ONLY = PERSON
    name = "p"

    def __init__(self, path: Path):
        self.settings = AiSettings(path, TASKS)

    def ai(self) -> AiSettings:
        return self.settings

    @staticmethod
    def now() -> str:
        return "2026-10-27T12:00:00+00:00"

    @staticmethod
    def ai_prompt(task: str, target: str, agent: str) -> tuple[str, str]:
        if target == "missing":
            raise LookupError("No item missing")
        return f"{task} {target}".strip(), f"Do {task} on {target or 'everything'} (by {agent})"


class Routes(AiTaskRoutes):
    def __init__(self, server):
        self.server = server


SESSION = [
    {"role": "system", "content": "You are the assessor."},
    {"role": "user", "content": "Do assess on VULN-1"},
    {"role": "assistant", "content": "I read the finding first.",
     "tool_calls": [{"id": "c1", "function": {"name": "tracker_finding", "arguments": '{"id": "VULN-1"}'}}]},
    {"role": "tool", "name": "tracker_finding", "content": '{"status": "success", "finding": {"id": "VULN-1"}}'},
    {"role": "assistant", "content": "",
     "tool_calls": [{"id": "c2", "function": {"name": "tracker_assess", "arguments": '{"id": "VULN-1"}'}}]},
    {"role": "tool", "name": "tracker_assess", "content": '{"status": "error", "error": "not_affected needs a justification"}'},
    {"role": "assistant", "content": "VULN-1 proposed as affected: requests 2.31.0 is used."},
]


@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.setattr(agent_tasks, "START_WAIT", 0.2)
    # The shared TLS context costs ~0.2 s once per process (measured, cached after): built here, not inside the window
    httpx_verify(True)
    routes = Routes(Server(tmp_path / "plugin.db"))
    app = FastAPI()
    app.state.store = routes.server.settings
    app.state.runs, app.state.sessions, app.state.result = [], {}, {"status": "success", "summary": "Assessed."}
    app.get("/p/ai")(routes.get_ai)
    app.post("/p/ai/{task}")(routes.post_ai)
    app.post("/p/ai/{task}/run")(routes.post_ai_run)
    app.get("/p/ai/runs")(routes.get_ai_runs)
    app.get("/p/ai/runs/{session_id}")(routes.get_ai_run)
    app.post("/p/ai/runs/{session_id}/reply")(routes.post_ai_reply)

    @app.post("/run")
    async def run(request: Request):
        body = await request.json()
        app.state.runs.append({**body, "cookie": request.headers.get("cookie"), "other": request.headers.get("x-other")})
        if body["agent_name"] == "ghost":
            raise HTTPException(status_code=404, detail="Agent 'ghost' not found")
        if body["agent_name"] == "slow":
            app.state.release = asyncio.Event()
            await app.state.release.wait()
        return app.state.result

    @app.get("/api/sessions/{session_id}")
    async def session(session_id: str, request: Request):
        app.state.read_with = request.headers.get("cookie")
        if session_id not in app.state.sessions:
            raise HTTPException(status_code=404, detail="Session not found")
        return {"session_id": session_id, "messages": app.state.sessions[session_id]}

    return app


def test_every_task_starts_with_a_person_and_its_default_agent(tmp_path):
    settings = AiSettings(tmp_path / "x.db", TASKS)
    assert [(t["id"], t["mode"], t["agent"]) for t in settings.all()] == [("assess", "person", "assessor"),
                                                                         ("plan", "person", "planner")]


def test_the_choice_is_kept_with_who_made_it(tmp_path):
    settings = AiSettings(tmp_path / "x.db", TASKS)
    settings.set("assess", "ai", "other_agent", "2026-10-27T12:00:00+00:00", "alice")
    again = AiSettings(tmp_path / "x.db", TASKS).get("assess")
    assert (again["mode"], again["agent"], again["default_agent"], again["changed_by"]) == ("ai", "other_agent", "assessor", "alice")


def test_the_panel_routes_show_and_change_the_choice(app):
    client = TestClient(app)
    data = client.get("/p/ai").json()
    assert [t["id"] for t in data["tasks"]] == ["assess", "plan"] and data["person_only"][0]["id"] == "decide"
    assert client.post("/p/ai/assess", json={"mode": "ai"}).json()["changed_by"] == "local"
    assert client.post("/p/ai/assess", json={"mode": "robot"}).status_code == 422
    assert client.post("/p/ai/nothing", json={"mode": "ai"}).status_code == 404


def test_an_agent_starts_only_when_a_person_chose_it(app):
    client = TestClient(app)
    assert client.post("/p/ai/assess/run", json={"target": "VULN-1"}).status_code == 409
    assert app.state.runs == []


def test_the_run_goes_to_run_as_the_person_who_asked(app):
    client = TestClient(app)
    client.post("/p/ai/assess", json={"mode": "ai"})
    answer = client.post("/p/ai/assess/run", json={"target": "VULN-1"},
                         headers={"cookie": "session=abc", "x-other": "not passed on"})
    assert answer.status_code == 200 and answer.json()["state"] == "done" and answer.json()["agent"] == "assessor"
    run = app.state.runs[0]
    assert (run["agent_name"], run["task"], run["session_title"]) == ("assessor", "Do assess on VULN-1 (by assessor)", "assess VULN-1")
    assert run["session_id"] == answer.json()["session_id"]
    assert (run["cookie"], run["other"]) == ("session=abc", None)


def test_a_batch_task_runs_without_a_target_and_a_targeted_one_does_not(app):
    client = TestClient(app)
    client.post("/p/ai/assess", json={"mode": "ai"})
    client.post("/p/ai/plan", json={"mode": "ai"})
    assert client.post("/p/ai/assess/run", json={}).status_code == 200
    assert client.post("/p/ai/plan/run", json={}).status_code == 422
    assert client.post("/p/ai/plan/run", json={"target": "missing"}).status_code == 404


def test_a_run_refused_at_once_is_answered_as_that_refusal(app):
    client = TestClient(app)
    client.post("/p/ai/assess", json={"mode": "ai", "agent": "ghost"})
    answer = client.post("/p/ai/assess/run", json={"target": "VULN-1"})
    assert answer.status_code == 404 and "ghost could not start" in answer.json()["detail"]


# ── following a run in the panel ─────────────────────────────────────────────

def test_a_run_is_followed_in_the_panel_with_what_the_agent_said_and_did(app):
    client = TestClient(app)
    client.post("/p/ai/assess", json={"mode": "ai"})
    started = client.post("/p/ai/assess/run", json={"target": "VULN-1"}, headers={"cookie": "session=abc"}).json()
    app.state.sessions[started["session_id"]] = SESSION
    listed = client.get("/p/ai/runs").json()["runs"]
    assert [(r["session_id"], r["agent"], r["title"], r["state"]) for r in listed] == [
        (started["session_id"], "assessor", "assess VULN-1", "done")]
    run = client.get(f"/p/ai/runs/{started['session_id']}", headers={"cookie": "session=abc"}).json()
    assert app.state.read_with == "session=abc"           # read as the person may read it
    assert [s["kind"] for s in run["steps"]] == ["task", "text", "tool", "result", "tool", "error", "text"]
    assert run["steps"][2] == {"kind": "tool", "name": "tracker_finding", "text": '{"id": "VULN-1"}'}
    assert "justification" in run["steps"][5]["text"]
    assert run["answer"] == "VULN-1 proposed as affected: requests 2.31.0 is used."


def test_a_later_user_message_is_shown_as_the_persons_answer():
    shown = steps([{"role": "user", "content": "Do it"}, {"role": "assistant", "content": "Which one?"},
                   {"role": "user", "content": "VULN-1"}])
    assert [s["kind"] for s in shown] == ["task", "text", "reply"]


def test_the_steps_leave_the_system_prompt_out_and_cut_long_texts():
    shown = steps([{"role": "system", "content": "secret prompt"}, {"role": "assistant", "content": "x" * 9000}])
    assert shown == [{"kind": "text", "text": "x" * agent_tasks.MAX_TEXT}]


def test_a_run_still_running_is_shown_as_running_and_then_as_done(app):
    with TestClient(app) as client:                # one event loop for the whole test: the run goes on in it
        client.post("/p/ai/assess", json={"mode": "ai", "agent": "slow"})
        started = client.post("/p/ai/assess/run", json={"target": "VULN-1"}).json()
        assert started["state"] == "running"
        run = client.get(f"/p/ai/runs/{started['session_id']}").json()
        assert (run["state"], run["steps"], run["answer"]) == ("running", [], "")   # not saved yet: nothing to show
        app.state.sessions[started["session_id"]] = SESSION[:3]
        run = client.get(f"/p/ai/runs/{started['session_id']}").json()
        assert [s["kind"] for s in run["steps"]] == ["task", "text", "tool"] and run["answer"] == ""
        client.portal.call(app.state.release.set)
        for _ in range(200):
            if client.get("/p/ai/runs").json()["runs"][0]["state"] != "running":
                break
            time.sleep(0.01)
        run = client.get(f"/p/ai/runs/{started['session_id']}").json()
        assert (run["state"], run["answer"]) == ("done", "I read the finding first.")


@pytest.mark.parametrize("result, state, error", [
    ({"errors": ["LLM unreachable"]}, "failed", "LLM unreachable"),
    ({"refused": "permission", "errors": ["No LLM permission"]}, "refused", "No LLM permission"),
    ({"cancelled": True}, "cancelled", ""),
])
def test_a_run_that_ends_badly_says_how(app, result, state, error):
    app.state.result = result
    client = TestClient(app)
    client.post("/p/ai/assess", json={"mode": "ai"})
    started = client.post("/p/ai/assess/run", json={"target": "VULN-1"}).json()
    run = client.get(f"/p/ai/runs/{started['session_id']}").json()
    assert (run["state"], run["error"]) == (state, error)


def test_a_run_refused_at_once_is_kept_as_refused(app):
    client = TestClient(app)
    client.post("/p/ai/assess", json={"mode": "ai", "agent": "ghost"})
    client.post("/p/ai/assess/run", json={"target": "VULN-1"})
    run = client.get("/p/ai/runs").json()["runs"][0]
    assert (run["state"], run["error"]) == ("refused", "Agent 'ghost' not found")


OTHERS = {"session_id": "other", "task": "assess", "agent": "a", "title": "t", "who": "bob", "started": "2026-10-30T09:00:00+00:00",
          "ended": "2026-10-30T09:01:00+00:00", "state": "done", "error": "", "summary": "", "process": "x", "turns": 1}


def test_a_person_sees_only_their_own_runs(app):
    app.state.store.save_run(OTHERS)      # bob's; without auth the asking person is "local"
    client = TestClient(app)
    assert client.get("/p/ai/runs").json() == {"runs": []}
    assert client.get("/p/ai/runs/other").status_code == 404
    assert client.post("/p/ai/runs/other/reply", json={"text": "go on"}).status_code == 404


def test_the_runs_outlast_a_restart_and_one_cut_off_reads_as_interrupted(app, tmp_path, monkeypatch):
    client = TestClient(app)
    client.post("/p/ai/assess", json={"mode": "ai"})
    done = client.post("/p/ai/assess/run", json={"target": "VULN-1"}).json()["session_id"]
    app.state.store.save_run({**OTHERS, "session_id": "cut", "who": "local", "state": "running", "ended": None,
                              "process": agent_tasks.PROCESS, "started": "2026-10-30T11:00:00+00:00"})
    monkeypatch.setattr(agent_tasks, "PROCESS", "the-next-process")       # the server started again
    again = AiSettings(tmp_path / "plugin.db", TASKS)
    assert {r["session_id"]: r["state"] for r in again.runs("local")} == {done: "done", "cut": "interrupted"}
    assert again.run(done)["summary"] == "Assessed." and again.run(done)["turns"] == 1


def test_a_run_marked_running_that_no_run_of_this_process_holds_is_interrupted(app):
    app.state.store.save_run({**OTHERS, "who": "local", "state": "running", "process": agent_tasks.PROCESS})
    assert app.state.store.run("other")["state"] == "interrupted"


def test_a_person_answers_the_agent_and_the_same_session_goes_on(app):
    client = TestClient(app)
    client.post("/p/ai/assess", json={"mode": "ai"})
    started = client.post("/p/ai/assess/run", json={"target": "VULN-1"}, headers={"cookie": "session=abc"}).json()
    sid = started["session_id"]
    answered = client.post(f"/p/ai/runs/{sid}/reply", json={"text": "Take requests 2.32.0 as the fix."},
                           headers={"cookie": "session=abc"})
    assert answered.status_code == 200 and answered.json()["session_id"] == sid
    again = app.state.runs[-1]
    assert (again["session_id"], again["agent_name"], again["task"], again["cookie"]) == (
        sid, "assessor", "Take requests 2.32.0 as the fix.", "session=abc")
    run = client.get("/p/ai/runs").json()["runs"]
    assert len(run) == 1 and (run[0]["turns"], run[0]["state"]) == (2, "done")
    app.state.sessions[sid] = [*SESSION, {"role": "user", "content": "Take requests 2.32.0 as the fix."},
                               {"role": "assistant", "content": "Noted: fixed by requests 2.32.0."}]
    shown = client.get(f"/p/ai/runs/{sid}").json()
    assert [s["kind"] for s in shown["steps"]][-2:] == ["reply", "text"] and shown["answer"] == "Noted: fixed by requests 2.32.0."


def test_an_answer_waits_for_the_agent_to_stop_and_for_the_task_to_be_an_agents(app):
    with TestClient(app) as client:
        client.post("/p/ai/assess", json={"mode": "ai", "agent": "slow"})
        sid = client.post("/p/ai/assess/run", json={"target": "VULN-1"}).json()["session_id"]
        assert client.post(f"/p/ai/runs/{sid}/reply", json={"text": "more"}).status_code == 409   # still at work
        client.portal.call(app.state.release.set)
        for _ in range(200):
            if client.get("/p/ai/runs").json()["runs"][0]["state"] != "running":
                break
            time.sleep(0.01)
        client.post("/p/ai/assess", json={"mode": "person"})
        refused = client.post(f"/p/ai/runs/{sid}/reply", json={"text": "more"})
        assert refused.status_code == 409 and "person" in refused.json()["detail"]
        assert client.post(f"/p/ai/runs/{sid}/reply", json={"text": ""}).status_code == 422
