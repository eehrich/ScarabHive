"""The chat-command endpoints the web UI resolves lines through.

The browser cannot read skill folders, so it asks the server — and the server
answers with the SAME parser the terminal chat uses. These tests pin that both
surfaces stay in step, and that the endpoint behaves when the input is junk.
"""
from __future__ import annotations

import sqlite3
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from agent_system.app import build_app
from agent_system.auth.security import create_access_token
from live_accounts import token_generation

DEV_SECRET = "published-signing-key-replace-with-your-own-0000000000"


@pytest.fixture(scope="module")
def client():
    """Client WITHOUT the lifespan: these routes need no startup state.

    ``app.state.config`` is set by ``build_app`` itself, so entering the lifespan
    would only buy plugin bootstrap this module does not use -- and holding it
    open across a module is what made this file hang.
    """
    return TestClient(build_app())


@pytest.fixture(scope="module")
def tooled_agent(client, auth_headers):
    """An agent that really has tools, plus its listing.

    Not the default one: ``chat_agent`` ships without a ``tools`` block, which
    means deny-all -- a correct answer of zero tools, and useless for showing
    that the grouping works.

    Asked from the app's own registry, not from GET /agents: that endpoint
    reads a module global the autouse reset in conftest empties before every
    test, so it answers [] in here no matter what is registered.

    Preferred is an agent whose tools really exercise the grouping: one where
    TWO registered names could claim the same tool (``coder_fs_read_file``
    fits both ``coder`` and ``coder_fs``). Without that a shortest-match bug
    groups everything just as plausibly -- measured, it survived the whole
    class.
    """
    from agent_system.servers.agent.server import Agent

    registry = client.app.state.tool_registry
    registered = list(registry.list())
    names = [name for name in registered if isinstance(registry.get(name), Agent)]
    assert names, "fixture: no agent registered at all"

    def claimants(tool_name: str) -> int:
        return sum(1 for server in registered
                   if tool_name == server or tool_name.startswith(server + "_"))

    fallback = None
    for name in names:
        data = client.get(f"/agents/{name}/tools", headers=auth_headers).json()
        if not data.get("total"):
            continue
        fallback = fallback or (name, data)
        if any(claimants(tool["name"]) > 1
               for group in data["groups"] for tool in group["tools"]):
            return name, data
    assert fallback, f"fixture: none of the {len(names)} agents reports a single tool"
    return fallback


@pytest.fixture(scope="module")
def auth_headers():
    row = sqlite3.connect("data/users.db").execute(
        "select id, username, role from users where username='admin'"
    ).fetchone()
    token = create_access_token(
        {"sub": row[1], "user_id": row[0], "role": row[2], "gen": token_generation(row[0])},
        secret_key=DEV_SECRET, algorithm="HS256",
    )
    return {"Authorization": f"Bearer {token}"}


class TestCatalogue:
    def test_commands_come_from_the_shared_catalogue(self, client, auth_headers):
        from agent_system.chat_commands import WEB, commands_for

        data = client.get("/chat/commands", headers=auth_headers).json()
        assert [c["name"] for c in data["commands"]] == [c.name for c in commands_for(WEB)]

    def test_exit_is_not_offered_to_a_browser(self, client, auth_headers):
        data = client.get("/chat/commands", headers=auth_headers).json()
        assert "exit" not in {c["name"] for c in data["commands"]}

    def test_skills_are_listed_with_their_description(self, client, auth_headers):
        """Autocomplete needs a name AND a hint, or the list is unreadable."""
        data = client.get("/chat/commands", headers=auth_headers).json()
        assert data["skills"], "no skills discovered - check skills.skill_dirs"
        for skill in data["skills"]:
            assert skill["name"] and skill["display"] == f"/{skill['name']}"
            assert "summary" in skill

    def test_a_skill_nobody_could_type_is_not_offered(
            self, client, auth_headers, monkeypatch):
        """The registry takes what a folder is called, the parser takes a
        command word. "3d-print" was offered here and went to the model as a
        message; "tools" was offered and ran the built-in. The terminal has
        filtered both for a while -- through the same function this uses."""
        import agent_system.skills as skills_module

        registry = SimpleNamespace(
            ensure_discovered=lambda dirs: None,
            list_skills=lambda: [
                SimpleNamespace(name=name, description="d", version="1")
                for name in ("writer", "3d-print", "tools")])
        monkeypatch.setattr(skills_module, "get_skill_registry", lambda: registry)

        data = client.get("/chat/commands", headers=auth_headers).json()
        assert [s["name"] for s in data["skills"]] == ["writer"]


class TestResolve:
    def test_builtin_command(self, client, auth_headers):
        r = client.post("/chat/resolve", json={"line": "/sessions"}, headers=auth_headers).json()
        assert (r["kind"], r["name"]) == ("command", "sessions")

    def test_plain_message_passes_through(self, client, auth_headers):
        r = client.post("/chat/resolve", json={"line": "wie geht es dir?"},
                        headers=auth_headers).json()
        assert (r["kind"], r["text"]) == ("message", "wie geht es dir?")

    def test_a_path_is_a_message(self, client, auth_headers):
        """A sysadmin agent gets paths typed at it."""
        r = client.post("/chat/resolve", json={"line": "/etc/hosts anzeigen"},
                        headers=auth_headers).json()
        assert r["kind"] == "message"

    def test_double_slash_escape(self, client, auth_headers):
        r = client.post("/chat/resolve", json={"line": "//help ist kein Kommando"},
                        headers=auth_headers).json()
        assert (r["kind"], r["text"]) == ("message", "/help ist kein Kommando")

    def test_unknown_carries_a_suggestion(self, client, auth_headers):
        r = client.post("/chat/resolve", json={"line": "/sesions"}, headers=auth_headers).json()
        assert r["kind"] == "unknown"
        assert r["suggestion"] == "/sessions"

    def test_skill_is_expanded_to_its_body(self, client, auth_headers):
        catalogue = client.get("/chat/commands", headers=auth_headers).json()
        name = catalogue["skills"][0]["name"]

        r = client.post("/chat/resolve", json={"line": f"/{name}"}, headers=auth_headers).json()
        assert (r["kind"], r["name"]) == ("skill", name)
        assert r["text"], "skill body was empty"
        # The frontmatter belongs to the header, not to the instructions.
        assert not r["text"].lstrip().startswith("---")

    def test_skill_arguments_are_not_lost(self, client, auth_headers):
        """A skill without a placeholder still has to see what was typed."""
        catalogue = client.get("/chat/commands", headers=auth_headers).json()
        name = catalogue["skills"][0]["name"]

        r = client.post("/chat/resolve", json={"line": f"/{name} tu etwas Bestimmtes"},
                        headers=auth_headers).json()
        assert "tu etwas Bestimmtes" in r["text"]


class TestBadInput:
    def test_missing_line_is_an_empty_message(self, client, auth_headers):
        r = client.post("/chat/resolve", json={}, headers=auth_headers).json()
        assert r["kind"] == "message"

    def test_non_string_line_is_rejected(self, client, auth_headers):
        assert client.post("/chat/resolve", json={"line": 42},
                           headers=auth_headers).status_code == 400

    def test_endpoints_require_authentication(self, client):
        assert client.get("/chat/commands").status_code == 401
        assert client.post("/chat/resolve", json={"line": "/help"}).status_code == 401


@pytest.fixture(scope="module")
def commanded_agent(client, auth_headers) -> str:
    """An agent that really has a plugin command to offer.

    Not the default one: ``chat_agent`` ships without a ``tools`` block, so it
    may dispatch nothing and gets no plugin commands at all -- which is the
    other half of the pair these tests need.
    """
    from agent_system.servers.agent.server import Agent

    registry = client.app.state.tool_registry
    for name in registry.list():
        if not isinstance(registry.get(name), Agent):
            continue
        listed = client.get(f"/chat/commands?surface=web&agent={name}",
                            headers=auth_headers).json()
        if listed.get("plugin_commands"):
            return name
    raise AssertionError("fixture: no agent offers a plugin command")


class TestPluginCommands:
    """Plugin commands in the browser: the same list and the same
    authorization the terminal has.

    The security property this pins is that the caller names a COMMAND and the
    server resolves it against what THIS agent may dispatch -- naming a tool,
    or a command another agent has, must not work.
    """

    def test_the_list_is_per_agent(self, commanded_agent, client, auth_headers):
        """The whole point: a command whose tool an agent may not call is not
        listed for it. chat_agent may call nothing, so it gets nothing."""
        entry = client.get("/agents", headers=auth_headers).json().get("default")
        assert entry, "fixture: no default agent"

        offered = client.get(f"/chat/commands?surface=web&agent={commanded_agent}",
                             headers=auth_headers).json()
        deny_all = client.get(f"/chat/commands?surface=web&agent={entry}",
                              headers=auth_headers).json()

        assert offered["plugin_commands"], "fixture: this agent was chosen for having one"
        assert deny_all["plugin_commands"] == []
        assert deny_all["commands"], "the built-ins must not depend on the agent"

    def test_every_command_is_offered_with_a_spelling_and_a_hint(self, commanded_agent,
                                                                 client, auth_headers):
        """What autocomplete needs, and the qualified form next to it.

        NOT measured here: that the spelling is the RIGHT one when a plugin
        names a command like a built-in -- no shipped command does, so this
        data cannot tell the two apart. That rule is pinned where it lives,
        in tests/cli/test_plugin_commands.py::spellings.
        """
        listed = client.get(f"/chat/commands?surface=web&agent={commanded_agent}",
                            headers=auth_headers).json()["plugin_commands"]

        assert listed, "fixture: this agent was chosen for having a command"
        for command in listed:
            assert command["spelling"], "autocomplete has nothing to insert"
            assert command["qualified"] == f"{command['qualified'].split(':')[0]}:{command['name']}"
            assert command["display"].startswith("/" + command["spelling"])
            assert command["kind"] == "plugin"

    def test_a_command_really_runs_on_the_agent_that_was_named(self, commanded_agent,
                                                               client, auth_headers):
        """The end of the path: dispatch, tool, formatted answer.

        Without a session the compaction command answers "Session context not
        available" -- a real dispatch with nothing to compact, which is what
        makes this safe to run in a test.
        """
        listed = client.get(f"/chat/commands?surface=web&agent={commanded_agent}",
                            headers=auth_headers).json()["plugin_commands"][0]

        response = client.post("/chat/command",
                               json={"name": listed["spelling"],
                                     "agent_name": commanded_agent},
                               headers=auth_headers)

        assert response.status_code == 200, response.text
        answer = response.json()
        assert answer["name"] == listed["qualified"]
        assert answer["text"], "the command answered with nothing at all"

    def test_a_caller_without_an_agent_gets_the_entry_agent(self, client, auth_headers):
        """The same agent /run would have used -- one rule, not two.

        Here that is the deny-all chat_agent, so the list is empty; the
        assertion below states that premise instead of leaving the empty list
        looking like a rule of its own.
        """
        entry = client.get("/agents", headers=auth_headers).json()["default"]
        entry_list = client.get(f"/chat/commands?surface=web&agent={entry}",
                                headers=auth_headers).json()["plugin_commands"]

        listed = client.get("/chat/commands?surface=web", headers=auth_headers).json()

        assert listed["plugin_commands"] == entry_list
        assert listed["commands"], "the built-ins are agent-independent"

    def test_resolve_only_knows_the_command_with_its_agent(self, commanded_agent, client, auth_headers):
        """Same line, two answers: the agent decides whether /compact exists."""
        spelling = client.get(f"/chat/commands?surface=web&agent={commanded_agent}",
                              headers=auth_headers).json()["plugin_commands"][0]["spelling"]

        without = client.post("/chat/resolve", json={"line": f"/{spelling}"},
                              headers=auth_headers).json()
        with_agent = client.post("/chat/resolve",
                                 json={"line": f"/{spelling} rest", "agent_name": commanded_agent},
                                 headers=auth_headers).json()

        assert without["kind"] == "unknown"
        assert with_agent["kind"] == "plugin"
        assert with_agent["payload"] == "rest"

    def test_the_caller_cannot_name_a_tool(self, commanded_agent, client, auth_headers):
        """The command runs a tool, but the caller may only name the COMMAND.
        Handing the tool through would be the second, unguarded entry point
        the plugin-command design exists to avoid."""
        listed = client.get(f"/chat/commands?surface=web&agent={commanded_agent}",
                            headers=auth_headers).json()["plugin_commands"][0]
        registry = client.app.state.tool_registry
        from agent_system.plugin_commands import collect_plugin_commands
        tool = next(c.tool for c in collect_plugin_commands(registry.get(commanded_agent))
                    if c.qualified == listed["qualified"])

        response = client.post("/chat/command",
                               json={"name": tool, "agent_name": commanded_agent},
                               headers=auth_headers)

        assert response.status_code == 404
        assert "is not a command" in response.json()["detail"]

    def test_a_command_this_agent_may_not_run_is_refused(self, commanded_agent, client, auth_headers):
        """The same command name, an agent that may not dispatch its tool."""
        entry = client.get("/agents", headers=auth_headers).json()["default"]
        spelling = client.get(f"/chat/commands?surface=web&agent={commanded_agent}",
                              headers=auth_headers).json()["plugin_commands"][0]["spelling"]

        response = client.post("/chat/command",
                               json={"name": spelling, "agent_name": entry},
                               headers=auth_headers)

        assert response.status_code == 404

    @pytest.mark.parametrize("named", ["no_such_agent", "file_ops"])
    def test_a_name_that_is_no_agent_is_refused_as_such(self, named, client, auth_headers):
        """Both 404s must say WHICH kind they are.

        The status alone cannot tell "no such agent" from "not a command this
        agent can run" -- and the second is what a fall-back to the entry
        agent would answer, which is exactly the mistake this pins. ``file_ops``
        is registered but no Agent: without the type check it reaches
        run_plugin_command, whose first move is dispatch_tool_call -- an
        attribute a plain tool server does not have.
        """
        response = client.post("/chat/command",
                               json={"name": "compact", "agent_name": named},
                               headers=auth_headers)

        assert response.status_code == 404
        assert response.json()["detail"] == "no such agent"

    def test_an_overlong_payload_is_refused(self, commanded_agent, client, auth_headers):
        from agent_system.app import MAX_CHAT_LINE

        response = client.post("/chat/command",
                               json={"name": "compact", "agent_name": commanded_agent,
                                     "payload": "x" * (MAX_CHAT_LINE + 1)},
                               headers=auth_headers)

        assert response.status_code == 413

    def test_a_session_of_another_user_is_refused(self, commanded_agent, client, auth_headers):
        """The command acts ON the session -- compaction rewrites it -- so a
        foreign id must not become a tool's working set. Seeded in the
        in-memory tracker the ownership check reads first, which keeps this
        test off the real session directory.
        """
        tracker = client.app.state.agent._session_tracker
        foreign = "parity-probe-not-yours"
        tracker.set_session_metadata(foreign, {"user_id": "somebody_else"})
        try:
            response = client.post("/chat/command",
                                   json={"name": "compact", "agent_name": commanded_agent,
                                         "session_id": foreign},
                                   headers=auth_headers)
        finally:
            tracker._session_metadata.pop(foreign, None)

        assert response.status_code == 403, response.text

    def test_it_requires_authentication(self, client):
        assert client.post("/chat/command", json={"name": "compact"}).status_code == 401


class TestAgentTools:
    """What ``/tools`` answers with in the browser.

    The neighbouring /allowed-tools cannot serve it: its "available" list holds
    SERVER names, so the web chat had no way to the tools themselves and said
    "only in the terminal".
    """

    def test_every_tool_carries_the_server_it_came_from(self, tooled_agent, client, auth_headers):
        """The LONGEST registered name that fits, not merely one that fits:
        ``coder_fs_read_file`` belongs to ``coder_fs``, and ``coder`` is
        registered too. Checking "some prefix matches" would pass either way.
        """
        from agent_system.chat_commands import UNKNOWN_SERVER

        _agent, data = tooled_agent
        registered = list(client.app.state.tool_registry.list())
        listed = [tool for group in data["groups"] for tool in group["tools"]]
        assert any(sum(1 for server in registered
                       if tool["name"] == server or tool["name"].startswith(server + "_")) > 1
                   for tool in listed), \
            "fixture: no tool that two server names could claim -- a shortest-match " \
            "bug would group this agent just as plausibly"

        assert len(listed) == data["total"], "a tool fell out of its group"
        assert any(group["server"] != UNKNOWN_SERVER for group in data["groups"]), \
            "every tool landed in the unknown group -- the servers never reached the grouping"

        for group in data["groups"]:
            for tool in group["tools"]:
                candidates = [s for s in registered
                              if tool["name"] == s or tool["name"].startswith(s + "_")]
                expected = max(candidates, key=len) if candidates else UNKNOWN_SERVER
                assert group["server"] == expected, \
                    f"{tool['name']} is filed under {group['server']}, not {expected}"

    def test_the_endpoint_does_not_filter(self, tooled_agent, client, auth_headers):
        """Filtering stays with the caller, as it is in the terminal.

        A server that returns only the matches also returns a ``total`` that
        can no longer tell "this agent has no tools" from "nothing matched" --
        and the browser would have to guess which sentence to show. So a
        filter parameter must not quietly grow back here.
        """
        agent, everything = tooled_agent
        tools = [tool for group in everything["groups"] for tool in group["tools"]]
        assert len(tools) > 1, "fixture: one tool cannot show the absence of filtering"

        asked = client.get(f"/agents/{agent}/tools?filter={tools[0]['name']}",
                           headers=auth_headers).json()

        assert asked["total"] == everything["total"], \
            "the endpoint narrowed its answer -- total no longer counts what the agent has"
        assert asked["groups"] == everything["groups"]

    def test_an_unknown_agent_is_a_404(self, client, auth_headers):
        assert client.get("/agents/no_such_agent_here/tools",
                          headers=auth_headers).status_code == 404

    def test_a_tool_server_is_not_an_agent(self, client, auth_headers):
        """Naming a plain tool server has to say so, not answer with an empty
        tool list that reads like a broken agent."""
        from agent_system.servers.agent.server import Agent

        registry = client.app.state.tool_registry
        plain = next((name for name in registry.list()
                      if not isinstance(registry.get(name), Agent)), None)
        assert plain, "fixture: no non-agent server registered to ask about"

        response = client.get(f"/agents/{plain}/tools", headers=auth_headers)

        assert response.status_code == 400
        assert "not an agent" in response.json()["detail"]

    def test_it_requires_authentication(self, client):
        assert client.get("/agents/whoever/tools").status_code == 401

    def test_a_listing_that_fails_is_an_error_not_zero_tools(
            self, client, auth_headers, tooled_agent, monkeypatch):
        """The browser answers total 0 with \"tools.allowed is empty\"."""
        name, _data = tooled_agent
        agent = client.app.state.tool_registry.get(name)

        async def broken(params):
            raise RuntimeError("discovery broke")

        monkeypatch.setattr(agent, "_list_usable_tools_with_details", broken)
        response = client.get(f"/agents/{name}/tools", headers=auth_headers)

        assert response.status_code == 500
        assert "discovery broke" in response.json()["detail"]


@pytest.fixture
def vars_session(client):
    """A session id nobody else uses, wiped again afterwards.

    The tracker hangs off the app's agent singleton and outlives a single
    test, so a leftover variable would surface as another test's mysterious
    extra entry.
    """
    import uuid

    sid = f"vars-{uuid.uuid4().hex[:8]}"
    yield sid
    tracker = getattr(getattr(client.app.state, "agent", None), "_session_tracker", None)
    if tracker is not None:
        # delete_session, not clear_session_template_vars: the ownership test
        # also leaves METADATA behind, and a stray owner would make the next
        # test's session belong to somebody else.
        tracker.delete_session(sid)


class TestVarsEndpoint:
    """``/chat/vars`` -- the browser's half of ``/vars``.

    The variables are the ones ``--vars`` fills and the agent server reads per
    turn, so what these pin is that the web surface writes to the SAME place
    the terminal does, through the SAME parser.
    """

    def _set(self, client, headers, sid, payload):
        return client.post("/chat/vars",
                           json={"session_id": sid, "payload": payload},
                           headers=headers)

    def test_a_fresh_session_has_no_variables(self, client, auth_headers, vars_session):
        response = client.get(f"/chat/vars?session_id={vars_session}", headers=auth_headers)
        assert response.status_code == 200
        assert response.json()["vars"] == {}

    def test_setting_shows_up_in_the_listing(self, client, auth_headers, vars_session):
        posted = self._set(client, auth_headers, vars_session, "lang=de book_id=7")
        assert posted.status_code == 200
        assert posted.json()["changed"] is True

        listed = client.get(f"/chat/vars?session_id={vars_session}",
                            headers=auth_headers).json()
        assert listed["vars"] == {"lang": "de", "book_id": "7"}

    def test_it_writes_where_the_next_turn_reads(self, client, auth_headers, vars_session):
        """Not the session file: the agent's tracker is what prompt rendering
        consults, so a value written anywhere else would show in the panel and
        never reach the model."""
        self._set(client, auth_headers, vars_session, "lang=de")
        tracker = client.app.state.agent._session_tracker
        assert tracker.get_session_template_vars(vars_session) == {"lang": "de"}

    def _listed(self, client, headers, sid):
        """Read the variables BACK from the server.

        Not the POST's own echo: that is the value the endpoint computed, and
        asserting on it proves only that the arithmetic was right. Removal has
        to be verified where it must actually have happened -- the tracker
        merges on write, so a missing clear leaves the old key in place while
        the echo looks perfect.
        """
        return client.get(f"/chat/vars?session_id={sid}", headers=headers).json()["vars"]

    def test_unset_removes_only_the_named_one(self, client, auth_headers, vars_session):
        self._set(client, auth_headers, vars_session, "lang=de keep=yes")
        self._set(client, auth_headers, vars_session, "unset lang")
        assert self._listed(client, auth_headers, vars_session) == {"keep": "yes"}

    def test_clear_empties_the_session(self, client, auth_headers, vars_session):
        self._set(client, auth_headers, vars_session, "lang=de keep=yes")
        self._set(client, auth_headers, vars_session, "clear")
        assert self._listed(client, auth_headers, vars_session) == {}

    def test_a_foreign_session_is_refused(self, client, auth_headers, vars_session):
        """Ownership, not just authentication. These endpoints address the
        shared in-memory tracker by session id alone, so without the check any
        logged-in user could read and rewrite someone else's variables."""
        client.app.state.agent._session_tracker.set_session_metadata(
            vars_session, {"user_id": "somebody-else"})

        read = client.get(f"/chat/vars?session_id={vars_session}", headers=auth_headers)
        written = self._set(client, auth_headers, vars_session, "lang=de")

        assert read.status_code == 403
        assert written.status_code == 403

    def test_a_refused_line_leaves_the_old_values_alone(self, client, auth_headers,
                                                        vars_session):
        """The dangerous failure: reporting an error AND having eaten the
        variables that were already set."""
        self._set(client, auth_headers, vars_session, "lang=de")
        refused = self._set(client, auth_headers, vars_session, "oops").json()

        assert refused["errors"] and refused["changed"] is False
        assert refused["vars"] == {"lang": "de"}
        still = client.get(f"/chat/vars?session_id={vars_session}",
                           headers=auth_headers).json()
        assert still["vars"] == {"lang": "de"}

    def test_the_grammar_is_the_terminals(self, client, auth_headers, vars_session):
        """A second, browser-only splitter would make the same line mean two
        things depending on where it was typed."""
        after = self._set(client, auth_headers, vars_session,
                          'greeting="hallo welt"').json()
        assert after["vars"] == {"greeting": "hallo welt"}

    def test_session_id_is_required(self, client, auth_headers):
        assert client.get("/chat/vars", headers=auth_headers).status_code == 400
        assert client.post("/chat/vars", json={"payload": "a=1"},
                           headers=auth_headers).status_code == 400

    def test_an_overlong_payload_is_refused(self, client, auth_headers, vars_session):
        response = self._set(client, auth_headers, vars_session, "a=" + "x" * 100_001)
        assert response.status_code == 413

    def test_it_requires_authentication(self, client, vars_session):
        assert client.get(f"/chat/vars?session_id={vars_session}").status_code == 401
        assert client.post("/chat/vars",
                           json={"session_id": vars_session, "payload": "a=1"}).status_code == 401

    def _named_agent(self, client):
        """A registered agent that is NOT the default one.

        The ownership hole only shows there: every agent carries its own
        SessionTracker (measured: 122 registered, none sharing the default's),
        so a check against the default tracker looked at an object that never
        holds the metadata of a session running on another agent.
        """
        from agent_system.servers.agent.server import Agent

        registry = client.app.state.tool_registry
        default = client.app.state.agent
        for name in registry.list():
            try:
                candidate = registry.get(name)
            except Exception:
                continue
            if isinstance(candidate, Agent) and candidate is not default:
                return name, candidate
        pytest.skip("fixture: no second agent registered")

    def test_a_foreign_session_on_a_named_agent_is_refused(self, client, auth_headers,
                                                           vars_session):
        """The version of the ownership test that actually bites.

        Setting the metadata on the DEFAULT tracker tests the one path where
        check and write happen to touch the same object. With `agent_name` the
        write goes to that agent's tracker, and the check has to follow it
        there -- otherwise a logged-in user reads and overwrites someone
        else's session variables with a 200.
        """
        name, agent = self._named_agent(client)
        agent._session_tracker.set_session_metadata(vars_session,
                                                    {"user_id": "somebody-else"})
        agent._session_tracker.set_session_template_vars(vars_session,
                                                         {"secret": "geheim"})
        try:
            read = client.get(
                f"/chat/vars?session_id={vars_session}&agent_name={name}",
                headers=auth_headers)
            written = client.post(
                "/chat/vars",
                json={"session_id": vars_session, "agent_name": name,
                      "payload": "secret=pwned"},
                headers=auth_headers)

            assert read.status_code == 403
            assert written.status_code == 403
            assert agent._session_tracker.get_session_template_vars(
                vars_session) == {"secret": "geheim"}
        finally:
            agent._session_tracker.delete_session(vars_session)

    def test_an_unknown_agent_is_a_404_not_a_crash(self, client, auth_headers,
                                                   vars_session):
        """The browser always sends agent_name when an agent is selected, so
        this arm is the production path -- and it was unexecuted."""
        assert client.get(
            f"/chat/vars?session_id={vars_session}&agent_name=gibt_es_nicht",
            headers=auth_headers).status_code == 404
        assert client.post(
            "/chat/vars",
            json={"session_id": vars_session, "agent_name": "gibt_es_nicht",
                  "payload": "a=1"},
            headers=auth_headers).status_code == 404

    def test_a_quoted_value_is_read_back_from_the_store(self, client, auth_headers,
                                                        vars_session):
        """Was asserted on the POST's own echo, which is the value the endpoint
        computed -- a store that mangled it would have stayed green."""
        self._set(client, auth_headers, vars_session, 'greeting="hallo welt"')
        assert self._listed(client, auth_headers,
                            vars_session) == {"greeting": "hallo welt"}

    def test_a_bare_listing_reports_no_change(self, client, auth_headers, vars_session):
        """`changed` gates the "in effect from your next message" note, and an
        empty payload must not take the write path at all."""
        response = client.post("/chat/vars",
                               json={"session_id": vars_session, "payload": ""},
                               headers=auth_headers).json()
        assert response["changed"] is False

    def test_a_removal_reaches_the_session_file(self, client, auth_headers,
                                                vars_session, tmp_path, monkeypatch):
        """The web half of the persistence fix.

        Only the storage ROOT is redirected -- everything else is the real
        path: the real endpoint, the real SessionManager, the real file. The
        suite must not write into the project's own data/sessions.
        """
        import types

        from agent_system import app as app_module
        from agent_system.services.session_manager import SessionManager

        manager = SessionManager(storage_path=str(tmp_path))
        asyncio_run = __import__("asyncio").run
        asyncio_run(manager.create_session(user_id="admin", session_id=vars_session))
        monkeypatch.setattr(app_module, "_session_service",
                            types.SimpleNamespace(session_manager=manager))

        self._set(client, auth_headers, vars_session, "lang=de keep=yes")
        removed = self._set(client, auth_headers, vars_session, "unset lang").json()

        assert removed["persisted"] is True, "the endpoint never wrote to disk"
        on_disk = asyncio_run(manager.load_session("admin", vars_session))["context_vars"]
        assert on_disk == {"keep": "yes"}, f"removal did not reach disk: {on_disk}"

    def test_an_unset_on_a_session_not_yet_loaded_keeps_the_other_values(
            self, client, auth_headers, vars_session, tmp_path, monkeypatch):
        """The hazard the REPLACE write created.

        A session opened in the browser is not in the tracker until a turn
        runs. Computing from that empty tracker and then persisting the result
        as a replacement would wipe every variable the file holds. So the
        persisted set has to be part of the base.
        """
        import types

        from agent_system import app as app_module
        from agent_system.services.session_manager import SessionManager

        asyncio_run = __import__("asyncio").run
        manager = SessionManager(storage_path=str(tmp_path))
        session = asyncio_run(manager.create_session(user_id="admin",
                                                     session_id=vars_session))
        session["context_vars"] = {"lang": "de", "book_id": "7", "gone": "x"}
        asyncio_run(manager.save_session(session))
        monkeypatch.setattr(app_module, "_session_service",
                            types.SimpleNamespace(session_manager=manager))

        # Nothing in the tracker for this session -- exactly the opened-old-
        # session case.
        assert client.app.state.agent._session_tracker.get_session_template_vars(
            vars_session) == {}

        listed = client.get(f"/chat/vars?session_id={vars_session}",
                            headers=auth_headers).json()["vars"]
        removed = self._set(client, auth_headers, vars_session, "unset gone").json()

        assert listed == {"lang": "de", "book_id": "7", "gone": "x"}, \
            "the listing ignored the persisted variables"
        assert removed["vars"] == {"lang": "de", "book_id": "7"}
        on_disk = asyncio_run(manager.load_session("admin",
                                                   vars_session))["context_vars"]
        assert on_disk == {"lang": "de", "book_id": "7"}, \
            f"the other variables were wiped: {on_disk}"
