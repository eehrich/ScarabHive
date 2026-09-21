"""The role vocabulary, and the ladder a client walks when its route lacks one.

``developer`` is the role for information the RUN puts in front of the model
mid-conversation -- a budget, a deadline, what a job just did -- as opposed to
``system`` (what the agent is) and ``user`` (what a person said). The OpenAI
Responses API has it as an input item role; most other wire formats do not,
and each refuses a different set:

    Responses input items   user, assistant, system, developer   -> native
    Chat Completions        developer, system, user, assistant, tool -> native
                            ("with o1 models and newer, `developer` messages
                            replace the previous `system` messages")
    OpenRouter chat         user, assistant, system, tool -> no developer
    Anthropic Messages      user, assistant  ("there is no `system` role for
                            input messages in the Messages API")
    Gemini generateContent  user, model      (400: "Role 'developer' is not
                            supported. Please use a valid role: MODEL, USER")
    Ollama /api/chat        system, user, assistant, tool

So there is no single wire form. What every route DOES have is a rung of this
ladder, and the whole point is that the note keeps its PLACE in the history:
hoisting it into the system prompt would make a fact that started to hold at
turn nine read as if it had held from the start.

    developer  the role itself
    system     a system turn in the message list -- still an instruction, and
               told apart from the conversation by its role
    user       a user turn, wrapped in <developer_note> tags, because here the
               role no longer says what the text is. The tags are the only
               thing left that distinguishes it from something a person typed.

A client names two rungs: the highest its WIRE FORMAT permits (a fact about
the API, not about the model), and the one to use when nothing is declared.
Those differ where the endpoint speaks a format richer than the backend
behind it -- an OpenAI-compatible host may be llama.cpp, whose chat template
renders an unknown role as nothing at all. Declaring
``capabilities.developer_role`` on the model entry lifts it to what that
backend really takes.
"""
from __future__ import annotations

import logging
from typing import Optional

logger = logging.getLogger(__name__)

SYSTEM = "system"
DEVELOPER = "developer"
USER = "user"
ASSISTANT = "assistant"
TOOL = "tool"

#: Roles whose content is an instruction rather than a turn of the
#: conversation: what stands at the head of a history, and what the sequence
#: rules must not mistake for the first thing a person said.
#:
#: NOT a promise that these survive everything. Compaction's
#: keep_system_messages and the summariser deliberately do NOT read this set:
#: a developer note is bound to a moment, and one that is old enough to be
#: compacted away SHOULD go. Whatever must outlive that belongs in the system
#: prompt, or in a hook that puts it back on every call.
INSTRUCTION_ROLES = frozenset({SYSTEM, DEVELOPER})

#: Every role this system produces. Nothing validates against it (ChatMessage
#: keeps role as a plain str, and a provider may invent one), but a reader
#: asking "what can arrive here" has one place to look.
KNOWN_ROLES = (SYSTEM, DEVELOPER, USER, ASSISTANT, TOOL)

#: The rungs, best first.
RUNGS = (DEVELOPER, SYSTEM, USER)

NOTE_OPEN = "<developer_note>"
NOTE_CLOSE = "</developer_note>"


def _field(msg: object, name: str) -> object:
    """One field of a message, whether it arrives as a dict or a ChatMessage."""
    if isinstance(msg, dict):
        return msg.get(name)
    return getattr(msg, name, None)


def role_of(msg: object) -> str:
    """The role of a message, whether it arrives as a dict or a ChatMessage."""
    return str(_field(msg, "role") or "")


#: Roles a note can wear once it is in the message list. `system` is left out
#: on purpose: the blocks plugins insert with that role stand at the HEAD, in
#: front of everything the searches below walk back through.
_NOTE_ROLES = frozenset({USER, DEVELOPER})


def is_injected_note(msg: object) -> bool:
    """Something the run or a hook put in front of the model, not a turn taken.

    Two searches walk back from the end of the history to find the last thing a
    PERSON wrote: tool_preload (act once per turn) and context_engineer (does a
    user message trigger media compaction). Both used to skip ``user`` messages
    carrying ``injected_by``, which was every note there was. The loop's notes
    are ``developer`` now: on the role alone the walk stops at the first note
    and finds nothing behind it.

    agent_continuation asks the same question the other way round -- it counts
    the follow-ups it already sent by their marker and anchors on a user
    message without one -- so it reads the fields itself.
    """
    return role_of(msg) in _NOTE_ROLES and bool(_field(msg, "injected_by"))


def opens_a_turn(msg: object) -> bool:
    """Whether this message is the HEAD of a turn -- what a request stands on.

    Two kinds qualify: what a person (or a pipeline) sent, and the wake of a
    woken run. A woken run's task is a ``developer`` message
    (``cli_utils/agent_runner.wake_message``), and it opens its turn exactly as
    a typed line does -- it just is not a person talking.

    NOT the notes the loop and the hooks add mid-turn (the step budget, a loop
    intervention, a scripted follow-up, a debate post). Those carry
    ``injected_by`` and belong to the request in FRONT of them; counting them
    as turns aged that request and had compaction archive the very task still
    being worked on.

    The other question -- "what did a PERSON write" -- is not this one, and a
    wake is not an answer to it: ``is_injected_note`` serves the searches that
    ask it. The two are exact opposites over the same set of roles, which is
    why they live next to each other.
    """
    return role_of(msg) in _NOTE_ROLES and not _field(msg, "injected_by")


def is_input(msg: object) -> bool:
    """Whether the model is being asked to act on this message.

    Any ``user`` turn, whoever put it there -- a person, a scripted follow-up,
    a debate post, a direct message delivered mid-run -- and the wake of a
    woken run.

    This is the TAIL question: what stands last in front of the answer. A
    reminder placed "before the last user message", a forum post delivered into
    the current turn, and the check "is there anything here for the model at
    all" all ask it. Anchoring any of them on ``opens_a_turn`` instead buries
    them in front of the whole answered exchange, which is the one thing they
    are placed to avoid.

    ``opens_a_turn`` is the HEAD question. The two differ exactly on the
    messages the run added INSIDE a turn, and every caller has to know which of
    the two it wants.
    """
    return role_of(msg) == USER or opens_a_turn(msg)


def leading_instructions(messages: list) -> list:
    """The instruction block at the head of a history.

    Everything standing before the conversation starts: the system prompt, and
    any developer note placed with it. Both history rebuilds in the agent
    server used to take system messages only -- one of them only
    ``messages[0]`` -- so a second system message, or a note beside it, fell
    out of the rebuilt history without a word.
    """
    block = []
    for msg in messages:
        if role_of(msg) not in INSTRUCTION_ROLES:
            break
        block.append(msg)
    return block


def as_note(text: str) -> str:
    """Wrap text so a user turn still says it is not a user speaking."""
    return f"{NOTE_OPEN}\n{text}\n{NOTE_CLOSE}"


#: Config mistakes already reported. Two of the six routes resolve the rung
#: per MESSAGE, so an unchanged config line would otherwise be logged a couple
#: of hundred times in one run and bury everything else in the file.
_reported: set[tuple[str, str, str]] = set()


def _report_once(key: tuple[str, str, str], message: str, *args) -> None:
    if key in _reported:
        return
    _reported.add(key)
    logger.warning(message, *args)


def resolve_rung(declared: Optional[str], *, ceiling: str, default: str, route: str) -> str:
    """Which rung a developer message rides on this route.

    ``declared`` is capabilities.developer_role from the model entry, or None.
    ``ceiling`` is what the wire format permits at all -- a declaration above
    it is a config error that would 400 every call, so it is refused here,
    once, instead of at the provider on every call.
    """
    if declared is None:
        return default
    if declared not in RUNGS:
        _report_once((declared, ceiling, route),
                     "capabilities.developer_role=%r is not one of %s; "
                     "using %r on %s", declared, RUNGS, default, route)
        return default
    if RUNGS.index(declared) < RUNGS.index(ceiling):
        _report_once((declared, ceiling, route),
                     "capabilities.developer_role=%r is above what %s accepts "
                     "(%r); using %r", declared, route, ceiling, ceiling)
        return ceiling
    return declared


def rung_for_position(rung: str, *, last: bool, opens: bool = False) -> str:
    """Which rung a developer message rides, given WHERE it sits.

    A developer message is read but demands no answer -- that is its definition
    and what the notes the loop adds mid-turn are for. The last message is a
    different job: it is what the model is being asked, and a woken run's whole
    task arrives that way (``cli_utils/agent_runner.wake_message``).

    Measured 21.09.2026 through the production clients, on a real wake history
    (an answer, then the task -- the shape every wake has, because a wake
    follows an answer):

        ~deepseek/deepseek-v4-flash-latest   7/15 empty answers  ->  0/10
        google/gemini-3.5-flash-lite         10/10 HTTP 400      ->  0/3

    Google says it outright -- "Requests ending with a model turn are not
    supported" -- because it does not count a developer item as a turn. DeepSeek
    does not refuse; it continues the previous text instead of answering. One
    cause, two symptoms. Removing the developer messages does NOT heal it
    (5/6 still empty): what heals is a last message that ASKS.

    Here rather than in the clients, and not where such a message is built: the
    stored transcript has to keep saying who spoke (``developer`` with
    ``injected_by``, held by tests/agent/test_agent_step_budget_note.py), more
    than one place appends one, and six routes would otherwise each have to
    learn this separately -- which is how the Responses route had it for an hour
    and the Chat-Completions route did not.

    The same holds where a user turn is MISSING at the start (``opens``, see
    ``conversation_opener`` -- which leaves a leading note in front of a user
    turn alone). A woken run's request need not carry a user turn at all --
    compaction may have trimmed it, and fd110ba9 made compaction and
    the message validator keep the wake as a valid opener instead of deleting
    the run's work to find one. On a route that hoists system messages out, a
    wake left on the system rung then leaves the conversation opening on the
    model's own tool call, which Gemini refuses. Not measured on the wire:
    derived from that rule, and the reason is the same as at the tail -- the
    message that stands where a turn must be has to be one.

    Not a route here: the Realtime session (llm_openai/realtime_adapter) gives
    every developer message to the conversation as a ``system`` item, the last
    one included. ``response.create`` asks for a response explicitly there, so
    the "gets nothing" failure measured above has no obvious way in -- also not
    measured, and named so nobody reads the six routes as all of them.

    What each route still owns is the REWRITING: the tags that say who is
    speaking once the role no longer does, and the content-part shape they go
    in (``input_text`` on the Responses API, ``text`` on Chat Completions).
    """
    return USER if last or opens else rung


def conversation_opener(messages: list) -> object | None:
    """The developer message that has to open the conversation as a turn, or None.

    Only where NO user turn stands in front of the model's first message -- a
    woken run whose request lost its user turn. Then the developer message
    right in front of that first message is the one standing where a turn must
    be, and it is returned. Otherwise None, and nothing moves: a developer note
    ahead of a user turn is the leading block (``leading_instructions``), which
    stays exactly as it is.

    Compared by identity (``msg is opener``), so each route passes the list it
    actually sends.
    """
    candidate = None
    for msg in messages:
        role = role_of(msg)
        if role == DEVELOPER:
            candidate = msg
        elif role != SYSTEM:
            return None if role == USER else candidate
    return candidate


def developer_turn(text: str, rung: str) -> tuple[str, str]:
    """(role, text) for a developer note sent on ``rung``."""
    if rung == DEVELOPER:
        return DEVELOPER, text
    if rung == SYSTEM:
        return SYSTEM, text
    return USER, as_note(text)
