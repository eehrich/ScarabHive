"""LLM request/response hooks carry the session of the running request.

The request id comes from the current_request_id context var; the agent's
SessionTracker already maps every running request to its session. Without that
lookup the message debugger stored every LLM request with an empty session.
"""
from unittest.mock import AsyncMock, Mock

import pytest

from agent_system.hooks import HookType
from agent_system.mcp.status import current_request_id
from agent_system.servers.agent.components.hook_integration import HookIntegrationManager
from agent_system.servers.agent.components.session_tracking import SessionTracker


class _Client:
    def set_llm_hooks(self, on_pre_request=None, on_post_response=None):
        self.pre, self.post = on_pre_request, on_post_response


@pytest.fixture
def wired():
    agent = Mock()
    agent.name = "test_agent"
    agent.agent_config = Mock(hooks=None)
    agent._session_tracker = SessionTracker({})
    agent._session_tracker.register_request("req-1", "sess-1", {})
    manager = HookIntegrationManager(agent)
    manager.registry = Mock(execute_hooks=AsyncMock())
    client = _Client()
    manager.wire_llm_hooks(client)
    return manager, client


@pytest.mark.asyncio
@pytest.mark.parametrize("request_id, session", [("req-1", "sess-1"), ("unknown", "")])
async def test_llm_hooks_carry_the_session_of_the_request(wired, request_id, session):
    manager, client = wired
    token = current_request_id.set(request_id)
    try:
        await client.pre({"payload": {}, "provider": "p", "model": "m"})
        await client.post({"response_data": {}, "provider": "p", "model": "m"})
    finally:
        current_request_id.reset(token)

    contexts = [call.args[1] for call in manager.registry.execute_hooks.call_args_list]
    assert [c.hook_type for c in contexts] == [HookType.PRE_LLM_REQUEST, HookType.POST_LLM_RESPONSE]
    assert [c.session_id for c in contexts] == [session, session]


@pytest.mark.asyncio
@pytest.mark.parametrize("info, served_by", [
    # A streamed answer has no response_data: the backend arrives only as the client's routing record.
    ({"routing": {"selected": "Google AI Studio", "available": ["Google AI Studio", "Google"]}}, "Google AI Studio"),
    ({"routing": None}, None),
    ({}, None),
])
async def test_the_response_hook_names_the_backend_the_client_read(wired, info, served_by):
    manager, client = wired

    await client.post({"provider": "openai_httpx", "model": "m", "is_streaming": True, **info})

    context = manager.registry.execute_hooks.call_args.args[1]
    assert context.metadata["served_by"] == served_by
