"""Tests for StuckEscalator (time-boxed, budget-capped model escalation) and the
tool-result error classifier used to trigger it."""

from __future__ import annotations

from types import SimpleNamespace

from agent_system.servers.agent.escalation import StuckEscalator
from agent_system.servers.agent.server import Agent


def make(enabled=True, rounds=2, max_calls=6):
    return StuckEscalator(enabled=enabled, rounds=rounds, max_calls=max_calls)


class TestWindow:
    def test_no_escalation_without_trigger(self):
        esc = make()
        assert not esc.active
        assert esc.begin_step() is False

    def test_trigger_opens_a_window_of_rounds(self):
        esc = make(rounds=2)
        assert esc.trigger("loop") == "loop"      # reason returned once
        assert esc.active
        assert esc.begin_step() is True           # step 1 advanced
        assert esc.begin_step() is True           # step 2 advanced
        assert esc.begin_step() is False          # window closed
        assert not esc.active

    def test_trigger_ignored_while_window_open(self):
        esc = make(rounds=3)
        assert esc.trigger("loop") == "loop"
        assert esc.trigger("loop again") is None   # already escalated → no reset
        # still exactly 3 advanced steps, not extended
        assert [esc.begin_step() for _ in range(4)] == [True, True, True, False]

    def test_retrigger_after_window_closes(self):
        esc = make(rounds=1, max_calls=6)
        assert esc.trigger("loop") == "loop"
        assert esc.begin_step() is True
        assert esc.begin_step() is False           # window closed
        # stuck again → new window
        assert esc.trigger("loop2") == "loop2"
        assert esc.begin_step() is True


class TestBudget:
    def test_budget_caps_total_advanced_calls(self):
        esc = make(rounds=3, max_calls=4)
        esc.trigger("a")
        used = sum(esc.begin_step() for _ in range(3))     # 3 advanced
        esc.trigger("b")                                    # window = min(3, 4-3)=1
        used += sum(esc.begin_step() for _ in range(3))     # only 1 more
        assert used == 4
        assert esc.calls_used == 4
        # budget exhausted → no more windows
        assert esc.trigger("c") is None
        assert esc.begin_step() is False

    def test_window_never_exceeds_remaining_budget(self):
        esc = make(rounds=10, max_calls=3)
        esc.trigger("a")
        assert sum(esc.begin_step() for _ in range(10)) == 3


class TestDisabled:
    def test_disabled_never_escalates(self):
        for esc in (make(enabled=False), make(rounds=0), make(max_calls=0)):
            assert esc.trigger("loop") is None
            assert esc.begin_step() is False
            assert not esc.enabled


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
        a.agent_config = SimpleNamespace(
            llm_profile=cfg.get("llm_profile", ["std", "advanced"]),
            available_llm_profiles=(cfg.get("llm_profile", ["std", "advanced"])
                                    if isinstance(cfg.get("llm_profile", ["std", "advanced"]), list)
                                    else [cfg.get("llm_profile")]),
            auto_escalate_on_stuck=cfg.get("auto_escalate_on_stuck", True),
            escalate_rounds=cfg.get("escalate_rounds", 2),
            escalate_max_calls=cfg.get("escalate_max_calls", 6),
        )
        return a

    def test_enabled_with_advanced_profile(self):
        esc = self._agent()._create_stuck_escalator(already_advanced=False)
        assert esc.enabled

    def test_disabled_without_advanced_profile(self):
        esc = self._agent(llm_profile=["only_one"])._create_stuck_escalator(already_advanced=False)
        assert not esc.enabled

    def test_disabled_when_already_advanced(self):
        esc = self._agent()._create_stuck_escalator(already_advanced=True)
        assert not esc.enabled

    def test_disabled_when_flag_off(self):
        esc = self._agent(auto_escalate_on_stuck=False)._create_stuck_escalator(already_advanced=False)
        assert not esc.enabled
