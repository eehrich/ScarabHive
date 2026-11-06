"""Which developer notes ride the user rung: those the model answered (message_roles.notes_that_ask)."""
from agent_system.llm.message_roles import DEVELOPER, notes_that_ask


def _ids(messages, asking):
    return [i for i, m in enumerate(messages) if id(m) in asking]


def test_the_note_ending_the_request_and_the_notes_beside_it_ask():
    msgs = [{"role": "user", "content": "go"}, {"role": "tool", "content": "r"},
            {"role": DEVELOPER, "content": "a"}, {"role": DEVELOPER, "content": "b"}]
    assert _ids(msgs, notes_that_ask(msgs)) == [2, 3]


def test_a_note_the_model_answered_still_asks():
    msgs = [{"role": "user", "content": "go"}, {"role": DEVELOPER, "content": "a"},
            {"role": "assistant", "content": "x"}, {"role": "user", "content": "next"}]
    assert _ids(msgs, notes_that_ask(msgs)) == [1]


def test_a_responses_function_call_is_an_answer_and_its_output_is_not():
    msgs = [{"type": "message", "role": "user", "content": "go"},
            {"type": "message", "role": DEVELOPER, "content": "a"},
            {"type": "function_call", "call_id": "c"},
            {"type": "message", "role": DEVELOPER, "content": "b"},
            {"type": "function_call_output", "call_id": "c"}]
    assert _ids(msgs, notes_that_ask(msgs)) == [1]


def test_an_output_item_nobody_listed_still_counts_as_the_answer():
    """A server tool's call (web_search_call) is the model's output too; listing outputs
    instead of what asks turned the note back to the developer rung behind it."""
    msgs = [{"type": "message", "role": "user", "content": "go"},
            {"type": "message", "role": DEVELOPER, "content": "a"},
            {"type": "web_search_call", "id": "ws"},
            {"type": "message", "role": "assistant", "content": "x"},
            {"type": "message", "role": "user", "content": "next"}]
    assert _ids(msgs, notes_that_ask(msgs)) == [1]
