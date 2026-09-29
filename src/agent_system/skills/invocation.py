"""Turning ``/skill-name some args`` into the message the agent receives.

A skill invoked from chat is not a tool call: its body BECOMES the turn's
instruction. That keeps it identical to what the ecosystem does (Claude Code,
Codex) and, more importantly, identical to what ``always`` already does inside
the system prompt -- the same text, just triggered by a person instead of a
config entry.

The placeholder rules follow the same convention, because skills are meant to
be portable between tools:

* ``$ARGUMENTS`` is replaced by everything after the skill name,
* ``$1``, ``$2`` … and ``$ARGUMENTS[N]`` take a single whitespace-separated
  argument (quoted groups stay together),
* a skill that declares no placeholder still gets the input, appended as
  ``ARGUMENTS: …`` -- dropping it silently is how a typed instruction
  disappears without a trace.
"""
from __future__ import annotations

import re
import shlex
from typing import List, Sequence

from .registry import Skill

#: ``$ARGUMENTS[2]``, ``$ARGUMENTS`` and ``$2`` -- longest form first, or
#: ``$ARGUMENTS[2]`` would match ``$ARGUMENTS`` and leave a stray ``[2]``.
#:
#: The short form is deliberately ONE digit with nothing numeric behind it.
#: An unbounded ``\$(\d+)`` swallows every dollar amount and hex constant a
#: skill happens to contain -- "never exceed $100" became "never exceed ",
#: and ``dc.w $0180,$0F00`` was mangled into nonsense. Skills are prose; a
#: dollar followed by digits is far more often money or hex than a parameter.
#: Beyond nine arguments there is still ``$ARGUMENTS[10]``.
_PLACEHOLDER = re.compile(r"\$ARGUMENTS\[(\d+)\]|\$ARGUMENTS\b|\$([1-9])(?![0-9])")

#: Above this, argument splitting uses plain whitespace instead of shlex.
#: shlex is a character-at-a-time state machine and goes quadratic on long
#: input: 900 KB of arguments blocked the event loop for seconds. Nobody
#: addresses a skill's ``$3`` with a megabyte of text, and ``$ARGUMENTS``
#: still receives all of it.
_SHLEX_LIMIT = 4096


def split_arguments(arguments: str) -> List[str]:
    """Split an argument string, keeping quoted groups together.

    ``posix=False`` on purpose: the POSIX mode eats backslashes, which turns
    a Windows path typed as ``/read C:\\Users\\me\\notes.md`` into
    ``C:Usersmenotes.md``. Surrounding quotes are stripped afterwards so a
    quoted group still arrives as one clean argument.

    Falls back to plain whitespace splitting when the quoting is unbalanced --
    a stray apostrophe in ``/fix-issue don't crash`` must not turn into an
    error message instead of a turn.
    """
    text = arguments.strip()
    if not text:
        return []
    if len(text) > _SHLEX_LIMIT:
        return text.split()
    try:
        parts = shlex.split(text, posix=False)
    except ValueError:
        return text.split()
    return [_unquote(part) for part in parts]


def _unquote(part: str) -> str:
    """Drop one layer of surrounding quotes left by non-POSIX splitting."""
    if len(part) >= 2 and part[0] == part[-1] and part[0] in ('"', "'"):
        return part[1:-1]
    return part


def expand(body: str, arguments: str) -> str:
    """Substitute the argument placeholders in *body*.

    Positional references beyond what was typed become empty strings rather
    than an error: a skill may legitimately take an optional second argument.
    """
    positional: Sequence[str] = split_arguments(arguments)
    used = False

    def replace(match: re.Match) -> str:
        nonlocal used
        used = True
        index = match.group(1) or match.group(2)
        if index is None:
            return arguments.strip()
        position = int(index)
        # $1 is the first argument; $0 is not a thing, so treat it as $1.
        offset = position - 1 if position > 0 else 0
        return positional[offset] if offset < len(positional) else ""

    expanded = _PLACEHOLDER.sub(replace, body)
    if not used and arguments.strip():
        # The skill takes no placeholders but the user typed something. Saying
        # it out loud beats discarding it -- they meant it for this skill.
        expanded = f"{expanded.rstrip()}\n\nARGUMENTS: {arguments.strip()}"
    return expanded


def invoke(skill: Skill, arguments: str = "") -> str:
    """The full message for *skill*, with *arguments* substituted."""
    return expand(skill.body(), arguments)
