"""What a person's "allow for this session" covers.

"Allow calls like this" covers less than the tool. A call with a ``command`` text covers its first words: ``git status
-s`` grants ``git status …`` (the words up to the first one that is no plain word, at most three). Every other
argument must be the same -- an SSH call's machine, and a terminal call's env_vars: with ``GIT_CONFIG_*`` or ``PATH``
in them an allowed ``git status`` runs any program. A command with anything a shell reads as more than words --
``;`` ``&&`` ``|`` ``$( )``, a redirection, a glob, quotes, a character that is not printable or a space other than
blank and tab (Python splits there, a shell does not) -- covers only itself. Without that rule an
allowed ``ls`` would run ``ls; rm -rf ~``, and such a command never matches a prefix someone allowed either. A call
without a command covers exactly its arguments.

"Allow every call of the tool" is the tool key itself -- what "allow for this session" was before.
"""
from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Mapping, Tuple

__all__ = ["call_grant"]

#: Words a prefix may take after the first one: ``npm run build``.
PREFIX_WORDS = 3
#: Anything a shell (bash, PowerShell, cmd) reads as more than plain words.
_SHELL = re.compile(r"[;&|`$<>(){}\[\]*?~!#%^'\"\\\n\r]")
#: A word that names a subcommand, not a file, an option or a value.
_WORD = re.compile(r"[A-Za-z][A-Za-z0-9_-]*\Z")


def _digest(value: Any) -> str:
    # ASCII escapes: a lone surrogate (a cut emoji) encodes there, and as UTF-8 it raised -- the hook failed and
    # blocked the call instead of asking.
    text = json.dumps(value, sort_keys=True, default=str)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:32]


def call_grant(tool_key: str, arguments: Mapping[str, Any]) -> Tuple[str, str, Tuple[str, ...]]:
    """This call's "allow calls like this": the key it grants, what that covers as the person reads it, and the keys
    of the grants that already cover it (a grant of ``git`` or ``git status`` covers ``git status origin``)."""
    command = arguments.get("command")
    if (isinstance(command, str) and command.strip() and not _SHELL.search(command)
            and all(char.isprintable() or char == "\t" for char in command)):
        words = re.split(r"[ \t]+", command.strip())
        prefix = [words[0]]
        for word in words[1:PREFIX_WORDS]:
            if not _WORD.match(word):
                break
            prefix.append(word)
        others = {key: value for key, value in arguments.items() if key != "command"}
        keys = tuple(f"{tool_key}#command:{_digest([prefix[:n], others])}" for n in range(1, len(prefix) + 1))
        where = "".join(f", {key} {value}" for key, value in sorted(others.items()) if isinstance(value, str))
        if any(not isinstance(value, str) for value in others.values()):
            where += ", the other arguments as in this call"
        return keys[-1], f"'{' '.join(prefix)} …'{where}", keys
    key = f"{tool_key}#call:{_digest(dict(arguments))}"
    return key, "this exact call", (key,)
