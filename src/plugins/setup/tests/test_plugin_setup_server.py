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

        async def probe(config, profile=None, llm_config=None):
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
        assert state["keys"] and set(state["keys"][0]) == {"name", "state", "named_in", "from_environment"}
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
    def test_the_state_is_for_admins_only(self, app, users, who, code, monkeypatch):
        from datetime import timedelta
        # the app's config is this machine's: a gate that let the write through must fail here, not write there
        monkeypatch.setattr(module, "write_key", lambda *args: pytest.fail("a key was written past the gate"))
        monkeypatch.setattr(module, "ensure_signing_key", lambda *args: pytest.fail("a signing key past the gate"))

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
            assert client.post("/plugins/setup/key", headers=headers,
                               json={"name": "OPENROUTER_API_KEY", "value": "x"}).status_code == code
            assert client.post("/plugins/setup/signing-key", headers=headers, json={}).status_code == code

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


@pytest.fixture
def machine(tmp_path, monkeypatch):
    """An installation of this test's own: a master naming ${SETUP_TEST_KEY} in a server entry, loaded as the API
    loads it, with the config service a reload reads from. What the test takes into the environment goes with it."""
    from agent_system.config import settings
    from agent_system.services.config_service import ConfigService
    monkeypatch.setattr(settings, "_secrets_from_file", {})
    monkeypatch.setattr(settings, "_secrets_loaded", set())  # the load below marks local.env read before it exists
    monkeypatch.setenv(settings.SECRETS_FROM_FILE_ENV, "")
    for name in ("SETUP_TEST_KEY", "AUTH_SECRET_KEY", "SETUP_UNNAMED_KEY"):
        monkeypatch.setenv(name, "")
        monkeypatch.delenv(name)
    master = tmp_path / "config.yaml"
    master.write_text("auth:\n  enabled: false\n  secret_key: \"published-signing-key-replace-with-your-own-0000000000\"\n"
                      "plugins:\n  servers:\n    target:\n      type: nothing\n      token: \"${SETUP_TEST_KEY}\"\n",
                      encoding="utf-8")
    config = load_settings(str(master))
    app = FastAPI()
    registry = PluginWebRegistry()
    server = SetupServer("setup", config, ToolServerConfig(type="setup", enabled=True))
    registry.register_web_plugin("setup", server)
    registry.apply_to_app(app)
    app.state.config, app.state.config_service, app.state.config_path = config, ConfigService(), str(master)
    app.state.setup_server = server
    app.dependency_overrides[require_admin_viewer] = lambda: None
    return app, master


def settings_file(master):
    from agent_system.config.settings import _read_secrets_file
    return _read_secrets_file(master.parent / "local.env")


class TestTheKeyEndpoints:
    def test_a_key_is_written_to_local_env_and_the_next_chat_uses_it(self, machine):
        """The chat's next message builds its client from app.state.config (AppContext.live_config): the reload after
        the write is what carries the key there, and os.environ what carries it to a session the API wakes."""
        app, master = machine
        value = "sk-or-v1-" + secrets.token_hex(16)

        answer = TestClient(app).post("/plugins/setup/key", json={"name": "SETUP_TEST_KEY", "value": value})

        assert answer.status_code == 200, answer.text
        assert value not in answer.text
        assert answer.json()["reload_error"] is None, answer.json()
        assert {key["name"]: key["state"] for key in answer.json()["state"]["keys"]}["SETUP_TEST_KEY"] == "set"
        assert get_tool_server_config("target", app.state.config).token == value
        assert __import__("os").environ["SETUP_TEST_KEY"] == value
        assert "SETUP_TEST_KEY=" + value in (master.parent / "local.env").read_text(encoding="utf-8")
        assert not (master.parent / "secrets.env").exists()

    def test_a_body_that_is_no_json_is_a_400(self, machine):
        """request.json() raised unguarded: broken JSON answered 500."""
        app, master = machine

        answer = TestClient(app, raise_server_exceptions=False).post(
            "/plugins/setup/key", content=b"{not json", headers={"Content-Type": "application/json"})

        assert answer.status_code == 400, answer.text
        assert not (master.parent / "local.env").exists()

    def test_only_a_key_the_configuration_names_is_taken(self, machine):
        """Not PATH, not a variable some plugin never reads: the name must be one the files name."""
        app, master = machine

        answer = TestClient(app).post("/plugins/setup/key", json={"name": "SETUP_UNNAMED_KEY", "value": "v"})

        assert answer.status_code == 400, answer.text
        assert not (master.parent / "local.env").exists() and "SETUP_UNNAMED_KEY" not in __import__("os").environ

    def test_a_key_set_by_the_environment_cannot_be_entered(self, machine, monkeypatch):
        app, master = machine
        monkeypatch.setenv("SETUP_TEST_KEY", "from-the-shell")

        answer = TestClient(app).post("/plugins/setup/key", json={"name": "SETUP_TEST_KEY", "value": "other"})

        assert answer.status_code == 409, answer.text
        assert not (master.parent / "local.env").exists()

    @pytest.mark.parametrize("value", ["a\nB=c", "a\u2028AUTH_SECRET_KEY=mine", "a\x85B=c", "tab\there"])
    def test_a_value_the_file_cannot_hold_is_refused(self, machine, value):
        """splitlines() breaks at U+2028 and U+0085 too: such a value would set another variable on the next read."""
        app, master = machine

        answer = TestClient(app).post("/plugins/setup/key", json={"name": "SETUP_TEST_KEY", "value": value})

        assert answer.status_code == 400, answer.text
        assert not (master.parent / "local.env").exists() and "SETUP_TEST_KEY" not in __import__("os").environ

    def test_a_value_the_environment_cannot_take_is_not_written(self, machine, monkeypatch):
        """Windows takes name=value up to 32767 units: written but not taken, the answer said "not written" while the
        file held it."""
        app, master = machine
        monkeypatch.setattr(module, "environment_takes", lambda name, value: False)

        answer = TestClient(app).post("/plugins/setup/key", json={"name": "SETUP_TEST_KEY", "value": "v"})

        assert answer.status_code == 400 and "longer" in answer.json()["detail"], answer.text
        assert not (master.parent / "local.env").exists()

    def test_the_chat_test_tries_the_config_the_api_runs_now(self, machine, monkeypatch):
        """A key saved in the panel reloads the config; Test the chat must try that one, not the start's."""
        app, master = machine
        seen = {}

        async def probe(config, llm_config=None):
            seen.update(config=config, llm_config=llm_config)
            return {"ok": True}
        monkeypatch.setattr(module, "probe_chat", probe)
        app.state.config = app.state.config.model_copy()  # what a reload put there

        assert TestClient(app).post("/plugins/setup/probe", json={}).status_code == 200
        assert seen["llm_config"] is app.state.config and seen["config"] is not app.state.config, seen

    async def test_the_probe_tool_tries_the_config_a_saved_key_reloaded(self, machine, monkeypatch):
        """The tool has no request to ask the app: after a save it probes the config the save reloaded."""
        app, master = machine
        server = app.state.setup_server
        seen = []

        async def probe(config, profile=None, llm_config=None):
            seen.append(llm_config)
            return {"ok": True, "profile": "p"}
        monkeypatch.setattr(module, "probe_chat", probe)

        await server.call_with_status("setup_probe_chat", AT_THE_MACHINE)
        assert TestClient(app).post("/plugins/setup/key", json={"name": "SETUP_TEST_KEY", "value": "v1"}).status_code == 200
        await server.call_with_status("setup_probe_chat", AT_THE_MACHINE)

        assert seen[0] is None and seen[1] is app.state.config, seen

    def test_a_key_is_written_even_when_the_reload_fails_and_it_says_so(self, machine, monkeypatch):
        """Someone else's broken edit on disk: the key is saved and in the process; the answer says a restart applies
        it to the configuration."""
        from agent_system.api.admin_endpoints import ConfigReloadFailed
        app, master = machine

        def failing(app):
            raise ConfigReloadFailed("config failed to parse, nothing reloaded: bad indent")
        monkeypatch.setattr(module, "reload_app_config", failing)

        answer = TestClient(app).post("/plugins/setup/key", json={"name": "SETUP_TEST_KEY", "value": "sk-saved"})

        assert answer.status_code == 200 and "bad indent" in answer.json()["reload_error"], answer.text
        assert settings_file(master) == {"SETUP_TEST_KEY": "sk-saved"}

    def test_the_signing_key_is_no_api_key(self, machine):
        """Once the local layer names ${AUTH_SECRET_KEY}, the loader's files name it -- it is still not listed, nor
        taken as a key: a short one would stop the next start."""
        app, master = machine
        client = TestClient(app)
        assert client.post("/plugins/setup/signing-key", json={}).status_code == 200

        state = client.get("/plugins/setup/state").json()
        answer = client.post("/plugins/setup/key", json={"name": "AUTH_SECRET_KEY", "value": "short"})

        assert "AUTH_SECRET_KEY" not in {key["name"] for key in state["keys"]}, state["keys"]
        assert "SETUP_TEST_KEY" in {key["name"] for key in state["keys"]}, "fixture: the listing is not empty"
        assert answer.status_code == 400, answer.text
        assert settings_file(master)["AUTH_SECRET_KEY"] != "short"

    @pytest.mark.parametrize("path", ["key", "signing-key"])
    def test_a_page_elsewhere_cannot_send_it(self, machine, path):
        app, master = machine

        forged = TestClient(app).post(f"/plugins/setup/{path}", data={"name": "SETUP_TEST_KEY", "value": "v"})

        assert forged.status_code == 415, forged.text
        assert not (master.parent / "local.env").exists()

    def test_an_own_signing_key_is_made_for_the_next_start(self, machine, monkeypatch):
        from agent_system.config import settings
        app, master = machine

        answer = TestClient(app).post("/plugins/setup/signing-key", json={})

        assert answer.status_code == 200, answer.text
        auth = answer.json()["state"]["auth"]
        assert auth["configured_signing_key_known"] is False, auth
        monkeypatch.setattr(settings, "_secrets_loaded", set())  # the next start: a process that has read nothing yet
        assert load_settings(str(master)).auth.secret_key == settings_file(master)["AUTH_SECRET_KEY"]

    def test_a_signing_key_that_cannot_be_written_says_why(self, machine):
        app, master = machine
        (master.parent / "local.yaml").write_text("auth:\n  secret_key: \"set-by-hand\"\n", encoding="utf-8")

        answer = TestClient(app).post("/plugins/setup/signing-key", json={})

        assert answer.status_code == 409 and "itself" in answer.json()["detail"], answer.text


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
