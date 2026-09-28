"""agent-run --list-sessions reads the session store its runs use.

A run honours AGENT_SESSION_STORAGE_PATH (InitializationService), but the
listing opened <source>/data/sessions whatever the variable said -- it listed
sessions the runs next to it never saw, and none of theirs.
"""
from __future__ import annotations

import agent_system.agent_run as agent_run
from agent_system.services.session_manager import SessionManager


async def test_the_listing_reads_the_store_the_variable_names(tmp_path, monkeypatch, capsys):
    store = tmp_path / "sessions"
    await SessionManager(storage_path=str(store)).create_session(
        user_id="cli_user", session_id="probe-s1", title="probe-listing-title")
    monkeypatch.setenv("AGENT_SESSION_STORAGE_PATH", str(store))

    await agent_run.main_async("", list_sessions="")

    assert "probe-listing-title" in capsys.readouterr().out
