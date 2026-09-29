"""What the person reads of a call's arguments before they allow it.

Bounded, and never at the cost of an argument: the path of a file write behind
its long content, a command behind a padding argument the tool ignores, a field
inside a nested argument, a name with line breaks in it -- none may push what
decides the call out of view.

* Every argument is named, nested ones by their leaves (``opts.cmd``,
  ``files[2]``); a name that is not plain is quoted and escaped.
* One-line values come first, in the order the model sent them, as JSON.
* Text with line breaks (a script, a heredoc) follows as a block of its own
  lines, each marked, so it reads as code and cannot pass for another argument.
* The values share a budget: short ones stand whole, long ones lose their
  middle, never their head or tail. Past MAX_LEAVES leaves the rest are counted.
"""
from __future__ import annotations

import json
import re
import unicodedata
from typing import Any, List, Mapping, Tuple

#: What the page shows of a call's arguments, in characters (spread over the values).
ARGUMENTS_PREVIEW_CHARS = 4000
#: Every value keeps at least this much of its head and tail, however many there are.
MIN_VALUE_CHARS = 80
#: Longest argument name shown.
MAX_KEY_CHARS = 200
#: Most arguments named one by one (nested ones count by their leaves); the rest
#: are counted, and the preview says it is not whole.
MAX_LEAVES = 300
#: A list longer than this is shown as one value, not item by item.
MAX_LIST_ITEMS = 50
#: What marks a line of a multi-line value.
BLOCK_MARK = "  │ "

_PLAIN_NAME = re.compile(r"^[A-Za-z0-9_.\-\[\]]+$")
#: Unicode categories shown escaped: control, format (bidi overrides, zero-width
#: characters), line and paragraph separators. Text the person reads before they
#: allow a call must read as it runs -- a bidi override makes code read otherwise.
_INVISIBLE = frozenset({"Cc", "Cf", "Zl", "Zp"})


def _escape_invisible(text: str, keep: str = "") -> str:
    return "".join(
        char if char in keep or unicodedata.category(char) not in _INVISIBLE else f"\\u{ord(char):04x}"
        for char in text)


def _json(value: Any) -> str:
    """``value`` as JSON on one line: control characters escaped -- also the line
    and paragraph separators, which ``ensure_ascii=False`` leaves raw."""
    try:
        text = json.dumps(value, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        text = json.dumps(str(value), ensure_ascii=False)
    return _escape_invisible(text)


def _block_text(value: str) -> str:
    """A multi-line value as its lines, with every invisible character but tab
    and line feed shown escaped (a carriage return would draw over a line)."""
    return _escape_invisible(value, keep="\t\n")


class _Index(int):
    """A list position in a leaf's path (a mapping's key is never one)."""


def _middle_cut(text: str, keep: int) -> str:
    """``text`` with its middle left out when longer than ``keep``: head and tail stay."""
    if len(text) <= keep:
        return text
    head = keep // 2
    tail = keep - head
    return f"{text[:head]} … {len(text) - keep} characters … {text[len(text) - tail:]}"


def _middle_cut_lines(text: str, keep: int) -> str:
    """As _middle_cut, for a block: the cut is a line of its own, and the lines
    on either side of it are whole -- unless head or tail is one long line."""
    if len(text) <= keep:
        return text
    head = text[:keep // 2]
    tail = text[len(text) - (keep - keep // 2):]
    if "\n" in head:
        head = head[:head.rfind("\n")]
    if "\n" in tail:
        tail = tail[tail.find("\n") + 1:]
    left_out = len(text) - len(head) - len(tail)
    return f"{head}\n… {left_out} characters left out …\n{tail}"


def _name(parts: Tuple[Any, ...]) -> Tuple[str, bool]:
    """(the shown name of a leaf, whether it was cut)."""
    name = ""
    for part in parts:
        if isinstance(part, _Index):
            name += f"[{int(part)}]"
        else:
            text = str(part)
            text = text if _PLAIN_NAME.fullmatch(text) else _json(text)
            name += text if not name else f".{text}"
    cut = len(name) > MAX_KEY_CHARS
    return (_middle_cut(name, MAX_KEY_CHARS) if cut else name), cut


def _leaves(value: Any, path: Tuple[Any, ...], out: list) -> None:
    """Mappings and short lists become their leaves (``opts.cmd``, ``files[2]``)."""
    if isinstance(value, Mapping) and value:
        for key, item in value.items():
            _leaves(item, path + (key,), out)
    elif isinstance(value, (list, tuple)) and value and len(value) <= MAX_LIST_ITEMS:
        for index, item in enumerate(value):
            _leaves(item, path + (_Index(index),), out)
    else:
        out.append((path, value))


def arguments_preview(arguments: Mapping[str, Any]) -> Tuple[str, bool]:
    """(what the person reads of the call's arguments, whether any of it was cut)."""
    leaves: list = []
    for key, value in arguments.items():
        _leaves(value, (key,), leaves)
    hidden = max(0, len(leaves) - MAX_LEAVES)
    items: List[Tuple[str, str, bool]] = []   # (name, text, is a block)
    names_cut = False
    for path, value in leaves[:MAX_LEAVES]:
        name, name_cut = _name(path)
        names_cut = names_cut or name_cut
        block = isinstance(value, str) and "\n" in value
        items.append((name, _block_text(value) if block else _json(value), block))
    budget = ARGUMENTS_PREVIEW_CHARS - sum(len(name) + 6 for name, _, _ in items)
    keep: dict[int, int] = {}
    for n, index in enumerate(sorted(range(len(items)), key=lambda i: len(items[i][1]))):
        share = max(MIN_VALUE_CHARS, budget // (len(items) - n)) if budget > 0 else MIN_VALUE_CHARS
        keep[index] = min(len(items[index][1]), share)
        budget -= keep[index]
    cut = names_cut or bool(hidden) or any(keep[i] < len(text) for i, (_, text, _) in enumerate(items))
    lines = [f"{name}: {_middle_cut(text, keep[i])}"
             for i, (name, text, block) in enumerate(items) if not block]
    for i, (name, text, block) in enumerate(items):
        if block:
            shown = _middle_cut_lines(text, keep[i])
            lines.append(f"{name}: ({text.count(chr(10)) + 1} lines)")
            lines.extend(f"{BLOCK_MARK}{line}" for line in shown.split("\n"))
    if hidden:
        lines.append(f"… and {hidden} more arguments not shown")
    return "\n".join(lines) if lines else "(no arguments)", cut
