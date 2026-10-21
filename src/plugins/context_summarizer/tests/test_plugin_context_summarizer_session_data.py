"""has_session_data: the chat's "Open in" menu offers the summarizer for a session once the hook recorded an event
of it -- applied, rejected or skipped, as the panel lists them all."""
import asyncio

from agent_system.config.models import AgentSystemConfig, ToolServerConfig
from plugins.context_summarizer.plugin import PLUGIN_FACTORY


def test_a_session_with_a_recorded_event_holds_data():
    plugin = PLUGIN_FACTORY("context_summarizer", AgentSystemConfig(), ToolServerConfig())
    plugin.server._hooks_impl._record({"session_id": "s-skipped", "status": "skipped"})

    assert asyncio.run(plugin.has_session_data("s-skipped", "ada")) is True
    assert asyncio.run(plugin.has_session_data("s-other", "ada")) is False
