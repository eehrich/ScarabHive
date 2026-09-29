"""The panel catalogue: what a plugin declares, what a viewer sees, and the route serving it."""
from __future__ import annotations

import logging
import sys
import textwrap
from dataclasses import asdict, replace
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from agent_system.auth.middleware import SecurityHeadersMiddleware
from agent_system.config.models import (AuthConfig, EndpointSecurityConfig, EndpointSecurityRule, PluginSecurityConfig,
                                         ToolServerConfig)
from agent_system.plugins import tool_adapter, web_adapter
from agent_system.ui.catalog import (
    PanelSpecError,
    build_catalog,
    core_panels,
    disambiguate,
    plugin_panel,
    roles_allowed,
)
from agent_system.ui.resources import sprite_icons
from agent_system.ui.routes import router

ICONS = sprite_icons()
REPO = Path(__file__).resolve().parents[2]


def spec(**overrides):
    base = {
        "endpoint": "/plugins/probe/",
        "title": "Probe",
        "description": "A panel for tests",
        "icon": "bug",
        "category": "debug",
        "keywords": ["requests"],
        "window": {"width": 800, "height": 600},
        "contexts": {"request": "/plugins/probe/?request_id={request_id}"},
    }
    base.update(overrides)
    return base


def test_a_declared_panel_becomes_a_catalogue_entry():
    panel = plugin_panel("probe", spec(), ICONS)

    assert asdict(panel) == {
        "id": "probe", "title": "Probe", "url": "/plugins/probe/", "icon": "bug", "category": "debug",
        "description": "A panel for tests", "keywords": ["requests"], "roles": [],
        "window": {"width": 800, "height": 600},
        "contexts": {"request": "/plugins/probe/?request_id={request_id}"}, "group": "", "help": "",
    }


def test_a_window_without_size_gets_the_default_size():
    panel = plugin_panel("probe", spec(window={"width": 1000}), ICONS)

    assert panel.window["width"] == 1000
    assert panel.window["height"] > 0


@pytest.mark.parametrize("broken, complaint", [
    (spec(button={"enabled": True}), "unknown keys ['button']"),
    (spec(roles=["admin"]), "unknown keys ['roles']"),
    ({**spec(), True: "x"}, "unknown keys ['True']"),
    ({k: v for k, v in spec().items() if k != "icon"}, "needs icon as text"),
    (spec(category="tools"), "category 'tools'"),
    (spec(category=["debug"]), "needs category as text"),
    (spec(icon="no-such-icon"), "icon 'no-such-icon'"),
    (spec(icon={"name": "bug"}), "needs icon as text"),
    (spec(title=42), "needs title as text"),
    (spec(endpoint={"a": 1}), "needs endpoint as text"),
    (spec(endpoint="https://elsewhere.example/"), "plain path on this server"),
    (spec(endpoint="//elsewhere.example/plugins/probe/"), "plain path on this server"),
    (spec(endpoint=r"/\elsewhere.example/plugins/probe/"), "plain path on this server"),
    (spec(endpoint="/plugins/probe/../../ui/panels/settings", contexts={}), "plain path on this server"),
    (spec(endpoint="/plugins/probe/%2e%2e/other/", contexts={}), "plain path on this server"),
    # a browser strips trailing spaces and control characters, and tabs anywhere
    (spec(endpoint="/plugins/probe/.. ", contexts={}), "plain path on this server"),
    (spec(endpoint="/plugins/probe/..\x0b", contexts={}), "plain path on this server"),
    (spec(endpoint="/\t/evil.example/plugins/probe/", contexts={}), "plain path on this server"),
    (spec(endpoint="/plugins/other/", contexts={}), "must lie under /plugins/probe/"),
    (spec(contexts={"request": "/plugins/probe/../x/?request_id={request_id}"}), "plain path on this server"),
    (spec(description=["x"]), "description must be text"),
    (spec(keywords="requests"), "keywords must be a list"),
    (spec(contexts={"agent": "/plugins/probe/"}), "unknown context 'agent'"),
    (spec(contexts=["request"]), "contexts must map"),
    (spec(contexts={"request": "/elsewhere/?request_id={request_id}"}), "below the endpoint"),
    (spec(contexts={"session": "/plugins/probe/?session_id="}), "exactly {session_id}"),
    (spec(contexts={"session": "/plugins/probe/?session_id={sesion_id}"}), "exactly {session_id}"),
    (spec(contexts={"session": "/plugins/probe/?session_id={request_id}"}), "exactly {session_id}"),
    (spec(window={"width": "wide"}), "window must be"),
    (spec(window={"width": -5}), "window must be"),
    (spec(window={"width": True}), "window must be"),
    (spec(window={"width": 800, "singleton": True}), "window must be"),
    (spec(window=[800, 600]), "window must be"),
    ("a panel", "must be a mapping"),
])
def test_a_panel_the_shell_could_not_show_is_refused(broken, complaint):
    with pytest.raises(PanelSpecError, match=complaint.replace("[", r"\[").replace("]", r"\]").replace("{", r"\{")):
        plugin_panel("probe", broken, ICONS)


def test_a_context_must_lie_below_an_endpoint_without_a_trailing_slash():
    """Text prefix is not enough: /ui/panels/probes is not below /ui/panels/probe."""
    declared = spec(endpoint="/ui/panels/probe", contexts={"session": "/ui/panels/probes?session_id={session_id}"})

    with pytest.raises(PanelSpecError, match="below the endpoint"):
        plugin_panel("probe", declared, ICONS, root="/")


def test_core_panels_follow_the_rules_plugins_follow():
    """Core panels are built in code, not parsed -- so they are held to the parser here."""
    panels = core_panels(audit_enabled=True, profiling_enabled=True, memory_profiling_enabled=True)

    for panel in panels:
        declared = asdict(panel)
        declared["endpoint"] = declared.pop("url")
        for built_only in ("id", "roles", "group", "help"):
            del declared[built_only]
        assert plugin_panel(panel.id, declared, ICONS, root="/") == replace(panel, roles=[])


def test_admin_dashboards_are_listed_only_while_their_feature_is_on():
    off = {p.id for p in core_panels(audit_enabled=False, profiling_enabled=False, memory_profiling_enabled=False)}
    on = {p.id for p in core_panels(audit_enabled=True, profiling_enabled=True, memory_profiling_enabled=True)}

    assert on - off == {"security_audit", "performance", "memory_profile"}


def test_no_two_panels_share_a_title_across_core_and_shipped_plugins():
    """Only instances of one plugin are told apart by name; a core panel and a plugin never are."""
    from agent_system.plugins.schema_loader import load_schema_from_dir

    core = [p.title for p in core_panels(audit_enabled=True, profiling_enabled=True, memory_profiling_enabled=True)]
    shipped = []
    for schema in sorted(REPO.glob("src/plugins*/*/schema.yaml")):
        web_ui = load_schema_from_dir(schema.parent, {"name": schema.parent.name}).get("web_ui") or {}
        if "panel" in web_ui:
            shipped.append(web_ui["panel"]["title"])
    assert len(shipped) > 10, "fixture: the shipped panels were not found"

    titles = core + shipped

    assert len(titles) == len(set(titles)), sorted(t for t in titles if titles.count(t) > 1)


@pytest.mark.parametrize("policies, roles", [
    ([(False, None)], []),
    ([(True, None)], []),
    ([(False, "admin")], []),
    ([(True, "guest")], ["admin", "guest", "user"]),
    ([(True, "user")], ["admin", "user"]),
    ([(True, "ADMIN")], ["admin"]),
    ([(True, 2)], []),  # an operator's typo ranks as no role, as the enforcement ranks it -- no crash
    ([(True, "user"), (True, "admin")], ["admin"]),  # every layer must let the role in
    ([(True, "admin"), (False, None)], ["admin"]),
])
def test_a_plugin_panel_is_for_the_roles_its_routes_admit(policies, roles):
    assert roles_allowed(*policies) == roles


def test_a_role_restricted_panel_is_hidden_from_other_roles():
    core = core_panels(audit_enabled=True, profiling_enabled=False, memory_profiling_enabled=False)
    users = replace(plugin_panel("users", spec(endpoint="/plugins/users/", contexts={}), ICONS), roles=["admin"])

    as_admin = {p["id"] for p in build_catalog("admin", core, [users])["panels"]}
    as_user = {p["id"] for p in build_catalog("user", core, [users])["panels"]}

    assert {"users", "security_audit"} <= as_admin
    assert as_admin - as_user == {"users", "security_audit"}


def test_instances_of_one_plugin_are_told_apart_and_grouped():
    def instance(name, title):
        return plugin_panel(name, spec(endpoint=f"/plugins/{name}/", contexts={}, title=title), ICONS)

    first = instance("summarizer_fast", "Context Summarizer")
    second = instance("summarizer_deep", "Context Summarizer")
    single = instance("logs", "Logs")

    panels = disambiguate([first, second, single])

    assert [p.title for p in panels] == [
        "Context Summarizer · summarizer_fast", "Context Summarizer · summarizer_deep", "Logs"]
    assert [p.group for p in panels] == ["Context Summarizer", "Context Summarizer", ""]


def test_an_instance_the_viewer_may_not_open_does_not_group_the_one_they_may():
    def instance(name, roles):
        panel = plugin_panel(name, spec(endpoint=f"/plugins/{name}/", contexts={}, title="Sub-Agents"), ICONS)
        return replace(panel, roles=roles)

    catalog = build_catalog("user", [], [instance("sam_writer", ["admin"]), instance("sam_skills", [])])

    assert [(p["id"], p["title"], p["group"]) for p in catalog["panels"]] == [("sam_skills", "Sub-Agents", "")]


def test_every_panel_url_may_be_framed_by_the_shell():
    """The shell shows every panel in an iframe; the security headers decide whether the browser renders it."""
    urls = [p.url for p in core_panels(audit_enabled=True, profiling_enabled=True, memory_profiling_enabled=True)]
    urls.append("/plugins/message_debugger/")

    async def ok(scope, receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    client = TestClient(SecurityHeadersMiddleware(ok))
    framing = {url: client.get(url).headers.get("x-frame-options") for url in urls}
    assert client.get("/").headers.get("x-frame-options") == "DENY", "fixture: the shell itself must not be framable"

    assert framing == {url: "SAMEORIGIN" for url in urls}


# ------------------------------------------------------------------ the route


def auth(enabled: bool, endpoint_rules=(), **plugin_security) -> SimpleNamespace:
    return SimpleNamespace(auth=AuthConfig(
        enabled=enabled, plugin_security=PluginSecurityConfig(**plugin_security),
        endpoint_security=EndpointSecurityConfig(rules=list(endpoint_rules))), plugins=None)


@pytest.fixture
def app():
    app = FastAPI()
    app.include_router(router)
    app.state.config = auth(False)
    return app


@pytest.fixture
def registered(monkeypatch):
    """Put plugins into the registries the route reads, as plugin loading leaves them."""
    def register(instance, web_ui):
        monkeypatch.setitem(web_adapter.plugin_web_registry.web_plugins, instance, object())
        monkeypatch.setitem(tool_adapter.plugin_tool_registry.plugin_servers, instance,
                            SimpleNamespace(plugin_schema={"web_ui": web_ui}))
    return register


def test_the_catalogue_lists_the_panels_registered_plugins_declare(app, registered):
    registered("probe", {"panel": spec(), "endpoints": []})
    registered("routes_only", {"endpoints": [{"path": "/x"}]})

    data = TestClient(app).get("/api/ui/catalog").json()

    ids = [p["id"] for p in data["panels"]]
    assert "probe" in ids and "routes_only" not in ids
    assert {"session", "system", "settings"} <= set(ids)
    assert [c["id"] for c in data["categories"]][:2] == ["session", "writer"]


def test_a_plugin_panel_names_its_guide_for_the_shell_help_button(app, registered, tmp_path):
    for name, readme in (("documented", "# Documented"), ("silent", None)):
        folder = tmp_path / name
        folder.mkdir()
        (folder / "plugin.toml").write_text(f'[plugin]\nname = "{name}"\n', encoding="utf-8")
        if readme:
            (folder / "README.md").write_text(readme, encoding="utf-8")
    registered("docs_instance", {"panel": spec(endpoint="/plugins/docs_instance/", contexts={})})
    registered("silent", {"panel": spec(endpoint="/plugins/silent/", contexts={})})
    app.state.config.plugins = SimpleNamespace(plugin_dirs=[str(tmp_path)],
                                               servers={"docs_instance": ToolServerConfig(type="documented")})

    panels = {p["id"]: p for p in TestClient(app).get("/api/ui/catalog").json()["panels"]}

    assert (panels["docs_instance"]["help"], panels["silent"]["help"], panels["help"]["help"]) == ("documented", "", "")


def test_a_plugin_loaded_like_production_reaches_the_catalogue(app, tmp_path, monkeypatch):
    """Real registration: schema rendered for an instance name that is not the directory name."""
    package = tmp_path / "probe_web_plugin"
    package.mkdir()
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "plugin.py").write_text(textwrap.dedent("""
        from fastapi import APIRouter
        from agent_system.plugins.web_base import SchemaBasedPluginWebInterface

        class ProbeWebPlugin(SchemaBasedPluginWebInterface):
            def get_web_router(self):
                return APIRouter()
    """), encoding="utf-8")
    (package / "schema.yaml").write_text(textwrap.dedent("""
        web_ui:
          panel:
            endpoint: "/plugins/{{ name }}/"
            title: "Probe"
            icon: bug
            category: debug
            contexts:
              session: "/plugins/{{ name }}/?session_id={session_id}"
    """), encoding="utf-8")
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.delitem(sys.modules, "probe_web_plugin.plugin", raising=False)
    from probe_web_plugin.plugin import ProbeWebPlugin

    plugin = ProbeWebPlugin("probe_instance", system_config=None, server_config=None)
    tool_adapter.plugin_tool_registry.register_existing_plugin_instance("probe_instance", plugin, None, None)

    panels = {p["id"]: p for p in TestClient(app).get("/api/ui/catalog").json()["panels"]}

    assert panels["probe_instance"]["url"] == "/plugins/probe_instance/"
    assert panels["probe_instance"]["contexts"] == {"session": "/plugins/probe_instance/?session_id={session_id}"}


@pytest.mark.parametrize("web_ui", [
    {"panel": spec(endpoint="/plugins/legacy/", contexts={}, type="iframe")},
    {"panel": []},
    ["not", "a", "mapping"],
    {"panel": spec(category=["debug"])},
])
def test_a_broken_panel_is_left_out_loudly_not_the_whole_catalogue(app, registered, caplog, web_ui):
    registered("probe", {"panel": spec()})
    registered("legacy", web_ui)

    with caplog.at_level(logging.ERROR, logger="agent_system.ui.routes"):
        response = TestClient(app).get("/api/ui/catalog")

    ids = [p["id"] for p in response.json()["panels"]]
    assert response.status_code == 200
    assert "probe" in ids and "legacy" not in ids
    assert any("legacy" in record.getMessage() for record in caplog.records)


def test_without_authentication_the_viewer_is_the_owner_and_sees_everything(app, registered, monkeypatch):
    import agent_system.utils.profiling as profiling

    monkeypatch.setattr(profiling, "PROFILING_ENABLED", True)  # an admin-only core dashboard
    web_adapter.init_plugin_security(app.state.config.auth)
    registered("users", {"panel": spec(endpoint="/plugins/users/", contexts={})})

    panels = {p["id"]: p for p in TestClient(app).get("/api/ui/catalog").json()["panels"]}

    assert panels["users"]["roles"] == []
    assert panels["performance"]["roles"] == ["admin"]


@pytest.fixture
def signed_in(app, monkeypatch):
    """Authentication on, the viewer chosen per request; never the real data/users.db."""
    import agent_system.auth.database as database
    import agent_system.auth.dependencies as dependencies

    viewer = {"user": None}

    async def current_viewer(request, credentials, api_key, db):
        return viewer["user"]

    monkeypatch.setattr(dependencies, "get_optional_user", current_viewer)
    monkeypatch.setattr(database, "get_db", lambda: None)

    def as_role(role, active=True):
        viewer["user"] = None if role is None else SimpleNamespace(role=SimpleNamespace(value=role), is_active=active)
        return TestClient(app).get("/api/ui/catalog")
    return as_role


def test_with_authentication_a_panel_is_listed_for_the_roles_its_route_admits(app, registered, signed_in):
    """The plugin route security in config.yaml decides, not the schema: admin-only routes, admin-only panel."""
    app.state.config = auth(True, default_min_role="user", plugin_overrides={"users": {"min_role": "admin"}})
    web_adapter.init_plugin_security(app.state.config.auth)
    registered("users", {"panel": spec(endpoint="/plugins/users/", contexts={})})
    registered("probe", {"panel": spec()})

    ids = {role: {p["id"] for p in signed_in(role).json()["panels"]} for role in ("admin", "user", "guest")}

    assert {"users", "probe"} <= ids["admin"]
    assert "probe" in ids["user"] and "users" not in ids["user"]
    assert not {"users", "probe"} & ids["guest"]


def test_an_app_wide_endpoint_rule_hides_a_panel_too(app, registered, signed_in):
    """The global middleware guards /plugins/ as well: a panel it refuses is not offered."""
    app.state.config = auth(True, endpoint_rules=[
        EndpointSecurityRule(pattern="/plugins/probe/*", policy="require_auth", min_role="admin")])
    web_adapter.init_plugin_security(app.state.config.auth)
    registered("probe", {"panel": spec()})
    registered("logs", {"panel": spec(endpoint="/plugins/logs/", contexts={})})

    as_user = {p["id"] for p in signed_in("user").json()["panels"]}
    as_admin = {p["id"] for p in signed_in("admin").json()["panels"]}

    assert "logs" in as_user and "probe" not in as_user
    assert "probe" in as_admin


def test_without_plugin_route_security_no_plugin_panel_is_offered(app, registered, signed_in, caplog):
    """Security is set up when the plugin routes are mounted; before that, nobody can be told who may open them."""
    app.state.config = auth(True)
    registered("probe", {"panel": spec()})

    with caplog.at_level(logging.ERROR, logger="agent_system.ui.routes"):
        ids = {p["id"] for p in signed_in("admin").json()["panels"]}

    assert "probe" not in ids and {"session", "system"} <= ids
    assert any("security is not set up" in record.getMessage() for record in caplog.records)


@pytest.mark.parametrize("role, active", [(None, True), ("admin", False)])
def test_without_an_active_account_there_is_no_catalogue(app, signed_in, role, active):
    app.state.config = auth(True)

    assert signed_in(role, active).status_code == 401


@pytest.mark.parametrize("name, script", [
    ("session", "/static/js/panels/session.js"),
    ("system", "/static/js/panels/system.js"),
    ("settings", "/static/js/panels/settings.js"),
])
def test_every_core_panel_renders_on_the_kit(app, name, script):
    page = TestClient(app).get(f"/ui/panels/{name}")

    assert page.status_code == 200
    assert script in page.text and "/static/kit/kit.css" in page.text


def test_an_unknown_core_panel_is_not_found(app):
    assert TestClient(app).get("/ui/panels/index").status_code == 404
