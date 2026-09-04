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
