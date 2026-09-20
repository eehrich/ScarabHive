"""A developer note reaches the model on every route -- and never as its words.

``developer`` is the role for what the RUN tells the model mid-conversation.
Only two of our six wire formats have it; the rest refuse it outright (Gemini
answers 400 "Role 'developer' is not supported. Please use a valid role:
MODEL, USER") or hand an unknown role to a chat template that renders it as
nothing. So every client maps it to a rung its format permits.

This is an anti-drift test, not six unit tests: the same history goes through
every client's real mapping, and the assertions are about the CLASS of
failure, not about one route's output. Three things must hold everywhere:

    the note arrives          -- a route that drops it answers 200 and lies
    it is not the model's     -- the worst outcome is not loss but a note
                                 mapped to assistant/model, which the model
                                 then reads as its own earlier conclusion
    it keeps its place        -- after the assistant turn it comments on and
                                 before the question that follows. Hoisting it
                                 into the system prompt dates it back to the
                                 start of the run

THE SEAM: each case drives the real function that builds the provider payload
out of a ChatMessage list -- the mapping is what was changed and what is
measured. The request loop around it is NOT exercised here; it is the same
loop every other message has always ridden.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from agent_system.config.models import ModelCapabilitiesConfig  # noqa: E402
from agent_system.llm.message_roles import (  # noqa: E402
    DEVELOPER, NOTE_CLOSE, NOTE_OPEN, RUNGS, SYSTEM, USER,
    as_note, is_injected_note, leading_instructions, resolve_rung,
)
from agent_system.llm.models import ChatMessage  # noqa: E402

@pytest.fixture(autouse=True)
def _forget_reported_config_mistakes():
    """resolve_rung reports a bad config line ONCE per process.

    Without this every test after the first would watch a silent function and
    call it proof -- and the order they run in would decide which one.
    """
    from agent_system.llm import message_roles
    message_roles._reported.clear()
    yield
    message_roles._reported.clear()


NOTE = "Budget: two chapters left, then the run stops."
ANSWER = "I looked it up."
QUESTION = "And what now?"


def history() -> list[ChatMessage]:
    """The case every route gets: a note between a finished turn and the next.

    Not at the head, on purpose -- a note at the head is indistinguishable
    from the system prompt, and would let a route pass by hoisting it there.
    """
    return [
        ChatMessage(role=SYSTEM, content="You are a careful assistant."),
        ChatMessage(role=USER, content="Find the number."),
        ChatMessage(role="assistant", content=ANSWER),
        ChatMessage(role=DEVELOPER, content=NOTE, injected_by="test.budget"),
        ChatMessage(role=USER, content=QUESTION),
    ]


# --------------------------------------------------------------------------
# Each route, reduced to (role, text) pairs in wire order.
# --------------------------------------------------------------------------

def _text_of(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        out = []
        for part in content:
            if isinstance(part, dict):
                out.append(part.get("text") or part.get("input_text") or "")
            else:
                out.append(str(part))
        return "\n".join(out)
    return "" if content is None else str(content)


def _responses(caps=None) -> list[tuple[str, str]]:
    """Through _build_payload, not _messages_to_input.

    The rung is applied at the END of the payload build, after the cache
    markers -- reading the intermediate item list would show a note that is
    still on the `developer` rung and call the lowering untested.
    """
    from plugins.llm_openai_compat.openai_responses_client import OpenAIResponsesClient
    client = OpenAIResponsesClient(model="openai/gpt-5.1", api_key="k",
                                   base_url="https://openrouter.ai/api/v1",
                                   capabilities=caps)
    return [(item.get("role", item.get("type", "?")), _text_of(item.get("content")))
            for item in client._build_payload(history(), None)["input"]]


def _chat_completions(caps=None) -> list[tuple[str, str]]:
    from plugins.llm_openai_compat.httpx_client import HTTPXOpenAIClient
    client = HTTPXOpenAIClient(model="openai/gpt-5.1", api_key="k",
                               base_url="https://openrouter.ai/api/v1",
                               capabilities=caps)
    dicts = [m.model_dump(exclude_none=True, mode="json") for m in history()]
    client._postprocess_messages_for_provider(dicts)
    return [(d.get("role", "?"), _text_of(d.get("content"))) for d in dicts]


def _openai(caps=None) -> list[tuple[str, str]]:
    """Through the REAL chat() path, with only the SDK call replaced.

    Calling _apply_developer_rung by hand would prove the method works and say
    nothing about whether the three serialisers still call it -- removing one
    of those calls would leave every assertion green.
    """
    import asyncio
    from unittest.mock import AsyncMock, MagicMock, patch

    from plugins.llm_openai.openai_client import OpenAIAsyncClient

    with patch("openai.AsyncOpenAI"):
        client = OpenAIAsyncClient(model="gpt-5.1", api_key="k", capabilities=caps)

    sent: dict = {}

    async def create(**opts):
        sent["messages"] = opts["messages"]
        answer = MagicMock()
        answer.choices = [MagicMock(message=MagicMock(content="ok", tool_calls=None))]
        answer.usage = None
        return answer

    client._client = MagicMock()
    client._client.chat.completions.create = AsyncMock(side_effect=create)
    asyncio.run(client.chat(history()))
    return [(d.get("role", "?"), _text_of(d.get("content"))) for d in sent["messages"]]


def _anthropic(caps=None) -> list[tuple[str, str]]:
    from unittest.mock import MagicMock, patch
    with patch("anthropic.AsyncAnthropic") as sdk:
        sdk.return_value = MagicMock()
        from plugins.llm_anthropic.anthropic_client import AnthropicAsyncClient
        client = AnthropicAsyncClient(model="claude-sonnet-4-5", api_key="k",
                                      capabilities=caps)
    _system, messages = client._convert_messages(history())
    return [(m.get("role", "?"), _text_of(m.get("content"))) for m in messages]


def _gemini(caps=None) -> list[tuple[str, str]]:
    """Through prepare_messages_for_gemini, the entry point the HTTP client
    uses -- not the inner converter. Calling the inner one directly would
    prove the conversion works while the wrapper quietly stopped passing the
    declaration down."""
    from plugins.llm_gemini.gemini_utils import prepare_messages_for_gemini
    _system, contents = prepare_messages_for_gemini(
        history(), tools=[], include_critical_instruction=False,
        developer_role=getattr(caps, "developer_role", None))
    return [(c.get("role", "?"),
             "\n".join(p.get("text", "") for p in c.get("parts", []) if isinstance(p, dict)))
            for c in contents]


def _ollama(caps=None) -> list[tuple[str, str]]:
    from unittest.mock import patch
    with patch("httpx.AsyncClient"):
        from plugins.llm_ollama.ollama_client import OllamaNativeAsyncClient
        client = OllamaNativeAsyncClient(model="qwen3", capabilities=caps)
    return [(d.get("role", "?"), _text_of(d.get("content")))
            for d in client._map_messages(history())]


#: route -> (mapping, the rung its SPEC allows). The rung is a fact about the
#: wire format, quoted in llm/message_roles.py -- not a preference.
ROUTES = {
    "responses": (_responses, DEVELOPER),
    "chat_completions": (_chat_completions, SYSTEM),
    "openai": (_openai, DEVELOPER),
    "anthropic": (_anthropic, USER),
    "gemini": (_gemini, USER),
    "ollama": (_ollama, SYSTEM),
}

#: The model's own voice, whatever the format calls it. A note landing on one
#: of these is worse than a lost note: the model reads it as something it
#: concluded itself, and will not question it.
MODEL_ROLES = ("assistant", "model")


def _note_line(wire: list[tuple[str, str]]) -> tuple[int, str, str]:
    """(index, role, text) of the one line carrying the note."""
    hits = [(i, role, text) for i, (role, text) in enumerate(wire) if NOTE in text]
    assert len(hits) == 1, f"the note must appear exactly once, found {len(hits)} in {wire}"
    return hits[0]


@pytest.mark.parametrize("route", sorted(ROUTES))
def test_the_note_reaches_every_route(route):
    """Nothing may drop it. A dropped note is a 200 that lies."""
    wire = ROUTES[route][0]()
    assert any(NOTE in text for _role, text in wire), \
        f"{route} sent no line carrying the note: {wire}"


@pytest.mark.parametrize("route", sorted(ROUTES))
def test_the_note_is_never_the_models_own_words(route):
    """It must not ride an assistant/model turn -- the one mapping that does
    not lose the text but corrupts it. Gemini's fallback did exactly this:
    every role that was not "user" became "model"."""
    _index, role, _text = _note_line(ROUTES[route][0]())
    assert role not in MODEL_ROLES, f"{route} put the note in the model's mouth (role={role!r})"
    assert role in RUNGS, f"{route} used role {role!r}, which is not a rung of the ladder"


@pytest.mark.parametrize("route", sorted(ROUTES))
def test_the_note_keeps_its_place(route):
    """Between the turn it comments on and the question that follows.

    This is the whole reason the note is a message and not an addition to the
    system prompt: hoisted up there it reads as if it had held from turn one.

    Measured on the ORDER OF THE TEXT, not on the item index, because Gemini
    merges consecutive same-role turns -- the note and the question arrive as
    one content with the note first, which is the same thing the model reads.
    """
    flat = "\n".join(text for _role, text in ROUTES[route][0]())
    answer, note, question = flat.find(ANSWER), flat.find(NOTE), flat.find(QUESTION)
    assert answer >= 0 and question >= 0, f"{route} lost the surrounding turns: {flat!r}"
    assert answer < note < question, \
        f"{route} moved the note out of its place: {flat!r}"


@pytest.mark.parametrize("route", sorted(ROUTES))
def test_the_route_uses_the_rung_its_spec_allows(route):
    mapping, expected = ROUTES[route]
    _index, role, _text = _note_line(mapping())
    assert role == expected, f"{route} used the {role!r} rung, its format allows {expected!r}"


@pytest.mark.parametrize("route", sorted(ROUTES))
def test_a_note_on_the_user_rung_says_it_is_not_a_person(route):
    """On the lowest rung the role no longer says what the text is, so the
    tags must. Without them the model cannot tell the note from something the
    person typed -- and a user turn outranks nothing."""
    mapping, expected = ROUTES[route]
    _index, role, text = _note_line(mapping())
    if role != USER:
        pytest.skip(f"{route} does not need the tags: it uses the {expected!r} rung")
    assert NOTE_OPEN in text and NOTE_CLOSE in text, f"{route} sent a bare user turn: {text!r}"


# --------------------------------------------------------------------------
# What the model entry may change, and what it may not
# --------------------------------------------------------------------------

@pytest.mark.parametrize("route,lowered", [("responses", SYSTEM), ("responses", USER),
                                           ("chat_completions", USER), ("openai", SYSTEM)])
def test_a_model_entry_can_lower_the_rung(route, lowered):
    """For a gateway that forwards to a backend without the role."""
    _index, role, _text = _note_line(
        ROUTES[route][0](ModelCapabilitiesConfig(developer_role=lowered)))
    assert role == lowered


def test_chat_completions_defaults_below_what_its_format_allows():
    """The format has `developer`; this client points at whatever base_url
    says, and OpenRouter documents only user/assistant/system/tool. So the
    default is `system` and the entry opts UP -- the one route where the
    declared value raises rather than lowers."""
    _index, default_role, _ = _note_line(_chat_completions())
    assert default_role == SYSTEM
    _index, raised, _ = _note_line(
        _chat_completions(ModelCapabilitiesConfig(developer_role=DEVELOPER)))
    assert raised == DEVELOPER


@pytest.mark.parametrize("route", ["gemini", "anthropic", "ollama"])
def test_a_declaration_above_the_wire_format_is_refused_and_said_out_loud(route, caplog):
    """Sending it would 400 every call of that model, so the cap belongs in the
    client, not at the provider -- and a config value that cannot be honoured
    has to be SAID, because ignoring it in silence is how a mistake survives.

    Driven through the real route, not through resolve_rung with the route's
    name as a label: a helper called by hand would pass even if the client
    never read capabilities at all.
    """
    caplog.set_level("WARNING")
    _index, role, text = _note_line(
        ROUTES[route][0](ModelCapabilitiesConfig(developer_role=DEVELOPER)))

    assert role != DEVELOPER, f"{route} sent a role its format refuses"
    assert role == ROUTES[route][1]
    if role == USER:
        assert NOTE_OPEN in text
    assert "above what" in caplog.text, \
        f"{route} swallowed a declaration it cannot honour: {caplog.text!r}"


def test_an_unreadable_declaration_falls_back_and_says_so(caplog):
    assert resolve_rung("shout", ceiling=DEVELOPER, default=SYSTEM, route="x") == SYSTEM
    assert "not one of" in caplog.text


def test_nothing_declared_leaves_the_route_to_decide():
    assert resolve_rung(None, ceiling=DEVELOPER, default=SYSTEM, route="x") == SYSTEM


def test_what_counts_as_a_note_the_searches_walk_past():
    """Who looks back for the last thing a PERSON wrote skips these.

    A `system` block does NOT belong in the set, though plugins mark theirs the
    same way: those stand at the HEAD, in front of everything the searches walk
    through, and counting them would make the walk run off the front of a
    history whose prompts happen to carry a marker."""
    assert is_injected_note({"role": DEVELOPER, "injected_by": "agent.step_budget"})
    assert is_injected_note(ChatMessage(role=USER, content="go on",
                                        injected_by="agent_continuation.followup"))
    assert not is_injected_note({"role": DEVELOPER, "content": "put here on purpose"})
    assert not is_injected_note(ChatMessage(role=USER, content="what a person typed"))
    assert not is_injected_note({"role": SYSTEM, "injected_by": "okf"})


def test_a_config_mistake_is_reported_once_and_then_kept_quiet(caplog):
    """Two of the six routes resolve the rung per MESSAGE. An unchanged config
    line would otherwise be logged a couple of hundred times in one run and
    bury everything else -- while the answer stays the same every time."""
    caplog.set_level("WARNING")
    for _ in range(50):
        assert resolve_rung(DEVELOPER, ceiling=USER, default=USER, route="a route") == USER
    assert len([r for r in caplog.records if "above what" in r.getMessage()]) == 1

    # a DIFFERENT mistake still gets through -- the quiet is per config line
    resolve_rung("shout", ceiling=USER, default=USER, route="a route")
    assert any("not one of" in r.getMessage() for r in caplog.records)


# --------------------------------------------------------------------------
# The volatile note: rebuilt per call, and never part of the record
# --------------------------------------------------------------------------

def _payload(caps=None, messages=None):
    from plugins.llm_openai_compat.openai_responses_client import OpenAIResponsesClient
    client = OpenAIResponsesClient(model="openai/gpt-5.1", api_key="k",
                                   base_url="https://openrouter.ai/api/v1",
                                   capabilities=caps)
    return client._build_payload(messages or history(), None)


def test_the_note_rides_in_the_input_at_its_place():
    """Nothing lifts it out any more.

    The Responses API's `instructions` field used to take the newest volatile
    note. It sits at the TOP of the context, so a text that changes every call
    rewrote the head of the prompt and the cached prefix behind it was gone --
    and once the hook plugins started appending marked blocks of their own,
    "the newest marked note" named whichever of them came last, most often
    carrying the loop's closing request away from the end, where it is the
    only thing that makes it work."""
    payload = _payload()
    assert "instructions" not in payload
    assert any(NOTE in _text_of(item.get("content")) for item in payload["input"])


def test_a_capability_config_cannot_bring_the_field_back():
    """The whole option is gone, not merely defaulted off."""
    payload = _payload(ModelCapabilitiesConfig())
    assert "instructions" not in payload
    assert not hasattr(ModelCapabilitiesConfig(), "instructions_field")
    roles = [item.get("role") for item in payload["input"]]
    assert roles[-2:] == [DEVELOPER, USER], \
        f"the note no longer sits where it was put: {roles}"


def _persisted(messages) -> list[str]:
    """What the agent's REAL persistence step keeps, as contents.

    The REAL SessionTracker is used, not a double: the rule lives inside it,
    so a fake tracker would make this test agree with any broken filter.
    Nothing else is replaced -- _persist_conversation runs as it does in a
    request, minus the disk.
    """
    import asyncio

    from agent_system.servers.agent.components.session_tracking import SessionTracker
    from agent_system.servers.agent.server import Agent

    tracker = SessionTracker()
    server = Agent.__new__(Agent)
    server._session_tracker = tracker
    asyncio.run(Agent._persist_conversation(server, "sid", messages,
                                            to_disk=False, note="test"))
    return [m.content for m in tracker.get_session_messages("sid")]


def test_a_volatile_note_is_not_written_into_the_session():
    """It is rebuilt for every call. Stored, it would keep the value of the
    turn it happened to be built on, and the injector would add the next one
    beside it -- the session filling up with stale budgets."""
    contents = _persisted(history())
    assert NOTE not in contents, f"the volatile note was stored: {contents}"
    assert QUESTION in contents, "fixture is vacuous: nothing was stored at all"


def test_a_note_placed_deliberately_stays_in_the_session():
    """Without injected_by nobody rebuilds it, so dropping it would lose it."""
    placed = [m for m in history()]
    placed[3] = ChatMessage(role=DEVELOPER, content=NOTE)
    assert NOTE in _persisted(placed)


def test_every_writer_of_session_messages_drops_the_volatile_note():
    """The rule sits in the tracker, not in one caller: five places write
    session messages and only one of them filtered anything. Reached through
    the tracker directly, the way the other four reach it."""
    from agent_system.servers.agent.components.session_tracking import SessionTracker
    tracker = SessionTracker()
    tracker.set_session_messages("sid", history())
    contents = [m.content for m in tracker.get_session_messages("sid")]
    assert NOTE not in contents, contents
    assert QUESTION in contents, "fixture is vacuous: nothing was stored"


# --------------------------------------------------------------------------
# The rebuilt history, and what the cache marker may sit on
# --------------------------------------------------------------------------

def test_the_rebuilt_head_leaves_out_what_the_hook_already_handed_back():
    """What a hook returns differs per hook, so there is no fixed set to
    subtract. context_engineer strips the prompts and returns only its own
    compaction messages; context_summarizer returns the whole list, prompts
    included, and says so in a comment. Prepending blindly sent the breadcrumb
    twice on one and both prompts twice on the other."""
    import json

    from agent_system.servers.agent.components.hook_integration import (
        is_compaction_system_message,
    )
    from agent_system.servers.agent.server import _instruction_head

    breadcrumb = ChatMessage(role=SYSTEM, content=json.dumps(
        {"type": "pruned_notice", "removed": 12, "note": "12 messages left the view"}))
    assert is_compaction_system_message(breadcrumb), "fixture is not a compaction message"
    prompts = [ChatMessage(role=SYSTEM, content="the agent prompt"),
               ChatMessage(role=SYSTEM, content="the tools prompt")]
    messages = [*prompts, breadcrumb, ChatMessage(role=USER, content="q")]

    # context_engineer: only its own compaction messages come back
    stripped = _instruction_head(messages, [breadcrumb, ChatMessage(role=USER, content="q")])
    assert [m.content for m in stripped] == ["the agent prompt", "the tools prompt"]

    # context_summarizer: the whole list comes back, prompts included
    whole = _instruction_head(messages, [*prompts, breadcrumb,
                                         ChatMessage(role=USER, content="summary")])
    assert whole == [], f"the prompts were sent twice: {[m.content for m in whole]}"

    # nothing shared: the head is needed in full
    assert len(_instruction_head(messages, [ChatMessage(role=USER, content="q")])) == 3


def _cache_marked(content) -> bool:
    """Either marker style: Anthropic's cache_control or OpenAI's breakpoint."""
    if isinstance(content, list):
        return any(isinstance(part, dict)
                   and ("cache_control" in part or "prompt_cache_breakpoint" in part)
                   for part in content)
    return False


def test_the_anthropic_payload_never_marks_the_note():
    """Driven through _convert_messages, which is where the breakpoint is set.

    The note is lowered to a user turn inside that same function, so a guard
    that only knows the `developer` role sees nothing -- exactly how this went
    wrong the first time. The order is what makes it hold: markers first,
    rung last.
    """
    from unittest.mock import MagicMock, patch
    with patch("anthropic.AsyncAnthropic") as sdk:
        sdk.return_value = MagicMock()
        from plugins.llm_anthropic.anthropic_client import AnthropicAsyncClient
        client = AnthropicAsyncClient(model="claude-sonnet-4-5", api_key="k",
                                      enable_prompt_caching=True, prompt_cache_mode=None)
    # the note LAST, which is where `injection_position: end` puts it
    messages = [*history(), ChatMessage(role=DEVELOPER, content=NOTE,
                                        injected_by="test.budget")]
    del messages[3]

    _system, converted = client._convert_messages(messages)

    marked = [m for m in converted if _cache_marked(m.get("content"))]
    assert marked, "nothing was marked at all -- the fixture does not reach the caching branch"
    assert not any(NOTE in _text_of(m.get("content")) for m in marked), \
        "the breakpoint sits on the note, whose text is rewritten every call"


@pytest.mark.parametrize("declared", [None, SYSTEM])
def test_the_responses_payload_never_marks_the_note(declared):
    """Through the real payload build, on the marker style that marks at all.

    Both rungs matter here. On `developer` the guard in mark_conversation_tail
    does the work; on `system` the note becomes a system message and
    mark_last_system takes the LAST system message -- so lowering it before
    the markers would take the breakpoint off the system prompt AND put it on
    a line that changes every call. (The OpenAI marker style is not a case:
    without a CACHE_BREAKPOINT sentinel in the text it marks nothing at all.)
    """
    from plugins.llm_openai_compat.openai_responses_client import OpenAIResponsesClient
    client = OpenAIResponsesClient(
        model="openai/gpt-5.1", api_key="k", base_url="https://openrouter.ai/api/v1",
        prompt_cache_key="auto", prompt_cache_marker_style="anthropic",
        prompt_cache_mode="multi_turn",
        capabilities=ModelCapabilitiesConfig(developer_role=declared) if declared else None)
    messages = [*history(), ChatMessage(role=DEVELOPER, content=NOTE,
                                        injected_by="test.budget")]
    del messages[3]

    items = client._build_payload(messages, None)["input"]

    marked = [i for i in items if _cache_marked(i.get("content"))]
    assert marked, "nothing was marked at all -- the fixture does not reach the caching branch"
    assert not any(NOTE in _text_of(i.get("content")) for i in marked), \
        f"the breakpoint sits on the note: {marked}"


def test_the_conversation_cache_breakpoint_skips_a_volatile_note():
    """The tail marker takes the LAST message whatever its role. A note sitting
    there -- which is exactly where `injection_position: end` puts it -- would
    carry the breakpoint, and since its text is rebuilt every call the prefix
    up to the marker would differ every turn. The tail cache would never hit
    again, which is the opposite of what marking it is for."""
    from agent_system.llm.cache_key import mark_conversation_tail

    answer = {"role": "assistant", "content": [{"type": "text", "text": ANSWER}]}
    note = {"role": DEVELOPER, "content": [{"type": "text", "text": NOTE}]}
    assert mark_conversation_tail([answer, note]) is True

    assert "cache_control" in answer["content"][-1], \
        "the breakpoint did not move back to the conversation"
    assert "cache_control" not in note["content"][-1], \
        "the breakpoint sits on a line that is rewritten every call"


# --------------------------------------------------------------------------
# The history around it
# --------------------------------------------------------------------------

def test_the_leading_block_holds_every_instruction_not_just_the_first():
    """Both history rebuilds in the agent server used to keep system messages
    only -- one of them only messages[0] -- so a second system message, or a
    note standing beside the prompt, fell out of the rebuilt history."""
    messages = [ChatMessage(role=SYSTEM, content="a"),
                ChatMessage(role=DEVELOPER, content="b"),
                ChatMessage(role=SYSTEM, content="c"),
                ChatMessage(role=USER, content="first question"),
                ChatMessage(role=SYSTEM, content="not leading any more")]
    assert [m.content for m in leading_instructions(messages)] == ["a", "b", "c"]


def test_the_leading_block_reads_dicts_too():
    """The legacy compaction path hands this rebuild plain dicts."""
    assert len(leading_instructions([{"role": SYSTEM, "content": "a"},
                                     {"role": DEVELOPER, "content": "b"},
                                     {"role": USER, "content": "q"}])) == 2


def test_the_validator_does_not_take_a_note_for_the_first_user_message():
    """The sequence repair POPS whatever stands first and is not `user`. With
    the note counted as a conversation turn it was deleted without a word."""
    from plugins.message_validator.hooks import InternalMessageValidator
    result = InternalMessageValidator().validate_and_repair(
        [ChatMessage(role=SYSTEM, content="prompt"),
         ChatMessage(role=DEVELOPER, content=NOTE),
         ChatMessage(role=USER, content="question")], context="test")

    assert not [i for i in result.issues if i.type == "invalid_first_message"], \
        f"the note was reported as a wrong first message: {result.issues}"
    assert [m.role for m in result.repaired_messages] == [SYSTEM, DEVELOPER, USER], \
        f"the repair removed the note: {result.repair_summary}"


def test_an_unknown_role_is_not_silently_dropped(caplog):
    """The Responses item chain had no else at all: a role it did not know
    left the request without a trace. That is exactly how the developer role
    was lost before this work, so the hole itself is nailed shut."""
    from plugins.llm_openai_compat.openai_responses_client import OpenAIResponsesClient
    client = OpenAIResponsesClient(model="m", api_key="k", base_url="https://openrouter.ai/api/v1")
    items = client._messages_to_input([ChatMessage(role="narrator", content=NOTE)])
    assert [_text_of(i.get("content")) for i in items] == [NOTE]
    assert "unknown message role" in caplog.text


def test_an_unknown_role_never_becomes_the_models_own_words():
    """Gemini mapped every role that was not "user" to "model". A role nobody
    planned for then arrived as something the model had said itself -- the one
    outcome worse than dropping it."""
    from plugins.llm_gemini.gemini_utils import convert_openai_messages_to_gemini
    _system, contents = convert_openai_messages_to_gemini(
        [ChatMessage(role="user", content="q"), ChatMessage(role="narrator", content=NOTE)],
        include_critical_instruction=False)
    carrying = [c for c in contents
                if any(NOTE in p.get("text", "") for p in c.get("parts", []))]
    assert carrying, f"the text was lost: {contents}"
    assert carrying[0]["role"] != "model"


def test_a_running_repair_does_not_carry_the_note_off_with_it():
    """The repair loop only runs once something else is broken. With the note
    counted as the first conversation turn, THAT repair popped it -- so the
    note survived a clean history and vanished from a dirty one."""
    from plugins.message_validator.hooks import InternalMessageValidator
    messages = [ChatMessage(role=SYSTEM, content="prompt"),
                ChatMessage(role=DEVELOPER, content=NOTE),
                ChatMessage(role=USER, content="question"),
                # an answer promising a tool result that never came: a real
                # issue, so the repair pass actually runs
                ChatMessage(role="assistant", content="", tool_calls=[
                    {"id": "call_1", "type": "function",
                     "function": {"name": "f", "arguments": "{}"}}])]

    result = InternalMessageValidator().validate_and_repair(messages, context="test")

    assert result.issues, "fixture is vacuous: nothing was repaired at all"
    assert NOTE in [m.content for m in result.repaired_messages], \
        f"the repair took the note with it: {result.repair_summary}"


def test_the_tag_wrapper_keeps_the_text_whole():
    assert as_note("x") == f"{NOTE_OPEN}\nx\n{NOTE_CLOSE}"


def test_the_gemini_sdk_path_reads_the_declaration_too(caplog):
    """Two entry points reach the same converter: prepare_messages_for_gemini
    (the HTTP client) and the SDK client's own _convert_messages_to_sdk. A
    declaration that only one of them passes down is a config that works or
    not depending on which client the profile happens to pick."""
    from unittest.mock import MagicMock, patch

    caplog.set_level("WARNING")
    with patch("plugins.llm_gemini.gemini_sdk_client.genai", MagicMock()):
        from plugins.llm_gemini.gemini_sdk_client import GeminiSDKClient
        client = GeminiSDKClient.__new__(GeminiSDKClient)
        client.capabilities = ModelCapabilitiesConfig(developer_role=DEVELOPER)
        _system, contents = client._convert_messages_to_sdk(history())

    assert "above what" in caplog.text, "the SDK path swallowed the declaration"
    carrying = [c for c in contents if NOTE in "".join(
        getattr(p, "text", "") or "" for p in (c.parts or []))]
    assert carrying and carrying[0].role == USER, \
        f"the note did not arrive as a user turn: {contents}"


def test_the_realtime_session_takes_the_note_as_a_system_item_at_its_place():
    """A realtime conversation item takes user, assistant or system. The role
    went in verbatim, so a developer note failed the session build outright --
    and folding it into the instructions would date it back to turn one."""
    from plugins.llm_openai.realtime_adapter import to_request_input
    instructions, items = to_request_input(history())

    assert instructions == "You are a careful assistant."
    carrying = [i for i in items if NOTE in _text_of(i.get("content"))]
    assert len(carrying) == 1, f"the note is not in the items: {items}"
    assert carrying[0]["role"] == SYSTEM
    assert NOTE not in (instructions or ""), "the note was folded into the instructions"
    assert items.index(carrying[0]) > 0, "the note lost its place"


# --------------------------------------------------------------------------
# What a person reading the session sees
# --------------------------------------------------------------------------

def test_the_transcript_shows_the_note():
    """A transcript that hides it reads as if the agent knew things nobody
    told it."""
    from agent_system.chat_actions import transcript_markdown
    text = transcript_markdown(history(), agent_name="a", session_id="s")
    assert NOTE in text, f"the note is missing from the transcript:\n{text}"


def test_the_context_breakdown_names_the_notes_instead_of_lumping_them():
    from agent_system.chat_actions import context_breakdown
    # The system message has no kind of its own here (it is counted as the
    # system_prompt when one is passed), so "other" holds it -- and must NOT
    # hold the note as well.
    parts = context_breakdown(history())["parts"]
    assert parts.get("notes", {}).get("count") == 1, parts
    assert parts["other"]["count"] == 1, f"the note fell into the unnamed bucket too: {parts}"


def test_a_breakdown_without_notes_does_not_show_an_empty_line():
    from agent_system.chat_actions import context_breakdown
    plain = [m for m in history() if m.role != DEVELOPER]
    assert "notes" not in context_breakdown(plain)["parts"]


def _woken_history() -> list[ChatMessage]:
    """What a woken run opens with: the wake is the whole conversation, first
    and last, and it carries no marker (cli_utils/agent_runner.wake_message)."""
    return [
        ChatMessage(role=SYSTEM, content="You are a careful assistant."),
        ChatMessage(role=DEVELOPER, content=NOTE),
    ]


def test_a_note_alone_is_still_a_conversation_anthropic():
    """The route with the real risk: the Messages API has no system role among
    the input messages and wants a `user` turn first. A wake is a run's ONLY
    input, so if the lowering ran on freshly built notes rather than on the
    whole converted list, this is where it would 400."""
    from unittest.mock import MagicMock, patch
    with patch("anthropic.AsyncAnthropic") as sdk:
        sdk.return_value = MagicMock()
        from plugins.llm_anthropic.anthropic_client import AnthropicAsyncClient
        client = AnthropicAsyncClient(model="claude-sonnet-4-5", api_key="k")

    _system, messages = client._convert_messages(_woken_history())

    assert [m.get("role") for m in messages] == [USER], messages
    assert NOTE in _text_of(messages[0].get("content"))
