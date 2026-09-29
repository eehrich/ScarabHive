"""Who may read and change a session's task list: a user her own sessions', an admin all, everyone while
authentication is off. The panel listed, started, completed and deleted the tasks of any session whose id it was
given."""
import asyncio
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from tests.session_owners import admin, two_users, user, viewed_by

BASE = "/plugins/todo"


@pytest.fixture
def served(tmp_path, monkeypatch):
    from plugins.todo.plugin import PLUGIN_FACTORY

    two_users(tmp_path, monkeypatch)
    plugin = PLUGIN_FACTORY("todo", {}, SimpleNamespace(storage_path=str(tmp_path / "todos")))
    task_ids = {}
    for session_id in ("s-alice", "s-bob"):
        created = asyncio.run(plugin.server.create_todo(title=f"a task of {session_id}",
                                                        context={"session_id": session_id}))
        task_ids[session_id] = created["task_id"]
    app = FastAPI()
    app.include_router(plugin.get_web_router())
    return TestClient(app), viewed_by(app), app, task_ids


def _tasks(client, session_id):
    return [(task["title"], task["status"])
            for task in client.get(f"{BASE}/tasks?session_id={session_id}").json()["tasks"]]


def test_her_own_session_s_tasks_and_not_another_users(served):
    client, viewer, _app, _ids = served
    viewer["user"] = user("alice")

    assert _tasks(client, "s-alice") == [("a task of s-alice", "not-started")]
    assert _tasks(client, "s-bob") == [], "another user's tasks were listed"
    # answered as a session that does not exist is, so the answer does not say whether it does
    foreign = client.get(f"{BASE}/tasks?session_id=s-bob").json()
    viewer["user"] = admin()
    assert foreign == client.get(f"{BASE}/tasks?session_id=s-nobody").json()


@pytest.mark.parametrize("change", ["start", "complete", "delete"])
def test_another_users_task_is_not_changed(served, change):
    client, viewer, _app, ids = served
    viewer["user"] = user("alice")

    def ask(task_id):
        if change == "delete":
            return client.delete(f"{BASE}/tasks/{task_id}?session_id=s-bob")
        return client.post(f"{BASE}/tasks/{task_id}/{change}?session_id=s-bob")

    refused = ask(ids["s-bob"])

    assert refused.status_code == 404
    viewer["user"] = admin()
    assert refused.json()["detail"] == ask("task_that_is_not_there").json()["detail"].replace(
        "task_that_is_not_there", ids["s-bob"]), "answered otherwise than a task that is not there"
    assert _tasks(client, "s-bob") == [("a task of s-bob", "not-started")], f"bob's task was {change}d"


def test_her_own_task_is_changed(served):
    client, viewer, _app, ids = served
    viewer["user"] = user("alice")

    assert client.post(f"{BASE}/tasks/{ids['s-alice']}/start?session_id=s-alice").status_code == 200
    assert client.post(f"{BASE}/tasks/{ids['s-alice']}/complete?session_id=s-alice").status_code == 200
    assert _tasks(client, "s-alice") == [("a task of s-alice", "completed")]
    assert client.delete(f"{BASE}/tasks/{ids['s-alice']}?session_id=s-alice").status_code == 200
    assert _tasks(client, "s-alice") == []


def test_an_admin_reads_and_changes_every_session_s(served):
    client, viewer, _app, ids = served
    viewer["user"] = admin()

    assert _tasks(client, "s-bob") == [("a task of s-bob", "not-started")]
    assert client.post(f"{BASE}/tasks/{ids['s-bob']}/start?session_id=s-bob").status_code == 200
    assert _tasks(client, "s-bob") == [("a task of s-bob", "in-progress")]


def test_with_authentication_off_every_session_is_the_one_person_s(served):
    client, viewer, app, ids = served
    app.state.config = SimpleNamespace(auth=SimpleNamespace(enabled=False))
    viewer["user"] = None

    assert _tasks(client, "s-bob") == [("a task of s-bob", "not-started")]
    assert client.delete(f"{BASE}/tasks/{ids['s-bob']}?session_id=s-bob").status_code == 200


def test_nobody_signed_in_sees_no_users_tasks(served):
    """With authentication on, a request nobody signed in to is the viewer "anonymous": not alice."""
    client, viewer, _app, ids = served
    viewer["user"] = None

    assert _tasks(client, "s-alice") == []
    assert client.delete(f"{BASE}/tasks/{ids['s-alice']}?session_id=s-alice").status_code == 404
