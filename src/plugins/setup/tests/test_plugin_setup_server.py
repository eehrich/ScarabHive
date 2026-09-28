"""The setup server: its two tools through call_with_status, the panel's endpoints through the router the
app mounts. The real configuration throughout; the root conftest fakes only the LLM's network edge."""
import json
import secrets
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from agent_system.api.debug_endpoints import require_admin_viewer
from agent_system.config.models import ToolServerConfig
from agent_system.config.settings import get_tool_server_config, load_settings
from agent_system.plugins.web_adapter import PluginEndpointSecurityEnforcer, PluginWebRegistry
from plugins.setup import server as module
from plugins.setup import status
from plugins.setup.server import SetupServer, installation_state


@pytest.fixture(scope="module")
def loaded():
    return load_settings()


def with_auth(config, **fields):
    return config.model_copy(update={"auth": config.auth.model_copy(update=fields)})


@pytest.fixture
def config(loaded, tmp_path):
    """The real configuration, naming a user database of this test's own -- the status reads the configured one."""
    return with_auth(loaded, database_path=str(tmp_path / "users.db"))


@pytest.fixture
def server(config):
    return SetupServer("setup", config, ToolServerConfig(type="setup", enabled=True))


def add_user(db, name, role, active=True):
    from agent_system.auth.models import UserCreate
    db.create_user(UserCreate(username=name, email=f"{name}@example.com", password="a-long-password",
                              role=role, is_active=active))


@pytest.fixture
def users(config, monkeypatch):
    """The configured user database, set up as the API sets it up: an admin ada, a user bob, a deactivated admin eve."""
    from agent_system.auth import database
    from agent_system.auth.models import UserRole
    db = database.UserDatabase(config.auth.database_path)
    for name, role, active in (("ada", UserRole.ADMIN, True), ("bob", UserRole.USER, True),
                               ("eve", UserRole.ADMIN, False)):
        add_user(db, name, role, active)
    monkeypatch.setattr(database, "_db", db)
    return db


def without_auth(config):
    return with_auth(config, enabled=False)


def written(config, folder: Path, key: str):
    """*config*, loaded from a master file of its own naming *key*: the file a restart reads, as load_settings
    records it. The key this machine's config.yaml names must not decide a test."""
    master = folder / "config.yaml"
    master.write_text(f"auth:\n  secret_key: {json.dumps(key)}\n", encoding="utf-8")
    config = config.model_copy()
    config._source_path = str(master)
    return config


#: A key of this installation's own: made now, printed nowhere.
OWN_KEY = secrets.token_urlsafe(32)


@pytest.fixture(autouse=True)
def _not_the_api(monkeypatch):
    """This process signs nothing unless a test says it is the API: an app another test built with authentication
    may have left set_jwt_config's state behind."""
    from agent_system.auth import security
    monkeypatch.setattr(security, "AUTH_ENFORCED", False)


#: agent-cli at the machine, where no account of that name exists: the owner.
AT_THE_MACHINE = {"_user_id": "cli_user"}


class TestTheTools:
    async def test_status_lists_every_key_by_state_and_no_value(self, server, config, monkeypatch):
        monkeypatch.setenv("OPENROUTER_API_KEY", "***REMOVED***")

        result = await server.call_with_status("setup_status", AT_THE_MACHINE)

        assert result["status"] == "success", result
        names = {key["name"]: key for key in result["keys"]}
        assert names["OPENROUTER_API_KEY"]["state"] == "set" and names["OPENROUTER_API_KEY"]["named_in"], names
        assert "0123456789abcdef" not in repr(result)
        assert result["chat"]["agent"] == config.default_agent

    async def test_probe_chat_tries_the_default_chat(self, server, config):
        result = await server.call_with_status("setup_probe_chat", AT_THE_MACHINE)

        assert result["status"] == "success" and result["ok"] is True, result
        assert result["agent"] == config.default_agent

    async def test_probe_chat_says_which_profiles_there_are(self, server, config):
        """Only the ones it would probe: a batch profile named here would be refused next."""
        from plugins.setup.probe import runs_as_a_batch
        batch = [name for name in config.llm_system.profiles if runs_as_a_batch(config, name)]
        assert batch, "the configuration has no batch profile: this test would be vacuous"

        result = await server.call_with_status("setup_probe_chat", {**AT_THE_MACHINE, "profile": "no-such-profile"})

        assert result["status"] == "error" and "no-such-profile" in result["error"], result
        assert "omit it" in result["error"] and "chat" in result["error"], result
        named = result["error"].split("name one of: ")[1].split(" and ")[0].split(", ")
        probed = sorted(name for name in config.llm_system.profiles if name not in batch)
        assert named == probed[:30], named

    async def test_probe_chat_uses_the_configuration_the_server_runs_with(self, server, monkeypatch):
        """A fresh load read a different file under --config, and a key entered since reaches neither."""
        seen = []

        async def probe(config, profile=None):
            seen.append(config)
            return {"ok": True, "agent": None, "profile": profile}
        monkeypatch.setattr(module, "probe_chat", probe)

        await server.call_with_status("setup_probe_chat", AT_THE_MACHINE)

        assert len(seen) == 1 and seen[0] is server.system_config  # the very object, not an equal fresh load

    @pytest.mark.parametrize("tool", ["setup_status", "setup_probe_chat"])
    @pytest.mark.parametrize("who", ["bob", "eve", "anonymous"])
    async def test_whoever_is_no_active_admin_is_refused(self, server, users, tool, who):
        refused = await server.call_with_status(tool, {"_user_id": who})
        allowed = await server.call_with_status(tool, {"_user_id": "ada"})

        assert refused["status"] == "error" and "administrator" in refused["error"], refused
        assert allowed["status"] == "success", allowed

    async def test_without_authentication_the_one_user_is_the_owner(self, config, users):
        """The API without authentication opens a user database on the first chat all the same (get_db), and its
        runs are "anonymous": the database is there, and still there is only the owner."""
        server = SetupServer("setup", without_auth(config), ToolServerConfig(type="setup", enabled=True))

        assert (await server.call_with_status("setup_status", {"_user_id": "anonymous"}))["status"] == "success"

    async def test_in_a_cli_process_the_configured_accounts_decide(self, server, users, monkeypatch):
        """agent-cli sets up no user database -- also where the API starts it to wake a web user's session
        (session_presence.wake_command, --session-user <that user>). The configured one decides, read without
        being set up; cli_user without an account is the one at the machine."""
        from agent_system.auth import database
        monkeypatch.setattr(database, "_db", None)
        before = Path(users.db_path).read_bytes()

        outcomes = {who: (await server.call_with_status("setup_status", {"_user_id": who}))["status"]
                    for who in ("ada", "bob", "eve", "cli_user", "anonymous", "")}

        assert outcomes == {"ada": "success", "bob": "error", "eve": "error", "cli_user": "success",
                            "anonymous": "error", "": "error"}, outcomes
        assert database._db is None and Path(users.db_path).read_bytes() == before, "the check set up or wrote a database"

    @pytest.mark.parametrize("database, owner, said", [("missing", "success", "only an administrator"),
                                                       ("without a users table", "error", "could not be read")])
    async def test_without_a_readable_user_database_nobody_is_an_admin(self, server, config, monkeypatch,
                                                                       database, owner, said):
        """No configured file: no account can exist, and agent-cli at the machine is the owner. A file that cannot
        be read (no users table; a locked one fails the same way) might hold an old account named cli_user: then
        nobody is let in -- and told so, not "administrators only", which a real admin would pass on as the answer."""
        import sqlite3

        from agent_system.auth import database as users_module
        monkeypatch.setattr(users_module, "_db", None)
        if database == "without a users table":
            sqlite3.connect(config.auth.database_path).close()
        assert Path(config.auth.database_path).exists() is (database != "missing"), "fixture"

        results = {who: await server.call_with_status("setup_status", {"_user_id": who})
                   for who in ("ada", "bob", "cli_user")}

        outcomes = {who: result["status"] for who, result in results.items()}
        assert outcomes == {"ada": "error", "bob": "error", "cli_user": owner}, outcomes
        refusals = [result["error"] for result in results.values() if result["status"] == "error"]
        assert refusals and all(said in refusal for refusal in refusals), refusals  # the tool's own refusal

    @pytest.mark.parametrize("key, shared, known", [(OWN_KEY, None, False), (status.SHIPPED_SIGNING_KEYS[0], True, True)],
                             ids=["own", "shipped"])
    async def test_the_tool_does_not_vouch_for_the_key_the_api_signs_with(self, config, tmp_path, key, shared, known):
        """The tool may run outside the API (agent-cli, a woken session): an own key configured there says nothing
        of the running API's -- that is null, never "no". What the config file names it can tell; a known key
        configured is to fix wherever it is read."""
        server = SetupServer("setup", written(with_auth(config, secret_key=key), tmp_path, key),
                             ToolServerConfig(type="setup", enabled=True))

        result = await server.call_with_status("setup_status", AT_THE_MACHINE)

        assert (result["auth"]["shared_signing_key"], result["auth"]["configured_signing_key_known"]) == (shared, known)

    async def test_in_the_api_the_tool_judges_the_key_it_signs_with(self, config, tmp_path, monkeypatch):
        """A chat's tool call runs in the API: an own key signs, a pull put the shipped one into the file -- the
        running key is fine, the next restart is not."""
        from agent_system.auth import security
        monkeypatch.setattr(security, "SECRET_KEY", OWN_KEY)
        monkeypatch.setattr(security, "AUTH_ENFORCED", True)
        started = with_auth(config, secret_key=OWN_KEY)
        server = SetupServer("setup", written(started, tmp_path, status.SHIPPED_SIGNING_KEYS[0]),
                             ToolServerConfig(type="setup", enabled=True))

        auth = (await server.call_with_status("setup_status", AT_THE_MACHINE))["auth"]

        assert (auth["shared_signing_key"], auth["signing_key_needs_restart"],
                auth["configured_signing_key_known"]) == (False, True, True), auth

    async def test_an_account_named_cli_user_is_judged_as_the_account(self, server, users, monkeypatch):
        """The name is reserved since 22.09.2026: an account made before still logs in, and is no owner for its name."""
        from agent_system.auth import database
        from agent_system.auth.models import UserRole
        monkeypatch.setattr(database.UserDatabase, "RESERVED_USERNAMES", frozenset())
        add_user(users, "cli_user", UserRole.USER)

        assert (await server.call_with_status("setup_status", {"_user_id": "cli_user"}))["status"] == "error"

    async def test_the_keys_come_from_the_files_the_config_was_loaded_from(self, tmp_path):
        """agent-cli --config sets no AGENT_CONFIG_PATH: the default file would list another installation's keys."""
        (tmp_path / "config.yaml").write_text("plugins:\n  servers:\n    x:\n      key: ${ONLY_HERE_KEY}\n",
                                              encoding="utf-8")
        config = load_settings(str(tmp_path / "config.yaml"))
        server = SetupServer("setup", config, ToolServerConfig(type="setup", enabled=True))

        result = await server.call_with_status("setup_status", AT_THE_MACHINE)

        assert [key["name"] for key in result["keys"]] == ["ONLY_HERE_KEY"], result


class TestThePanelContract:
    def test_the_state_carries_every_field_the_panel_reads(self, config):
        """static/panel.js reads these; a renamed field left the panel on its skeletons, the stubbed browser test green."""
        state = installation_state(config)

        assert set(state) == {"keys", "chat", "auth"}
        assert state["keys"] and set(state["keys"][0]) == {"name", "state", "named_in"}
        assert set(state["chat"]) == {"agent", "profile"}
        assert set(state["auth"]) == {"admin", "default_admin_password", "shared_signing_key",
                                      "signing_key_needs_restart", "configured_signing_key_known"}


@pytest.fixture
def app(server, config):
    app = FastAPI()
    registry = PluginWebRegistry()  # the plugin's router and static files, mounted as the app mounts them
    registry.register_web_plugin("setup", server)
    registry.apply_to_app(app)
    app.state.config = config
    return app


class TestThePanelEndpoints:
    @pytest.mark.parametrize("who, code", [(None, 401), ("bob", 403), ("ada", 200)])
    def test_the_state_is_for_admins_only(self, app, users, who, code):
        from datetime import timedelta

        from agent_system.auth.security import create_access_token
        headers = {}
        if who:
            account = users.get_user_by_username(who)
            token = create_access_token({"sub": who, "user_id": account.id, "role": account.role.value},
                                        expires_delta=timedelta(minutes=5))
            headers = {"Authorization": f"Bearer {token}"}
        client = TestClient(app)

        assert client.get("/plugins/setup/state", headers=headers).status_code == code
        if code != 200:
            assert client.post("/plugins/setup/probe", headers=headers, json={}).status_code == code

    def test_without_authentication_the_owner_reads_it(self, app, config):
        app.state.config = without_auth(config)

        assert TestClient(app).get("/plugins/setup/state").status_code == 200

    def test_an_admin_reads_the_state_and_tries_the_chat(self, app, monkeypatch):
        monkeypatch.setenv("OPENROUTER_API_KEY", "***REMOVED***")
        app.dependency_overrides[require_admin_viewer] = lambda: None
        client = TestClient(app)

        state = client.get("/plugins/setup/state")
        probe = client.post("/plugins/setup/probe", json={})
        forged = client.post("/plugins/setup/probe", data={"a": "form"})  # what a page elsewhere can send

        assert state.status_code == 200 and state.json()["keys"], state.text
        assert "0123456789abcdef" not in state.text
        assert probe.status_code == 200 and probe.json()["ok"] is True, probe.text
        assert forged.status_code == 415, forged.text

    def test_the_chat_is_the_one_that_started_whatever_a_reload_says(self, app, config):
        """A reload replaces app.state.config and moves neither the chat's entry agent nor its client (app.py):
        the state and the probe name the agent the chat runs, not the one the reloaded config names."""
        app.state.config = config.model_copy(update={"default_agent": "an_agent_only_the_reloaded_config_names"})
        app.dependency_overrides[require_admin_viewer] = lambda: None
        client = TestClient(app)

        probe = client.post("/plugins/setup/probe", json={}).json()
        state = client.get("/plugins/setup/state").json()

        assert probe["agent"] == state["chat"]["agent"] == config.default_agent, (probe, state["chat"])

    @pytest.mark.parametrize("now_on_disk, verdict", [("own", (True, True, False)), ("shipped", (False, True, True))])
    def test_a_key_written_since_the_start_waits_for_a_restart(self, app, server, config, monkeypatch, tmp_path,
                                                               now_on_disk, verdict):
        """The API signs with the key it set at start for as long as it runs, whatever the file or a reload says
        since. An own key written over the shipped one is not in use yet; the shipped one back on disk (a pull)
        is to fix before the restart makes every login forgeable -- told apart from the first."""
        from agent_system.auth import security
        shipped = status.SHIPPED_SIGNING_KEYS[0]
        running, on_disk = (shipped, OWN_KEY) if now_on_disk == "own" else (OWN_KEY, shipped)
        started = with_auth(config, enabled=True, secret_key=running)
        monkeypatch.setattr(server, "system_config", written(started, tmp_path, on_disk))
        monkeypatch.setattr(security, "SECRET_KEY", running)  # as set_jwt_config did at start
        monkeypatch.setattr(security, "AUTH_ENFORCED", True)
        app.dependency_overrides[require_admin_viewer] = lambda: None

        state = TestClient(app).get("/plugins/setup/state").json()["auth"]

        assert (state["shared_signing_key"], state["signing_key_needs_restart"],
                state["configured_signing_key_known"]) == verdict, state

    def test_started_without_authentication_nothing_signs_to_compare(self, app, server, config, monkeypatch,
                                                                      tmp_path):
        started = with_auth(config, enabled=False, secret_key=OWN_KEY)
        monkeypatch.setattr(server, "system_config", written(started, tmp_path, OWN_KEY))
        app.dependency_overrides[require_admin_viewer] = lambda: None

        state = TestClient(app).get("/plugins/setup/state").json()["auth"]

        assert (state["shared_signing_key"], state["signing_key_needs_restart"],
                state["configured_signing_key_known"]) == (False, None, False), state

    def test_the_page_renders_on_the_kit(self, app):
        page = TestClient(app).get("/plugins/setup/")

        assert page.status_code == 200 and "/plugins/setup/static/panel.js" in page.text, page.text[:300]


class TestTheRealConfiguration:
    def test_it_ships_its_server_entry(self, config):
        """Its server entry ships with the plugin (agents/setup.yaml), read through the config's include."""
        entry = get_tool_server_config("setup", config)

        assert entry is not None and entry.type == "setup", entry

    @pytest.mark.parametrize("path", ["/plugins/setup/", "/plugins/setup/state"])
    def test_its_routes_are_for_admins(self, config, path):
        """The catalog shows a panel to the roles its route rules admit (ui/routes.plugin_panels)."""
        policy = PluginEndpointSecurityEnforcer(config.auth).get_plugin_policy("setup", path)

        assert policy["min_role"] == "admin", policy
