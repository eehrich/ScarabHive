"""Continuing a session keeps the agent and the LLM it was started with.

`agent-cli chat --agent X --llm Y` then `agent-cli chat --session <id>` used to
carry the conversation on with the CONFIG defaults: another agent, another
model, no word about it. Both values are in every session record; these pin
down that they are read back, and the three cases where they must not win.
"""
from __future__ import annotations

import pytest
from types import SimpleNamespace

from agent_system.agent_cli import stored_session_settings
from agent_system.cli_utils.session_defaults import (
    choose_agent_name,
    choose_llm_profile,
    usable_session_defaults,
)
from agent_system.services.session_manager import SessionManager


class TestChooseAgentName:
    def test_an_explicit_agent_wins(self):
        assert choose_agent_name("typed", "stored", "default") == "typed"

    def test_the_session_beats_the_config_default(self):
        assert choose_agent_name(None, "stored", "default") == "stored"

    def test_without_a_session_the_default_stands(self):
        assert choose_agent_name(None, None, "default") == "default"


class TestChooseLlmProfile:
    def test_an_explicit_profile_wins(self):
        assert choose_llm_profile("typed", "stored", "a", "a", "d") == "typed"

    def test_the_stored_profile_is_used_for_the_stored_agent(self):
        assert choose_llm_profile(None, "stored", "a", "a", "d") == "stored"

    def test_a_profile_stored_for_another_agent_is_not_forced(self):
        """--agent b --session <started with a>: b has its own profile chain,
        and a's choice may not even be in it."""
        assert choose_llm_profile(None, "stored", "a", "b", "d") is None

    def test_the_agents_own_default_is_not_an_override(self):
        """Returning it would build a second client that changes nothing."""
        assert choose_llm_profile(None, "d", "a", "a", "d") is None

    def test_nothing_stored_means_no_override(self):
        assert choose_llm_profile(None, None, None, "a", "d") is None


class TestUsableSessionDefaults:
    """A session record is a memory, not an instruction.

    Sessions outlive the config that made them: on this repo 707 of 2914
    cli_user sessions name an agent that no longer exists. The gates further
    down were written for names a PERSON typed and can only abort, so handing
    them a stale stored name would turn "continue this conversation" into a
    hard exit over something the user never mentioned.
    """

    def _config(self, agents=("known",), profiles=("p_known",)):
        servers = {name: SimpleNamespace(agent_config=object()) for name in agents}
        # A tool server: present, but no agent_config -- the same raw gate
        # servers/agent/entry.py applies.
        servers["a_tool_server"] = SimpleNamespace(agent_config=None)
        return SimpleNamespace(
            plugins=SimpleNamespace(servers=servers),
            llm_system=SimpleNamespace(profiles={n: object() for n in profiles}))

    def test_known_values_pass_through(self):
        assert usable_session_defaults("known", "p_known", self._config()) == (
            "known", "p_known")

    def test_an_agent_the_config_no_longer_defines_is_dropped(self):
        assert usable_session_defaults("v5b_story_designer", "p_known",
                                       self._config()) == (None, "p_known")

    def test_a_tool_server_is_not_an_agent(self):
        assert usable_session_defaults("a_tool_server", None,
                                       self._config()) == (None, None)

    def test_a_retired_llm_profile_is_dropped(self):
        assert usable_session_defaults("known", "or-qwen-full",
                                       self._config()) == ("known", None)

    def test_nothing_stored_stays_nothing(self):
        assert usable_session_defaults(None, None, self._config()) == (None, None)


class TestStoredSessionSettings:
    """Read through the REAL SessionManager, against a session it wrote."""

    @pytest.fixture
    def manager(self, tmp_path):
        return SessionManager(storage_path=str(tmp_path / "sessions"))

    async def _make(self, manager, session_id, agent_name, llm_profile):
        await manager.create_session(
            user_id="u", session_id=session_id,
            agent_name=agent_name, llm_profile=llm_profile)

    def test_the_stored_values_come_back(self, manager):
        import asyncio

        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(
                self._make(manager, "s1", "v6_synopsis_writer", "llm_writer_creative"))
        finally:
            loop.close()

        # A SECOND manager over the same directory, so the read goes to disk:
        # the writing manager caches the record it just created, and reading
        # it back out of that cache would prove nothing about what was stored.
        reader = SessionManager(storage_path=str(manager.storage_path))
        assert stored_session_settings(reader, "u", "s1") == (
            "v6_synopsis_writer", "llm_writer_creative")

    def _with(self, manager, **fields):
        import asyncio

        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(self._make(manager, "s3", "coder", "ran_on_this"))
            record = loop.run_until_complete(manager.load_session("u", "s3"))
            record.update(fields)
            loop.run_until_complete(manager.save_session(record))
        finally:
            loop.close()
        return SessionManager(storage_path=str(manager.storage_path))

    def test_a_record_that_says_nobody_picked_continues_on_the_agents_own(self, manager):
        """llm_profile names what RAN: continuing on it froze the agent's profile of that day, and the
        save after it recorded that as a pick the browser then held on to."""
        reader = self._with(manager, llm_profile_override=None)
        assert stored_session_settings(reader, "u", "s3") == ("coder", None)

    def test_a_picked_profile_comes_back(self, manager):
        reader = self._with(manager, llm_profile_override="picked")
        assert stored_session_settings(reader, "u", "s3") == ("coder", "picked")

    def test_an_unknown_session_is_not_an_error(self, manager):
        # --session also NAMES a new session; the load further down is what
        # reports a real problem.
        assert stored_session_settings(manager, "u", "never-existed") == (None, None)

    def test_without_a_session_id_nothing_is_read(self, manager):
        assert stored_session_settings(manager, "u", None) == (None, None)

    def test_a_session_of_another_user_is_not_read(self, manager):
        import asyncio

        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(self._make(manager, "s2", "a", "p"))
        finally:
            loop.close()

        assert stored_session_settings(manager, "someone-else", "s2") == (None, None)
