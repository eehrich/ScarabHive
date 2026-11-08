"""Turning ``/skill-name some args`` into the message the agent receives.

A skill invoked from chat is not a tool call: its body BECOMES the turn's
instruction. That keeps it identical to what the ecosystem does (Claude Code,
Codex) and, more importantly, identical to what ``always`` already does inside
the system prompt -- the same text, just triggered by a person instead of a
config entry.

The placeholder rules are Claude Code's (its skills documentation, "Available
string substitutions"), because skills are meant to be portable between tools:

* ``$ARGUMENTS`` is replaced by everything after the skill name, as typed,
* ``$ARGUMENTS[N]`` and its shorthand ``$N`` take one argument by 0-based
  index -- ``$0`` is the first -- split shell-style (quoted groups stay
  together),
* ``$name`` takes the argument at the position of ``name`` in the skill's
  ``arguments`` frontmatter, and is empty when nothing was typed there,
* an indexed placeholder without an argument at its position stays as it is
  written, so a price like ``$100`` or a hex constant like ``$0180`` survives;
  one with a letter behind its digits (``$0F00``) is no placeholder at all,
* a single backslash before a placeholder (``\\$1.00``) keeps it literal and
  drops the backslash; a doubled one (``\\\\$1``) stays and does not escape,
* when no placeholder received an argument, the input is appended as
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
#: ``$100`` is index 100, not ``$1`` followed by ``00``: with fewer arguments it
#: stays as written, which is what keeps money and hex in prose intact. No word
#: character may follow either: ``$0F00`` and ``$1a`` are hex, not ``$0`` + ``F00``.
_INDEXED = r"\$ARGUMENTS\[(?P<index>\d+)\]|\$ARGUMENTS\b|\$(?P<short>\d+)(?!\w)"

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


def _pattern(names: Sequence[str]) -> re.Pattern:
    """The placeholders of a skill with the declared argument *names*."""
    forms = _INDEXED
    if names:
        # Longest first, and no word character behind: $issue must not take
        # the front of $issue_id. A hyphen ends the name ($issue-specific).
        alternatives = "|".join(re.escape(n) for n in sorted(set(names), key=len, reverse=True))
        forms += rf"|\$(?P<name>{alternatives})(?!\w)"
    return re.compile(rf"(?P<escape>\\)?(?:{forms})")


def expand(body: str, arguments: str, names: Sequence[str] = ()) -> str:
    """Substitute the argument placeholders in *body*.

    *names* are the skill's declared ``arguments``, in order.
    """
    typed = arguments.strip()
    positional: Sequence[str] = split_arguments(arguments)
    position_of = {name: i for i, name in reversed(list(enumerate(names)))}
    received = False

    def replace(match: re.Match) -> str:
        token = match.group(0)
        if match.group("escape"):
            if match.start() > 0 and body[match.start() - 1] == "\\":
                # "\\$1": a doubled backslash escapes nothing and stays.
                return "\\" + replace_token(match, token[1:])
            return token[1:]
        return replace_token(match, token)

    def replace_token(match: re.Match, token: str) -> str:
        nonlocal received
        name = match.groupdict().get("name")
        if name is not None:
            # A named placeholder counts as received even without a value.
            received = True
            offset = position_of[name]
            return positional[offset] if offset < len(positional) else ""
        index = match.group("index") or match.group("short")
        if index is None:
            received = received or bool(typed)
            return typed
        offset = int(index)
        if offset >= len(positional):
            return token
        received = True
        return positional[offset]

    expanded = _pattern(names).sub(replace, body)
    if not received and typed:
        # No placeholder took the input. Saying it out loud beats discarding
        # it -- the user meant it for this skill.
        expanded = f"{expanded.rstrip()}\n\nARGUMENTS: {typed}"
    return expanded


def invoke(skill: Skill, arguments: str = "") -> str:
    """The full message for *skill*, with *arguments* substituted."""
    return expand(skill.body(), arguments, skill.argument_names)
