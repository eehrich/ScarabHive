"""Tests for StuckEscalator (time-boxed, budget-capped model escalation) and the
tool-result error classifier used to trigger it."""

from __future__ import annotations

from types import SimpleNamespace

from agent_system.servers.agent.escalation import StuckEscalator
from agent_system.servers.agent.server import Agent


def make(enabled=True, rounds=2, max_calls=6):
    return StuckEscalator(enabled=enabled, rounds=rounds, max_calls=max_calls)


def step(esc) -> bool:
    """Mimic the run loop's happy path: a step runs advanced iff a window is
    open, and consumes a budget round only then."""
    advanced = esc.active
    if advanced:
        esc.consume()
    return advanced


class TestWindow:
    def test_no_escalation_without_trigger(self):
        esc = make()
        assert not esc.active
        assert step(esc) is False

    def test_trigger_opens_a_window_of_rounds(self):
        esc = make(rounds=2)
        assert esc.trigger("loop") == "loop"      # reason returned once
        assert esc.active
        assert step(esc) is True           # step 1 advanced
        assert step(esc) is True           # step 2 advanced
        assert step(esc) is False          # window closed
        assert not esc.active

    def test_trigger_ignored_while_window_open(self):
        esc = make(rounds=3)
        assert esc.trigger("loop") == "loop"
        assert esc.trigger("loop again") is None   # already escalated → no reset
        # still exactly 3 advanced steps, not extended
        assert [step(esc) for _ in range(4)] == [True, True, True, False]

    def test_retrigger_after_window_closes(self):
        esc = make(rounds=1, max_calls=6)
        assert esc.trigger("loop") == "loop"
        assert step(esc) is True
        assert step(esc) is False           # window closed
        # stuck again → new window
        assert esc.trigger("loop2") == "loop2"
        assert step(esc) is True

    def test_open_window_not_wasted_when_advanced_not_used(self):
        # A step that does NOT use the advanced model (rate-limit fallback /
        # client unavailable) must not consume the window: peek active, skip
        # consume() -> the round survives for the next real advanced call.
        esc = make(rounds=2)
        esc.trigger("loop")
        assert esc.active            # window open
        # ... step runs on a fallback: we peek but never call consume()
        assert esc.active            # still open, nothing spent
        assert esc.calls_used == 0
        assert step(esc) is True     # next real advanced step consumes
        assert esc.calls_used == 1


class TestBudget:
    def test_budget_caps_total_advanced_calls(self):
        esc = make(rounds=3, max_calls=4)
        esc.trigger("a")
        used = sum(step(esc) for _ in range(3))     # 3 advanced
        esc.trigger("b")                                    # window = min(3, 4-3)=1
        used += sum(step(esc) for _ in range(3))     # only 1 more
        assert used == 4
        assert esc.calls_used == 4
        # budget exhausted → no more windows
        assert esc.trigger("c") is None
        assert step(esc) is False

    def test_window_never_exceeds_remaining_budget(self):
        esc = make(rounds=10, max_calls=3)
        esc.trigger("a")
        assert sum(step(esc) for _ in range(10)) == 3


class TestDisabled:
    def test_disabled_never_escalates(self):
        for esc in (make(enabled=False), make(rounds=0), make(max_calls=0)):
            assert esc.trigger("loop") is None
            assert step(esc) is False
            assert not esc.enabled

    def test_disable_closes_window_and_stops_escalating(self):
        # When the advanced client can't be built the caller disables the
        # escalator so the (never-consumed) window doesn't stay open forever.
        esc = make(rounds=3)
        esc.trigger("loop")
        assert esc.active
        esc.disable()
        assert not esc.active
        assert not esc.enabled
        assert esc.trigger("loop again") is None   # stays off for the run


class TestToolMessageErrorClassifier:
    def _msg(self, content):
        return SimpleNamespace(content=content, role="tool")

    def test_status_error_shape(self):
        assert Agent._tool_message_is_error(self._msg('{"status": "error", "error": "x"}'))

    def test_bare_error_shape(self):
        assert Agent._tool_message_is_error(self._msg('{"error": "Channel not found"}'))

    def test_success_shapes_not_error(self):
        assert not Agent._tool_message_is_error(self._msg('{"status": "ok", "doc": "d"}'))
        assert not Agent._tool_message_is_error(self._msg('{"status": "posted", "message_id": 7}'))

    def test_error_none_with_status_not_error(self):
        assert not Agent._tool_message_is_error(self._msg('{"status": "ok", "error": null}'))

    def test_non_json_not_error(self):
        assert not Agent._tool_message_is_error(self._msg("just some text result"))
        assert not Agent._tool_message_is_error(self._msg(""))
        assert not Agent._tool_message_is_error(self._msg('["a", "list"]'))


class TestCreateStuckEscalatorGating:
    """The factory disables escalation unless it makes sense."""

    def _agent(self, **cfg):
        a = Agent.__new__(Agent)  # skip heavy __init__
        # Ketten-Semantik: Advanced-Modell kommt aus llm_profile_advanced[0]
        a.agent_config = SimpleNamespace(
            llm_profile=cfg.get("llm_profile", ["std"]),
            default_llm_profile=cfg.get("default_llm_profile", "std"),
            advanced_llm_profile=cfg.get("advanced_llm_profile", "advanced"),
            auto_escalate_on_stuck=cfg.get("auto_escalate_on_stuck", True),
            escalate_rounds=cfg.get("escalate_rounds", 2),
            escalate_max_calls=cfg.get("escalate_max_calls", 6),
        )
        return a

    def test_enabled_with_advanced_profile(self):
        esc = self._agent()._create_stuck_escalator(already_advanced=False)
        assert esc.enabled

    def test_disabled_without_advanced_profile(self):
        esc = self._agent(advanced_llm_profile=None)._create_stuck_escalator(already_advanced=False)
        assert not esc.enabled

    def test_disabled_when_advanced_equals_default(self):
        # advanced == default kann keinen anderen Client bauen (Guard
        # spiegelt _get_escalation_llm) — Escalator wäre ein toter Trigger.
        esc = self._agent(advanced_llm_profile="std")._create_stuck_escalator(already_advanced=False)
        assert not esc.enabled

    def test_disabled_when_already_advanced(self):
        esc = self._agent()._create_stuck_escalator(already_advanced=True)
        assert not esc.enabled

    def test_disabled_when_flag_off(self):
        esc = self._agent(auto_escalate_on_stuck=False)._create_stuck_escalator(already_advanced=False)
        assert not esc.enabled

    def test_only_an_override_off_the_agents_own_chain_is_a_switch(self):
        """--llm-params alone, or a machine call's llm_params or cheaper chain member, run one of the agent's own
        models: escalation stays on. Its advanced profile, a foreign one -- or a client that does not say
        which -- is a switch."""
        agent = self._agent(llm_profile=["std", "cheap"])
        assert not agent._switches_model(None)
        assert not agent._switches_model(SimpleNamespace(profile_name="std"))
        assert not agent._switches_model(SimpleNamespace(profile_name="cheap"))
        assert agent._switches_model(SimpleNamespace(profile_name="advanced"))
        assert agent._switches_model(SimpleNamespace(profile_name="other"))
        assert agent._switches_model(object())
        assert not self._agent(llm_profile="std")._switches_model(SimpleNamespace(profile_name="std"))
        # its advanced profile is a switch also where the chain has it as a fallback
        assert self._agent(llm_profile=["std", "advanced"])._switches_model(SimpleNamespace(profile_name="advanced"))
