"""The answer route as the app mounts it answers the instance the agents call.

The activation chain fails silently: a route mounted on another instance than
the one in the registry would take every answer ("ok") for a broker nobody
waits on. Builds the real app on a configuration of its own -- this plugin,
authentication off -- runs its startup (where plugin routes are mounted), opens
a question on the registry's instance and answers it over HTTP.
"""
from __future__ import annotations

from pathlib import Path

import yaml
from fastapi.testclient import TestClient

from agent_system.app import build_app
from plugins.ask_user.questions import UserAnswer, UserQuestion
from plugins.ask_user.server import AskUserServer

PLUGINS = Path(__file__).resolve().parents[2]


def _config(tmp_path: Path) -> Path:
    config = {
        "auth": {"enabled": False},
        "plugins": {
            "plugin_dirs": [str(PLUGINS)],
            "default_config": {"type": "agent", "enabled": True,
                               "agent_config": {"llm_profile": "normal", "tools": {}}},
            "servers": {"ask_user": {"type": "ask_user", "enabled": True, "config": {"ask_timeout": 30}}},
        },
        "network": {"host": "127.0.0.1", "port": 8000, "ssl_verify": True, "disable_cache": False},
        "logging": {"enabled": True, "level": "INFO"},
        "llm_system": {"models": {"gpt-4": {"provider": "openai", "model": "gpt-4"}},
                       "profiles": {"normal": {"model_ref": "gpt-4"}}, "default_profile": "normal"},
    }
    path = tmp_path / "config" / "config.yaml"
    path.parent.mkdir()
    path.write_text(yaml.safe_dump(config), encoding="utf-8")
    return path


def test_the_mounted_route_answers_the_registrys_instance(tmp_path, monkeypatch):
    path = _config(tmp_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("AGENT_SESSION_STORAGE_PATH", str(tmp_path / "sessions"))
    app = build_app(str(path))

    with TestClient(app) as client:
        server = app.state.tool_registry.get("ask_user")
        assert isinstance(server, AskUserServer), f"the registry holds {type(server).__name__}"

        async def ask():
            return server.broker.open_question(
                UserQuestion, owner="alice", session_id="s", request_id="mounted1_001",
                agent_name="coder", timeout=30, question="Which database?", options=("Postgres", "SQLite"))

        question = client.portal.call(ask)   # on the loop the app serves its routes on
        try:
            listed = client.get("/plugins/ask_user/pending?request_id=mounted1")
            answered = client.post("/plugins/ask_user/answer",
                                   json={"question_id": question.id, "choices": ["SQLite"]})
        finally:
            client.portal.call(lambda: server.broker.close(question))

    assert listed.status_code == 200, listed.text
    assert [q["id"] for q in listed.json()["questions"]] == [question.id]
    assert answered.status_code == 200, answered.text
    assert question.answer.done() and not question.answer.cancelled(), "the answer went to another broker"
    assert question.answer.result() == UserAnswer(choices=("SQLite",), text="", answered_by="anonymous")
