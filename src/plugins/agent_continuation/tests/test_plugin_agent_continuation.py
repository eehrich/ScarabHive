"""Unit tests for agent_continuation plugin.

Tests the hook-based agent continuation system that prevents agents from
stopping prematurely with intermediate status reports.
"""

import pytest
from pathlib import Path
from unittest.mock import MagicMock, AsyncMock

from plugins.agent_continuation.hooks import AgentContinuationPlugin
from agent_system.hooks import HookContext, HookType


# --------------------------------------------------------------------------
# Fixtures
# --------------------------------------------------------------------------

PLUGIN_DIR = Path(__file__).resolve().parents[4] / "src" / "plugins" / "agent_continuation"


def _make_context(
    agent_name: str = "test_agent",
    content: str = "Some output",
    tool_calls: list | None = None,
    step: int = 1,
    request_id: str = "req-1",
    session_id: str = "sess-1",
    agent: object | None = None,
    hook_config: dict | None = None,
) -> HookContext:
    """Build a minimal HookContext for POST_LLM_CALL testing."""
    llm_response: dict = {
        "assistant": {
            "content": content,
        },
    }
    if tool_calls is not None:
        llm_response["assistant"]["tool_calls"] = tool_calls

    return HookContext(
        hook_type=HookType.POST_LLM_CALL,
        request_id=request_id,
        session_id=session_id,
        agent=agent,
        agent_name=agent_name,
        llm_response=llm_response,
        step=step,
        hook_config=hook_config or {},
    )


def _make_plugin(
    *,
    strategy: str = "rules",
    max_continuations: int = 10,
    agent_rules: dict | None = None,
    default_continue_message: str = "Continue with your task.",
) -> AgentContinuationPlugin:
    """Construct a plugin with custom config (bypassing plugins.yaml).

    Note: the hook is ``enabled: false`` by default in schema.yaml.
    Agents opt-in via ``hooks.overrides`` in their agent YAML.  The
    plugin itself does NOT gate by agent name — when the hook fires,
    the agent already opted in.
    """
    server_config = MagicMock()
    server_config.config = {
        "strategy": strategy,
        "max_continuations": max_continuations,
        "agent_rules": agent_rules or {},
        "default_continue_message": default_continue_message,
    }
    return AgentContinuationPlugin(PLUGIN_DIR, server_config)


# ===========================================================================
# Test initialization
# ===========================================================================

class TestPluginInitialization:
    """Test plugin construction and configuration."""

    def test_default_config_from_schema(self):
        """Plugin initializes with schema defaults when server_config has no config."""
        server_config = MagicMock()
        server_config.config = None
        plugin = AgentContinuationPlugin(PLUGIN_DIR, server_config)

        assert plugin._strategy == "rules"
        assert plugin._max_continuations == 10
        assert plugin._agent_rules == {}

    def test_custom_config_override(self):
        """Runtime config from plugins.yaml overrides schema defaults."""
        plugin = _make_plugin(
            strategy="hybrid",
            max_continuations=5,
        )

        assert plugin._strategy == "hybrid"
        assert plugin._max_continuations == 5

    def test_agent_rules_loaded(self):
        """Per-agent rules are loaded correctly."""
        rules = {
            "my_agent": {
                "strategy": "rules",
                "default": "continue",
                "rules": [
                    {"type": "keyword_final", "keywords": ["done"]},
                ],
            }
        }
        plugin = _make_plugin(agent_rules=rules)
        assert "my_agent" in plugin._agent_rules
        assert plugin._agent_rules["my_agent"]["default"] == "continue"


# ===========================================================================
# Test hook dispatch — skip / no-op scenarios
# ===========================================================================

class TestHookSkipScenarios:
    """Test cases where the hook should NOT intervene."""

    @pytest.mark.asyncio
    async def test_skip_when_tool_calls_present(self):
        """Tool-calling responses are ALWAYS skipped (loop continues anyway)."""
        plugin = _make_plugin(agent_rules={"test_agent": {"default": "continue"}})
        ctx = _make_context(
            content="status update",
            tool_calls=[{"id": "tc_1", "function": {"name": "do_stuff"}}],
        )

        result = await plugin.evaluate_completion(ctx)

        assert result.success is True
        assert result.modified is False
        assert result.metadata.get("continue") is not True

    @pytest.mark.asyncio
    async def test_skip_when_content_empty(self):
        """Empty responses are skipped."""
        plugin = _make_plugin(agent_rules={"test_agent": {"default": "continue"}})
        ctx = _make_context(content="")

        result = await plugin.evaluate_completion(ctx)

        assert result.success is True
        assert result.metadata.get("continue") is not True

    @pytest.mark.asyncio
    async def test_skip_when_content_whitespace(self):
        """Whitespace-only responses are skipped."""
        plugin = _make_plugin(agent_rules={"test_agent": {"default": "continue"}})
        ctx = _make_context(content="   \n\t  ")

        result = await plugin.evaluate_completion(ctx)

        assert result.success is True
        assert result.metadata.get("continue") is not True

    @pytest.mark.asyncio
    async def test_unconfigured_agent_uses_global_defaults(self):
        """Agents without agent_rules entry use global strategy defaults.

        Note: the hook registry gates activation per-agent (enabled: false
        by default).  If the hook fires, the agent opted in.  An agent
        without a specific agent_rules entry simply gets default='final'.
        """
        plugin = _make_plugin(
            strategy="rules",
            agent_rules={"other_agent": {"default": "continue"}},
        )
        ctx = _make_context(agent_name="unknown_agent")

        result = await plugin.evaluate_completion(ctx)

        # No agent_rules → _evaluate_rules gets empty dict → default "final"
        assert result.success is True
        assert result.metadata.get("continue") is not True


# ===========================================================================
# Test rule engine
# ===========================================================================

class TestRuleEngine:
    """Test the rule-based evaluation strategy."""

    @pytest.mark.asyncio
    async def test_keyword_continue_match(self):
        """keyword_continue rule triggers continuation when keyword found."""
        plugin = _make_plugin(
            strategy="rules",
            agent_rules={
                "test_agent": {
                    "default": "final",
                    "rules": [
                        {
                            "type": "keyword_continue",
                            "keywords": ["sub-agent completed", "next step"],
                        },
                    ],
                },
            },
        )
        ctx = _make_context(content="The sub-agent completed the first task.")

        result = await plugin.evaluate_completion(ctx)

        assert result.metadata.get("continue") is True
        assert "keyword_continue" in result.metadata.get("continuation_reason", "")

    @pytest.mark.asyncio
    async def test_keyword_continue_case_insensitive(self):
        """Keyword matching is case-insensitive."""
        plugin = _make_plugin(
            strategy="rules",
            agent_rules={
                "test_agent": {
                    "default": "final",
                    "rules": [
                        {"type": "keyword_continue", "keywords": ["NEXT STEP"]},
                    ],
                },
            },
        )
        ctx = _make_context(content="Proceeding to the next step now.")

        result = await plugin.evaluate_completion(ctx)

        assert result.metadata.get("continue") is True

    @pytest.mark.asyncio
    async def test_keyword_final_match(self):
        """keyword_final rule prevents continuation."""
        plugin = _make_plugin(
            strategy="rules",
            agent_rules={
                "test_agent": {
                    "default": "continue",
                    "rules": [
                        {"type": "keyword_continue", "keywords": ["status"]},
                        {"type": "keyword_final", "keywords": ["all tasks completed"]},
                    ],
                },
            },
        )
        ctx = _make_context(content="All tasks completed successfully.")

        result = await plugin.evaluate_completion(ctx)

        assert result.metadata.get("continue") is not True

    @pytest.mark.asyncio
    async def test_keyword_final_takes_priority_over_default_continue(self):
        """keyword_final overrides default='continue'."""
        plugin = _make_plugin(
            strategy="rules",
            agent_rules={
                "test_agent": {
                    "default": "continue",
                    "rules": [
                        {"type": "keyword_final", "keywords": ["workflow complete"]},
                    ],
                },
            },
        )
        ctx = _make_context(content="The workflow complete and everything looks good.")

        result = await plugin.evaluate_completion(ctx)

        assert result.metadata.get("continue") is not True

    @pytest.mark.asyncio
    async def test_keyword_continue_before_keyword_final(self):
        """When both keywords match same text, keyword_continue wins."""
        plugin = _make_plugin(
            strategy="rules",
            agent_rules={
                "test_agent": {
                    "default": "final",
                    "rules": [
                        # keyword_continue is listed first → checked first
                        {"type": "keyword_continue", "keywords": ["progress"]},
                        {"type": "keyword_final", "keywords": ["progress"]},
                    ],
                },
            },
        )
        ctx = _make_context(content="Making progress on the task.")

        result = await plugin.evaluate_completion(ctx)

        # keyword_continue wins — even when both match
        assert result.metadata.get("continue") is True

    @pytest.mark.asyncio
    async def test_keyword_continue_wins_over_final_regardless_of_order(self):
        """When both keyword_continue and keyword_final match, continue wins."""
        plugin = _make_plugin(
            strategy="rules",
            agent_rules={
                "test_agent": {
                    "default": "final",
                    "rules": [
                        # keyword_final is listed FIRST
                        {"type": "keyword_final", "keywords": ["fertig"]},
                        {"type": "keyword_continue", "keywords": ["problem"]},
                    ],
                },
            },
        )
        # Contains both "fertig" and "problem"
        ctx = _make_context(
            content="Das Buch ist eigentlich fertig aber es gibt ein Problem."
        )

        result = await plugin.evaluate_completion(ctx)

        assert result.metadata.get("continue") is True
        reason = result.metadata.get("continuation_reason", "")
        assert "overrides" in reason

    @pytest.mark.asyncio
    async def test_keyword_final_wins_when_continue_not_matched(self):
        """keyword_final triggers when no keyword_continue matches."""
        plugin = _make_plugin(
            strategy="rules",
            agent_rules={
                "test_agent": {
                    "default": "continue",
                    "rules": [
                        {"type": "keyword_final", "keywords": ["fertig"]},
                        {"type": "keyword_continue", "keywords": ["problem"]},
                    ],
                },
            },
        )
        # Contains only "fertig", not "problem"
        ctx = _make_context(content="Das Buch ist fertig und approved.")

        result = await plugin.evaluate_completion(ctx)

        assert result.metadata.get("continue") is not True

    @pytest.mark.asyncio
    async def test_min_length_rule_short_response(self):
        """Short responses trigger continuation with min_length rule."""
        plugin = _make_plugin(
            strategy="rules",
            agent_rules={
                "test_agent": {
                    "default": "final",
                    "rules": [
                        {"type": "min_length", "min_chars": 200},
                    ],
                },
            },
        )
        ctx = _make_context(content="Task started.")

        result = await plugin.evaluate_completion(ctx)

        assert result.metadata.get("continue") is True
        assert "too short" in result.metadata.get("continuation_reason", "")

    @pytest.mark.asyncio
    async def test_min_length_rule_long_response(self):
        """Long responses pass min_length check (no forced continuation)."""
        plugin = _make_plugin(
            strategy="rules",
            agent_rules={
                "test_agent": {
                    "default": "final",
                    "rules": [
                        {"type": "min_length", "min_chars": 10},
                    ],
                },
            },
        )
        ctx = _make_context(
            content="This is a thorough and detailed response to the request."
        )

        result = await plugin.evaluate_completion(ctx)

        # Length check passes → falls through to default (final)
        assert result.metadata.get("continue") is not True

    @pytest.mark.asyncio
    async def test_step_check_rule_below_min(self):
        """step_check triggers continuation when step < min_steps."""
        plugin = _make_plugin(
            strategy="rules",
            agent_rules={
                "test_agent": {
                    "default": "final",
                    "rules": [
                        {"type": "step_check", "min_steps": 5},
                    ],
                },
            },
        )
        ctx = _make_context(content="Here is my analysis.", step=2)

        result = await plugin.evaluate_completion(ctx)

        assert result.metadata.get("continue") is True
        assert "below min steps" in result.metadata.get("continuation_reason", "")

    @pytest.mark.asyncio
    async def test_step_check_rule_at_min(self):
        """step_check does NOT trigger when step >= min_steps."""
        plugin = _make_plugin(
            strategy="rules",
            agent_rules={
                "test_agent": {
                    "default": "final",
                    "rules": [
                        {"type": "step_check", "min_steps": 5},
                    ],
                },
            },
        )
        ctx = _make_context(content="Here is my analysis.", step=5)

        result = await plugin.evaluate_completion(ctx)

        assert result.metadata.get("continue") is not True

    @pytest.mark.asyncio
    async def test_default_continue_when_no_rules_match(self):
        """default='continue' keeps agent going when no rule matched."""
        plugin = _make_plugin(
            strategy="rules",
            agent_rules={
                "test_agent": {
                    "default": "continue",
                    "rules": [],
                },
            },
        )
        ctx = _make_context(content="Some intermediate output.")

        result = await plugin.evaluate_completion(ctx)

        assert result.metadata.get("continue") is True
        assert "default action is continue" in result.metadata.get(
            "continuation_reason", ""
        )

    @pytest.mark.asyncio
    async def test_default_final_when_no_rules_match(self):
        """default='final' lets agent stop when no rule matched."""
        plugin = _make_plugin(
            strategy="rules",
            agent_rules={
                "test_agent": {
                    "default": "final",
                    "rules": [],
                },
            },
        )
        ctx = _make_context(content="Some output.")

        result = await plugin.evaluate_completion(ctx)

        assert result.metadata.get("continue") is not True

    @pytest.mark.asyncio
    async def test_empty_rules_with_no_default(self):
        """No rules + no default → defaults to 'final'."""
        plugin = _make_plugin(
            strategy="rules",
            agent_rules={
                "test_agent": {
                    "rules": [],
                },
            },
        )
        ctx = _make_context(content="Some output.")

        result = await plugin.evaluate_completion(ctx)

        assert result.metadata.get("continue") is not True

    @pytest.mark.asyncio
    async def test_keyword_regex_final_match(self):
        """keyword_final with regex=true supports regex patterns."""
        plugin = _make_plugin(
            strategy="rules",
            agent_rules={
                "test_agent": {
                    "default": "continue",
                    "rules": [
                        {
                            "type": "keyword_final",
                            "regex": True,
                            "keywords": [r"buch\s+ist\s+(final\s+)?(freigegeben|approved)"],
                        },
                    ],
                },
            },
        )
        ctx = _make_context(content="Das Buch ist final freigegeben worden.")

        result = await plugin.evaluate_completion(ctx)

        # keyword_final matched → agent stops (no continue metadata)
        assert result.metadata.get("continue") is not True
        assert result.success is True

    @pytest.mark.asyncio
    async def test_keyword_regex_continue_flexible_matching(self):
        """keyword_continue with regex supports flexible patterns."""
        plugin = _make_plugin(
            strategy="rules",
            agent_rules={
                "test_agent": {
                    "default": "final",
                    "rules": [
                        {
                            "type": "keyword_continue",
                            "regex": True,
                            "keywords": [r"aktuelle[rns]?\s+(status|stand)"],
                        },
                    ],
                },
            },
        )
        ctx = _make_context(content="Hier ist der aktuelle Stand der Dinge.")

        result = await plugin.evaluate_completion(ctx)

        assert result.metadata.get("continue") is True
        assert "keyword_continue regex match" in result.metadata.get("continuation_reason", "")

    @pytest.mark.asyncio
    async def test_keyword_regex_case_insensitive(self):
        """Regex matching is case-insensitive via re.IGNORECASE."""
        plugin = _make_plugin(
            strategy="rules",
            agent_rules={
                "test_agent": {
                    "default": "final",
                    "rules": [
                        {
                            "type": "keyword_continue",
                            "regex": True,
                            "keywords": [r"\bBLOCKER-STOPP\b"],
                        },
                    ],
                },
            },
        )
        ctx = _make_context(content="Ich habe einen blocker-stopp erreicht.")

        result = await plugin.evaluate_completion(ctx)

        assert result.metadata.get("continue") is True

    @pytest.mark.asyncio
    async def test_keyword_regex_invalid_pattern_logs_warning(self):
        """Invalid regex patterns are caught and logged."""
        plugin = _make_plugin(
            strategy="rules",
            agent_rules={
                "test_agent": {
                    "default": "final",
                    "rules": [
                        {
                            "type": "keyword_final",
                            "regex": True,
                            "keywords": [r"\b(unclosed"],  # Invalid regex
                        },
                    ],
                },
            },
        )
        ctx = _make_context(content="Some content")

        result = await plugin.evaluate_completion(ctx)

        # Invalid regex is skipped, falls through to default
        assert result.metadata.get("continue") is not True


# ===========================================================================
# Test budget / max_continuations
# ===========================================================================

class TestContinuationBudget:
    """Test that the per-request continuation budget is enforced."""

    @pytest.mark.asyncio
    async def test_budget_increments(self):
        """Each continuation increments the counter."""
        plugin = _make_plugin(
            max_continuations=5,
            strategy="rules",
            agent_rules={
                "test_agent": {
                    "default": "continue",
                    "rules": [],
                },
            },
        )
        ctx = _make_context(content="Intermediate output")

        result1 = await plugin.evaluate_completion(ctx)
        result2 = await plugin.evaluate_completion(ctx)

        assert result1.metadata["continuation_count"] == 1
        assert result2.metadata["continuation_count"] == 2

    @pytest.mark.asyncio
    async def test_budget_exhausted(self):
        """After max_continuations, the hook stops returning continue=True."""
        plugin = _make_plugin(
            max_continuations=3,
            strategy="rules",
            agent_rules={
                "test_agent": {
                    "default": "continue",
                    "rules": [],
                },
            },
        )
        ctx = _make_context(content="Status update")

        # Exhaust budget
        for _ in range(3):
            result = await plugin.evaluate_completion(ctx)
            assert result.metadata.get("continue") is True

        # 4th call should NOT continue
        result = await plugin.evaluate_completion(ctx)
        assert result.metadata.get("continue") is not True

    @pytest.mark.asyncio
    async def test_budget_per_request_id(self):
        """Budgets are tracked per request_id, not globally."""
        plugin = _make_plugin(
            max_continuations=2,
            strategy="rules",
            agent_rules={
                "test_agent": {
                    "default": "continue",
                    "rules": [],
                },
            },
        )

        ctx_a = _make_context(content="Status A", request_id="req-A")
        ctx_b = _make_context(content="Status B", request_id="req-B")

        # Each request uses its own budget
        r1 = await plugin.evaluate_completion(ctx_a)
        r2 = await plugin.evaluate_completion(ctx_b)

        assert r1.metadata["continuation_count"] == 1
        assert r2.metadata["continuation_count"] == 1

        # Exhaust request A
        await plugin.evaluate_completion(ctx_a)  # count = 2
        r_a3 = await plugin.evaluate_completion(ctx_a)  # should be blocked
        assert r_a3.metadata.get("continue") is not True

        # Request B still has budget
        r_b2 = await plugin.evaluate_completion(ctx_b)
        assert r_b2.metadata.get("continue") is True

    @pytest.mark.asyncio
    async def test_budget_cleanup_on_final(self):
        """Counter is cleaned up when agent produces a final answer."""
        plugin = _make_plugin(
            max_continuations=10,
            strategy="rules",
            agent_rules={
                "test_agent": {
                    "default": "final",
                    "rules": [
                        {"type": "keyword_continue", "keywords": ["status"]},
                    ],
                },
            },
        )

        # Trigger a continuation
        ctx = _make_context(content="Status update in progress", request_id="req-X")
        r = await plugin.evaluate_completion(ctx)
        assert r.metadata.get("continue") is True
        assert "req-X" in plugin._continuation_counts

        # Now produce a final answer (no keyword match → default final)
        ctx_final = _make_context(content="All done!", request_id="req-X")
        r2 = await plugin.evaluate_completion(ctx_final)
        assert r2.metadata.get("continue") is not True
        assert "req-X" not in plugin._continuation_counts


# ===========================================================================
# Test hook activation model
# ===========================================================================

class TestPerAgentBudget:
    """The budget is one of the keys an agent may override.

    Until 2026-09-01 it was read from the plugin config only, while every
    other key honoured the agent's ``hooks.overrides``. Twenty agent YAMLs
    carried a ``max_continuations`` that did nothing — two of them asking for
    MORE than the plugin default (book_architect 20, book_polisher 15) and
    silently getting 10.
    """

    @pytest.mark.asyncio
    async def test_agent_budget_lower_than_plugin_budget_wins(self):
        """An agent asking for less stops earlier than the plugin default."""
        plugin = _make_plugin(max_continuations=10, strategy="rules")
        ctx = _make_context(
            content="Status update",
            hook_config={"default": "continue", "max_continuations": 2},
        )

        assert (await plugin.evaluate_completion(ctx)).metadata["continue"] is True
        assert (await plugin.evaluate_completion(ctx)).metadata["continue"] is True
        assert (await plugin.evaluate_completion(ctx)).metadata.get("continue") is None

    @pytest.mark.asyncio
    async def test_agent_budget_higher_than_plugin_budget_wins(self):
        """An agent asking for more is no longer capped at the plugin value."""
        plugin = _make_plugin(max_continuations=2, strategy="rules")
        ctx = _make_context(
            content="Status update",
            hook_config={"default": "continue", "max_continuations": 4},
        )

        for expected in (1, 2, 3, 4):
            result = await plugin.evaluate_completion(ctx)
            assert result.metadata["continue"] is True
            assert result.metadata["continuation_count"] == expected
        assert (await plugin.evaluate_completion(ctx)).metadata.get("continue") is None

    @pytest.mark.asyncio
    async def test_budget_from_agent_rules_in_plugins_yaml(self):
        """The same key works through agent_rules, not just hook_config."""
        plugin = _make_plugin(
            max_continuations=10,
            strategy="rules",
            agent_rules={"test_agent": {"default": "continue",
                                        "max_continuations": 1}},
        )
        ctx = _make_context(content="Status update")

        assert (await plugin.evaluate_completion(ctx)).metadata["continue"] is True
        assert (await plugin.evaluate_completion(ctx)).metadata.get("continue") is None

    @pytest.mark.asyncio
    async def test_plugin_budget_applies_without_agent_value(self):
        """No agent value: the plugin default still decides."""
        plugin = _make_plugin(max_continuations=1, strategy="rules")
        ctx = _make_context(content="Status update",
                            hook_config={"default": "continue"})

        assert (await plugin.evaluate_completion(ctx)).metadata["continue"] is True
        assert (await plugin.evaluate_completion(ctx)).metadata.get("continue") is None

    @pytest.mark.asyncio
    @pytest.mark.parametrize("bad_value", ["viele", None, 0, -3, [5], True, False])
    async def test_unusable_agent_value_falls_back_to_plugin_budget(self, bad_value):
        """A typo must not disable the hook — 0 would mean 'never continue'."""
        plugin = _make_plugin(max_continuations=2, strategy="rules")
        ctx = _make_context(
            content="Status update",
            hook_config={"default": "continue", "max_continuations": bad_value},
        )

        assert (await plugin.evaluate_completion(ctx)).metadata["continue"] is True
        assert (await plugin.evaluate_completion(ctx)).metadata["continue"] is True
        assert (await plugin.evaluate_completion(ctx)).metadata.get("continue") is None


class TestHookActivation:
    """Test that the hook always evaluates (gating is done by hook registry)."""

    @pytest.mark.asyncio
    async def test_any_agent_evaluated_when_hook_fires(self):
        """When the hook fires any agent is evaluated (no internal gating).

        Activation is controlled by the hook registry via schema.yaml
        ``enabled: false`` + per-agent ``hooks.overrides``.
        """
        plugin = _make_plugin(
            strategy="rules",
            agent_rules={},  # No specific rules
        )
        # No agent_rules → _evaluate_rules gets empty dict → default "final"
        ctx = _make_context(agent_name="random_agent", content="Some output")

        result = await plugin.evaluate_completion(ctx)

        # With empty agent_rules and default "final", should not continue
        # but the hook DID execute (no skip)
        assert result.success is True

    @pytest.mark.asyncio
    async def test_agent_with_rules_triggers_continuation(self):
        """An agent with matching agent_rules gets continuation."""
        plugin = _make_plugin(
            strategy="rules",
            agent_rules={
                "my_agent": {
                    "default": "continue",
                    "rules": [],
                },
            },
        )
        ctx = _make_context(agent_name="my_agent", content="Partial result")

        result = await plugin.evaluate_completion(ctx)

        assert result.metadata.get("continue") is True


# ===========================================================================
# Test hook_config (inline agent config via hooks.overrides)
# ===========================================================================

class TestHookConfigInlineConfig:
    """Test that hook_config from agent YAML overrides agent_rules."""

    @pytest.mark.asyncio
    async def test_hook_config_takes_priority_over_agent_rules(self):
        """context.hook_config overrides agent_rules from plugins.yaml."""
        plugin = _make_plugin(
            strategy="rules",
            agent_rules={
                "test_agent": {
                    "default": "final",  # plugins.yaml says final
                    "rules": [],
                },
            },
        )
        # Agent YAML says continue via hook_config
        ctx = _make_context(
            content="Some output",
            hook_config={
                "default": "continue",  # agent override says continue
                "rules": [],
            },
        )

        result = await plugin.evaluate_completion(ctx)

        # hook_config wins → continue
        assert result.metadata.get("continue") is True

    @pytest.mark.asyncio
    async def test_hook_config_keyword_rules(self):
        """Inline keyword rules via hook_config work correctly."""
        plugin = _make_plugin(strategy="rules")
        ctx = _make_context(
            content="Sub-agent abgeschlossen, nächster Schritt folgt.",
            hook_config={
                "default": "final",
                "rules": [
                    {
                        "type": "keyword_continue",
                        "keywords": ["sub-agent abgeschlossen"],
                    },
                ],
            },
        )

        result = await plugin.evaluate_completion(ctx)

        assert result.metadata.get("continue") is True

    @pytest.mark.asyncio
    async def test_hook_config_final_keyword(self):
        """Inline final keyword via hook_config stops the agent."""
        plugin = _make_plugin(strategy="rules")
        ctx = _make_context(
            content="Buch ist fertig und approved.",
            hook_config={
                "default": "continue",
                "rules": [
                    {
                        "type": "keyword_final",
                        "keywords": ["buch ist fertig"],
                    },
                ],
            },
        )

        result = await plugin.evaluate_completion(ctx)

        assert result.metadata.get("continue") is not True

    @pytest.mark.asyncio
    async def test_hook_config_custom_continue_message(self):
        """continue_message from hook_config is used."""
        plugin = _make_plugin(
            default_continue_message="Global default",
            strategy="rules",
        )
        ctx = _make_context(
            content="Status report",
            hook_config={
                "default": "continue",
                "continue_message": "Inline: weitermachen!",
                "rules": [],
            },
        )

        result = await plugin.evaluate_completion(ctx)

        assert result.metadata["continue_message"] == "Inline: weitermachen!"

    @pytest.mark.asyncio
    async def test_hook_config_strategy_override(self):
        """Per-agent strategy from hook_config overrides global."""
        plugin = _make_plugin(strategy="llm")  # global

        ctx = _make_context(
            content="Output",
            hook_config={
                "strategy": "rules",  # agent says rules
                "default": "continue",
                "rules": [],
            },
        )

        # Should use rules, not LLM
        result = await plugin.evaluate_completion(ctx)

        assert result.metadata.get("continue") is True
        assert "default action" in result.metadata.get("continuation_reason", "")

    @pytest.mark.asyncio
    async def test_empty_hook_config_falls_back_to_agent_rules(self):
        """When hook_config is empty, agent_rules from plugins.yaml is used."""
        plugin = _make_plugin(
            strategy="rules",
            agent_rules={
                "test_agent": {
                    "default": "continue",
                    "rules": [],
                },
            },
        )
        ctx = _make_context(
            content="Output",
            hook_config={},  # empty → fallback
        )

        result = await plugin.evaluate_completion(ctx)

        assert result.metadata.get("continue") is True


# ===========================================================================
# Test HookResult metadata structure
# ===========================================================================

class TestHookResultStructure:
    """Test that the HookResult has the expected shape for the core loop."""

    @pytest.mark.asyncio
    async def test_continuation_result_metadata_shape(self):
        """Continuation result contains all expected metadata keys."""
        plugin = _make_plugin(
            strategy="rules",
            default_continue_message="Keep working!",
            agent_rules={
                "test_agent": {
                    "default": "continue",
                    "rules": [],
                },
            },
        )
        ctx = _make_context(content="Status report")

        result = await plugin.evaluate_completion(ctx)

        assert result.success is True
        assert result.modified is False
        assert result.metadata["continue"] is True
        assert result.metadata["continue_message"] == "Keep working!"
        assert result.metadata["continuation_count"] == 1
        assert isinstance(result.metadata["continuation_reason"], str)

    @pytest.mark.asyncio
    async def test_custom_continue_message_per_agent(self):
        """Per-agent continue_message overrides the global default."""
        plugin = _make_plugin(
            default_continue_message="Global default",
            strategy="rules",
            agent_rules={
                "test_agent": {
                    "default": "continue",
                    "continue_message": "Agent-specific: keep going!",
                    "rules": [],
                },
            },
        )
        ctx = _make_context(content="Intermediate output")

        result = await plugin.evaluate_completion(ctx)

        assert result.metadata["continue_message"] == "Agent-specific: keep going!"

    @pytest.mark.asyncio
    async def test_non_continuation_result_has_no_continue_key(self):
        """When not continuing, metadata should not contain 'continue'."""
        plugin = _make_plugin(
            strategy="rules",
            agent_rules={
                "test_agent": {
                    "default": "final",
                    "rules": [],
                },
            },
        )
        ctx = _make_context(content="Final answer.")

        result = await plugin.evaluate_completion(ctx)

        assert "continue" not in result.metadata


# ===========================================================================
# Test per-agent strategy override
# ===========================================================================

class TestPerAgentStrategyOverride:
    """Test that per-agent strategy overrides the global strategy."""

    @pytest.mark.asyncio
    async def test_agent_uses_rules_while_global_is_llm(self):
        """Agent's strategy takes precedence over global strategy."""
        plugin = _make_plugin(
            strategy="llm",  # global
            agent_rules={
                "test_agent": {
                    "strategy": "rules",  # override
                    "default": "continue",
                    "rules": [],
                },
            },
        )
        ctx = _make_context(content="Output")

        # Should use rules (no LLM call), default continue
        result = await plugin.evaluate_completion(ctx)

        assert result.metadata.get("continue") is True
        assert "default action is continue" in result.metadata.get(
            "continuation_reason", ""
        )


# ===========================================================================
# Test LLM evaluator
# ===========================================================================

class TestLLMEvaluator:
    """Test the LLM-based evaluation strategy."""

    @pytest.mark.asyncio
    async def test_llm_evaluation_continue(self):
        """LLM evaluator returning 'CONTINUE' triggers continuation."""
        plugin = _make_plugin(
            strategy="llm",
        )

        # Mock the evaluator LLM
        mock_llm = AsyncMock()
        mock_llm.chat = AsyncMock(return_value={
            "assistant": {"content": "CONTINUE"},
        })
        plugin._evaluator_llm = mock_llm

        ctx = _make_context(content="Status: sub-agent started task 1 of 5.")

        result = await plugin.evaluate_completion(ctx)

        assert result.metadata.get("continue") is True
        assert "LLM evaluation: CONTINUE" in result.metadata.get(
            "continuation_reason", ""
        )

    @pytest.mark.asyncio
    async def test_llm_evaluation_final(self):
        """LLM evaluator returning 'FINAL' lets agent stop."""
        plugin = _make_plugin(
            strategy="llm",
        )

        mock_llm = AsyncMock()
        mock_llm.chat = AsyncMock(return_value={
            "assistant": {"content": "FINAL"},
        })
        plugin._evaluator_llm = mock_llm

        ctx = _make_context(content="Here is the complete analysis.")

        result = await plugin.evaluate_completion(ctx)

        assert result.metadata.get("continue") is not True

    @pytest.mark.asyncio
    async def test_llm_evaluation_error_fallback_to_final(self):
        """When LLM call fails, default to final (safe fallback)."""
        plugin = _make_plugin(
            strategy="llm",
        )

        mock_llm = AsyncMock()
        mock_llm.chat = AsyncMock(side_effect=Exception("API error"))
        plugin._evaluator_llm = mock_llm

        ctx = _make_context(content="Some output")

        result = await plugin.evaluate_completion(ctx)

        assert result.metadata.get("continue") is not True

    @pytest.mark.asyncio
    async def test_llm_no_evaluator_available(self):
        """When no evaluator LLM can be created, default to final."""
        plugin = _make_plugin(
            strategy="llm",
        )
        # No mock set and context has no agent → _get_evaluator_llm returns None

        ctx = _make_context(content="Some output", agent=None)

        result = await plugin.evaluate_completion(ctx)

        assert result.metadata.get("continue") is not True


# ===========================================================================
# Test hybrid strategy
# ===========================================================================

class TestHybridStrategy:
    """Test the hybrid strategy (rules first, LLM fallback when no match)."""

    @pytest.mark.asyncio
    async def test_hybrid_rules_say_continue_skips_llm(self):
        """When rules say 'continue', LLM is not called."""
        plugin = _make_plugin(
            strategy="hybrid",
            agent_rules={
                "test_agent": {
                    "strategy": "hybrid",
                    "default": "final",
                    "rules": [
                        {"type": "keyword_continue", "keywords": ["status"]},
                    ],
                },
            },
        )

        mock_llm = AsyncMock()
        mock_llm.chat = AsyncMock(return_value={
            "assistant": {"content": "FINAL"},
        })
        plugin._evaluator_llm = mock_llm

        ctx = _make_context(content="Status: task in progress")

        result = await plugin.evaluate_completion(ctx)

        # Rules matched keyword_continue → continue without LLM
        assert result.metadata.get("continue") is True
        mock_llm.chat.assert_not_called()

    @pytest.mark.asyncio
    async def test_hybrid_keyword_final_skips_llm(self):
        """When keyword_final matched, LLM is NOT called (rule is decisive)."""
        plugin = _make_plugin(
            strategy="hybrid",
            agent_rules={
                "test_agent": {
                    "strategy": "hybrid",
                    "default": "final",
                    "rules": [
                        {"type": "keyword_final", "keywords": ["complete"]},
                    ],
                },
            },
        )

        mock_llm = AsyncMock()
        mock_llm.chat = AsyncMock(return_value={
            "assistant": {"content": "CONTINUE"},
        })
        plugin._evaluator_llm = mock_llm

        ctx = _make_context(content="Everything is complete now.")

        result = await plugin.evaluate_completion(ctx)

        # keyword_final matched → final, LLM not consulted
        assert result.metadata.get("continue") is not True
        mock_llm.chat.assert_not_called()

    @pytest.mark.asyncio
    async def test_hybrid_no_match_falls_to_llm_continue(self):
        """When no keyword matched, LLM decides — here LLM says CONTINUE."""
        plugin = _make_plugin(
            strategy="hybrid",
            agent_rules={
                "test_agent": {
                    "strategy": "hybrid",
                    "default": "final",
                    "rules": [
                        {"type": "keyword_final", "keywords": ["all done"]},
                    ],
                },
            },
        )

        mock_llm = AsyncMock()
        mock_llm.chat = AsyncMock(return_value={
            "assistant": {"content": "CONTINUE"},
        })
        plugin._evaluator_llm = mock_llm

        ctx = _make_context(content="I have finished the first phase.")

        result = await plugin.evaluate_completion(ctx)

        # No keyword matched → LLM called → CONTINUE
        assert result.metadata.get("continue") is True
        mock_llm.chat.assert_called_once()

    @pytest.mark.asyncio
    async def test_hybrid_no_match_falls_to_llm_final(self):
        """When no keyword matched, LLM decides — here LLM says FINAL."""
        plugin = _make_plugin(
            strategy="hybrid",
            agent_rules={
                "test_agent": {
                    "strategy": "hybrid",
                    "default": "final",
                    "rules": [],
                },
            },
        )

        mock_llm = AsyncMock()
        mock_llm.chat = AsyncMock(return_value={
            "assistant": {"content": "FINAL"},
        })
        plugin._evaluator_llm = mock_llm

        ctx = _make_context(content="Here is my complete analysis.")

        result = await plugin.evaluate_completion(ctx)

        # No rule matched → LLM called → FINAL
        assert result.metadata.get("continue") is not True
        mock_llm.chat.assert_called_once()


# ===========================================================================
# Test complex / end-to-end scenarios
# ===========================================================================

class TestEndToEnd:
    """Integration-style tests simulating realistic multi-step scenarios."""

    @pytest.mark.asyncio
    async def test_multi_step_continuation_then_final(self):
        """Simulate agent producing intermediate reports then a final answer."""
        plugin = _make_plugin(
            max_continuations=5,
            strategy="rules",
            agent_rules={
                "book_architect": {
                    "default": "continue",
                    "rules": [
                        {
                            "type": "keyword_continue",
                            "keywords": [
                                "sub-agent completed",
                                "validation complete",
                            ],
                        },
                        {
                            "type": "keyword_final",
                            "keywords": [
                                "all tasks completed",
                                "workflow complete",
                            ],
                        },
                    ],
                },
            },
        )

        request_id = "req-book-1"

        # Step 1: Intermediate report
        ctx1 = _make_context(
            agent_name="book_architect",
            content="Sub-agent completed chapter outline. Moving to next task.",
            request_id=request_id,
            step=1,
        )
        r1 = await plugin.evaluate_completion(ctx1)
        assert r1.metadata.get("continue") is True
        assert r1.metadata["continuation_count"] == 1

        # Step 2: Another intermediate
        ctx2 = _make_context(
            agent_name="book_architect",
            content="Validation complete for chapters 1–3. Starting chapter 4.",
            request_id=request_id,
            step=2,
        )
        r2 = await plugin.evaluate_completion(ctx2)
        assert r2.metadata.get("continue") is True
        assert r2.metadata["continuation_count"] == 2

        # Step 3: Final answer
        ctx3 = _make_context(
            agent_name="book_architect",
            content="All tasks completed. Here is the final book plan: ...",
            request_id=request_id,
            step=5,
        )
        r3 = await plugin.evaluate_completion(ctx3)
        assert r3.metadata.get("continue") is not True

        # Counter should be cleaned up
        assert request_id not in plugin._continuation_counts

    @pytest.mark.asyncio
    async def test_agent_with_no_matching_keywords_uses_default(self):
        """When no keywords match, default action applies."""
        plugin = _make_plugin(
            strategy="rules",
            agent_rules={
                "writer_agent": {
                    "default": "continue",
                    "rules": [
                        {
                            "type": "keyword_final",
                            "keywords": ["manuscript complete"],
                        },
                    ],
                },
            },
        )

        ctx = _make_context(
            agent_name="writer_agent",
            content="I've finished the first section. Moving on.",
        )

        result = await plugin.evaluate_completion(ctx)

        # No keyword matched → default continue
        assert result.metadata.get("continue") is True
        assert "default action" in result.metadata.get("continuation_reason", "")


# ===========================================================================
# SchemaBasedPluginHook dispatch: target_hook_name
# ===========================================================================

class TestSchemaDispatchWithTargetHookName:
    """Verify that on_post_llm_call dispatches correctly based on
    target_hook_name, fixing the bug where enabled=false in schema
    prevented the hook from firing even when the agent overrides it."""

    @pytest.mark.asyncio
    async def test_dispatch_with_target_hook_name_overrides_schema_enabled(self):
        """Hook with enabled=false in schema fires when
        target_hook_name is set (agent override via registry)."""
        plugin = _make_plugin(
            strategy="rules",
            agent_rules={
                "my_agent": {
                    "default": "continue",
                    "rules": [],
                },
            },
        )

        ctx = _make_context(
            agent_name="my_agent",
            content="Intermediate status report. What should I do next?",
        )
        # Simulate what the registry does: set target_hook_name
        ctx.target_hook_name = "evaluate_completion"

        result = await plugin.on_post_llm_call(ctx)

        # The hook SHOULD have fired despite schema enabled=false
        assert result.metadata.get("continue") is True

    @pytest.mark.asyncio
    async def test_dispatch_without_target_hook_name_uses_schema_enabled(self):
        """Without target_hook_name, fallback to schema-level enabled
        (backward compat). Since schema says enabled=false, hook does
        NOT fire."""
        plugin = _make_plugin(
            strategy="rules",
            agent_rules={
                "my_agent": {
                    "default": "continue",
                    "rules": [],
                },
            },
        )

        ctx = _make_context(
            agent_name="my_agent",
            content="Intermediate status report. What should I do next?",
        )
        # No target_hook_name set → old behavior

        result = await plugin.on_post_llm_call(ctx)

        # Schema says enabled=false → hook should NOT fire
        assert result.metadata.get("continue") is None or result.metadata.get("continue") is not True

    @pytest.mark.asyncio
    async def test_dispatch_target_hook_name_mismatched_skips(self):
        """If target_hook_name doesn't match the hook name, it's skipped."""
        plugin = _make_plugin(
            strategy="rules",
            agent_rules={
                "my_agent": {
                    "default": "continue",
                    "rules": [],
                },
            },
        )

        ctx = _make_context(
            agent_name="my_agent",
            content="Intermediate status report.",
        )
        ctx.target_hook_name = "some_other_hook"

        result = await plugin.on_post_llm_call(ctx)

        # No matching hook → nothing fired
        assert result.metadata.get("continue") is None or result.metadata.get("continue") is not True


# ===========================================================================
# Scripted follow-ups
# ===========================================================================

FOLLOWUPS = ["Review it once more.", "Now the complete final result."]


def _history(*markers):
    """A request from a person, then one user message per marker."""
    from agent_system.llm.models import ChatMessage
    from plugins.agent_continuation.hooks import FOLLOWUP_MARKER

    messages = [ChatMessage(role="user", content="Score chapter 3."),
                ChatMessage(role="assistant", content="Score: 7")]
    for marker in markers:
        injected = {"followup": FOLLOWUP_MARKER, "continue": "agent_continuation",
                    "person": None}[marker]
        messages.append(ChatMessage(role="user", content="...", injected_by=injected))
        messages.append(ChatMessage(role="assistant", content="Score: 6"))
    return messages


def _followup_context(messages, **config):
    ctx = _make_context(content="Final score: 6", hook_config={"followups": FOLLOWUPS, **config})
    ctx.messages = messages
    return ctx


class TestFollowups:

    @pytest.mark.asyncio
    async def test_sent_in_order_one_per_final_answer_then_final(self):
        from plugins.agent_continuation.hooks import FOLLOWUP_MARKER

        plugin = _make_plugin()
        first = await plugin.evaluate_completion(_followup_context(_history()))
        assert first.metadata["continue_message"] == FOLLOWUPS[0]
        assert first.metadata["continue_injected_by"] == FOLLOWUP_MARKER

        second = await plugin.evaluate_completion(_followup_context(_history("followup")))
        assert second.metadata["continue_message"] == FOLLOWUPS[1]

        done = await plugin.evaluate_completion(_followup_context(_history("followup", "followup")))
        assert not done.metadata.get("continue")

    @pytest.mark.asyncio
    async def test_a_message_from_a_person_starts_the_list_again(self):
        plugin = _make_plugin()
        result = await plugin.evaluate_completion(
            _followup_context(_history("followup", "followup", "person")))
        assert result.metadata["continue_message"] == FOLLOWUPS[0]

    @pytest.mark.asyncio
    async def test_a_plain_continuation_is_not_mistaken_for_a_person(self):
        plugin = _make_plugin()
        result = await plugin.evaluate_completion(
            _followup_context(_history("followup", "continue")))
        assert result.metadata["continue_message"] == FOLLOWUPS[1]

    @pytest.mark.asyncio
    async def test_plain_continuations_are_marked(self):
        plugin = _make_plugin()
        result = await plugin.evaluate_completion(_make_context(
            content="I will now read the file", hook_config={"default": "continue"}))
        assert result.metadata["continue_injected_by"] == "agent_continuation"

    @pytest.mark.asyncio
    async def test_a_status_report_gets_the_continue_message_first(self):
        plugin = _make_plugin()
        ctx = _followup_context(_history(), rules=[
            {"type": "keyword_continue", "keywords": ["final score"]}],
            continue_message="Keep going.")
        result = await plugin.evaluate_completion(ctx)
        assert result.metadata["continue_message"] == "Keep going."

    @pytest.mark.asyncio
    async def test_no_followup_past_the_budget(self):
        plugin = _make_plugin()
        plugin._continuation_counts["req-1"] = 2
        result = await plugin.evaluate_completion(
            _followup_context(_history(), max_continuations=2))
        assert not result.metadata.get("continue")

    @pytest.mark.asyncio
    async def test_a_single_string_is_one_followup_not_one_per_character(self):
        plugin = _make_plugin()
        first = await plugin.evaluate_completion(
            _followup_context(_history(), followups="Review it once more."))
        assert first.metadata["continue_message"] == "Review it once more."
        done = await plugin.evaluate_completion(
            _followup_context(_history("followup"), followups="Review it once more."))
        assert not done.metadata.get("continue")

    @pytest.mark.asyncio
    async def test_followups_can_be_limited_to_the_first_request_of_a_session(self):
        from agent_system.llm.models import ChatMessage

        def prompted(*markers):
            # as the server builds it: system prompt and tool note before the session
            return [ChatMessage(role="system", content="You score chapters."),
                    ChatMessage(role="system", content="Tools: none."), *_history(*markers)]

        plugin = _make_plugin()
        first = await plugin.evaluate_completion(_followup_context(prompted(), followups_on_continue=False))
        assert first.metadata["continue_message"] == FOLLOWUPS[0]
        second = await plugin.evaluate_completion(
            _followup_context(prompted("followup"), followups_on_continue=False))
        assert second.metadata["continue_message"] == FOLLOWUPS[1], "the first request keeps its whole list"
        continued = await plugin.evaluate_completion(
            _followup_context(prompted("followup", "followup", "person"), followups_on_continue=False))
        assert not continued.metadata.get("continue"), "a continued session gets no follow-up"
        default = await plugin.evaluate_completion(_followup_context(prompted("followup", "followup", "person")))
        assert default.metadata["continue_message"] == FOLLOWUPS[0], "default unchanged"

    @pytest.mark.asyncio
    async def test_a_quoted_false_does_not_pass_for_the_boolean(self, caplog):
        plugin = _make_plugin()
        continued = _history("followup", "followup", "person")
        with caplog.at_level("WARNING"):
            result = await plugin.evaluate_completion(_followup_context(continued, followups_on_continue="false"))
        assert result.metadata["continue_message"] == FOLLOWUPS[0]
        assert "followups_on_continue='false' is not a boolean" in caplog.text

    @pytest.mark.asyncio
    async def test_followups_count_against_the_budget(self):
        plugin = _make_plugin()
        await plugin.evaluate_completion(_followup_context(_history()))
        assert plugin._continuation_counts["req-1"] == 1
