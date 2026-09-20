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


def developer_turn(text: str, rung: str) -> tuple[str, str]:
    """(role, text) for a developer note sent on ``rung``."""
    if rung == DEVELOPER:
        return DEVELOPER, text
    if rung == SYSTEM:
        return SYSTEM, text
    return USER, as_note(text)
