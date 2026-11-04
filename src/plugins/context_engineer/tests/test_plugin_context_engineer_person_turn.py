"""The new turn is found behind what plugins appended after it.

tool_preload appends [assistant(injected_by), tool] after the person's
message; the walk stopped at that tool message, and media compaction on a user
message never fired on a turn a preload ran.
"""
from plugins.context_engineer.hooks import _person_turn_behind_injections


def test_behind_an_injected_tool_exchange():
    messages = [
        {"role": "user", "content": "old"},
        {"role": "assistant", "content": "a"},
        {"role": "user", "content": "new"},
        {"role": "assistant", "content": "", "injected_by": "tool_preload", "tool_calls": [{"id": "p"}]},
        {"role": "tool", "tool_call_id": "p", "content": "preloaded"},
        {"role": "user", "content": "note", "injected_by": "step_budget"},
    ]
    assert _person_turn_behind_injections(messages) == 2


def test_a_real_tool_result_is_not_skipped():
    messages = [
        {"role": "user", "content": "go"},
        {"role": "assistant", "content": "", "tool_calls": [{"id": "x"}]},
        {"role": "tool", "tool_call_id": "x", "content": "r"},
    ]
    assert _person_turn_behind_injections(messages) == 2
