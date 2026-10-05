"""The checks of a caller's JSON schema, and of an answer against it -- run in a process of their own.

Why a process: the schema is a client's input (openai_api), and neither jsonschema nor regex is built
to be bounded. A pattern backtracks, a ``$ref`` graph fans out exponentially, a counted repeat is
written out by regex's compiler (``((a{120}){120}){120}`` takes half a gigabyte). The API process
never runs them on client input: ``structured_output.SchemaWorkerPool`` starts this file as a
subprocess, gives every request a wall-clock deadline and kills the process when it passes. What is
here is pure and runs the same in a test.

Two layers, one inside the other:

- a strict subset of JSON Schema (``normalize_schema``), as OpenAI's strict mode has one: a keyword
  whitelist, ``$ref`` only to ``#/$defs/<name>`` or ``#``, ``$schema`` only at the root and only
  2020-12, annotations (``default``, ``examples``, ``x-*``) stripped, limits on depth, properties,
  enum values, size and pattern length, no ``$ref`` fan-out or cycle on one value. It removes what
  made the time limit bypassable (a subschema with its own ``$schema`` switches jsonschema to a class
  without the limit; ``unevaluatedProperties`` matches deep inside jsonschema);
- the answer's check (``check_value``) with every pattern matched by ``regex`` under a timeout.

The process boundary bounds whatever the subset still lets through.

Protocol: one JSON object per line on stdin, one per line on stdout.

  {"op": "prepare", "schema": {...}}
      -> {"ok": true, "schema": {...normalized...}} | {"ok": false, "field": "...", "message": "..."}
  {"op": "check", "kind": "json_schema"|"json_object", "schema": {...}|null, "name": "...", "text": "..."}
      -> {"ok": true, "text": "...", "errors": [...], "schema_failed": bool}

An answer carrying ``"retire": true`` is the worker's last: its memory grew past WORKER_MAX_RSS (or
ran out), and the pool starts a fresh one. This module imports nothing from agent_system at import
time: it runs by path, without the package's imports.
"""
from __future__ import annotations

import json
import re
import sys
import time
from contextvars import ContextVar
from typing import Any, Iterator, Optional
from urllib.parse import unquote

# ------------------------------------------------------------------ limits

#: What a schema may be. Like OpenAI's strict mode: ten levels of nesting, 5000 properties, 1000
#: enum values; the characters bound the request, and the schema goes out on every call of a run.
MAX_SCHEMA_CHARS = 100_000
MAX_SCHEMA_DEPTH = 10
MAX_PROPERTIES = 5_000
MAX_ENUM_VALUES = 1_000
MAX_PATTERN_CHARS = 1_000
#: How far a pattern's counted repeats may be written out (``_repeat_units``). regex's compiler
#: unrolls the MINIMUM of a repeat (build_REPEAT in _regex.c, the same from 2022.1.18 on; measured:
#: ``(?:(?:a{120}){120})`` some 14 600 units and 3 MB, one level more 470 MB, ``{0,120}`` nested three
#: deep nothing). Per pattern, and for all patterns of a schema together: compiling costs by the unit.
MAX_PATTERN_UNITS = 20_000
MAX_TOTAL_PATTERN_UNITS = 200_000
#: How many subschemas one value may be checked against through anyOf and ``$ref`` alone
#: (``_fan_out``): a chain of anyOf over two refs to the one before doubles with every link.
MAX_FAN_OUT = 1_000

#: What an answer may be before anything is checked (JSON levels).
MAX_ANSWER_CHARS = 1_000_000
MAX_ANSWER_DEPTH = 64

#: How many validation errors a check reports (and a repair note names).
MAX_REPORTED_ERRORS = 8

#: Seconds one pattern match may take, and all matches of one check together. Running out is a
#: failed match with its own message, never a pass.
PATTERN_TIMEOUT = 0.1
PATTERN_BUDGET = 0.5
#: Seconds one answer's check may take in all; past them it fails closed with its own message,
#: well before the pool's deadline kills the worker. Checked where the work multiplies (anyOf).
CHECK_BUDGET = 2.0
#: How long one validation message may be: jsonschema quotes the whole value it refused.
MAX_ERROR_CHARS = 300

#: A worker whose memory grew past this (the regex cache, a large answer) is retired after its answer.
WORKER_MAX_RSS = 256 * 1024 * 1024
#: A worker ends itself when one request runs longer than this (SIGALRM, POSIX): the pool kills it far
#: earlier, but a pool that is gone -- its process killed -- no longer can.
WORKER_REQUEST_SECONDS = 30
#: An idle worker nobody asks for this long exits (POSIX): the pool of an event loop that is gone.
WORKER_IDLE_SECONDS = 300.0
#: The address-space limit a worker sets itself where the platform enforces one (Linux). macOS takes
#: RLIMIT_AS without enforcing it: there only the pool's deadline bounds a worker's memory.
WORKER_ADDRESS_SPACE = 1024 * 1024 * 1024

EMPTY_ANSWER = "the answer is empty, not JSON"
#: The root ``$schema`` values taken. The schema is checked as 2020-12 whatever it names -- inside the
#: subset the three drafts mean the same, ``definitions`` aside, which becomes ``$defs`` -- and the
#: value goes: zod-to-json-schema (openai-node's zodResponseFormat) writes draft-07.
DRAFTS = frozenset(f"{scheme}://json-schema.org/{draft}/schema{tail}"
                   for scheme in ("http", "https")
                   for draft in ("draft/2020-12", "draft/2019-09", "draft-07")
                   for tail in ("", "#"))
#: How many compiled patterns one check keeps (regex's own cache is not used: 500 entries of large
#: patterns were half a gigabyte).
_PATTERN_CACHE = 16

# ------------------------------------------------------------------ the subset

#: Keywords a schema object may carry, and what their value has to be.
_SCHEMA_MAP = "schema map"
_SCHEMA = "schema"
_SCHEMA_OR_BOOL = "schema or bool"
_SCHEMA_LIST = "schema list"
_ALLOWED: dict[str, str] = {
    "type": "type", "properties": _SCHEMA_MAP, "required": "string list",
    "additionalProperties": _SCHEMA_OR_BOOL, "items": _SCHEMA_OR_BOOL, "prefixItems": _SCHEMA_LIST,
    "enum": "enum", "const": "any", "anyOf": _SCHEMA_LIST, "$defs": _SCHEMA_MAP, "$ref": "ref",
    "description": "string", "title": "string", "pattern": "pattern", "format": "string",
    "minimum": "number", "maximum": "number", "exclusiveMinimum": "number", "exclusiveMaximum": "number",
    "multipleOf": "positive number",
    "minLength": "count", "maxLength": "count", "minItems": "count", "maxItems": "count",
}
#: Annotations: they change nothing about which answer matches, and are taken out before anything
#: else sees the schema -- a subschema hidden in one is no subschema at all.
_STRIPPED = frozenset({"default", "examples", "$comment", "deprecated", "readOnly", "writeOnly"})
_TYPES = frozenset({"object", "array", "string", "number", "integer", "boolean", "null"})
_COUNTED = re.compile(r"\{(\d+)(?:,(\d*))?\}")


class SchemaRefused(ValueError):
    """A schema outside the subset, or over a limit; ``field`` is the request field it concerns."""

    def __init__(self, message: str, field: str = "schema"):
        super().__init__(message)
        self.field = field


def normalize_schema(schema: Any) -> dict:
    """The schema as it is sent and checked: JSON, inside the subset, annotations taken out.

    Raises SchemaRefused naming the keyword and where it stands.
    """
    if not isinstance(schema, dict):
        raise SchemaRefused("the schema must be a JSON object")
    try:
        encoded = json.dumps(schema, allow_nan=False)
    except (TypeError, ValueError, RecursionError) as error:
        raise SchemaRefused(f"the schema is not JSON: {error}") from None
    if len(encoded) > MAX_SCHEMA_CHARS:
        raise SchemaRefused(f"the schema is {len(encoded)} characters as JSON; at most {MAX_SCHEMA_CHARS} are taken")
    root = json.loads(encoded)
    declared = root.pop("$schema", None)
    if declared is not None and declared not in DRAFTS:
        raise SchemaRefused(f"$schema {declared!r}: only JSON Schema 2020-12, 2019-09 and draft-07 are supported")
    renamed = "definitions" in root
    if renamed:
        # draft-07's name for $defs (zod-to-json-schema): taken as $defs, its references rewritten below.
        if "$defs" in root:
            raise SchemaRefused("definitions and $defs together: name the definitions once")
        root["$defs"] = root.pop("definitions")

    nodes: dict[int, dict] = {}
    counts = {"properties": 0, "enum": 0, "units": 0}
    stack: list[tuple[dict, str, int]] = [(root, "#", 0)]
    while stack:
        node, where, depth = stack.pop()
        if depth > MAX_SCHEMA_DEPTH:
            raise SchemaRefused(f"the schema nests deeper than {MAX_SCHEMA_DEPTH} levels (at {where})")
        nodes[id(node)] = node
        for key in [key for key in node if key in _STRIPPED or key.startswith("x-")]:
            del node[key]
        ref = node.get("$ref")
        if renamed and isinstance(ref, str) and ref.startswith("#/definitions/"):
            node["$ref"] = "#/$defs/" + ref[len("#/definitions/"):]
        for key, value in node.items():
            kind = _ALLOWED.get(key)
            if kind is None:
                if key == "$schema":
                    raise SchemaRefused(f"$schema at {where}: only the root may name its draft")
                raise SchemaRefused(f"keyword {key!r} at {where} is not supported")
            if key == "$defs" and node is not root:
                raise SchemaRefused(f"$defs at {where}: definitions belong at the root")
            stack.extend(_children(key, kind, value, where, depth, counts))
    if counts["properties"] > MAX_PROPERTIES:
        raise SchemaRefused(f"the schema has {counts['properties']} properties; at most {MAX_PROPERTIES}")
    if counts["enum"] > MAX_ENUM_VALUES:
        raise SchemaRefused(f"the schema has {counts['enum']} enum and const values; at most {MAX_ENUM_VALUES}")
    if counts["units"] > MAX_TOTAL_PATTERN_UNITS:
        raise SchemaRefused(f"the schema's patterns together repeat too much: {counts['units']} units written out, "
                            f"at most {MAX_TOTAL_PATTERN_UNITS}")
    _check_references(root, nodes)
    _check_fan_out(root, nodes)
    _check_meta_schema(root)
    return root


def _children(key: str, kind: str, value: Any, where: str, depth: int,
              counts: dict[str, int]) -> Iterator[tuple[dict, str, int]]:
    """The subschemas under one keyword, after its value's kind is checked."""
    at = f"{where}/{key}"
    if kind == _SCHEMA_MAP:
        if not isinstance(value, dict):
            raise SchemaRefused(f"{at} must be an object of schemas")
        if key == "properties":
            counts["properties"] += len(value)
        for name, child in value.items():
            if not isinstance(child, dict):
                raise SchemaRefused(f"{at}/{name} must be a schema object")
            yield child, f"{at}/{name}", depth + 1
    elif kind in (_SCHEMA, _SCHEMA_OR_BOOL):
        if isinstance(value, bool) and kind == _SCHEMA_OR_BOOL:
            return
        if not isinstance(value, dict):
            raise SchemaRefused(f"{at} must be a schema object" + (" or a boolean" if kind == _SCHEMA_OR_BOOL else ""))
        yield value, at, depth + 1
    elif kind == _SCHEMA_LIST:
        if not isinstance(value, list) or not value:
            raise SchemaRefused(f"{at} must be a non-empty list of schemas")
        for index, child in enumerate(value):
            if not isinstance(child, dict):
                raise SchemaRefused(f"{at}/{index} must be a schema object")
            yield child, f"{at}/{index}", depth + 1
    elif kind == "type":
        names = value if isinstance(value, list) else [value]
        if not names or not all(isinstance(name, str) and name in _TYPES for name in names) \
                or len(set(names)) != len(names):
            raise SchemaRefused(f"{at} must be one of {sorted(_TYPES)} or a list of them")
    elif kind == "string list":
        if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
            raise SchemaRefused(f"{at} must be a list of strings")
    elif kind == "enum":
        if not isinstance(value, list) or not value:
            raise SchemaRefused(f"{at} must be a non-empty list")
        counts["enum"] += len(value)
    elif kind == "any":
        counts["enum"] += 1
    elif kind == "string":
        if not isinstance(value, str):
            raise SchemaRefused(f"{at} must be a string")
    elif kind in ("number", "positive number"):
        if isinstance(value, bool) or not isinstance(value, (int, float)) or (kind == "positive number" and value <= 0):
            raise SchemaRefused(f"{at} must be a {'positive ' if kind == 'positive number' else ''}number")
    elif kind == "count":
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise SchemaRefused(f"{at} must be a non-negative integer")
    elif kind == "ref":
        if not isinstance(value, str):
            raise SchemaRefused(f"{at} must be a string")
    elif kind == "pattern":
        counts["units"] += _check_pattern(value, at)


def _check_pattern(pattern: Any, at: str) -> int:
    """Refuses what the subset does not take; returns the pattern's repeat units."""
    import regex

    if not isinstance(pattern, str):
        raise SchemaRefused(f"{at} must be a string")
    if len(pattern) > MAX_PATTERN_CHARS:
        raise SchemaRefused(f"{at} is {len(pattern)} characters; at most {MAX_PATTERN_CHARS}")
    unsupported = _unsupported_group(pattern)
    if unsupported:
        raise SchemaRefused(f"{at}: the group {unsupported!r} is not supported (JSON Schema patterns are "
                            "ECMA-262: no inline flags, comments or recursion)")
    units = _repeat_units(pattern)
    if units > MAX_PATTERN_UNITS:
        raise SchemaRefused(f"{at} repeats too much: its counted repeats written out are {units} units, at most "
                            f"{MAX_PATTERN_UNITS} (for a length, use minLength/maxLength)")
    try:
        regex.compile(pattern, cache_pattern=False)
    except (regex.error, OverflowError, RecursionError, ValueError) as error:
        raise SchemaRefused(f"{at} is not a regular expression: {error}") from None
    return units


#: The group openings allowed: non-capturing, lookarounds, and named groups as Python writes them (the
#: meta-schema's "regex" check compiles with Python's re, which refuses ECMA's ``(?<name>``). Everything
#: else after ``(?`` -- inline flags (a verbose mode turns ``#`` into comments), comments, recursion,
#: atomic groups -- is refused: it would also change what _repeat_units reads.
_GROUP_OPENINGS = ("(?:", "(?=", "(?!", "(?<=", "(?<!", "(?P<")


def _unsupported_group(pattern: str) -> Optional[str]:
    """The first ``(?...`` opening outside the ECMA-262 set, or None. Escapes and classes are skipped
    exactly as _repeat_units skips them, so both read the same pattern."""
    i = 0
    while i < len(pattern):
        char = pattern[i]
        if char == "\\":
            i += 2
            continue
        if char == "[":
            i = _class_end(pattern, i)
            continue
        if char == "(" and pattern.startswith("(?", i):
            if not any(pattern.startswith(opening, i) for opening in _GROUP_OPENINGS):
                return pattern[i:i + 4]
        i += 1
    return None


def _class_end(pattern: str, start: int) -> int:
    """The index after the character class opening at ``start``."""
    j = start + 1
    if j < len(pattern) and pattern[j] == "^":
        j += 1
    if j < len(pattern) and pattern[j] == "]":
        j += 1
    while j < len(pattern) and pattern[j] != "]":
        j += 2 if pattern[j] == "\\" else 1
    return j + 1


def _repeat_units(pattern: str) -> int:
    """How large a pattern gets with its counted repeats written out -- what regex's compiler builds.

    Groups multiply by the MINIMUM of their repeat -- what regex unrolls (see MAX_PATTERN_UNITS);
    ``{0,30000}`` costs nothing, ``{120,}`` as much as ``{120}``. Sequences and alternatives add up (an
    overestimate is fine: the point is the product of nested minimums). Escapes and character classes
    count one each.
    """
    frames = [0]
    last = 0
    i = 0
    while i < len(pattern):
        char = pattern[i]
        if char == "\\":
            frames[-1] += 1
            last = 1
            i += 2
            continue
        if char == "[":
            frames[-1] += 1
            last = 1
            i = _class_end(pattern, i)
            continue
        if char == "(":
            frames.append(0)
            last = 0
            i += 1
            continue
        if char == ")":
            size = frames.pop() if len(frames) > 1 else 0
            frames[-1] += size
            last = size
            i += 1
            continue
        if char == "{":
            counted = _COUNTED.match(pattern, i)
            if counted:
                bound = int(counted.group(1))  # the minimum: regex unrolls that, not the rest
                frames[-1] += last * max(bound - 1, 0)
                i = counted.end()
                continue
        if char not in "*+?|":
            frames[-1] += 1
            last = 1
        i += 1
    return sum(frames)


def _ref_target(root: dict, ref: str) -> Optional[dict]:
    """``#`` or ``#/$defs/<name>`` (read as ``referencing`` reads a pointer), else None."""
    if ref == "#":
        return root
    if not ref.startswith("#/"):
        return None
    segments = unquote(ref[2:]).split("/")
    if len(segments) != 2 or segments[0] != "$defs":
        return None
    name = segments[1].replace("~1", "/").replace("~0", "~")
    target = root.get("$defs", {}).get(name)
    return target if isinstance(target, dict) else None


def _check_references(root: dict, nodes: dict[int, dict]) -> None:
    for node in nodes.values():
        ref = node.get("$ref")
        if ref is None:
            continue
        target = _ref_target(root, ref)
        # By identity: the target must be a schema this walk collected, not a value that looks like one.
        if target is None or id(target) not in nodes:
            raise SchemaRefused(f"$ref {ref!r}: only '#' and '#/$defs/<name>' of an existing definition are supported")


def _same_value_children(root: dict, node: dict) -> list[dict]:
    """The subschemas checked against the SAME value as ``node``: its anyOf branches and its $ref."""
    children = [child for child in node.get("anyOf", ()) if isinstance(child, dict)]
    if isinstance(node.get("$ref"), str):
        target = _ref_target(root, node["$ref"])
        if target is not None:
            children.append(target)
    return children


def _check_fan_out(root: dict, nodes: dict[int, dict]) -> None:
    """No cycle and no exponential fan-out on one value.

    A ``$ref`` cycle through anyOf and ``$ref`` alone never reaches a smaller part of the answer --
    jsonschema recurses until it fails. And a chain whose links each check the one before twice costs
    2^n on every value. Recursion that descends (``properties``, ``items``) is fine: the answer ends.
    """
    fan_out: dict[int, int] = {}
    on_path: set[int] = set()
    for start in nodes.values():
        stack: list[tuple[dict, int]] = [(start, 0)]
        while stack:
            node, state = stack.pop()
            key = id(node)
            if key in fan_out:
                continue
            children = _same_value_children(root, node)
            if state == 0:
                if key in on_path:
                    raise SchemaRefused("a $ref cycle that never descends into the answer (through anyOf/$ref only)")
                on_path.add(key)
                stack.append((node, 1))
                stack.extend((child, 0) for child in children if id(child) not in fan_out)
                continue
            on_path.discard(key)
            total = 1 + sum(fan_out.get(id(child), 1) for child in children)
            if total > MAX_FAN_OUT:
                raise SchemaRefused(f"one value would be checked against {total} subschemas through anyOf and "
                                    f"$ref; at most {MAX_FAN_OUT}")
            fan_out[key] = total


def _check_meta_schema(schema: dict) -> None:
    """The subset's own checks leave the JSON Schema meta-schema little to find; it runs anyway."""
    from jsonschema import Draft202012Validator
    from jsonschema.exceptions import SchemaError

    try:
        # No format checker: its "regex" format compiles with Python's re, which does not know what regex
        # does (\p{L}); _check_pattern compiled every pattern with regex already.
        Draft202012Validator.check_schema(schema, format_checker=None)
    except SchemaError as error:
        where = "/".join(str(part) for part in error.path) or "<root>"
        raise SchemaRefused(f"the schema is not a valid JSON schema: {where}: {error.message}") from None
    except (ValueError, RecursionError, OverflowError) as error:
        raise SchemaRefused(f"the schema cannot be checked: {type(error).__name__}: {error}") from None


# ------------------------------------------------------------------ the answer

def strip_whole_fence(answer: str) -> str:
    """A Markdown fence around the WHOLE answer taken off, nothing else (prose around a JSON block is no
    answer in the format). String operations only."""
    if not (len(answer) >= 6 and answer.startswith("```") and answer.endswith("```")):
        return answer
    newline = answer.find("\n")
    if newline < 0:
        return answer
    tag = answer[3:newline].rstrip("\r").rstrip(" \t")
    if tag and not all(char.isascii() and (char.isalnum() or char in "_-") for char in tag):
        return answer
    inner = answer[newline + 1:-3]
    if inner.endswith("\n"):
        inner = inner[:-1]
    if inner.endswith("\r"):
        inner = inner[:-1]
    return inner.strip()


def _not_json(constant: str) -> Any:
    """Python's json reads NaN and Infinity; RFC 8259 has neither, and a client's parser refuses them."""
    raise ValueError(f"{constant} is not a JSON value")


def json_depth(value: Any) -> int:
    """How many levels of objects and arrays ``value`` nests (iteratively: the point is a value too deep
    for recursion)."""
    deepest, stack = 0, [(value, 1)]
    while stack:
        node, depth = stack.pop()
        if isinstance(node, dict):
            children = node.values()
        elif isinstance(node, list):
            children = node
        else:
            continue
        deepest = max(deepest, depth)
        stack.extend((child, depth + 1) for child in children)
    return deepest


def parse_answer(text: Optional[str]) -> tuple[str, Any, list[str]]:
    """(answer as delivered, parsed value, errors) -- the part of a check that needs no schema."""
    answer = strip_whole_fence((text or "").strip())
    if not answer:
        return answer, None, [EMPTY_ANSWER]
    if len(answer) > MAX_ANSWER_CHARS:
        return answer, None, [f"the answer is {len(answer)} characters; at most {MAX_ANSWER_CHARS} are checked"]
    try:
        value = json.loads(answer, parse_constant=_not_json)
    except (ValueError, RecursionError) as error:
        return answer, None, [f"the answer is not valid JSON: {error}"]
    if json_depth(value) > MAX_ANSWER_DEPTH:
        return answer, value, [f"the answer nests deeper than {MAX_ANSWER_DEPTH} JSON levels"]
    return answer, value, []


def check_value(text: Optional[str], kind: str, schema: Optional[dict], name: str = "response") -> dict:
    """``text`` against the format: JSON at all, an object for json_object, the (normalized) schema's
    rules. The answer of a "check" request: text, errors, schema_failed (the schema itself could not be
    applied -- no answer can match, asking again is pointless)."""
    answer, value, errors = parse_answer(text)
    result: dict[str, Any] = {"text": answer, "errors": errors, "schema_failed": False, "value": value}
    if errors:
        return result
    if kind == "json_object":
        if not isinstance(value, dict):
            result["errors"] = [f"the answer is JSON, but a {type(value).__name__}, not an object"]
        return result
    validator = _validator(schema or {})
    started = time.monotonic()
    budget = _pattern_deadline.set(started + PATTERN_BUDGET)
    check = _check_state.set(_CheckState(deadline=started + CHECK_BUDGET))
    try:
        found = list(validator.iter_errors(value))
    except _CheckTooCostly:
        result.update(schema_failed=True, errors=[f"the answer could not be checked against this schema within "
                                                  f"{CHECK_BUDGET} s"])
        return result
    except Exception as error:  # noqa: BLE001 -- the caller's schema, applied: its failure is the format's
        result.update(schema_failed=True,
                      errors=[_short(f"the schema cannot be applied: {type(error).__name__}: {error}")])
        return result
    finally:
        _pattern_deadline.reset(budget)
        _check_state.reset(check)
    found.sort(key=lambda e: (list(e.absolute_path), e.message))
    for error in found[:MAX_REPORTED_ERRORS]:
        where = "/".join(str(part) for part in error.absolute_path) or "<root>"
        errors.append(_short(f"{where}: {error.message}"))
    if len(found) > MAX_REPORTED_ERRORS:
        errors.append(f"... and {len(found) - MAX_REPORTED_ERRORS} more")
    return result


def _validator(schema: dict) -> Any:
    """A 2020-12 validator that resolves references inside the schema and NOWHERE else, and matches its
    patterns with a time limit.

    jsonschema's default registry fetches a reference it does not know (``http://``, ``file://``, with
    urlopen and no timeout); an empty one turns such a reference into Unresolvable. The subset allows
    no ``$schema`` below the root, so no subschema switches jsonschema to a class without the limit.
    """
    from jsonschema import Draft202012Validator
    from jsonschema.validators import extend
    from referencing import Registry

    global _TIMED_CLASS
    if _TIMED_CLASS is None:
        _TIMED_CLASS = extend(Draft202012Validator, validators=_TIMED)
    return _TIMED_CLASS(schema, registry=Registry())


#: The deadline of the check running in this context (check_value); None outside one.
_pattern_deadline: ContextVar[Optional[float]] = ContextVar("schema_worker_pattern_deadline", default=None)


class _CheckState:
    """One check's deadline, what its anyOf branches already said about which value, and its compiled
    patterns (a few, most recent last)."""

    def __init__(self, deadline: float):
        self.deadline = deadline
        self.verdicts: dict[tuple[int, int], bool] = {}
        self.patterns: dict[str, Any] = {}


_check_state: ContextVar[Optional[_CheckState]] = ContextVar("schema_worker_check_state", default=None)


class _CheckTooCostly(Exception):
    pass


def _short(line: str) -> str:
    return line if len(line) <= MAX_ERROR_CHARS else line[:MAX_ERROR_CHARS - 3] + "..."


def _any_of(validator: Any, branches: list, instance: Any, schema: dict) -> Iterator[Any]:
    """jsonschema's anyOf, bounded.

    Stock anyOf checks every failing branch to its end and keeps all its errors: a recursive union
    (``child: A | B`` in A and in B, as pydantic writes one) costs 2^depth, for a valid answer too when
    the discriminating field comes after the recursive one. Here each (branch, value) is decided once
    -- sound inside the subset, where a reference means the same wherever it stands -- a branch stops
    at its first error, and the check's time is looked at before every new decision.
    """
    from jsonschema.exceptions import ValidationError

    state = _check_state.get()
    for index, branch in enumerate(branches):
        key = (id(branch), id(instance))
        verdict = state.verdicts.get(key) if state is not None else None
        if verdict is None:
            # >=: Windows' clock moves in ~15 ms steps, and a budget of 0 must still be spent.
            if state is not None and time.monotonic() >= state.deadline:
                raise _CheckTooCostly()
            verdict = next(iter(validator.descend(instance, branch, schema_path=index)), None) is None
            if state is not None:
                state.verdicts[key] = verdict
        if verdict:
            return
    shown = repr(instance)
    yield ValidationError(f"{_short(shown) if len(shown) > 80 else shown} is not valid under any of the given schemas")


class _PatternTimeout(Exception):
    pass


def _search(pattern: str, text: str) -> bool:
    """``regex.search`` within PATTERN_TIMEOUT and what is left of the check's PATTERN_BUDGET. Raises
    _PatternTimeout when time runs out."""
    import regex

    timeout = PATTERN_TIMEOUT
    deadline = _pattern_deadline.get()
    if deadline is not None:
        timeout = min(timeout, deadline - time.monotonic())
        if timeout <= 0:
            raise _PatternTimeout()
    state = _check_state.get()
    compiled = state.patterns.pop(pattern, None) if state is not None else None
    if compiled is None:
        compiled = regex.compile(pattern, cache_pattern=False)
    if state is not None:
        state.patterns[pattern] = compiled
        while len(state.patterns) > _PATTERN_CACHE:
            state.patterns.pop(next(iter(state.patterns)))
    try:
        return compiled.search(text, timeout=timeout) is not None
    except TimeoutError:
        raise _PatternTimeout() from None


def _timed_out(pattern: str, text: str) -> Any:
    from jsonschema.exceptions import ValidationError

    shown = repr(text) if len(text) <= 60 else repr(text[:60]) + "..."
    return ValidationError(f"{shown} could not be matched against the pattern {pattern!r} in the time "
                           f"allowed ({PATTERN_TIMEOUT} s per match, {PATTERN_BUDGET} s per answer) -- "
                           "counted as a mismatch")


def _timed_pattern(validator: Any, pattern: str, instance: Any, schema: dict) -> Iterator[Any]:
    from jsonschema.exceptions import ValidationError

    if not validator.is_type(instance, "string"):
        return
    try:
        matched = _search(pattern, instance)
    except _PatternTimeout:
        yield _timed_out(pattern, instance)
        return
    if not matched:
        yield ValidationError(f"{instance!r} does not match {pattern!r}")


def _multiple_of(validator: Any, divisor: Any, instance: Any, schema: dict) -> Iterator[Any]:
    """jsonschema's multipleOf decides a float by ``int(x / d) == x / d``: 0.07 is not a multiple of 0.01
    there, nor 2.3 of 0.1. Here both are the decimals they are written as (a JSON number's shortest repr)
    and are divided exactly."""
    from fractions import Fraction

    from jsonschema.exceptions import ValidationError

    if not validator.is_type(instance, "number"):
        return
    if Fraction(str(instance)) % Fraction(str(divisor)) != 0:
        yield ValidationError(f"{instance!r} is not a multiple of {divisor}")


#: The keyword jsonschema matches regular expressions in that the subset allows, with a time limit.
#: (patternProperties and unevaluatedProperties are outside the subset; the drift test pins the places.)
_TIMED = {"pattern": _timed_pattern, "anyOf": _any_of, "multipleOf": _multiple_of}
_TIMED_CLASS: Any = None


# ------------------------------------------------------------------ the process

def handle(request: dict) -> dict:
    """One request -> its answer. Never raises for a request's content."""
    op = request.get("op")
    if op == "prepare":
        try:
            return {"ok": True, "schema": normalize_schema(request.get("schema"))}
        except SchemaRefused as refused:
            return {"ok": False, "field": refused.field, "message": str(refused)}
    if op == "check":
        kind, schema = request.get("kind", "json_schema"), request.get("schema")
        if kind != "json_object":
            try:
                # Again, whoever sent it: the time limit holds only inside the subset.
                schema = normalize_schema(schema)
            except SchemaRefused as refused:
                return {"ok": True, "schema_failed": True, "errors": [f"the schema cannot be applied: {refused}"]}
        result = check_value(request.get("text"), kind, schema, request.get("name") or "response")
        # The caller has the text and parses it itself: only the verdict goes back, and stays small.
        return {"ok": True, "errors": result["errors"], "schema_failed": result["schema_failed"]}
    return {"ok": False, "field": "op", "message": f"unknown op {op!r}"}


def _rss() -> int:
    try:
        import psutil

        return psutil.Process().memory_info().rss
    except Exception:  # noqa: BLE001 -- without psutil the worker is only recycled by the pool's deadline
        return 0


def _limit_memory() -> None:
    if not sys.platform.startswith("linux"):
        return  # macOS/Windows: no enforced RLIMIT_AS -- the pool's deadline bounds the worker
    try:
        import resource

        resource.setrlimit(resource.RLIMIT_AS, (WORKER_ADDRESS_SPACE, WORKER_ADDRESS_SPACE))
    except (ImportError, ValueError, OSError):
        pass


def _next_line(idle_seconds: float) -> str:
    """The next request line; "" at EOF or after ``idle_seconds`` without one."""
    if idle_seconds > 0 and sys.platform == "win32":
        # select() takes sockets only there, so the line is read by a thread. Without an idle end a
        # worker stayed for as long as its pool. One whose time ran out leaves with os._exit: the
        # reader still blocks in readline, and every reply is flushed already.
        import os
        import threading

        line: list[str] = []
        reader = threading.Thread(target=lambda: line.append(sys.stdin.readline()), daemon=True)
        reader.start()
        reader.join(idle_seconds)
        if not line:
            os._exit(0)
        return line[0]
    if idle_seconds > 0:
        import select

        ready, _, _ = select.select([sys.stdin], [], [], idle_seconds)
        if not ready:
            return ""
    return sys.stdin.readline()


def _alarm(seconds: int) -> None:
    """SIGALRM's default action ends the process: a request past WORKER_REQUEST_SECONDS, when no pool is
    left to kill the worker. POSIX only."""
    try:
        import signal

        signal.alarm(seconds)
    except (ImportError, AttributeError, ValueError):
        pass


def main() -> None:
    _limit_memory()
    try:
        idle = float(sys.argv[1])
    except (IndexError, ValueError):
        idle = WORKER_IDLE_SECONDS
    while True:
        line = _next_line(idle)
        if not line:
            return
        _alarm(WORKER_REQUEST_SECONDS)
        try:
            reply = handle(json.loads(line))
        except MemoryError:
            reply = {"ok": False, "field": "schema", "message": "the check ran out of memory", "retire": True}
        except Exception as error:  # noqa: BLE001 -- an answer for every request; the pool reads it as a failure
            reply = {"ok": False, "field": "worker", "message": f"{type(error).__name__}: {error}"}
        try:
            import regex

            regex.purge()  # a compiled pattern stays in regex's cache: not in this process's
        except Exception:  # noqa: BLE001
            pass
        _alarm(0)
        if _rss() > WORKER_MAX_RSS:
            reply["retire"] = True
        sys.stdout.write(json.dumps(reply) + "\n")
        sys.stdout.flush()
        if reply.get("retire"):
            return


if __name__ == "__main__":
    main()
