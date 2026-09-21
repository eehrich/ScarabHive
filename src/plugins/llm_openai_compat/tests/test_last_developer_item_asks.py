"""A request must not END on a message that demands no answer.

A `developer` message is read but asks for nothing -- that is its definition,
and for the notes the loop adds mid-turn it is exactly right. The last item is
different: it is what the model is being asked, and a woken run's whole task
arrives that way.

Measured 21.09.2026 through the production client, real wake history from
`data/sessions/admin/wakeprobe02.json`:

    ~deepseek/deepseek-v4-flash-latest   7/15 empty answers  ->  0/10 after
    google/gemini-3.5-flash-lite         10/10 HTTP 400      ->  0/3  after

Google says it outright: "Requests ending with a model turn are not
supported." It does not count a developer item as a turn, so the request ends
on the model's own -- which is the shape every wake has, because a wake
follows an answer. DeepSeek does not refuse; it continues the previous text
instead of answering. One cause, two symptoms.

Lowered HERE and not where such a message is built: the stored transcript has
to keep saying who spoke (`developer` plus `injected_by`, held by
tests/agent/test_agent_step_budget_note.py), and there is more than one place
that appends one.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[3]))

from agent_system.llm.capabilities import ModelCapabilities
from agent_system.llm.message_roles import DEVELOPER, NOTE_CLOSE, NOTE_OPEN, USER
from plugins.llm_openai_compat.openai_responses_client import OpenAIResponsesClient


def _client(**kw):
    defaults = dict(model="~deepseek/deepseek-v4-flash-latest", api_key="sk-or-test",
                    base_url="https://openrouter.ai/api/v1")
    defaults.update(kw)
    return OpenAIResponsesClient(**defaults)


def _wake_shaped_items():
    """What a woken run sends: an answer, then the run's task. The message in
    front of the last item is the MODEL's -- that is what makes Google read the
    request as ending on a model turn."""
    return [
        {"role": USER, "content": "count to three"},
        {"role": DEVELOPER, "content": "step 4 of 30"},          # a note mid-turn
        {"role": "assistant", "content": "one two three"},
        {"role": DEVELOPER, "content": "You were woken. Act on it."},
    ]


def test_the_last_developer_item_is_turned_into_a_turn_that_asks():
    items = _wake_shaped_items()
    _client()._lower_developer_items(items)

    assert items[-1]["role"] == USER, "the request still ends on a message asking nothing"
    assert items[-1]["content"] == f"{NOTE_OPEN}\nYou were woken. Act on it.\n{NOTE_CLOSE}", (
        "lowered without the tags the role no longer carries: on the user rung "
        "nothing else says the run is speaking, not a person")


def test_a_developer_note_in_the_middle_keeps_its_role():
    """The other half, or the fix would just be 'no developer messages'.

    Removing them does NOT heal -- measured on the same payload: with every
    developer item deleted, 5/6 answers stayed empty and Gemini still refused.
    What heals is a last item that asks. A note bound to a turn that HAS
    something open is right where it is, and lowering it would put a second
    user turn into the middle of the conversation.
    """
    items = _wake_shaped_items()
    _client()._lower_developer_items(items)

    assert items[1]["role"] == DEVELOPER, "a mid-turn note was lowered too"
    assert items[1]["content"] == "step 4 of 30", "a mid-turn note was wrapped"


def test_a_route_that_takes_no_developer_role_still_lowers_everything():
    """The declared ceiling keeps working: it is about the ROUTE, the rule
    above is about the POSITION, and the two are independent."""
    client = _client(capabilities=ModelCapabilities(developer_role=USER))
    items = _wake_shaped_items()
    client._lower_developer_items(items)

    assert [i["role"] for i in items] == [USER, USER, "assistant", USER]
    assert items[1]["content"] == f"{NOTE_OPEN}\nstep 4 of 30\n{NOTE_CLOSE}"


def test_a_last_item_that_is_not_a_developer_message_is_untouched():
    """The rule reaches for the last item only when it is one -- an ordinary
    conversation must not have its final user turn rewritten."""
    items = [{"role": DEVELOPER, "content": "step 4 of 30"},
             {"role": USER, "content": "and now?"}]
    _client()._lower_developer_items(items)

    assert items[-1] == {"role": USER, "content": "and now?"}
    assert items[0]["role"] == DEVELOPER


def test_no_items_at_all_is_not_an_error():
    """`items[-1]` on an empty list would raise, and an empty input list is a
    shape this is called with (a request built from nothing but instructions)."""
    items: list = []
    _client()._lower_developer_items(items)
    assert items == []


@pytest.mark.parametrize("parts", [
    [{"type": "input_text", "text": "look at this"}, {"type": "input_image", "image_url": "x"}],
])
def test_a_lowered_last_item_keeps_its_parts(parts):
    """A note can carry an image. Wrapped part by part rather than stringified,
    or the image would reach the model as JSON text."""
    items = [{"role": "assistant", "content": "done"},
             {"role": DEVELOPER, "content": list(parts)}]
    _client()._lower_developer_items(items)

    assert items[-1]["role"] == USER
    assert items[-1]["content"][0] == {"type": "input_text", "text": NOTE_OPEN}
    assert items[-1]["content"][-1] == {"type": "input_text", "text": NOTE_CLOSE}
    assert items[-1]["content"][1:-1] == parts, "the parts themselves were rewritten"


# --------------------------------------------------------------------------
# The same rule on the OTHER route. Three of six providers go through Chat
# Completions, and it did NOT learn this when the Responses route did -- a
# review found the gap an hour later. That hour is what these tests are for.
# --------------------------------------------------------------------------

from plugins.llm_openai_compat.httpx_client import HTTPXOpenAIClient  # noqa: E402


def _chat_client(**kw):
    defaults = dict(model="anthropic/claude-sonnet-5", api_key="sk-or-test",
                    base_url="https://openrouter.ai/api/v1")
    defaults.update(kw)
    return HTTPXOpenAIClient(**defaults)


def _wake_shaped_messages():
    """Chat-Completions shape of the same conversation."""
    return [
        {"role": USER, "content": "count to three"},
        {"role": DEVELOPER, "content": "step 4 of 30"},
        {"role": "assistant", "content": "one two three"},
        {"role": DEVELOPER, "content": "You were woken. Act on it."},
    ]


def test_chat_completions_also_turns_the_last_developer_message_into_a_turn():
    """This route defaults to the `system` rung, so without the rule the wake
    arrived as a trailing system message behind the model's own answer -- the
    same shape, and the same silence."""
    msgs = _wake_shaped_messages()
    _chat_client()._apply_developer_rung(msgs)

    assert msgs[-1]["role"] == USER
    assert msgs[-1]["content"] == f"{NOTE_OPEN}\nYou were woken. Act on it.\n{NOTE_CLOSE}"
    assert msgs[1]["role"] == "system", "a mid-turn note left its own rung"
    assert msgs[1]["content"] == "step 4 of 30", "a mid-turn note was wrapped"


def test_chat_completions_lowers_the_last_one_even_when_the_route_takes_developer():
    """The route's ceiling and the position are independent. This client used
    to return early whenever the rung was `developer` -- and that early return
    is exactly what left the last message demanding nothing."""
    msgs = _wake_shaped_messages()
    _chat_client(capabilities=ModelCapabilities(developer_role=DEVELOPER))._apply_developer_rung(msgs)

    assert msgs[1]["role"] == DEVELOPER, "a mid-turn note was lowered although the route takes it"
    assert msgs[-1]["role"] == USER, "the request still ends on a message asking nothing"


def test_chat_completions_leaves_an_ordinary_last_turn_alone():
    msgs = [{"role": DEVELOPER, "content": "step 4 of 30"},
            {"role": USER, "content": "and now?"}]
    _chat_client()._apply_developer_rung(msgs)

    assert msgs[-1] == {"role": USER, "content": "and now?"}
    assert msgs[0]["role"] == "system"


def test_chat_completions_survives_an_empty_message_list():
    msgs: list = []
    _chat_client()._apply_developer_rung(msgs)
    assert msgs == []
