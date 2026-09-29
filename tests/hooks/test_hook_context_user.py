"""A hook knows whose call it is: HookContext.user_id.

A plugin that keeps data per user (the message debugger) records it with each
row; without it every row was nobody's and every user read everybody's. The
user is the one the session's run was opened for -- the API and agent-cli both
record it in the session metadata before the first step -- else the one the
request was registered under, else None: nobody's, never everybody's.
"""
from unittest.mock import AsyncMock, Mock

import pytest

from agent_system.core.request_context import current_run_user, register_request_user, release_request_user
from agent_system.hooks import HookContext, HookResult, HookType, PluginHook
from agent_system.hooks.registry import HookRegistry
from agent_system.llm import hook_notify
from agent_system.llm.models import ChatMessage
from agent_system.servers.agent.components.hook_integration import HookIntegrationManager
from agent_system.servers.agent.components.session_tracking import SessionTracker
from agent_system.tools.status import current_request_id


class _Client:
    def set_llm_hooks(self, on_pre_request=None, on_post_response=None):
        self.pre, self.post = on_pre_request, on_post_response


def _manager(metadata_user, registered_user):
    agent = Mock()
    agent.name = "chat"
    agent.agent_config = Mock(hooks=None)
    agent.get_live_tools_schema = Mock(return_value=None)
    agent._session_tracker = SessionTracker({})
    agent._session_tracker.register_request("req-1", "sess-1", {})
    agent._session_tracker.set_session_metadata("sess-1", {"agent_name": "chat", **(
        {"user_id": metadata_user} if metadata_user else {})})
    if registered_user:
        register_request_user("req-1", registered_user)
    manager = HookIntegrationManager(agent)
    manager.registry = Mock(execute_hooks=AsyncMock(side_effect=lambda hook_type, context, **_: context))
    manager.wants_llm_progress = Mock(return_value=True)
    return manager


@pytest.fixture(autouse=True)
def _forget_the_request():
    yield
    release_request_user("req-1")
    release_request_user("run-1")


async def _every_context(manager):
    """Each hook point the manager serves, once; the contexts it built."""
    client = _Client()
    manager.wire_llm_hooks(client)
    token = current_request_id.set("req-1")
    try:
        await client.pre({"payload": {}, "provider": "p", "model": "m"})
        await client.post({"response_data": {}, "provider": "p", "model": "m"})
    finally:
        current_request_id.reset(token)
    ids = dict(request_id="req-1", session_id="sess-1")
    messages = [ChatMessage(role="user", content="hi")]
    await manager.execute_pre_llm_hooks(messages=messages, step=1, **ids)
    await manager.execute_post_llm_hooks(messages=messages, llm_response={"content": "ok"}, step=1, **ids)
    await manager.execute_llm_progress_hooks(reasoning_text="t", reasoning_chars=1, previous_reasoning_chars=0,
                                             step=1, **ids)
    await manager.execute_pre_tool_hooks(tool_call={"name": "x"}, step=1, **ids)
    await manager.execute_post_tool_hooks(tool_call={"name": "x"}, tool_result={"ok": True}, step=1, **ids)
    await manager.execute_format_output_hooks(output="ok", **ids)
    await manager.execute_session_start_hooks(messages=messages, **ids)
    await manager.execute_session_end_hooks(messages=messages, **ids)
    return {call.args[0]: call.args[1] for call in manager.registry.execute_hooks.call_args_list}


@pytest.mark.parametrize("metadata_user, registered_user, expected", [
    ("alice", "bob", "alice"),   # the session's user, as its run recorded it
    (None, "bob", "bob"),        # else the user the request was registered under
    (None, None, None),          # else nobody's
])
async def test_every_context_the_agent_builds_names_the_user(metadata_user, registered_user, expected):
    contexts = await _every_context(_manager(metadata_user, registered_user))

    assert len(contexts) == 10, sorted(t.value for t in contexts)
    assert {hook_type.value: context.user_id for hook_type, context in contexts.items()} == {
        hook_type.value: expected for hook_type in contexts}


class _Rebuilder(PluginHook):
    """Returns a context built anew, field by field -- as context_engineer and
    context_summarizer do -- naming no user_id."""

    async def on_pre_llm_call(self, context: HookContext) -> HookResult:
        rebuilt = HookContext(hook_type=context.hook_type, request_id=context.request_id,
                              session_id=context.session_id, messages=list(context.messages or []))
        return HookResult(success=True, modified=True, context=rebuilt)


class _Reader(PluginHook):
    seen: list = []

    async def on_pre_llm_call(self, context: HookContext) -> HookResult:
        self.seen.append(context.user_id)
        return HookResult(success=True, modified=False, context=context)


@pytest.mark.parametrize("rebuilt_before", [False, True])
async def test_the_registry_hands_every_hook_the_user(rebuilt_before):
    """Each hook gets a copy of the context; a hook's rebuilt context is the next
    one's. Neither may lose whose call it is."""
    registry = HookRegistry(default_timeout=5.0)
    reader = _Reader("reader", {})
    reader.seen = []
    if rebuilt_before:
        await registry.register_hook(HookType.PRE_LLM_CALL, "rebuilder", _Rebuilder("rebuilder", {}),
                                     order_spec={"before": ["reader"]})
    await registry.register_hook(HookType.PRE_LLM_CALL, "reader", reader)

    result = await registry.execute_hooks(HookType.PRE_LLM_CALL, HookContext(
        hook_type=HookType.PRE_LLM_CALL, request_id="req-1", session_id="sess-1",
        messages=[ChatMessage(role="user", content="hi")], user_id="alice"))

    assert reader.seen == ["alice"]
    assert result.user_id == "alice"


@pytest.mark.parametrize("run, registered, run_user, expected", [
    ("run-1", "bob", "alice", "alice"),  # the user of the run it is made in (a stategraph run's own id)
    ("run-1", "alice", None, "alice"),   # else the user its run's id was registered under
    ("run-1", None, None, None),         # a run nobody names: nobody's -- not "anonymous"
    (None, None, None, None),            # outside any run
])
async def test_a_call_no_agent_wires_names_the_user_of_its_run(monkeypatch, run, registered, run_user, expected):
    registry = Mock(execute_hooks=AsyncMock())
    import agent_system.hooks as hooks_module
    monkeypatch.setattr(hooks_module, "get_hook_registry", lambda: registry)
    if registered:
        register_request_user(run, registered)
    token = current_request_id.set(run)
    user_token = current_run_user.set(run_user)
    try:
        await hook_notify.notify_request(provider="p", model="m", url="u")
        await hook_notify.notify_response(provider="p", model="m", url="u")
    finally:
        current_run_user.reset(user_token)
        current_request_id.reset(token)

    assert [call.args[1].user_id for call in registry.execute_hooks.call_args_list] == [expected, expected]
