"""POST /run with files: the uploads reach the run through message_with_attachments.

What the helper does is tested on its own (tests/other/test_message_with_attachments.py);
this is the wiring in the API: which upload becomes which kind, which model is
asked, and a refusal answering 400 before any run starts.
"""
from __future__ import annotations

from types import SimpleNamespace

import httpx
import pytest

from agent_system import app_state
from agent_system.llm.models import ChatMessage
from agent_system.servers.agent.server import Agent
from agent_system.services.session_manager import SessionManager
from agent_system.services.session_service import SessionService

pytestmark = pytest.mark.anyio


@pytest.fixture
def api(tmp_path, monkeypatch):
    from agent_system import app as app_mod
    from agent_system.config.models import AuthConfig

    class _Off:
        def __get__(self, obj, objtype=None):
            return False

        def __set__(self, obj, value):
            pass

    monkeypatch.setattr(AuthConfig, "enabled", _Off(), raising=False)
    monkeypatch.setenv("AGENT_SESSION_STORAGE_PATH", str(tmp_path))
    app = app_mod.build_app()
    monkeypatch.setattr(app_state, "session_service", SessionService(SessionManager(storage_path=str(tmp_path))))
    tasks = []

    async def run_events(self, task, request_id=None, session_id=None, **kwargs):
        tasks.append(task)
        yield {"type": "final", "summary": "done"}
        yield {"type": "end"}

    monkeypatch.setattr(Agent, "run_events", run_events)
    return SimpleNamespace(app=app, tasks=tasks)


async def _post(app, files):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post("/run", data={"task": "read this"}, files=files, timeout=60.0)
        return response, response.text


async def test_a_text_upload_reaches_the_run_inside_the_message(api):
    response, body = await _post(api.app, {"files": ("notes.md", b"the notes body", "text/markdown")})

    assert response.status_code == 200, body
    assert len(api.tasks) == 1, "the run never started"
    task = api.tasks[0]
    assert isinstance(task, ChatMessage), f"the upload never made it into the message: {task!r}"
    flat = " ".join(str(getattr(part, "content", "") or getattr(part, "text", "") or "") for part in task.content)
    assert "the notes body" in flat and "read this" in flat, flat


async def test_a_model_that_cannot_take_it_answers_400_before_the_run(api, monkeypatch):
    asked = []

    def ensure(model, *, images=0, audio=0, video=0):
        asked.append((model, images, audio))
        return f"{model} cannot see images" if images else None
    monkeypatch.setattr("agent_system.llm.capabilities.ensure_model_supports", ensure)

    response, body = await _post(api.app, {"files": ("pic.png", b"\x89PNG not really", "image/png")})

    assert response.status_code == 400, body
    assert "cannot see images" in body, body
    assert asked and asked[0][1:] == (1, 0), f"the image was not counted as one: {asked}"
    assert asked[0][0], "no model was asked -- the agent never reached the gate"
    assert not api.tasks, "a refused upload still started the run"


async def test_no_task_and_nothing_usable_answers_400(api):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api.app), base_url="http://test") as client:
        response = await client.post("/run", files={"files": ("blob.bin", b"\x00\x01", "application/octet-stream")},
                                     timeout=60.0)

    assert response.status_code == 400, response.text
    assert "Missing 'task'" in response.text
    assert not api.tasks, "a run started with nothing to do"


async def test_an_unreadable_file_answers_400_not_500(api):
    response, body = await _post(api.app, {"files": ("pic.png", b"not an image at all", "image/png")})

    assert response.status_code == 400, body
    assert "Cannot open image" in body, body
    assert not api.tasks
