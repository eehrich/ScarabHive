"""The chat-command endpoints the web UI resolves lines through.

The browser cannot read skill folders, so it asks the server — and the server
answers with the SAME parser the terminal chat uses. These tests pin that both
surfaces stay in step, and that the endpoint behaves when the input is junk.
"""
from __future__ import annotations

import sqlite3

import pytest
from fastapi.testclient import TestClient

from agent_system.app import build_app
from agent_system.auth.security import create_access_token

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

    registry = client.app.state.mcp_registry
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
        {"sub": row[1], "user_id": row[0], "role": row[2]},
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
        registered = list(client.app.state.mcp_registry.list())
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
        """Naming a plain MCP server has to say so, not answer with an empty
        tool list that reads like a broken agent."""
        from agent_system.servers.agent.server import Agent

        registry = client.app.state.mcp_registry
        plain = next((name for name in registry.list()
                      if not isinstance(registry.get(name), Agent)), None)
        assert plain, "fixture: no non-agent server registered to ask about"

        response = client.get(f"/agents/{plain}/tools", headers=auth_headers)

        assert response.status_code == 400
        assert "not an agent" in response.json()["detail"]

    def test_it_requires_authentication(self, client):
        assert client.get("/agents/whoever/tools").status_code == 401
