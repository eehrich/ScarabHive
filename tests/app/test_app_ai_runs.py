"""AI tasks of a panel against the real app: an agent started from the panel runs on the real /run, its session is
kept by the real SessionService, and the panel reads it back through the real /api/sessions -- what the person sees
in the side sheet "Agenten-Läufe". Only the agent's thinking is a stand-in: Agent.run_events writes a task, a tool
call with its answer and a reply into the session's conversation, as a real run does, and ends.

- the run is followed: state done, the task, the tool call, what it answered, the agent's words, the answer;
- a person answers it: the same session goes on, the agent sees what went before, the reply is shown as theirs;
- the runs are kept in the plugin's file: a store opened again (a restart) still lists them.
"""
from __future__ import annotations

from types import SimpleNamespace

import httpx
import pytest

from agent_system import app_state
from agent_system.llm.models import ChatMessage
from agent_system.plugins.agent_tasks import AgentTask, AiSettings, AiTaskRoutes
from agent_system.servers.agent.server import Agent
from agent_system.services.session_manager import SessionManager
from agent_system.services.session_service import SessionService

pytestmark = pytest.mark.anyio


class Plugin:
    PERSON_ONLY = ()
    name = "probe"

    def __init__(self, settings: AiSettings):
        self.settings = settings

    def ai(self) -> AiSettings:
        return self.settings

    @staticmethod
    def now() -> str:
        return "2026-10-30T12:00:00+00:00"

    @staticmethod
    def ai_prompt(task: str, target: str, agent: str) -> tuple[str, str]:
        return f"Assess {target}", f"Assess the finding {target}."


class Routes(AiTaskRoutes):
    def __init__(self, server):
        self.server = server


@pytest.fixture
def api(tmp_path, monkeypatch):
    from agent_system import app as app_mod
    from agent_system.config.models import AuthConfig
    from agent_system.plugins import agent_tasks

    class _Off:
        def __get__(self, obj, objtype=None):
            return False

        def __set__(self, obj, value):
            pass

    monkeypatch.setattr(AuthConfig, "enabled", _Off(), raising=False)
    monkeypatch.setenv("AGENT_SESSION_STORAGE_PATH", str(tmp_path / "sessions"))
    monkeypatch.setattr(agent_tasks, "START_WAIT", 10.0)       # the stand-in run ends at once: wait for it
    app = app_mod.build_app()
    manager = SessionManager(storage_path=str(tmp_path / "sessions"))
    monkeypatch.setattr(app_state, "session_service", SessionService(manager))
    app.state.session_manager = manager        # what the app's startup sets: /run and /api/sessions share it
    # /api/sessions comes with the sign-in (auth.enabled, as this installation runs it); the chat reads it too
    from agent_system.api.session_endpoints import session_router
    app.include_router(session_router)
    seen = {}

    async def run_events(self, task, request_id=None, session_id=None, **kwargs):
        before = list(self._session_tracker.get_session_messages(session_id))
        seen[len(seen)] = [m.content for m in before]
        text = task if isinstance(task, str) else task.content
        if not before:
            added = [ChatMessage(role="user", content=text),
                     ChatMessage(role="assistant", content="I read the finding first.",
                                 tool_calls=[{"id": "c1", "type": "function",
                                              "function": {"name": "tracker_finding", "arguments": '{"id": "VULN-1"}'}}]),
                     ChatMessage(role="tool", name="tracker_finding", tool_call_id="c1",
                                 content='{"status": "success", "finding": {"id": "VULN-1"}}'),
                     ChatMessage(role="assistant", content="VULN-1: affected, requests 2.31.0 is used. Which fix?")]
        else:
            added = [ChatMessage(role="user", content=text), ChatMessage(role="assistant", content="Noted: 2.32.0 fixes it.")]
        self._session_tracker.set_session_messages(session_id, before + added)
        yield {"type": "final", "summary": added[-1].content}
        yield {"type": "end"}

    monkeypatch.setattr(Agent, "run_events", run_events)
    agent = app.state.agent.name
    settings = AiSettings(tmp_path / "probe.db", (AgentTask("assess", "Assessing", "Proposes an assessment.", agent,
                                                            target="finding"),))
    routes = Routes(Plugin(settings))
    app.post("/plugins/probe/ai/{task}")(routes.post_ai)
    app.post("/plugins/probe/ai/{task}/run")(routes.post_ai_run)
    app.get("/plugins/probe/ai/runs")(routes.get_ai_runs)
    app.get("/plugins/probe/ai/runs/{session_id}")(routes.get_ai_run)
    app.post("/plugins/probe/ai/runs/{session_id}/reply")(routes.post_ai_reply)
    return SimpleNamespace(app=app, seen=seen, agent=agent, path=tmp_path / "probe.db")


def client(app) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test", timeout=30)


async def test_a_run_started_in_the_panel_is_read_back_from_its_real_session_and_answered_there(api):
    async with client(api.app) as http:
        assert (await http.post("/plugins/probe/ai/assess", json={"mode": "ai"})).status_code == 200
        started = (await http.post("/plugins/probe/ai/assess/run", json={"target": "VULN-1"})).json()
        sid = started["session_id"]
        assert started["state"] == "done", started

        run = (await http.get(f"/plugins/probe/ai/runs/{sid}")).json()
        assert (run["state"], run["agent"], run["title"], run["turns"]) == ("done", api.agent, "Assess VULN-1", 1)
        assert [s["kind"] for s in run["steps"]] == ["task", "text", "tool", "result", "text"]
        assert run["steps"][0]["text"] == "Assess the finding VULN-1."
        assert run["steps"][2] == {"kind": "tool", "name": "tracker_finding", "text": '{"id": "VULN-1"}'}
        assert run["answer"] == "VULN-1: affected, requests 2.31.0 is used. Which fix?"

        answered = await http.post(f"/plugins/probe/ai/runs/{sid}/reply", json={"text": "Take requests 2.32.0."})
        assert answered.status_code == 200 and answered.json()["session_id"] == sid
        # the second turn ran on the same session and saw the first
        assert api.seen[1][0] == "Assess the finding VULN-1." and len(api.seen[1]) == 4
        run = (await http.get(f"/plugins/probe/ai/runs/{sid}")).json()
        assert [s["kind"] for s in run["steps"]][-2:] == ["reply", "text"]
        assert (run["turns"], run["answer"]) == (2, "Noted: 2.32.0 fixes it.")
        listed = (await http.get("/plugins/probe/ai/runs")).json()["runs"]
        assert [r["session_id"] for r in listed] == [sid]

    reopened = AiSettings(api.path, ())
    assert [(r["session_id"], r["state"], r["turns"]) for r in reopened.runs("local")] == [(sid, "done", 2)]
