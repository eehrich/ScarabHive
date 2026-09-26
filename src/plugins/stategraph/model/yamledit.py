"""Comment-preserving edits of a machine file: the operations behind the panel's graph editor.

``apply_op(text, op)`` returns the new text of the root machine file. Every operation first changes the round-trip
document; the new text must then read back as exactly that document, or the operation refuses. How the text is
written, in order of preference:

- **Lines.** Removing or moving a whole node (a state, a transition) cuts and pastes its lines, together with the
  comment lines right above it at its own column; replacing a state's body puts the text the author typed there,
  indented to its place. Everything else stays byte-exact.
- **Splice.** Other edits render the document with ruamel and take from that rendering only the lines the edit
  changed (``agent_editor.store.splice``, the same three-way merge its editor writes config files with).
- **ruamel's rendering** of the whole document, when neither of the above reads back right (an unusual layout,
  a flow-style list). Comments survive it, the file's spacing may not.

An invalid operation raises ``EditError`` with a message for the author; the file text is never half-edited.
Operations keep the machine loadable, not valid: a transition to a state that is removed later, or an ``initial``
that names a removed state, is left to the validator, which says so where the author can see it.
"""

from __future__ import annotations

import difflib
import io
import re
import textwrap
from dataclasses import dataclass
from typing import Any, Callable, Iterator, Optional

from ruamel.yaml import YAML
from ruamel.yaml.comments import CommentedMap, CommentedSeq
from ruamel.yaml.error import YAMLError
from ruamel.yaml.scalarstring import LiteralScalarString, ScalarString

from plugins.agent_editor.store import splice

from .loader import to_plain
from .spec import NAME_PATTERN

#: (mapping, sequence, offset) indentation tried per file; the rendering closest to the file wins.
INDENTS = ((2, 4, 2), (2, 2, 0))
STATE_TYPES = ("state", "choice", "junction", "final")
TRANSITION_FIELDS = ("trigger", "target", "guard", "effect", "description")
_NAME = re.compile(NAME_PATTERN)


class EditError(ValueError):
    """An operation that cannot be applied; ``message`` says why, for the author."""

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


def apply_op(text: str, op: dict[str, Any]) -> str:
    """The machine file ``text`` with the edit ``op`` (``{"op": "add_state", ...}``) applied."""
    if not isinstance(op, dict):
        raise EditError("an edit is an object {op: <operation>, ...}")
    name = op.get("op")
    handler = _OPS.get(name) if isinstance(name, str) else None
    if handler is None:
        raise EditError(f"unknown edit {name!r}; one of: {', '.join(_OPS)}")
    return handler(_Edit(text), op)


# ------------------------------------------------------------------ operations

def _add_state(edit: "_Edit", op: dict[str, Any]) -> str:
    name = _new_name(edit, op.get("name"))
    state_type = op.get("type") or "state"
    if state_type not in STATE_TYPES:
        raise EditError(f"type {state_type!r}: one of {', '.join(STATE_TYPES)}")
    activity = op.get("do")
    if activity is not None and (not isinstance(activity, dict) or state_type != "state"):
        raise EditError("do is a mapping with one activity kind, and only a simple state has one")
    body = CommentedMap()
    if state_type != "state":
        body["type"] = state_type
    if activity is not None:
        body["do"] = _node(activity)
    parent = op.get("parent")
    if parent is None:
        edit.doc["states"][name] = body
        return edit.result()
    holder = _state_body(edit.state(parent))
    if holder.get("type", "state") != "state" or "do" in holder:
        raise EditError(f"{parent!r} cannot hold states: only a simple state without do becomes a composite")
    region = holder.get("states")
    if isinstance(region, CommentedMap) and region:
        region[name] = body
    else:
        # the first child makes the state a composite; its keys go before the transitions, where readers look
        for stale in ("initial", "states"):  # an empty region and its initial, left from an earlier removal
            holder.pop(stale, None)
        at = list(holder).index("transitions") if "transitions" in holder else len(holder)
        holder.insert(at, "initial", name)
        holder.insert(at + 1, "states", CommentedMap([(name, body)]))
    return edit.result()


def _remove_state(edit: "_Edit", op: dict[str, Any]) -> str:
    found = edit.state(op.get("name"))
    gone = {found.name} | {inner.name for inner in _states(found.body)}
    removals: list[tuple[Any, Any]] = []
    for state in edit.states():
        if state.name in gone:
            continue
        seq = _transitions(state.body)
        doomed = [index for index, item in enumerate(seq) if isinstance(item, dict) and item.get("target") in gone]
        if doomed and len(doomed) == len(seq):
            removals.append((state.body, "transitions"))
        else:
            removals.extend((seq, index) for index in doomed)
    if len(found.region) > 1:
        removals.append((found.region, found.name))
    elif found.owner is edit.doc:
        raise EditError("a machine needs at least one state: this is its last one")
    else:
        # the composite's last child: it becomes a simple state again
        removals.extend((found.owner, key) for key in ("initial", "states") if key in found.owner)
    blocks = [edit.block(container, key) for container, key in removals]
    for container, key in sorted(removals, key=lambda pair: pair[1] if isinstance(pair[1], int) else 0, reverse=True):
        del container[key]
    return edit.result(cut=blocks)


def _rename_state(edit: "_Edit", op: dict[str, Any]) -> str:
    found = edit.state(op.get("old"))
    new = _new_name(edit, op.get("new"))
    position = list(found.region).index(found.name)
    comment = found.region.ca.items.get(found.name)
    del found.region[found.name]
    found.region.insert(position, new, found.body)
    if comment is not None:
        found.region.ca.items[new] = comment
    for owner in [edit.doc] + [state.body for state in edit.states() if isinstance(state.body, CommentedMap)]:
        if owner.get("initial") == found.name:
            owner["initial"] = _like(owner["initial"], new)
    for state in edit.states():
        for item in _transitions(state.body):
            if isinstance(item, CommentedMap) and item.get("target") == found.name:
                item["target"] = _like(item["target"], new)
    return edit.result()


def _set_state(edit: "_Edit", op: dict[str, Any]) -> str:
    found = edit.state(op.get("name"))
    source = op.get("yaml")
    if not isinstance(source, str):
        raise EditError("yaml: the state's mapping as YAML text")
    shown = edit.value_text(found.region, found.name)
    fragment = textwrap.dedent(source).strip("\n")
    try:
        # with its final newline, as replace_body writes it: a `|` block at the end keeps the newline it ends with
        body = _yaml().load(fragment + "\n") if fragment.strip() else None
    except YAMLError as exc:
        raise EditError(f"the state's YAML does not parse: {exc}") from None
    if body is None:
        body = CommentedMap()
    if not isinstance(body, CommentedMap):
        raise EditError("the state's YAML must be a mapping (type, do, transitions, ...)")
    current = to_plain(found.body) if found.body is not None else {}
    if to_plain(body) == current and _plain_lines(fragment) in [_plain_lines(text) for text in shown]:
        return edit.text  # applied as shown: the file stays byte-exact, an inline comment included
    lines = edit.replace_body(found.region, found.name, fragment if body else "")
    found.region[found.name] = body
    return edit.result(lines=lines)


def _add_transition(edit: "_Edit", op: dict[str, Any]) -> str:
    found = edit.state(op.get("source"))
    fields = {key: op.get(key) for key in TRANSITION_FIELDS if op.get(key) not in (None, "")}
    _check_target(edit, fields.get("target"))
    item = CommentedMap((key, _node(fields[key])) for key in TRANSITION_FIELDS if key in fields)
    body = _state_body(found)
    seq = body.get("transitions")
    if isinstance(seq, CommentedSeq):
        seq.append(item)
    else:
        body["transitions"] = CommentedSeq([item])
    return edit.result()


def _update_transition(edit: "_Edit", op: dict[str, Any]) -> str:
    found, seq, index = _transition(edit, op)
    fields = op.get("fields")
    if not isinstance(fields, dict) or not fields:
        raise EditError("fields: an object of transition keys to set (null removes one)")
    unknown = sorted(set(fields) - set(TRANSITION_FIELDS))
    if unknown:
        raise EditError(f"unknown transition keys {', '.join(unknown)}; they are {', '.join(TRANSITION_FIELDS)}")
    item = seq[index]
    if not isinstance(item, CommentedMap):
        raise EditError(f"transition {index} of {found.name!r} is not a mapping; fix it in the YAML tab")
    if fields.get("target") is not None:
        _check_target(edit, fields["target"])
    for key, value in fields.items():
        if value is None or value == "":
            item.pop(key, None)
        elif key in item:
            item[key] = _like(item[key], value) if isinstance(value, str) and "\n" not in value else _node(value)
        else:
            item[key] = _node(value)
    return edit.result()


def _remove_transition(edit: "_Edit", op: dict[str, Any]) -> str:
    found, seq, index = _transition(edit, op)
    container, key = (found.body, "transitions") if len(seq) == 1 else (seq, index)
    blocks = [edit.block(container, key)]
    del container[key]
    return edit.result(cut=blocks)


def _move_transition(edit: "_Edit", op: dict[str, Any]) -> str:
    found, seq, index = _transition(edit, op)
    to = op.get("to")
    if not isinstance(to, int) or isinstance(to, bool) or not 0 <= to < len(seq):
        raise EditError(f"to: a position from 0 to {len(seq) - 1}")
    if to == index:
        return edit.text
    lines = edit.move_item(seq, index, to)
    item = seq[index]
    del seq[index]
    seq.insert(to, item)
    return edit.result(lines=lines)


def _set_initial(edit: "_Edit", op: dict[str, Any]) -> str:
    found = edit.state(op.get("name"))
    parent = op.get("parent")
    if parent is not None and parent != found.parent:
        raise EditError(f"{found.name!r} is not a direct child of {parent!r}; initial names a direct child")
    owner = found.owner
    if "initial" in owner:
        owner["initial"] = _like(owner["initial"], found.name)
    else:
        owner.insert(list(owner).index("states"), "initial", found.name)
    return edit.result()


_OPS: dict[str, Callable[["_Edit", dict[str, Any]], str]] = {
    "add_state": _add_state,
    "remove_state": _remove_state,
    "rename_state": _rename_state,
    "set_state": _set_state,
    "add_transition": _add_transition,
    "update_transition": _update_transition,
    "remove_transition": _remove_transition,
    "move_transition": _move_transition,
    "set_initial": _set_initial,
}


# ------------------------------------------------------------------ the document

@dataclass
class _State:
    name: str
    body: Any                 # the state's mapping (or whatever a broken file holds there)
    region: CommentedMap      # the ``states`` mapping it is a key of
    owner: CommentedMap       # the mapping that holds that region: the machine, or the composite state
    parent: Optional[str]     # the composite's name; None at the top level


def _states(owner: Any, parent: Optional[str] = None) -> Iterator[_State]:
    """Every state below ``owner``, a composite before its children."""
    region = owner.get("states") if isinstance(owner, CommentedMap) else None
    if not isinstance(region, CommentedMap):
        return
    for key, body in region.items():
        yield _State(str(key), body, region, owner, parent)
        yield from _states(body, str(key))


def _transitions(body: Any) -> CommentedSeq:
    seq = body.get("transitions") if isinstance(body, CommentedMap) else None
    return seq if isinstance(seq, CommentedSeq) else CommentedSeq()


def _state_body(found: _State) -> CommentedMap:
    if isinstance(found.body, CommentedMap):
        return found.body
    if found.body is None:  # `name:` with nothing after it
        found.region[found.name] = CommentedMap()
        return found.region[found.name]
    raise EditError(f"state {found.name!r} is not a mapping; fix it in the YAML tab first")


def _transition(edit: "_Edit", op: dict[str, Any]) -> tuple[_State, CommentedSeq, int]:
    found = edit.state(op.get("source"))
    seq = _transitions(found.body)
    index = op.get("index")
    if not isinstance(index, int) or isinstance(index, bool) or not 0 <= index < len(seq):
        raise EditError(f"{found.name!r} has no transition {index!r}" + (f" (0 to {len(seq) - 1})" if seq else ""))
    return found, seq, index


def _new_name(edit: "_Edit", name: Any) -> str:
    if not isinstance(name, str) or not _NAME.match(name):
        raise EditError(f"state name {name!r} must match {NAME_PATTERN} (lowercase, digits, underscore)")
    if any(state.name == name for state in edit.states()):
        raise EditError(f"a state {name!r} exists already; state names are unique within a machine")
    return name


def _check_target(edit: "_Edit", target: Any) -> None:
    if target is None:
        return
    if not isinstance(target, str) or not any(state.name == target for state in edit.states()):
        raise EditError(f"target {target!r} is no state of this machine")


def _node(value: Any) -> Any:
    """Plain data as round-trip nodes; text over several lines as a ``|`` block."""
    if isinstance(value, dict):
        return CommentedMap((str(key), _node(item)) for key, item in value.items())
    if isinstance(value, list):
        return CommentedSeq(_node(item) for item in value)
    if isinstance(value, str) and "\n" in value:
        return LiteralScalarString(value)
    return value


def _like(old: Any, new: Any) -> Any:
    """``new`` in the quoting ``old`` was written with."""
    return type(old)(new) if isinstance(old, ScalarString) and isinstance(new, str) else _node(new)


# ------------------------------------------------------------------ text

def _yaml(indent: tuple[int, int, int] = INDENTS[0]) -> YAML:
    yaml = YAML(typ="rt")
    yaml.allow_duplicate_keys = False
    yaml.preserve_quotes = True
    yaml.width = 4096  # a long guard stays on its line
    yaml.indent(mapping=indent[0], sequence=indent[1], offset=indent[2])
    return yaml


def _dump(doc: Any, indent: tuple[int, int, int]) -> str:
    buffer = io.StringIO()
    _yaml(indent).dump(doc, buffer)
    return buffer.getvalue()


def _reads_as(text: str, expected: Any) -> bool:
    try:
        return to_plain(_yaml().load(text)) == expected
    except YAMLError:
        return False


def _distance(a: str, b: str) -> int:
    matcher = difflib.SequenceMatcher(None, a.splitlines(), b.splitlines(), autojunk=False)
    return sum(max(i2 - i1, j2 - j1) for tag, i1, i2, j1, j2 in matcher.get_opcodes() if tag != "equal")


def _indent(line: str) -> int:
    return len(line) - len(line.lstrip(" "))


def _split(text: str) -> tuple[list[str], bool]:
    lines = text.splitlines(keepends=True)
    open_end = bool(lines) and not lines[-1].endswith("\n")
    if open_end:
        lines[-1] += "\n"
    return lines, open_end


def _plain_lines(text: str) -> list[str]:
    """A fragment's lines as compared for a no-op: without trailing spaces and the blank lines around them."""
    return [line.rstrip() for line in textwrap.dedent(text).strip("\n").split("\n")]


def _body_end(lines: list[str], line: int, column: int) -> int:
    """End (exclusive) of the lines below ``line`` that sit deeper than ``column``; blank lines after them excluded."""
    end = line + 1
    for index in range(line + 1, len(lines)):
        if not lines[index].strip():
            continue
        if _indent(lines[index]) <= column:
            break
        end = index + 1
    return end


class _Edit:
    """One operation on one file: the document, the file's layout, and the ways to write the result."""

    def __init__(self, text: str):
        self.text = text
        try:
            self.doc = _yaml().load(text)
        except YAMLError as exc:
            raise EditError(f"the file does not parse as YAML ({exc}); fix it in the YAML tab first") from None
        if not isinstance(self.doc, CommentedMap) or not isinstance(self.doc.get("states"), CommentedMap):
            raise EditError("the file is no machine with states; fix it in the YAML tab first")
        renderings: dict[tuple[int, int, int], str] = {}
        for indent in INDENTS:
            renderings[indent] = _dump(self.doc, indent)
            if renderings[indent] == text:
                break
        else:
            indent = min(renderings, key=lambda setting: _distance(text, renderings[setting]))
        self.indent = indent
        self.base = renderings[indent]  # the unedited document as ruamel writes it: splice's common ancestor
        self.lines, self.open_end = _split(text)

    # -- finding
    def states(self) -> Iterator[_State]:
        return _states(self.doc)

    def state(self, name: Any) -> _State:
        for state in self.states():
            if state.name == name:
                return state
        raise EditError(f"no state {name!r} in this machine")

    # -- line work, planned on the unedited text; None where the layout is not plain block style
    def block(self, container: Any, key: Any) -> Optional[tuple[int, int]]:
        """Lines of a mapping entry or a sequence item: the comment lines right above it at its column, its line,
        the deeper lines below."""
        try:
            line, column = container.lc.item(key) if isinstance(container, CommentedSeq) else container.lc.key(key)
        except (KeyError, AttributeError, TypeError):
            return None
        if line >= len(self.lines):
            return None
        if isinstance(container, CommentedSeq):
            head = self.lines[line][:column].rstrip()
            if not head.endswith("-"):
                return None
            column = len(head) - 1
        if self.lines[line][:column].strip():
            return None  # shares its line with something else (a flow collection, a first key after a dash)
        start = line
        while start > 0 and self.lines[start - 1].lstrip().startswith("#") and _indent(self.lines[start - 1]) == column:
            start -= 1
        return start, _body_end(self.lines, line, column)

    def move_item(self, seq: CommentedSeq, index: int, to: int) -> Optional[list[str]]:
        blocks = [self.block(seq, i) for i in range(len(seq))]
        if any(block is None for block in blocks):
            return None
        (start, end), target = blocks[index], blocks[to]
        chunk, rest = self.lines[start:end], self.lines[:start] + self.lines[end:]
        at = target[0] if to < index else target[1] - (end - start)  # type: ignore[index]
        return rest[:at] + chunk + rest[at:]

    def value_text(self, region: CommentedMap, name: str) -> list[str]:
        """The texts a state's value is shown as (the panel's stateFragment): the lines below its key, dedented; or
        the value on the key line, with and without its trailing comment. A key line the inspector cannot stand in
        for is refused: an anchor, a tag or an alias there, or a flow collection that goes on over the next lines."""
        try:
            line, column = region.lc.key(name)
        except (KeyError, AttributeError):
            return []
        own = self.lines[line] if line < len(self.lines) else ""
        head = re.match(rf"^( *){re.escape(name)} *:(?P<rest>.*)$", own.rstrip("\n"))
        if head is None or len(head.group(1)) != column:
            return []
        rest = head.group("rest").strip()
        if rest[:1] in ("&", "!", "*"):
            raise EditError(f"state {name!r} has an anchor, a tag or an alias on its key line; edit it in the YAML tab")
        if rest and not rest.startswith("#"):
            try:
                _yaml().load(rest + "\n")
            except YAMLError:
                raise EditError(f"state {name!r} starts on its key line and goes on below it; "
                                "edit it in the YAML tab") from None
            return [rest] + [rest[:at.start()] for at in re.finditer(r"\s#", rest)]
        return ["".join(self.lines[line + 1:_body_end(self.lines, line, column)])]

    def replace_body(self, region: CommentedMap, name: str, fragment: str) -> Optional[list[str]]:
        """The lines with the state's value replaced by ``fragment``, indented under its key."""
        try:
            line, column = region.lc.key(name)
        except (KeyError, AttributeError):
            return None
        own = self.lines[line] if line < len(self.lines) else ""
        head = re.match(rf"^( *){re.escape(name)} *:(?P<rest>.*)$", own.rstrip("\n"))
        if head is None or len(head.group(1)) != column:
            return None
        rest = head.group("rest").strip()
        inline = bool(rest) and not rest.startswith("#")
        end = line + 1 if inline else _body_end(self.lines, line, column)
        if not fragment:
            key_line = f"{' ' * column}{name}: {{}}\n"
            body: list[str] = []
        else:
            key_line = f"{' ' * column}{name}:\n" if inline else own
            pad = " " * (column + self.indent[0])
            body = [f"{pad}{text}\n" if text.strip() else "\n" for text in fragment.split("\n")]
        return self.lines[:line] + [key_line] + body + self.lines[end:]

    # -- writing
    def result(self, *, cut: Optional[list[Optional[tuple[int, int]]]] = None,
               lines: Optional[list[str]] = None) -> str:
        """The edited document as text: planned lines first, then the splice, then ruamel's own rendering."""
        expected = to_plain(self.doc)
        for candidate in self._candidates(cut, lines):
            if _reads_as(candidate, expected):
                return candidate
        raise EditError("the edit cannot be written so that the file reads back as intended; "
                        "change it in the YAML tab")

    def _candidates(self, cut: Optional[list[Optional[tuple[int, int]]]],
                    lines: Optional[list[str]]) -> Iterator[str]:
        if cut and all(block is not None for block in cut):
            yield self._join(self._cut(cut))  # type: ignore[arg-type]
        if lines is not None:
            yield self._join(lines)
        new = _dump(self.doc, self.indent)
        yield splice(self.text, self.base, new, True)
        yield splice(self.text, self.base, new, False)
        yield new

    def _cut(self, blocks: list[tuple[int, int]]) -> list[str]:
        ordered = sorted(set(blocks), reverse=True)
        # a block inside another one (a transition of a removed state) goes with the outer one
        kept = [b for b in ordered if not any(o != b and o[0] <= b[0] and b[1] <= o[1] for o in ordered)]
        lines = list(self.lines)
        for start, end in kept:
            if start > 0 and not lines[start - 1].strip():
                if end < len(lines) and not lines[end].strip():
                    end += 1  # blank above and below: one of them goes with the node
                elif end == len(lines):
                    start -= 1
            del lines[start:end]
        return lines

    def _join(self, lines: list[str]) -> str:
        text = "".join(lines)
        return text[:-1] if self.open_end and text.endswith("\n") else text
