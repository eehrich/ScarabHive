"""What a pre-LLM hook makes of the conversation is what the run saves.

The hook's list becomes the run's ``messages`` in the step loop
(_run_pre_llm_hooks / _select_llm_messages), and the run persists that list at
its end (_finalize_request). Nothing is staged in the session tracker for it:
HookIntegrationManager used to stage a copy there too, which the step loop
threw away right after the hooks, so it never was what got saved.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from agent_system.config.settings import load_settings
from agent_system.hooks import HookContext, HookRegistry, HookResult, HookType, PluginHook
from agent_system.llm.models import ChatMessage
from agent_system.servers.agent.server import Agent
from agent_system.tools.base import ToolServerRegistry

CONFIG = Path(__file__).resolve().parents[2] / "config" / "config.yaml"

BREADCRUMB = json.dumps({"type": "pruned_notice", "total_removed": 2, "hint": "older turns left the view"})


class AnswersAtOnce:
    """An LLM override that answers without tools and does not stream."""

    def supports_streaming(self) -> bool:
        return False

    async def chat_tools(self, messages, tools, cancellation_token=None, status_scope=None):
        return {"assistant": {"content": "the answer"}, "usage": {"total_tokens": 5}}


class DropsTheOldTurns(PluginHook):
    """Keeps the system messages and the last user message, with a breadcrumb for what went."""

    async def on_pre_llm_call(self, context: HookContext) -> HookResult:
        messages = context.messages or []
        system = [m for m in messages if m.role == "system"]
        rest = [m for m in messages if m.role != "system"]
        context.messages = system + [ChatMessage(role="system", content=BREADCRUMB)] + rest[-1:]
        return HookResult(success=True, modified=True, context=context)


@pytest.fixture
def agent() -> Agent:
    from agent_system.config.models import AgentConfig, ToolServerConfig

    system_config = load_settings(str(CONFIG))
    server_config = ToolServerConfig(type="agent", enabled=True,
                                     agent_config=getattr(system_config, "agent_config", None) or AgentConfig())
    agent = Agent("hook_persist_agent", system_config, server_config, ToolServerRegistry())
    agent._session_tracker.set_session_metadata("s1", {"user_id": "u1", "agent_name": agent.name,
                                                       "llm_profile": "normal"})
    agent._session_tracker.set_session_messages("s1", [
        ChatMessage(role="user", content="old question"),
        ChatMessage(role="assistant", content="old answer"),
    ])
    return agent


async def test_the_hook_modified_conversation_is_what_the_run_persists(agent):
    registry = HookRegistry(default_timeout=5.0)
    await registry.register_hook(HookType.PRE_LLM_CALL, "drops_old_turns", DropsTheOldTurns("drops_old_turns", {}))
    agent._hook_manager.registry = registry

    async for _ in agent.run_events("new question", request_id="r1", session_id="s1",
                                    llm_override=AnswersAtOnce()):
        pass

    saved = agent._session_tracker.get_session_messages("s1")
    contents = [m.content for m in saved]
    assert "old question" not in contents and "old answer" not in contents, "the hook's compaction was not saved"
    assert contents[-2:] == ["new question", "the answer"], contents
    assert BREADCRUMB in contents, "the breadcrumb the hook added was not saved"
    assert not any(m.role == "system" and m.content != BREADCRUMB for m in saved), "a prompt was saved"
    assert agent._session_tracker.get_compacted_messages("s1") is None, "something was left staged"
