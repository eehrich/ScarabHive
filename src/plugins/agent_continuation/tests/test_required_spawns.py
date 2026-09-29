"""required_spawns: an answer that skipped a required sub-agent goes back.

Measured on the story server (22.09.2026, 15 v6_story_panel sessions of three
days): the panel returned its result without spawning a single reviewer in
idee 2/2, titel 2/2, charakter 1/2 and milestones 1/2 — although the reviewers
stood in its rendered prompt. The gate reads what the session really spawned,
not what the answer claims.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from agent_system.hooks import HookContext, HookType
from agent_system.llm.models import ChatMessage
from plugins.agent_continuation.hooks import (
    REQUIRED_SPAWNS_MARKER, AgentContinuationPlugin)

PLUGIN_DIR = Path(__file__).resolve().parents[1]
TOOL = "v6_story_panel_sam_manage_sub_agent"
GATE = {
    "strategy": "rules",
    "default": "final",
    "max_continuations": 2,
    "required_spawns": {
        "agents": 'review_map.get(aufgabe, []) if panel_review_size > 0 and phase != "audit" else []',
        "tool": TOOL,
        "message": "3b fehlt: {missing}",
    },
}
REVIEW_MAP = {"charakter": ["v6_critic", "v6_creative_critic"]}


def _plugin() -> AgentContinuationPlugin:
    server_config = MagicMock()
    server_config.config = {"strategy": "rules", "max_continuations": 10, "agent_rules": {}}
    return AgentContinuationPlugin(PLUGIN_DIR, server_config)


def _agent(template_vars: dict, session_vars: dict):
    tracker = MagicMock()
    tracker.get_session_template_vars.return_value = session_vars
    return SimpleNamespace(agent_config=SimpleNamespace(template_vars=template_vars),
                           _session_tracker=tracker)


def _spawn(call_id: str, agent_type: str, *, operation: str | None = "create",
           result: dict | None = None) -> list[ChatMessage]:
    args = {"agent_type": agent_type, "task": "Review"}
    if operation:
        args["operation"] = operation
    call = {"id": call_id, "type": "function",
            "function": {"name": TOOL, "arguments": json.dumps(args)}}
    return [
        ChatMessage(role="assistant", content="", tool_calls=[call]),
        ChatMessage(role="tool", tool_call_id=call_id, name=TOOL,
                    content=json.dumps(result if result is not None
                                       else {"instance_id": f"sub_{call_id}", "status": "completed"})),
    ]


def _context(messages, *, hook_config=None, session_vars=None, request_id="req-1",
             template_vars=None):
    session_vars = {"aufgabe": "charakter", "phase": "synopsis", "panel_review_size": 1} \
        if session_vars is None else session_vars
    return HookContext(
        hook_type=HookType.POST_LLM_CALL,
        request_id=request_id,
        session_id="sess-1",
        agent=_agent({"review_map": REVIEW_MAP, "panel_review_size": 0}
                     if template_vars is None else template_vars, session_vars),
        agent_name="v6_story_panel",
        messages=messages,
        llm_response={"assistant": {"content": "# Resultat\n\nBestes Ergebnis ..."}},
        step=5,
        hook_config=dict(GATE if hook_config is None else hook_config),
    )


def _writers() -> list[ChatMessage]:
    return [m for i, w in enumerate(("inner", "outer", "relational"))
            for m in _spawn(f"w{i}", f"v6_synopsis_writer_{w}")]


class TestRequiredSpawns:

    @pytest.mark.asyncio
    async def test_an_answer_without_the_reviewers_goes_back_naming_them(self):
        result = await _plugin().evaluate_completion(_context(_writers()))
        assert result.metadata["continue"] is True
        assert result.metadata["continue_injected_by"] == REQUIRED_SPAWNS_MARKER
        assert result.metadata["continue_message"] == "3b fehlt: v6_critic, v6_creative_critic"

    @pytest.mark.asyncio
    async def test_only_the_missing_reviewer_is_named(self):
        messages = _writers() + _spawn("r1", "v6_critic")
        result = await _plugin().evaluate_completion(_context(messages))
        assert result.metadata["continue_message"] == "3b fehlt: v6_creative_critic"

    @pytest.mark.asyncio
    async def test_all_spawned_is_final_even_without_an_explicit_operation(self):
        # The manager infers create from agent_type; the panel relies on that.
        messages = _writers() + _spawn("r1", "v6_critic", operation=None) \
            + _spawn("r2", "v6_creative_critic", operation=None)
        result = await _plugin().evaluate_completion(_context(messages))
        assert not result.metadata.get("continue")

    @pytest.mark.asyncio
    async def test_a_refused_spawn_does_not_count(self):
        messages = _writers() + _spawn("r1", "v6_critic") + _spawn(
            "r2", "v6_creative_critic", result={"status": "error", "error": "not allowed"})
        result = await _plugin().evaluate_completion(_context(messages))
        assert result.metadata["continue_message"] == "3b fehlt: v6_creative_critic"

    @pytest.mark.asyncio
    async def test_a_spawn_through_another_tool_does_not_count(self):
        messages = _writers() + _spawn("r1", "v6_critic")
        for m in _spawn("r2", "v6_creative_critic"):
            for call in m.tool_calls or []:
                call["function"]["name"] = "other_sam_manage_sub_agent"
            messages.append(m)
        result = await _plugin().evaluate_completion(_context(messages))
        assert result.metadata["continue_message"] == "3b fehlt: v6_creative_critic"

    @pytest.mark.asyncio
    async def test_the_session_vars_decide_over_the_agent_defaults(self):
        # template_vars say panel_review_size 0; the coordinator's session says 1.
        result = await _plugin().evaluate_completion(_context(_writers()))
        assert result.metadata["continue"] is True
        off = await _plugin().evaluate_completion(_context(
            _writers(), request_id="req-2",
            session_vars={"aufgabe": "charakter", "phase": "synopsis", "panel_review_size": 0}))
        assert not off.metadata.get("continue")

    @pytest.mark.asyncio
    @pytest.mark.parametrize("session_vars", [
        {"aufgabe": "charakter", "phase": "audit", "panel_review_size": 1},
        {"aufgabe": "schauplatz_welten", "phase": "welten", "panel_review_size": 1},
    ])
    async def test_no_reviewers_required_is_final(self, session_vars):
        result = await _plugin().evaluate_completion(_context(_writers(), session_vars=session_vars))
        assert not result.metadata.get("continue")

    @pytest.mark.asyncio
    async def test_the_budget_ends_the_gate(self):
        plugin = _plugin()
        for _ in range(2):
            sent_back = await plugin.evaluate_completion(_context(_writers()))
            assert sent_back.metadata["continue"] is True
        result = await plugin.evaluate_completion(_context(_writers()))
        assert not result.metadata.get("continue")

    @pytest.mark.asyncio
    @pytest.mark.parametrize("spec", [
        {"agents": "review_map[", "tool": TOOL},       # syntax error
        {"agents": "undefined_var.get(aufgabe)", "tool": TOOL},
        {"agents": "review_map.get(aufgabe)"},          # no tool
        "not a mapping",
    ])
    async def test_a_broken_gate_lets_the_answer_through(self, spec):
        result = await _plugin().evaluate_completion(_context(
            _writers(), hook_config={**GATE, "required_spawns": spec}))
        assert not result.metadata.get("continue")


@pytest.fixture(scope="module")
def panel():
    """The panel's agent config through the real loader -- loaded once. A class-scoped fixture
    written as a method is deprecated in pytest 9 (its instance attributes never reach the tests)."""
    from agent_system.config.settings import get_tool_server_config, load_settings
    cfg = get_tool_server_config("v6_story_panel", load_settings())
    assert cfg is not None, "v6_story_panel not in the loaded config"
    return cfg.agent_config


class TestTheRealPanelConfig:
    """The panel YAML through the real loader: the gate and the prompt read the
    same review_map, and the configured expression names the tool the panel's
    manager really registers."""

    def _hooks(self, panel):
        hooks = panel.hooks if isinstance(panel.hooks, dict) else panel.hooks.model_dump()
        return hooks["overrides"]["agent_continuation.evaluate_completion"]

    @pytest.mark.asyncio
    async def test_a_charakter_panel_without_reviewers_goes_back(self, panel):
        hook_config = self._hooks(panel)
        assert hook_config["enabled"] is True
        ctx = _context(_writers(), hook_config=hook_config,
                       template_vars=dict(panel.template_vars))
        result = await _plugin().evaluate_completion(ctx)
        assert result.metadata["continue"] is True
        expected = ", ".join(panel.template_vars["review_map"]["charakter"])
        assert expected in result.metadata["continue_message"]

    def test_the_tool_is_the_panel_managers_tool(self, panel):
        from agent_system.config.settings import get_tool_server_config, load_settings
        tool = self._hooks(panel)["required_spawns"]["tool"]
        manager = tool.removesuffix("_manage_sub_agent")
        assert get_tool_server_config(manager, load_settings()) is not None, \
            f"no sub-agent manager named {manager!r}"
        assert f"{manager}/*" in panel.tools.allowed


def _response_with_calls(messages, calls, **kwargs) -> HookContext:
    ctx = _context(messages, **kwargs)
    ctx.llm_response = {"assistant": {"content": "", "tool_calls": calls}}
    return ctx


def _calls(*pairs):
    return [{"id": cid, "type": "function",
             "function": {"name": TOOL, "arguments": json.dumps({"agent_type": t})}}
            for cid, t in pairs]


class TestCompactedHistory:
    """context_engineer may replace old tool results by references or prune
    call and result together. A spawn the hook watched happen still counts."""

    @pytest.mark.asyncio
    async def test_a_spawn_pruned_from_the_history_still_counts(self):
        plugin = _plugin()
        await plugin.evaluate_completion(_response_with_calls(
            _writers(), _calls(("r1", "v6_critic"), ("r2", "v6_creative_critic"))))
        # The history the final answer is judged on no longer has those calls.
        result = await plugin.evaluate_completion(_context(_writers()))
        assert not result.metadata.get("continue")

    @pytest.mark.asyncio
    async def test_a_result_replaced_by_a_reference_still_counts(self):
        messages = _writers() + _spawn("r1", "v6_critic") + _spawn(
            "r2", "v6_creative_critic", result={"type": "tool_result_ref", "ref": "x"})
        result = await _plugin().evaluate_completion(_context(messages))
        assert not result.metadata.get("continue")

    @pytest.mark.asyncio
    async def test_a_watched_spawn_whose_result_says_error_does_not_count(self):
        plugin = _plugin()
        await plugin.evaluate_completion(_response_with_calls(
            _writers(), _calls(("r1", "v6_critic"), ("r2", "v6_creative_critic"))))
        messages = _writers() + _spawn("r1", "v6_critic") + _spawn(
            "r2", "v6_creative_critic",
            result={"instance_id": "sub_r2", "status": "completed", "outcome": "error"})
        result = await plugin.evaluate_completion(_context(messages))
        assert result.metadata["continue_message"] == "3b fehlt: v6_creative_critic"

    @pytest.mark.asyncio
    async def test_the_record_ends_with_the_final_answer(self):
        plugin = _plugin()
        await plugin.evaluate_completion(_response_with_calls(
            _writers(), _calls(("r1", "v6_critic"), ("r2", "v6_creative_critic"))))
        final = await plugin.evaluate_completion(_context(_writers()))
        assert not final.metadata.get("continue")
        assert plugin._seen_creates == {}
