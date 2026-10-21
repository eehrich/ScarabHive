"""has_session_data: the chat's "Open in" menu offers the usage panel for a session once a call of it was recorded,
or -- for a session that makes no call itself -- once it has sub-sessions, whose calls the panel counts with it."""
import asyncio

import pytest

from agent_system import app_state
from agent_system.services.session_manager import SessionManager
from agent_system.services.session_service import SessionService
from plugins.context_usage_tracker.plugin import ContextUsageTrackerPlugin

USER = "ada"


@pytest.fixture
def plugin(tmp_path, monkeypatch):
    service = SessionService(session_manager=SessionManager(storage_path=str(tmp_path / "sessions")))
    monkeypatch.setattr(app_state, "session_service", service)
    sessions = service.session_manager

    async def seed():
        for session_id in ("s-calls", "s-quiet", "s-parent"):
            await sessions.create_session(user_id=USER, session_id=session_id)
        await sessions.create_session(user_id=USER, session_id="s-child", parent_session_id="s-parent")

    asyncio.run(seed())
    plugin = ContextUsageTrackerPlugin(name="context_usage_tracker", system_config={},
                                       server_config={"storage_path": str(tmp_path / "usage.json")})
    # a call without a conversation (context_window 0) is listed by the panel as well
    plugin.tracker.record_usage(agent_id="a", agent_name="chat", session_id="s-calls", total_tokens=10)
    return plugin


def test_a_session_with_a_recorded_call_holds_data(plugin):
    assert plugin.has_session_data("s-calls", USER) is True
    assert plugin.has_session_data("s-quiet", USER) is False


def test_a_session_without_calls_of_its_own_holds_those_below_it(plugin):
    assert plugin.has_session_data("s-parent", USER) is True
    assert plugin.has_session_data("s-child", USER) is False


def test_another_users_session_is_offered(plugin):
    """An admin's view: under whom its sub-sessions lie is a scan away, so the panel is offered."""
    assert plugin.has_session_data("s-quiet", "an-admin") is True


def test_without_a_session_store_it_cannot_say_and_the_panel_is_offered(plugin, monkeypatch):
    monkeypatch.setattr(app_state, "session_service", None)
    assert plugin.has_session_data("s-quiet", USER) is True
