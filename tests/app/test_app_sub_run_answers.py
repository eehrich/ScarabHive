"""The stream carries every answer as the model wrote it -- a sub-agent's, relayed as `sub_run`, too.

The chat draws the Markdown in the browser (static/js/chat_module.js); the API renders nothing, so an
API client (n8n, a script, the OpenAI-compatible endpoint) gets the text the model wrote, and the chat
gets the same for the caller's answer and for its sub-agent's.

Drives the real build_app over ASGI; faked is the agent's run.
"""
from __future__ import annotations

import json

import httpx
import pytest

from agent_system.servers.agent.server import Agent

pytestmark = pytest.mark.anyio

ANSWER = "Icons **found**:\n\n- one\n- two\n\n<b>raw</b> & more"


def _disable_auth(monkeypatch):
    from agent_system.config.models import AuthConfig

    class _DisabledAuth:
        def __get__(self, obj, objtype=None):
            return False

        def __set__(self, obj, value):
            pass

    monkeypatch.setattr(AuthConfig, "enabled", _DisabledAuth(), raising=False)


def _events(body: str) -> list[dict]:
    return [json.loads(line[len("data: "):]) for line in body.splitlines() if line.startswith("data: ")]


async def test_every_answer_in_the_stream_is_the_text_the_model_wrote(tmp_path, monkeypatch):
    from agent_system import app as app_mod

    _disable_auth(monkeypatch)
    monkeypatch.setenv("AGENT_SESSION_STORAGE_PATH", str(tmp_path))

    async def run_events(self, task, request_id=None, session_id=None, **kwargs):
        def relayed(inner):
            return {"type": "sub_run", "run_id": f"{request_id}_003_async_x1", "spawned_by": f"{request_id}_003",
                    "depth_level": 2, "agent": "helper", "event": inner}

        yield relayed({"type": "thinking_complete", "step": 1,
                       "assistant": {"role": "assistant", "content": ANSWER}})
        yield relayed({"type": "final", "summary": ANSWER})
        yield {"type": "thinking_complete", "step": 1, "assistant": {"role": "assistant", "content": ANSWER}}
        yield {"type": "final", "summary": ANSWER}
        yield {"type": "end"}

    monkeypatch.setattr(Agent, "run_events", run_events)
    app = app_mod.build_app()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/events", params={"task": "go"}, timeout=60.0)
    assert response.status_code == 200, response.text
    events = _events(response.text)

    [own_final] = [e for e in events if e["type"] == "final"]
    [own_step] = [e for e in events if e["type"] == "thinking_complete"]
    inner = {e["event"]["type"]: e["event"] for e in events if e["type"] == "sub_run"}
    assert set(inner) == {"thinking_complete", "final"}, events

    assert [own_final["summary"], own_step["assistant"]["content"],
            inner["final"]["summary"], inner["thinking_complete"]["assistant"]["content"]] == [ANSWER] * 4


async def test_post_run_answers_with_the_text_the_model_wrote(tmp_path, monkeypatch):
    from agent_system import app as app_mod

    _disable_auth(monkeypatch)
    monkeypatch.setenv("AGENT_SESSION_STORAGE_PATH", str(tmp_path))

    async def run_events(self, task, request_id=None, session_id=None, **kwargs):
        yield {"type": "final", "summary": ANSWER}
        yield {"type": "end"}

    monkeypatch.setattr(Agent, "run_events", run_events)
    app = app_mod.build_app()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post("/run", json={"task": "go"}, timeout=60.0)
    assert response.status_code == 200, response.text
    assert response.json()["summary"] == ANSWER
