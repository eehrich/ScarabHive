"""has_session_data: the chat's "Open in" menu offers the debugger for a session only once something of it was
captured -- written through the capture hooks, as a run writes it."""
import asyncio

import pytest

from agent_system.config.models import AgentSystemConfig, ToolServerConfig
from agent_system.hooks import HookContext


@pytest.fixture
def plugin(tmp_path):
    from plugins.message_debugger.plugin import MessageDebuggerHybridPlugin

    server_config = ToolServerConfig()
    server_config.config = {"db_path": str(tmp_path / "debugger.db")}
    plugin = MessageDebuggerHybridPlugin("message_debugger", AgentSystemConfig(), server_config)
    yield plugin
    plugin._db.close()


def test_a_session_with_a_captured_request_or_turn_holds_data(plugin):
    hooks = plugin.hooks_plugin
    asyncio.run(hooks.debugger_capture_pre_request(HookContext(
        hook_type="pre_llm_request", agent_name="a", request_id="r1", session_id="s-request", user_id="ada",
        llm_provider="openai", llm_model="m", llm_request_payload={"messages": [{"role": "user", "content": "hi"}]})))
    asyncio.run(hooks.debugger_capture_pre_llm(HookContext(
        hook_type="pre_llm_call", agent_name="a", request_id="r2", session_id="s-turn", user_id="ada",
        messages=[{"role": "user", "content": "hi"}])))
    assert plugin._db.flush(timeout=5), "the captures were not written"

    assert plugin.has_session_data("s-request", "ada") is True
    assert plugin.has_session_data("s-turn", "ada") is True
    assert plugin.has_session_data("s-other", "ada") is False
