"""A sub-agent's answer, relayed as `sub_run`, reaches the browser formatted like its caller's.

The API renders a run's answer to HTML on its way out (the format_output hooks):
a final's summary, a finished step's content. A sub-agent's answer comes wrapped
in a `sub_run` envelope and was passed through as it was -- markdown in the chat
next to its caller's rendered answer.

Drives the real build_app over ASGI. Faked are the agent's run and the one
boundary behind the formatting -- the hooks themselves, which a test app does not
load: a fixed formatter stands in for them. The same text goes out once as the
caller's answer and once inside the envelope, so the test does not depend on what
the formatter makes of it -- only that it is applied to both.
"""
from __future__ import annotations

import json

import httpx
import pytest

from agent_system.servers.agent.components.hook_integration import HookIntegrationManager
from agent_system.servers.agent.server import Agent

pytestmark = pytest.mark.anyio

ANSWER = "Icons **found**:\n\n- one\n- two"


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


async def test_a_relayed_answer_is_formatted_like_the_callers(tmp_path, monkeypatch):
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

    async def format_output(self, output, request_id=None, session_id=None, output_format="text"):
        return f"<rendered>{output}</rendered>", "html"

    monkeypatch.setattr(Agent, "run_events", run_events)
    monkeypatch.setattr(HookIntegrationManager, "execute_format_output_hooks", format_output)
    app = app_mod.build_app()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/events", params={"task": "go"}, timeout=60.0)
    assert response.status_code == 200, response.text
    events = _events(response.text)

    [own_final] = [e for e in events if e["type"] == "final"]
    [own_step] = [e for e in events if e["type"] == "thinking_complete"]
    inner = {e["event"]["type"]: e["event"] for e in events if e["type"] == "sub_run"}
    assert set(inner) == {"thinking_complete", "final"}, events

    assert own_final["summary"] != ANSWER, "fixture: the formatter changed nothing, so nothing is compared"
    assert (inner["final"]["summary"], inner["final"].get("content_format")) == \
        (own_final["summary"], own_final.get("content_format"))
    assert (inner["thinking_complete"]["assistant"]["content"], inner["thinking_complete"].get("content_format")) == \
        (own_step["assistant"]["content"], own_step.get("content_format"))
