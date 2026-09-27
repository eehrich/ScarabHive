"""The editor's view of a machine: the root file's states and transitions as flat lists for the panel's canvas.

Built from the round-trip document, not from the validated spec, so a machine with a schema error (an unknown key,
a wrong type) still shows its graph: the problems say what is wrong, the canvas shows where. Only a root file that
does not parse as a YAML mapping gives the empty graph. Nothing here raises on a broken machine.

Paths use the loader's notation (``states.review.states.inner``, ``states.judge.transitions[1]``), the same the
problems carry, so the panel can pin a problem to the state or transition it is about.
"""

from __future__ import annotations

import io
import json
from typing import Any, Optional

from ruamel.yaml import YAML
from ruamel.yaml.comments import CommentedMap, CommentedSeq, merge_attrib

from plugins.stategraph.kinds import kind_of, parse_activity
from .loader import MachineTree, dotted, line_of, to_plain
from .spec import TRIGGER_DONE, is_wait_state

STATE_TYPES = ("state", "choice", "junction", "final")


def empty_graph(machine_id: Optional[str] = None) -> dict[str, Any]:
    return {"id": machine_id, "title": "", "description": "", "initial": None, "states": [], "transitions": [],
            "imports": {}, "params": {}, "events": {}, "context": {}}


def graph_view(tree: MachineTree) -> dict[str, Any]:
    """States (pre-order, a composite before its children) and transitions of the root machine file."""
    loaded = tree.files.get(tree.root)
    doc = _quoted(loaded.text, loaded.doc) if loaded is not None else None
    if not isinstance(doc, dict):
        return empty_graph()
    graph = empty_graph(_text(doc.get("id")))
    graph["title"] = _text(doc.get("title")) or ""
    graph["description"] = _text(doc.get("description")) or ""
    graph["initial"] = _text(doc.get("initial"))
    graph["imports"] = {str(alias): str(ref) for alias, ref in _mapping(doc.get("imports")).items()
                        if isinstance(ref, str)}
    graph["params"] = {str(name): to_plain(field) if isinstance(field, dict) else {}
                       for name, field in _mapping(doc.get("params")).items()}
    graph["events"] = {str(name): to_plain(event) if isinstance(event, dict) else {}
                       for name, event in _mapping(doc.get("events")).items()}
    graph["context"] = to_plain(_mapping(doc.get("context")))
    for key in ("group", "python", "vars_from"):
        graph[key] = _text(doc.get(key))
    graph["yaml"], graph["locked"] = _texts({key: doc[key] for key in MACHINE_OBJECTS if key in doc})
    _walk(doc, _mapping(doc.get("states")), None, ["states"], graph)
    return graph


def _walk(doc: Any, states: dict[Any, Any], parent: Optional[str], prefix: list[Any], graph: dict[str, Any]) -> None:
    for name, raw in states.items():
        name = str(name)
        path = prefix + [name]
        body = raw if isinstance(raw, dict) else {}
        children = _mapping(body.get("states"))
        transitions = body.get("transitions") if isinstance(body.get("transitions"), list) else []
        kind, label, icon = _activity(body.get("do"))
        state_type = body.get("type") if body.get("type") in STATE_TYPES else "state"
        triggers = [str(t.get("trigger") or TRIGGER_DONE) for t in transitions if isinstance(t, dict)]
        graph["states"].append({
            "name": name,
            "parent": parent,
            "type": state_type,
            "composite": bool(children),
            "initial": _text(body.get("initial")) if children else None,
            "kind": kind,
            "label": label,
            "icon": icon,
            "wait": is_wait_state(state_type, bool(children), body.get("do") is not None, triggers),
            "entry": _text(body.get("entry")),
            "exit": _text(body.get("exit")),
            "max_visits": body.get("max_visits") if isinstance(body.get("max_visits"), int) else None,
            "timeout": to_plain(body.get("timeout")) if isinstance(body.get("timeout"), (int, float, str)) else None,
            "status": _text(body.get("status")),
            "description": _text(body.get("description")) or "",
            "do": to_plain(body["do"]) if isinstance(body.get("do"), dict) else None,
            "output": to_plain(body.get("output")),
            "finally": to_plain(body.get("finally")),
            "line": line_of(doc, path),
            "path": dotted(path),
        })
        # what the inspector shows as YAML text: the activity's objects, a final's output, the finally activity
        activity = body.get("do") if isinstance(body.get("do"), dict) else {}
        texts, locked = _texts({
            **{f"do.{key}": value for key, value in activity.items() if isinstance(value, (dict, list))},
            **{key: body[key] for key in ("output", "finally") if key in body}})
        shared = _shared_keys(body)
        if "do" in shared or getattr(getattr(activity, "anchor", None), "value", None) or getattr(activity, merge_attrib, None):
            shared.append("do")  # the whole activity is another place's too: edited in the YAML tab
        graph["states"][-1]["yaml"], graph["states"][-1]["locked"] = texts, sorted(set(locked + shared))
        for index, item in enumerate(transitions):
            if not isinstance(item, dict):
                continue  # the index stays the list index: edits address transitions by it
            target = _text(item.get("target"))
            where = path + ["transitions", index]
            graph["transitions"].append({
                "id": f"{name}#{index}",
                "source": name,
                "index": index,
                "target": target,
                "trigger": _text(item.get("trigger")) or TRIGGER_DONE,
                "guard": _code(item.get("guard")),
                "effect": _code(item.get("effect")),
                "description": _text(item.get("description")) or "",
                "internal": target is None,
                "line": line_of(doc, where),
                "path": dotted(where),
            })
        if children:
            _walk(doc, children, name, path + ["states"], graph)


def _activity(raw: Any) -> tuple[Optional[str], str, Optional[str]]:
    """(kind key, canvas label, icon) of a ``do`` mapping; a broken one still shows its kind where it names one."""
    if raw is None:
        return None, "", None
    try:
        kind = kind_of(raw)
    except ValueError:  # KindLookupError: no kind key, several, or not a mapping
        return None, "", None
    try:
        activity_kind, spec = parse_activity(to_plain(raw))
        label = activity_kind.label(spec)
    except Exception:  # a label is decoration: an invalid activity must not cost the whole graph
        value = raw.get(kind.key)
        label = value if isinstance(value, str) else kind.title
    return kind.key, str(label or ""), kind.icon


#: Machine keys the inspector edits as YAML text.
MACHINE_OBJECTS = ("limits", "params", "events", "context", "vars", "imports", "resources", "finally")


def _texts(values: dict[str, Any]) -> tuple[dict[str, str], list[str]]:
    """``({path: YAML text}, [locked paths])``: a value with an anchor, an alias or a merge in it is edited in the
    YAML tab (the text alone would lose what ties it to the rest of the file)."""
    texts: dict[str, str] = {}
    locked: list[str] = []
    for path, value in values.items():
        if _tied(value):
            locked.append(path)
        elif isinstance(value, (CommentedMap, CommentedSeq)):
            buffer = io.StringIO()
            yaml = YAML(typ="rt")
            yaml.width = 4096
            yaml.indent(mapping=2, sequence=4, offset=2)
            yaml.dump(value, buffer)
            texts[path] = buffer.getvalue()
        else:  # a scalar: as JSON, which reads back as the same YAML scalar
            texts[path] = json.dumps(to_plain(value), ensure_ascii=False)
    return texts, locked


def _quoted(text: str, doc: Any) -> Any:
    """The file read again with its quotes kept, for texts the editor sends back (``doc``: where that fails)."""
    try:
        yaml = YAML(typ="rt")
        yaml.preserve_quotes = True
        return yaml.load(text)
    except Exception:  # the loader read it: this does not fail in practice, and doc says the same without quotes
        return doc


def _shared_keys(body: Any) -> list[str]:
    """A state's keys another place sees: inherited through its own merge, inherited by a state that merges it, or
    every key of a state other places alias."""
    if not isinstance(body, CommentedMap):
        return []
    heirs = list(getattr(body, "_ref", None) or [])
    aliased = bool(getattr(getattr(body, "anchor", None), "value", None)) and not heirs
    return [str(key) for key in body
            if aliased or not body._unmerged_contains(key) or any(not heir._unmerged_contains(key) for heir in heirs)]


def _tied(value: Any) -> bool:
    """An anchor (and so an alias of it) or a merge anywhere in ``value``."""
    todo, seen = [value], set()
    while todo:
        node = todo.pop()
        if isinstance(node, (dict, list)):
            if id(node) in seen:
                return True  # reached twice: an alias
            seen.add(id(node))
        anchor = getattr(node, "anchor", None)
        if getattr(anchor, "value", None) or getattr(node, merge_attrib, None):
            return True
        if isinstance(node, dict):
            todo.extend(node.values())
        elif isinstance(node, list):
            todo.extend(node)
    return False


def _mapping(value: Any) -> dict[Any, Any]:
    return value if isinstance(value, dict) else {}


def _text(value: Any) -> Optional[str]:
    return str(value) if isinstance(value, str) else None


def _code(value: Any) -> Optional[str]:
    """A guard or effect as text; a scalar YAML read as another type (``guard: true``) comes back as Python text."""
    if value is None:
        return None
    return value if isinstance(value, str) else str(to_plain(value))
