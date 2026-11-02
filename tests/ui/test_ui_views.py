"""The two views of the shell (ui/views.py, the routes in ui/routes.py): admins see everything and may switch to the
simple view; every other role sees the simple view, whose panels and topics the admins set."""
from __future__ import annotations

import json

from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from agent_system.plugins import web_adapter
from agent_system.ui.routes import router
from agent_system.ui.views import DEFAULT_TOPICS, ViewError, ViewStore, check_topics
from test_catalog import auth, registered, spec  # noqa: F401  (fixture)

TOPIC = {"id": "tax", "label": "Tax question", "hint": "", "agent": "tax_advisor", "quick": "", "deep": "think"}


@pytest.fixture
def app(tmp_path):
    app = FastAPI()
    app.include_router(router)
    app.state.config = auth(False)
    app.state.ui_views = ViewStore(tmp_path / "ui" / "views.json")
    return app


@pytest.fixture
def as_user(app, monkeypatch):
    """Authentication on; as_user(role, name) is who asks next -- with every client, until the next call. Never the
    real data/users.db."""
    import agent_system.auth.database as database
    import agent_system.auth.dependencies as dependencies

    viewer = {"user": None}

    async def current_viewer(request, credentials, api_key, db):
        return viewer["user"]

    monkeypatch.setattr(dependencies, "get_optional_user", current_viewer)
    monkeypatch.setattr(database, "get_db", lambda: None)
    app.state.config = auth(True, default_min_role="user", plugin_overrides={"users": {"min_role": "admin"}})
    web_adapter.init_plugin_security(app.state.config.auth)

    def who(role, name="alice"):
        viewer["user"] = SimpleNamespace(role=SimpleNamespace(value=role), is_active=True, username=name)
        return TestClient(app)
    return who


def test_a_user_always_gets_the_simple_view_with_the_default_panels_there_are(app, registered, as_user):
    assert "todo" not in as_user("user", "anna").get("/api/ui/view").json()["panels"], \
        "a default that is not installed is not listed"
    registered("todo", {"panel": spec(endpoint="/plugins/todo/", contexts={})})
    registered("message_debugger", {"panel": spec(endpoint="/plugins/message_debugger/", contexts={})})
    view = as_user("user", "anna").get("/api/ui/view").json()
    assert (view["role"], view["mode"], view["can_switch"]) == ("user", "simple", False)
    assert "todo" in view["panels"] and "message_debugger" not in view["panels"]
    assert [t["id"] for t in view["topics"]] == [t["id"] for t in DEFAULT_TOPICS]


def test_an_admin_sees_everything_until_they_choose_the_simple_view_and_back(app, as_user):
    admin = as_user("admin")
    assert admin.get("/api/ui/view").json() | {"topics": None} == {"role": "admin", "mode": "admin", "can_switch": True,
                                                                  "panels": None, "topics": None}
    assert admin.put("/api/ui/view/mode", json={"mode": "simple"}).json() == {"mode": "simple"}
    simple = admin.get("/api/ui/view").json()
    assert simple["mode"] == "simple" and isinstance(simple["panels"], list)
    assert as_user("admin", "other").get("/api/ui/view").json()["mode"] == "admin", "the choice is each admin's own"
    as_user("admin").put("/api/ui/view/mode", json={"mode": "admin"})
    assert as_user("admin").get("/api/ui/view").json()["panels"] is None


def test_only_an_admin_may_switch_or_set_the_user_view(app, as_user):
    user = as_user("user", "anna")
    assert user.put("/api/ui/view/mode", json={"mode": "admin"}).status_code == 403
    assert user.get("/api/ui/user-view").status_code == 403
    assert user.put("/api/ui/user-view", json={"panels": [], "topics": [TOPIC]}).status_code == 403
    assert user.get("/ui/panels/user_view").status_code == 403
    assert as_user("admin").put("/api/ui/view/mode", json={"mode": "everything"}).status_code == 422


def test_the_admin_sets_the_panels_and_topics_users_get(app, registered, as_user):
    registered("tracker", {"panel": spec(endpoint="/plugins/tracker/", contexts={})})
    registered("users", {"panel": spec(endpoint="/plugins/users/", contexts={})})
    admin = as_user("admin")
    offered = admin.get("/api/ui/user-view").json()
    ids = {panel["id"] for panel in offered["available"]}
    assert "tracker" in ids and "users" not in ids, "only what a user may open can be shown to users"
    assert offered["chosen"] is False

    saved = admin.put("/api/ui/user-view", json={"panels": ["help", "tracker", "help"], "topics": [TOPIC]}).json()
    assert saved["panels"] == ["help", "tracker"] and saved["chosen"] is True
    view = as_user("user", "anna").get("/api/ui/view").json()
    assert view["panels"] == ["help", "tracker"] and view["topics"] == [TOPIC]

    refused = as_user("admin").put("/api/ui/user-view", json={"panels": ["users"], "topics": [TOPIC]})
    assert refused.status_code == 422 and "users" in refused.json()["detail"]


def test_without_authentication_the_owner_is_an_admin(app):
    client = TestClient(app)
    assert client.get("/api/ui/view").json()["mode"] == "admin"
    assert client.get("/ui/panels/user_view").status_code == 200
    assert "user_view" in {p["id"] for p in client.get("/api/ui/catalog").json()["panels"]}


@pytest.mark.parametrize("topics, complaint", [
    ([], "1 to 8"),
    ([TOPIC] * 9, "1 to 8"),
    ([TOPIC, TOPIC], "unique"),
    ([{**TOPIC, "id": "Bad Id"}], "lower case"),
    ([{**TOPIC, "label": ""}], "label of 1 to 40"),
    ([{**TOPIC, "agent": "rm -rf /"}], "no agent or profile name"),
    (["tax"], "object"),
])
def test_a_topic_that_cannot_be_kept_is_refused_and_says_why(topics, complaint):
    with pytest.raises(ViewError, match=complaint):
        check_topics(topics)


def test_a_broken_file_gives_the_defaults_until_the_next_save_and_broken_topics_fall_back(tmp_path):
    path = tmp_path / "views.json"
    path.write_text("{not json", encoding="utf-8")
    store = ViewStore(path)
    assert store.user_view(["help"])["panels"] == ["help"] and store.mode_of("alice") == "admin"
    store.set_mode("alice", "simple")
    assert ViewStore(path).mode_of("alice") == "simple", "the save wrote it whole"
    for shape in ('{"modes": "x"}', '{"user_view": "x"}'):
        path.write_text(shape, encoding="utf-8")
        assert store.mode_of("alice") == "admin" and store.user_view(["help"])["panels"] == ["help"]
        store.set_mode("alice", "simple")
        assert store.mode_of("alice") == "simple"
    path.write_text('{"user_view": {"panels": ["help"], "topics": "nonsense"}}', encoding="utf-8")
    assert ViewStore(path).user_view(["help"])["topics"][0]["id"] == DEFAULT_TOPICS[0]["id"]


@pytest.mark.parametrize("broken", ["{one stray comma,", "[1, 2]"])
def test_a_save_over_a_broken_file_keeps_it_beside_it(tmp_path, broken):
    path = tmp_path / "views.json"
    path.write_text(broken, encoding="utf-8")
    store = ViewStore(path)
    kept = lambda: sorted(p.read_text(encoding="utf-8") for p in tmp_path.glob("views.json.broken-*"))  # noqa: E731
    store.mode_of("alice")
    assert path.read_text(encoding="utf-8") == broken and kept() == [], "reading alone leaves it"
    store.set_mode("alice", "simple")
    assert kept() == [broken] and store.mode_of("alice") == "simple"
    path.write_text(f"{broken} ", encoding="utf-8")   # the next slip
    store.set_user_view(["help"], [dict(DEFAULT_TOPICS[0])], ["help"], "alice")
    assert kept() == sorted([broken, f"{broken} "]), "each keeps its own copy"
    store.set_mode("alice", "admin")
    assert store.user_view(["help"])["chosen"] and len(kept()) == 2, "a whole file is not set aside"


def test_two_saves_at_once_leave_a_whole_file_on_the_apps_one_store(app, as_user, tmp_path):
    """One store per app: its lock keeps saves apart. A store per request locked nothing -- measured: a corrupt file."""
    app.state.ui_views = None
    from agent_system import paths
    import threading

    original = paths.data_path
    paths.data_path = lambda *parts: tmp_path.joinpath(*parts)
    try:
        client = as_user("admin", "alice")
        client.get("/api/ui/view")
        store = app.state.ui_views
        assert store is not None and client.get("/api/ui/view") and app.state.ui_views is store, "the same store again"
        failed = []

        def saves(name):
            try:
                for mode in ("simple", "admin") * 20:
                    store.set_mode(name, mode)
            except Exception as error:  # noqa: BLE001 - what the test counts
                failed.append(error)

        threads = [threading.Thread(target=saves, args=(f"u{i}",)) for i in range(4)]
        [t.start() for t in threads]
        [t.join() for t in threads]
        assert failed == [] and json.loads(store.path.read_text(encoding="utf-8"))["modes"]["u0"] == "admin"
        assert [p.name for p in store.path.parent.iterdir()] == ["views.json"], "no temporary file stays behind"
    finally:
        paths.data_path = original


def test_a_topic_on_an_agent_the_viewer_may_not_run_is_not_told(app, as_user, monkeypatch):
    from agent_system.ui import routes

    monkeypatch.setattr(routes, "_agent_gates", lambda: {"tax_advisor": "admin"})
    admin = as_user("admin")
    assert admin.put("/api/ui/user-view", json={"panels": [], "topics": [TOPIC, {**TOPIC, "id": "open", "agent": ""}]}).status_code == 200
    assert admin.get("/api/ui/user-view").json()["not_for_users"] == ["tax_advisor"]
    assert [t["id"] for t in admin.get("/api/ui/view").json()["topics"]] == ["tax", "open"]
    assert [t["id"] for t in as_user("user", "anna").get("/api/ui/view").json()["topics"]] == ["open"]
