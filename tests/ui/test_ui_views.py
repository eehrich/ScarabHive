"""The views (ui/views.py, the routes in ui/routes.py): admins are offered every panel, everyone else the panels the
admins released; a panel with two views starts simple for everyone, and only admins may switch."""
from __future__ import annotations

import json

from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from agent_system.plugins import web_adapter
from agent_system.ui.routes import router
from agent_system.ui.views import ViewStore
from test_catalog import auth, registered, spec  # noqa: F401  (fixture)


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


def test_a_user_is_offered_the_default_panels_there_are_and_always_the_simple_view(app, registered, as_user):
    assert "todo" not in as_user("user", "anna").get("/api/ui/view").json()["panels"], \
        "a default that is not installed is not offered"
    registered("todo", {"panel": spec(endpoint="/plugins/todo/", contexts={})})
    registered("message_debugger", {"panel": spec(endpoint="/plugins/message_debugger/", contexts={})})
    view = as_user("user", "anna").get("/api/ui/view").json()
    assert (view["role"], view["mode"], view["can_switch"]) == ("user", "simple", False)
    assert "todo" in view["panels"] and "message_debugger" not in view["panels"]
    assert "topics" not in view


def test_an_admin_is_offered_every_panel_starts_simple_and_switches_and_back(app, as_user):
    admin = as_user("admin")
    assert admin.get("/api/ui/view").json() == {"role": "admin", "mode": "simple", "can_switch": True, "panels": None}
    assert admin.put("/api/ui/view/mode", json={"mode": "admin"}).json() == {"mode": "admin"}
    assert as_user("admin").get("/api/ui/view").json()["mode"] == "admin"
    assert as_user("admin", "other").get("/api/ui/view").json()["mode"] == "simple", "the choice is each admin's own"
    as_user("admin").put("/api/ui/view/mode", json={"mode": "simple"})
    assert as_user("admin").get("/api/ui/view").json() == {"role": "admin", "mode": "simple", "can_switch": True,
                                                           "panels": None}


def test_only_an_admin_may_switch_or_release_panels(app, as_user):
    user = as_user("user", "anna")
    assert user.put("/api/ui/view/mode", json={"mode": "admin"}).status_code == 403
    assert user.get("/api/ui/user-view").status_code == 403
    assert user.put("/api/ui/user-view", json={"panels": []}).status_code == 403
    assert user.get("/ui/panels/user_view").status_code == 403
    assert as_user("admin").put("/api/ui/view/mode", json={"mode": "everything"}).status_code == 422


def test_the_admin_releases_the_panels_users_are_offered(app, registered, as_user):
    registered("tracker", {"panel": spec(endpoint="/plugins/tracker/", contexts={})})
    registered("users", {"panel": spec(endpoint="/plugins/users/", contexts={})})
    admin = as_user("admin")
    offered = admin.get("/api/ui/user-view").json()
    ids = {panel["id"] for panel in offered["available"]}
    assert "tracker" in ids and "users" not in ids, "only what a user may open can be released"
    assert offered["chosen"] is False

    saved = admin.put("/api/ui/user-view", json={"panels": ["help", "tracker", "help"]}).json()
    assert {"panels": saved["panels"], "chosen": saved["chosen"]} == {"panels": ["help", "tracker"], "chosen": True}
    assert "topics" not in saved
    assert as_user("user", "anna").get("/api/ui/view").json()["panels"] == ["help", "tracker"]

    refused = as_user("admin").put("/api/ui/user-view", json={"panels": ["users"]})
    assert refused.status_code == 422 and "users" in refused.json()["detail"]
    assert as_user("admin").put("/api/ui/user-view", json={"panels": "help"}).status_code == 422


def test_without_authentication_the_owner_is_an_admin_who_starts_simple(app):
    client = TestClient(app)
    assert client.get("/api/ui/view").json() == {"role": "admin", "mode": "simple", "can_switch": True, "panels": None}
    assert client.get("/ui/panels/user_view").status_code == 200
    assert "user_view" in {p["id"] for p in client.get("/api/ui/catalog").json()["panels"]}


def test_a_broken_file_gives_the_defaults_until_the_next_save(tmp_path):
    path = tmp_path / "views.json"
    path.write_text("{not json", encoding="utf-8")
    store = ViewStore(path)
    assert store.user_view(["help"])["panels"] == ["help"] and store.mode_of("alice") == "simple"
    store.set_mode("alice", "admin")
    assert ViewStore(path).mode_of("alice") == "admin", "the save wrote it whole"
    for shape in ('{"modes": "x"}', '{"user_view": "x"}'):
        path.write_text(shape, encoding="utf-8")
        assert store.mode_of("alice") == "simple" and store.user_view(["help"])["panels"] == ["help"]
        store.set_mode("alice", "admin")
        assert store.mode_of("alice") == "admin"


@pytest.mark.parametrize("broken", ["{one stray comma,", "[1, 2]"])
def test_a_save_over_a_broken_file_keeps_it_beside_it(tmp_path, broken):
    path = tmp_path / "views.json"
    path.write_text(broken, encoding="utf-8")
    store = ViewStore(path)
    kept = lambda: sorted(p.read_text(encoding="utf-8") for p in tmp_path.glob("views.json.broken-*"))  # noqa: E731
    store.mode_of("alice")
    assert path.read_text(encoding="utf-8") == broken and kept() == [], "reading alone leaves it"
    store.set_mode("alice", "admin")
    assert kept() == [broken] and store.mode_of("alice") == "admin"
    path.write_text(f"{broken} ", encoding="utf-8")   # the next slip
    store.set_user_view(["help"], ["help"], "alice")
    assert kept() == sorted([broken, f"{broken} "]), "each keeps its own copy"
    store.set_mode("alice", "simple")
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
