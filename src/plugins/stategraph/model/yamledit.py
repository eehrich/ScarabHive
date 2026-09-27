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
import warnings
from dataclasses import dataclass
from typing import Any, Callable, Iterator, Optional

from ruamel.yaml import YAML
from ruamel.yaml.comments import CommentedMap, CommentedSeq, merge_attrib
from ruamel.yaml.error import ReusedAnchorWarning, YAMLError
from ruamel.yaml.scalarstring import FoldedScalarString, LiteralScalarString, ScalarString

from plugins.agent_editor.store import splice

from .loader import to_plain, yaml_bounds
from .spec import NAME_PATTERN, check_state_name

#: (mapping, sequence, offset) indentation tried per file; the rendering closest to the file wins.
INDENTS = ((2, 4, 2), (2, 2, 0))
STATE_TYPES = ("state", "choice", "junction", "final")
TRANSITION_FIELDS = ("trigger", "target", "guard", "effect", "description")
#: What the inspector's forms set with update_state / update_machine; structure (states, transitions, initial) has
#: its own operations, and a machine's id is its file name.
STATE_FIELDS = ("type", "description", "entry", "exit", "max_visits", "timeout", "status", "output", "finally", "do")
MACHINE_FIELDS = ("title", "description", "group", "vars_from", "limits", "params", "events", "context", "vars",
                  "imports", "resources", "finally")
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
        edit.untied(f"add state {name!r}", edit.doc["states"])
        edit.doc["states"][name] = body
        return edit.result()
    holder = _state_body(edit.state(parent))
    if holder.get("type", "state") != "state" or "do" in holder:
        raise EditError(f"{parent!r} cannot hold states: only a simple state without do becomes a composite")
    region = holder.get("states")
    edit.untied(f"add state {name!r} to {parent!r}", holder, region)
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
    edit.untied(f"remove state {found.name!r}", *(container for container, _ in removals))
    blocks = [edit.block(container, key) for container, key in removals]
    for container, key in sorted(removals, key=lambda pair: pair[1] if isinstance(pair[1], int) else 0, reverse=True):
        del container[key]
    return edit.result(cut=blocks)


def _rename_state(edit: "_Edit", op: dict[str, Any]) -> str:
    found = edit.state(op.get("old"))
    new = _new_name(edit, op.get("new"))
    edit.untied(f"rename state {found.name!r}", found.region)
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
    lines = edit.replace_body(found.region, found.name, fragment)
    if lines is not None:
        # read in its place in the file: an alias of an anchor elsewhere resolves, an anchor of its own reaches the
        # aliases after it; the typed lines are then the result as they stand
        try:
            doc = _parse(edit._join(lines), "the state's YAML")
        except EditError as exc:  # a state tied to another place: most likely an anchor it held is gone
            if edit.tied(found.body, whole=True):
                raise EditError(f"{_shared(f'state {found.name!r}')} ({exc.message})") from None
            raise
        if not isinstance(doc, CommentedMap) or not isinstance(doc.get("states"), CommentedMap):
            raise EditError("the state's YAML breaks the file around it (check its indentation)")
        bodies = {state.name: state.body for state in _states(doc)}
        if found.name not in bodies:  # a quote or an indentation ran past it and took it
            raise EditError(_beyond(found.name))
        body = bodies[found.name]
        if body is None:  # a typed null: the machine would not load
            raise EditError("the state's YAML must be a mapping (type, do, transitions, ...)")
    else:  # a layout the lines cannot stand in for: the text alone, rendered into the file
        doc = None
        # with its final newline, as replace_body writes it: a `|` block at the end keeps the newline it ends with
        body = _parse(fragment + "\n", "the state's YAML") if fragment.strip() else None
    if body is None:
        body = CommentedMap()
    if not isinstance(body, CommentedMap):
        raise EditError("the state's YAML must be a mapping (type, do, transitions, ...)")
    current = to_plain(found.body) if found.body is not None else {}
    if to_plain(body) == current and _plain_lines(fragment) in [_plain_lines(text) for text in shown]:
        return edit.text  # applied as shown: the file stays byte-exact, an inline comment included
    # an anchor or alias in it: the typed lines keep them, or the edit is refused
    tied = f"state {found.name!r}" if edit.tied(found.body, whole=True) else ""
    found.region[found.name] = body  # the old document with this state's value new, nothing else
    if doc is not None:
        # the typed lines read as a new file; it must be that document: an anchor the text keeps or reuses
        # reaches other states, a quote or an indentation can run past the state
        if to_plain(edit.doc) != to_plain(doc):
            raise EditError(_shared(tied) if tied else _beyond(found.name))
        edit.doc = doc
    return edit.result(lines=lines, tied=tied)


def _add_transition(edit: "_Edit", op: dict[str, Any]) -> str:
    found = edit.state(op.get("source"))
    fields = {key: op.get(key) for key in TRANSITION_FIELDS if op.get(key) not in (None, "")}
    _check_target(edit, fields.get("target"))
    item = CommentedMap((key, _node(fields[key])) for key in TRANSITION_FIELDS if key in fields)
    body = _state_body(found)
    seq = body.get("transitions")
    edit.untied(f"state {found.name!r}", seq)
    edit.untied_key(f"state {found.name!r}", body, "transitions")
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
    merged_from = list(getattr(item, merge_attrib, None) or [])  # the mappings it merges
    for key, value in fields.items():  # an item that merges another: its own keys only
        edit.untied_key(f"state {found.name!r}", item, key)
        if value in (None, "") and key in item and any(key in source for source in merged_from):
            raise EditError(f"state {found.name!r}: without its own {key}, the transition takes the one it merges; "
                            "edit it in the YAML tab")
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
    # an anchor or a merge in the list: only the moved lines keep them where they are (an alias above its anchor
    # does not read; a rendering would move the anchor into another item)
    tied = f"state {found.name!r}" if edit.tied(seq, whole=True) else ""
    item = seq[index]
    del seq[index]
    seq.insert(to, item)
    return edit.result(lines=lines, tied=tied)


def _set_initial(edit: "_Edit", op: dict[str, Any]) -> str:
    found = edit.state(op.get("name"))
    parent = op.get("parent")
    if parent is not None and parent != found.parent:
        raise EditError(f"{found.name!r} is not a direct child of {parent!r}; initial names a direct child")
    owner = found.owner
    edit.untied(f"initial {found.name!r}", owner)
    if "initial" in owner:
        owner["initial"] = _like(owner["initial"], found.name)
    else:
        owner.insert(list(owner).index("states"), "initial", found.name)
    return edit.result()


def _update_state(edit: "_Edit", op: dict[str, Any]) -> str:
    """Keys of a state and of its activity: ``fields`` {key: value}, ``do`` {key: value}; null or "" removes one,
    ``{"$yaml": text}`` is a value typed as YAML (an object). Another activity kind is its key set and the old one's
    removed: ``{"agent": null, "task": null, "decide": "noul", ...}``."""
    found = edit.state(op.get("name"))
    fields, activity = op.get("fields") or {}, op.get("do")
    if not isinstance(fields, dict) or not (activity is None or isinstance(activity, dict)) or not (fields or activity):
        raise EditError("fields: state keys to set (null removes one); do: keys of its activity to set")
    unknown = sorted(set(fields) - set(STATE_FIELDS))
    if unknown:
        raise EditError(f"unknown state keys {', '.join(unknown)}; they are {', '.join(STATE_FIELDS)}")
    if fields.get("type") not in (None, "", *STATE_TYPES):
        raise EditError(f"type {fields['type']!r}: one of {', '.join(STATE_TYPES)}")
    what = f"state {found.name!r}"
    body = _state_body(found)
    for key, value in fields.items():
        if key == "type" and value == "state":
            value = None  # the default: no key
        _put(edit, what, body, key, _value(value, key), before=("transitions", "states"))
    if activity:
        from ..kinds.base import REGISTRY  # the kind keys go first, where readers look

        do = body.get("do")
        if do is None:
            do = CommentedMap()
        elif not isinstance(do, CommentedMap):
            raise EditError(f"{what}: its do is not a mapping; fix it in the YAML tab")
        else:  # an activity another state aliases: _put refuses each key (untied_key)
            edit.untied_key(what, body, "do")
        for key, value in activity.items():
            _put(edit, what, do, str(key), _value(value, f"do.{key}"), first=key in REGISTRY)
        if not do:
            _put(edit, what, body, "do", None)
        elif body.get("do") is not do:  # a new activity (also where `do:` stood empty)
            _put(edit, what, body, "do", do, before=("transitions", "states"))
    return edit.result()


def _update_machine(edit: "_Edit", op: dict[str, Any]) -> str:
    """Top-level keys of the machine, as update_state sets a state's."""
    fields = op.get("fields")
    if not isinstance(fields, dict) or not fields:
        raise EditError("fields: machine keys to set (null removes one)")
    unknown = sorted(set(fields) - set(MACHINE_FIELDS))
    if unknown:
        raise EditError(f"unknown machine keys {', '.join(unknown)}; they are {', '.join(MACHINE_FIELDS)}")
    for key, value in fields.items():
        _put(edit, "the machine", edit.doc, key, _value(value, key), before=("initial", "states"))
    return edit.result()


def _value(value: Any, where: str) -> Any:
    """A form's value: "" is none, {"$yaml": text} the nodes the text reads as (its comments kept)."""
    if isinstance(value, dict) and set(value) == {"$yaml"}:
        text = value["$yaml"]
        if not isinstance(text, str):
            raise EditError(f"{where}: $yaml is YAML text")
        return _parse(text, where) if text.strip() else None
    return None if value == "" else value


def _put(edit: "_Edit", what: str, mapping: CommentedMap, key: str, value: Any, *, before: tuple[str, ...] = (),
         first: bool = False) -> None:
    """Set or (None) remove one key of ``mapping``; a new key goes first or before the first of ``before``."""
    edit.untied_key(what, mapping, key)
    old = mapping.get(key)
    if isinstance(old, (CommentedMap, CommentedSeq)) and edit.tied(old, whole=True):
        raise EditError(_shared(f"{what}: {key}"))  # its anchor, or a merge in it: other places see it
    if value is None:
        mapping.pop(key, None)
        return
    node = value if isinstance(value, (CommentedMap, CommentedSeq)) else (
        _like(old, value) if key in mapping and isinstance(value, str) and "\n" not in value else _node(value))
    if key in mapping:
        mapping[key] = node
        return
    keys = list(mapping)
    at = 0 if first else min((keys.index(name) for name in before if name in mapping), default=len(keys))
    mapping.insert(at, key, node)


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
    "update_state": _update_state,
    "update_machine": _update_machine,
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
    item = seq[index]
    edit.untied(f"state {found.name!r}", seq)
    if edit.reached.get(id(item), 0) > 1 or getattr(item, "_ref", None):  # aliased, or merged into another item
        raise EditError(_shared(f"state {found.name!r}"))
    edit.untied_key(f"state {found.name!r}", found.body, "transitions")
    return found, seq, index


def _new_name(edit: "_Edit", name: Any) -> str:
    if not isinstance(name, str) or not _NAME.fullmatch(name):
        raise EditError(f"state name {name!r} must match {NAME_PATTERN} (lowercase, digits, underscore)")
    try:
        check_state_name(name)
    except ValueError as exc:  # finally, resources: the machine would not load
        raise EditError(str(exc)) from None
    if any(state.name == name for state in edit.states()):
        raise EditError(f"a state {name!r} exists already; state names are unique within a machine")
    return name


def _check_target(edit: "_Edit", target: Any) -> None:
    if target is None:
        return
    if not isinstance(target, str) or not any(state.name == target for state in edit.states()):
        raise EditError(f"target {target!r} is no state of this machine")


def _beyond(name: str) -> str:
    return (f"state {name!r}: the typed YAML changes more than this state (an anchor used elsewhere, or a quote or an "
            "indentation that runs past it); edit it in the YAML tab")


def _key_line(name: str) -> re.Pattern:
    """A state's key line: its name plain or quoted, then the rest."""
    key = re.escape(name)
    return re.compile(rf"^( *)(?:{key}|\"{key}\"|'{key}') *:(?P<rest>.*)$")


def _shared(what: str) -> str:
    return (f"{what}: this changes YAML that is shared with another place (an anchor and its alias, or a merge key "
            "<<); edit it in the YAML tab")


def _reached(doc: Any) -> dict[int, int]:
    """How many ways lead to each mapping and sequence of ``doc``; more than one: an alias or a merge shares it
    (everything below a shared node too). The walk expands aliases: ``yaml_bounds`` has bounded that."""
    counts: dict[int, int] = {}

    def walk(node: Any) -> None:
        if isinstance(node, (CommentedMap, CommentedSeq)):
            counts[id(node)] = counts.get(id(node), 0) + 1
            for child in node.values() if isinstance(node, CommentedMap) else node:
                walk(child)

    walk(doc)
    return counts


def _below(node: Any) -> Iterator[Any]:
    """``node`` and every mapping and sequence below it, each once."""
    seen: set[int] = set()
    todo = [node]
    while todo:
        item = todo.pop()
        if isinstance(item, (CommentedMap, CommentedSeq)) and id(item) not in seen:
            seen.add(id(item))
            yield item
            todo.extend(item.values() if isinstance(item, CommentedMap) else item)


def _parse(text: str, what: str) -> Any:
    """Typed YAML as round-trip nodes, bounded like a file; what does not read is an EditError naming ``what``."""
    try:
        with warnings.catch_warnings():
            # a typed anchor under a name the file has already would rebind the aliases after it
            warnings.simplefilter("error", ReusedAnchorWarning)
            doc = _yaml().load(text)
    except ReusedAnchorWarning as exc:
        raise EditError(f"{what} names an anchor the file has already: {exc}") from None
    except YAMLError as exc:
        raise EditError(f"{what} does not parse: {exc}") from None
    except RecursionError:
        raise EditError(f"{what} is nested too deeply to read") from None
    except Exception as exc:  # ruamel's constructor: a merge key that names its own anchor
        raise EditError(f"{what} does not read as YAML: {exc}") from None
    too_big = yaml_bounds(doc)
    if too_big:
        raise EditError(f"{what}: {too_big.removeprefix('YAML: ')}")
    return doc


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
    """``new`` in the quoting ``old`` was written with; a folded text (``>``) folded again at its width."""
    if isinstance(old, FoldedScalarString) and isinstance(new, str):
        return _refold(old, new)
    return type(old)(new) if isinstance(old, ScalarString) and isinstance(new, str) else _node(new)


def _refold(old: FoldedScalarString, new: str) -> FoldedScalarString:
    """``new`` as a folded text broken at spaces no wider than ``old``'s longest line."""
    text = str(old)
    cuts = [0, *(getattr(old, "fold_pos", None) or []), len(text)]
    width = max(b - a for a, b in zip(cuts, cuts[1:]))
    folded = FoldedScalarString(new)
    positions, start = [], 0
    while len(new) - start > width:
        cut = new.rfind(" ", start, start + width + 1)
        if cut <= start:  # a word longer than the line: it stays whole
            cut = new.find(" ", start + width)
            if cut < 0:
                break
        positions.append(cut)
        start = cut + 1
    folded.fold_pos = positions
    return folded


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
        doc = _yaml().load(text)
    except Exception:  # YAMLError, RecursionError, or ruamel's constructor
        return False
    return not yaml_bounds(doc) and to_plain(doc) == expected  # a typed anchor may enlarge the aliases after it


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
        except RecursionError:
            raise EditError("the file is nested too deeply to read as YAML; fix it in the YAML tab first") from None
        except Exception as exc:  # ruamel's constructor: a merge key that names its own anchor
            raise EditError(f"the file does not read as YAML ({exc}); fix it in the YAML tab first") from None
        too_big = yaml_bounds(self.doc)
        if too_big:
            raise EditError(f"{too_big}; fix it in the YAML tab first")
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
        self.reached = _reached(self.doc)

    def tied(self, *nodes: Any, whole: bool = False) -> bool:
        """Whether YAML ties ``nodes`` to another place of the file: a node reached twice (an anchor and its alias)
        or a mapping in a merge (``<<: *base``). ``whole``: or anything below them."""
        for node in nodes:
            for part in _below(node) if whole else [node]:
                merged = getattr(part, merge_attrib, None) or getattr(part, "_ref", None)  # it merges, or is merged
                if merged or self.reached.get(id(part), 0) > 1:
                    return True
        return False

    def untied(self, what: str, *nodes: Any) -> None:
        """Refuse to change ``nodes`` where YAML ties them to another place: the edit would change that place as
        well, or fail inside ruamel."""
        if self.tied(*nodes):
            raise EditError(_shared(what))

    def untied_key(self, what: str, mapping: Any, key: str) -> None:
        """Refuse to set or remove ``key`` of ``mapping`` where another place sees it: the mapping is reached twice,
        its ``key`` comes through its own merge (``<<: *base``), or a mapping that merges it inherits ``key``. A
        mapping's own key beside a merge is its own business."""
        if not isinstance(mapping, CommentedMap):
            return
        inherited = key in mapping and not mapping._unmerged_contains(key)
        heirs = [other for other in getattr(mapping, "_ref", ()) if not other._unmerged_contains(key)]
        if inherited or heirs or self.reached.get(id(mapping), 0) > 1:
            raise EditError(_shared(what))

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
        head = _key_line(name).match(own.rstrip("\n"))
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
        head = _key_line(name).match(own.rstrip("\n"))
        if head is None or len(head.group(1)) != column:
            return None
        rest = head.group("rest").strip()
        inline = bool(rest) and not rest.startswith("#")
        end = line + 1 if inline else _body_end(self.lines, line, column)
        pad = " " * (column + self.indent[0])
        if all(not text.strip() or text.lstrip().startswith("#") for text in fragment.split("\n")):
            # nothing but comments (or nothing): an empty mapping, not a null the machine would not load
            key_line = f"{' ' * column}{name}: {{}}\n"
            body: list[str] = [f"{pad}{text.strip()}\n" for text in fragment.split("\n") if text.strip()]
        else:
            key_line = f"{' ' * column}{name}:\n" if inline else own
            body = [f"{pad}{text}\n" if text.strip() else "\n" for text in fragment.split("\n")]
        return self.lines[:line] + [key_line] + body + self.lines[end:]

    # -- writing
    def result(self, *, cut: Optional[list[Optional[tuple[int, int]]]] = None,
               lines: Optional[list[str]] = None, tied: str = "") -> str:
        """The edited document as text: planned lines first, then the splice, then ruamel's own rendering.

        ``tied`` (what the edit is about): the edit touches an anchor or an alias, so only the planned lines may
        write it -- a rendering would expand the alias into a copy or drop the anchor."""
        expected = to_plain(self.doc)
        for candidate in self._candidates(cut, lines, render=not tied):
            if _reads_as(candidate, expected):
                return candidate
        if tied:
            raise EditError(_shared(tied))
        raise EditError("the edit cannot be written so that the file reads back as intended; "
                        "change it in the YAML tab")

    def _candidates(self, cut: Optional[list[Optional[tuple[int, int]]]],
                    lines: Optional[list[str]], render: bool = True) -> Iterator[str]:
        if cut and all(block is not None for block in cut):
            yield self._join(self._cut(cut))  # type: ignore[arg-type]
        if lines is not None:
            yield self._join(lines)
        if not render:
            return
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
