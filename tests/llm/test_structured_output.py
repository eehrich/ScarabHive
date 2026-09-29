"""Structured output's core: the request, who may send it, the schema's subset, the answer's check.

Three layers are tested separately. What ``ResponseFormat`` checks when it is built (cheap, in this
process). The worker's rules -- the strict subset and the answer's check -- as the pure functions of
``schema_worker``, run here directly: a test process is no API process. And the process boundary
itself, through ``SchemaWorkerPool``: a check that does not end in time is killed, and the next one
gets a fresh worker.
"""
from __future__ import annotations

import asyncio
import json
import time
from unittest.mock import AsyncMock, MagicMock

import pytest

from agent_system.config.models import ModelCapabilitiesConfig
from agent_system.llm import schema_worker, structured_output
from agent_system.llm.models import LLMClient
from agent_system.llm.schema_worker import SchemaRefused, check_value, normalize_schema
from agent_system.llm.structured_output import (
    JSON_OBJECT, JSON_SCHEMA, InvalidResponseFormat, ResponseFormat, StructuredOutputUnsupported,
    check_answer, close_schema_workers, declared_support, instruction_text, prepare_response_format,
    require_response_format, supports_response_format, worker_pool,
)

SCHEMA = {"type": "object", "properties": {"city": {"type": "string"}, "days": {"type": "integer"}},
          "required": ["city", "days"], "additionalProperties": False}
#: Catastrophic for Python's re (hours) and for regex too: every match runs out of time.
CATASTROPHIC = "^(a|a)*$"
EVIL_TEXT = "a" * 40 + "b"


@pytest.fixture(autouse=True)
async def _workers():
    yield
    await close_schema_workers()


def _check(text, schema, kind=JSON_SCHEMA):
    return check_value(text, kind, normalize_schema(schema) if schema is not None else None)


class _Route(LLMClient):
    """A client whose route carries json_schema only (as Anthropic's does)."""

    response_format_kinds = (JSON_SCHEMA,)

    def __init__(self, capabilities=None):
        self.model = "m-1"
        self.capabilities = capabilities


# ------------------------------------------------------------------ building a format (in this process)

def test_a_format_checks_what_is_cheap_when_it_is_built():
    with pytest.raises(InvalidResponseFormat, match="needs its schema"):
        ResponseFormat(type=JSON_SCHEMA)
    with pytest.raises(InvalidResponseFormat, match="takes no schema"):
        ResponseFormat(type=JSON_OBJECT, schema=SCHEMA)
    with pytest.raises(InvalidResponseFormat, match="type must be one of"):
        ResponseFormat(type="text")
    for name in ("has spaces", "x" * 65, "", "é"):
        with pytest.raises(InvalidResponseFormat, match="name") as refused:
            ResponseFormat(schema=SCHEMA, name=name)
        assert refused.value.field == "name"
    with pytest.raises(InvalidResponseFormat, match="not JSON"):
        ResponseFormat(schema={"type": "number", "maximum": float("nan")})
    with pytest.raises(InvalidResponseFormat, match="characters as JSON"):
        ResponseFormat(schema={"type": "object", "description": "x" * schema_worker.MAX_SCHEMA_CHARS})


def test_the_format_keeps_its_own_copy_of_the_schema():
    schema = {"type": "object", "properties": {"a": {"type": "string"}}}
    fmt = ResponseFormat(schema=schema)
    schema["properties"]["a"]["type"] = "integer"
    assert fmt.schema["properties"]["a"]["type"] == "string"


# ------------------------------------------------------------------ who may send it

def test_route_and_model_entry_both_have_to_say_yes():
    fmt = ResponseFormat(schema=SCHEMA)
    assert supports_response_format(_Route(ModelCapabilitiesConfig(structured_output=True)), fmt)
    assert not supports_response_format(_Route(ModelCapabilitiesConfig()), fmt), "the model entry did not declare it"
    assert not supports_response_format(_Route(None), fmt)
    # The route has no field for JSON mode, whatever the model entry says.
    assert not supports_response_format(_Route(ModelCapabilitiesConfig(structured_output=True)),
                                        ResponseFormat(type=JSON_OBJECT))
    plain = LLMClient()
    plain.capabilities = ModelCapabilitiesConfig(structured_output=True)
    assert not supports_response_format(plain, fmt), "a client that wired nothing was handed a format"


def test_json_mode_in_the_catalogue_unlocks_nothing():
    """The catalogue's json_mode values were never verified: native JSON mode needs structured_output."""
    assert not declared_support({"json_mode": True}, JSON_OBJECT)
    assert declared_support({"structured_output": True}, JSON_OBJECT)
    assert not declared_support(ModelCapabilitiesConfig(json_mode=True), JSON_OBJECT)
    assert declared_support({"structured_output": True}, JSON_SCHEMA)
    assert not declared_support({"structured_output": "yes"}, JSON_SCHEMA)


def test_a_stand_in_that_cannot_say_is_a_client_without_the_field():
    fmt = ResponseFormat(schema=SCHEMA)
    assert not supports_response_format(object(), fmt)
    assert not supports_response_format(MagicMock(), fmt)
    assert not supports_response_format(AsyncMock(), fmt)  # its coroutine is closed, not left to warn


def test_the_refusal_is_typed_and_says_what_is_missing():
    fmt = ResponseFormat(schema=SCHEMA)
    with pytest.raises(StructuredOutputUnsupported, match="capabilities.structured_output: true") as refused:
        require_response_format(_Route(ModelCapabilitiesConfig()), fmt)
    assert refused.value.model == "m-1" and refused.value.kind == JSON_SCHEMA
    with pytest.raises(StructuredOutputUnsupported, match="no wire field for json_object"):
        require_response_format(_Route(ModelCapabilitiesConfig(structured_output=True)),
                                ResponseFormat(type=JSON_OBJECT))
    require_response_format(_Route(ModelCapabilitiesConfig(structured_output=True)), fmt)
    require_response_format(_Route(), None)


# ------------------------------------------------------------------ the subset (the worker's rules, run here)

@pytest.mark.parametrize("schema, named", [
    # The review's bypasses of the pattern time limit, and its cost attacks, each refused by name.
    ({"default": {"$schema": "https://json-schema.org/draft/2020-12/schema", "pattern": CATASTROPHIC},
      "$ref": "#/default"}, "'#/default'"),
    ({"examples": [{"pattern": CATASTROPHIC}], "$ref": "#/examples/0"}, "'#/examples/0'"),
    ({"x-hidden": {"pattern": CATASTROPHIC}, "$ref": "#/x-hidden"}, "'#/x-hidden'"),
    ({"const": {"pattern": CATASTROPHIC}, "$ref": "#/const"}, "'#/const'"),
    ({"$schema": "http://json-schema.org/draft-03/schema#", "extends": {"pattern": CATASTROPHIC}}, "draft-03"),
    ({"type": [{"pattern": CATASTROPHIC}]}, "#/type"),
    ({"$defs": {"d": {"patternProperties": {"^a": {}}, "unevaluatedProperties": False}}, "$ref": "#/$defs/d"},
     "'patternProperties'"),
    ({"type": "object", "properties": {"a": {"$schema": "https://json-schema.org/draft/2020-12/schema"}}},
     "only the root may name its draft"),
    ({"allOf": [{"type": "string"}]}, "'allOf'"),
    ({"oneOf": [{"type": "string"}]}, "'oneOf'"),
    ({"type": "object", "$dynamicAnchor": "meta"}, "'$dynamicAnchor'"),
    ({"$id": "urn:x", "type": "object"}, "'$id'"),
    ({"$ref": "https://json-schema.org/draft/2020-12/schema"}, "only '#' and '#/$defs/<name>'"),
    ({"$ref": "#/$defs/missing"}, "only '#' and '#/$defs/<name>'"),
    ({"type": "object", "properties": {"a": {"$defs": {"b": {}}}}}, "definitions belong at the root"),
    ({"type": "string", "pattern": "^(?:(?:(?:a{120}){120}){120})$"}, "repeats too much"),
    ({"type": "string", "pattern": "x" * (schema_worker.MAX_PATTERN_CHARS + 1)}, "at most 1000"),
    ({"type": "string", "pattern": "("}, "not a regular expression"),
    # A comment or a verbose mode hid the counted repeats from the count (the review's bypass).
    ({"type": "string", "pattern": "(?#[)((a{40}){40}){40}(?#])"}, "'(?#['"),
    ({"type": "string", "pattern": "(?x)#[\n((a{40}){40}){40}\n#]"}, "'(?x)'"),
    ({"type": "string", "pattern": "(a|b(?R))"}, "'(?R)'"),
    ({"type": "string", "pattern": "(?>a+)b"}, "'(?>a'"),
    ({"type": "string", "pattern": "(?i)abc"}, "'(?i)'"),
    ({"type": "nope"}, "#/type must be one of"),
    ({"type": "array", "minItems": -1}, "non-negative integer"),
    ({"enum": list(range(schema_worker.MAX_ENUM_VALUES + 1))}, "enum and const values"),
    ({"type": "object", "properties": {f"p{i}": {} for i in range(schema_worker.MAX_PROPERTIES + 1)}},
     "properties; at most"),
])
def test_what_is_outside_the_subset_is_refused_by_name(schema, named):
    with pytest.raises(SchemaRefused) as refused:
        normalize_schema(schema)
    assert named in str(refused.value), str(refused.value)


def _chain(n: int, keyword: str = "anyOf") -> dict:
    defs: dict = {"a0": {"type": "string"}}
    for i in range(1, n + 1):
        defs[f"a{i}"] = {keyword: [{"$ref": f"#/$defs/a{i - 1}"}, {"$ref": f"#/$defs/a{i - 1}"}]}
    return {"$defs": defs, "$ref": f"#/$defs/a{n}"}


@pytest.mark.timeout(5, method="signal")  # a cycle the walk does not notice never ends
def test_fan_out_and_cycles_on_one_value_are_refused_recursion_that_descends_is_not():
    started = time.monotonic()
    with pytest.raises(SchemaRefused, match="subschemas through anyOf and \\$ref"):
        normalize_schema(_chain(30))
    assert time.monotonic() - started < 1.0, "the bound was computed by walking 2^30 paths"
    normalize_schema(_chain(7))  # 510 on one value (each link: itself, two branches, their refs): within the bound
    with pytest.raises(SchemaRefused, match="1021 subschemas"):
        normalize_schema(_chain(8))
    with pytest.raises(SchemaRefused, match="cycle"):
        normalize_schema({"anyOf": [{"$ref": "#"}, {"type": "string"}]})
    with pytest.raises(SchemaRefused, match="cycle"):
        normalize_schema({"$defs": {"a": {"$ref": "#/$defs/b"}, "b": {"anyOf": [{"$ref": "#/$defs/a"}]}},
                          "$ref": "#/$defs/a"})
    tree = normalize_schema({"type": "object", "properties": {
        "children": {"type": "array", "items": {"$ref": "#"}}}})
    assert check_value('{"children": [{"children": []}]}', JSON_SCHEMA, tree)["errors"] == []


def test_the_nesting_limit_counts_schema_levels():
    deep: dict = {"type": "object"}
    node = deep
    for _ in range(schema_worker.MAX_SCHEMA_DEPTH):
        node["properties"] = {"x": {"type": "object"}}
        node = node["properties"]["x"]
    normalize_schema(deep)  # at the limit
    node["properties"] = {"x": {"type": "object"}}
    with pytest.raises(SchemaRefused, match="nests deeper"):
        normalize_schema(deep)


def test_what_clients_send_passes_and_annotations_are_taken_out():
    """pydantic v2 and the openai SDK's strict schemas are inside the subset; default, examples and x-*
    are stripped before anything sees them, and the root's $schema (2020-12) too."""
    from openai.lib._pydantic import to_strict_json_schema
    from pydantic import BaseModel, Field

    class Leg(BaseModel):
        to: str = Field(pattern="^[A-Z][a-z]+$", examples=["Oslo"])
        days: int = 2

    class Trip(BaseModel):
        legs: list[Leg]
        note: str | None = None

    for schema in (Trip.model_json_schema(), to_strict_json_schema(Trip)):
        normalized = normalize_schema({"$schema": "https://json-schema.org/draft/2020-12/schema", **schema})
        text = json.dumps(normalized)
        assert "$schema" not in normalized and '"default"' not in text and '"examples"' not in text
        good = '{"legs": [{"to": "Oslo", "days": 1}], "note": null}'
        assert check_value(good, JSON_SCHEMA, normalized)["errors"] == []
        assert check_value(good.replace("Oslo", "oslo"), JSON_SCHEMA, normalized)["errors"]
    assert "x-extra" not in normalize_schema({"type": "string", "x-extra": {"$ref": "#/nowhere"}})


def test_decimal_multiples_are_multiples():
    """jsonschema decides a float multipleOf by int(x / d) == x / d: 0.07 was no multiple of 0.01 there."""
    for divisor, fits, misfits in ((0.01, ("0.07", "19.99", "20", "-0.03"), ("0.075", "1.001")),
                                   (0.1, ("0.3", "0.7", "2.3", "1e2"), ("2.35", "0.05")),
                                   (3, ("9", "-3", "9e3"), ("10", "1.5"))):
        schema = normalize_schema({"type": "number", "multipleOf": divisor})
        for text in fits:
            assert check_value(text, JSON_SCHEMA, schema)["errors"] == [], (divisor, text)
        for text in misfits:
            assert check_value(text, JSON_SCHEMA, schema)["errors"] == [f"<root>: {json.loads(text)!r} is not a "
                                                                       f"multiple of {divisor}"], (divisor, text)


def test_unicode_classes_regex_knows_are_taken():
    schema = normalize_schema({"type": "string", "pattern": "^\\p{L}+$"})
    assert check_value(json.dumps("Straße"), JSON_SCHEMA, schema)["errors"] == []
    assert check_value(json.dumps("abc1"), JSON_SCHEMA, schema)["errors"]


def test_zod_to_json_schema_output_is_taken():
    """openai-node's zodResponseFormat (zod-to-json-schema): draft-07, definitions, #/definitions refs."""
    zod = {"$schema": "http://json-schema.org/draft-07/schema#", "$ref": "#/definitions/Trip",
           "definitions": {
               "Leg": {"type": "object", "properties": {"to": {"type": "string"}, "days": {"type": "number"}},
                       "required": ["to", "days"], "additionalProperties": False},
               "Trip": {"type": "object", "properties": {
                   "legs": {"type": "array", "items": {"$ref": "#/definitions/Leg"}},
                   "note": {"type": ["string", "null"]}},
                   "required": ["legs", "note"], "additionalProperties": False}}}
    normalized = normalize_schema(zod)
    assert "definitions" not in normalized and set(normalized["$defs"]) == {"Leg", "Trip"}
    assert normalized["$ref"] == "#/$defs/Trip" and "$schema" not in normalized
    assert normalized["$defs"]["Trip"]["properties"]["legs"]["items"] == {"$ref": "#/$defs/Leg"}
    assert check_value('{"legs": [{"to": "Oslo", "days": 2}], "note": null}', JSON_SCHEMA, normalized)["errors"] == []
    assert check_value('{"legs": [{"to": 1, "days": 2}], "note": null}', JSON_SCHEMA, normalized)["errors"]
    for draft in ("https://json-schema.org/draft/2019-09/schema", "http://json-schema.org/draft-07/schema"):
        normalize_schema({"$schema": draft, "type": "object"})
    with pytest.raises(SchemaRefused, match="definitions and \\$defs together"):
        normalize_schema({"definitions": {"a": {}}, "$defs": {"b": {}}, "type": "object"})
    with pytest.raises(SchemaRefused, match="draft-04"):
        normalize_schema({"$schema": "http://json-schema.org/draft-04/schema#", "type": "object"})


def test_the_ecma_group_forms_pass():
    for pattern in ("^(?:ab)+$", "a(?=b)", "a(?!b)", "(?<=a)b", "(?<!a)b", "(?P<y>\\d{4})", "[(?#]x", "\\(?#"):
        normalize_schema({"type": "string", "pattern": pattern})


def test_the_repeat_count_counts_what_regex_unrolls_the_minimum():
    """Measured, and in build_REPEAT (_regex.c): regex unrolls a repeat's minimum, not its range."""
    normalize_schema({"type": "string", "pattern": "^[\\s\\S]{0,30000}$"})
    normalize_schema({"type": "string", "pattern": "^(?:(?:(?:a{0,120}){0,120}){0,120})$"})
    for pattern in ("^(?:(?:(?:a{120,}){120,}){120,})$", "^(?:(?:(?:a{60,120}){60,120}){60,120})$"):
        with pytest.raises(SchemaRefused, match="repeats too much"):
            normalize_schema({"type": "string", "pattern": pattern})


def test_all_patterns_of_a_schema_together_are_bounded():
    """The review's 99 KB schema: 2451 definitions of ((a{141}){141}), each within the pattern bound -- 3.35 s
    to prepare. Together they are refused."""
    many = {"$defs": {f"d{i}": {"pattern": "((a{141}){141})"} for i in range(12)}, "type": "string"}
    with pytest.raises(SchemaRefused, match="patterns together repeat too much"):
        normalize_schema(many)
    normalize_schema({"$defs": {f"d{i}": {"pattern": "((a{141}){141})"} for i in range(9)}, "type": "string"})


def test_patterns_bypass_regex_s_own_cache():
    """regex keeps 500 compiled patterns: of the large kind half a gigabyte. The worker compiles without it."""
    import regex
    import regex._main

    regex.purge()
    schema = normalize_schema({"type": "object", "properties": {
        f"p{i}": {"type": "string", "pattern": f"^(?:x{{{i + 1}}})+$"} for i in range(40)}})
    check_value(json.dumps({f"p{i}": "x" * (i + 1) for i in range(40)}), JSON_SCHEMA, schema)
    assert not regex._main._cache, f"{len(regex._main._cache)} patterns in regex's cache"


def test_the_repeat_count_follows_nesting():
    units = schema_worker._repeat_units
    assert units("^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$") < 50
    assert units("^(?:(?:a{120}){120})$") < schema_worker.MAX_PATTERN_UNITS
    assert units("^(?:(?:(?:a{120}){120}){120})$") > 1_000_000
    assert units(r"^\{99999}$") < 20, "an escaped brace is no repeat"


# ------------------------------------------------------------------ the answer (the worker's check, run here)

def test_an_answer_matching_the_schema_passes_and_a_whole_answer_fence_comes_off():
    plain = _check('{"city": "Oslo", "days": 3}', SCHEMA)
    assert plain["errors"] == [] and plain["value"] == {"city": "Oslo", "days": 3}
    fenced = _check('```json\n{"city": "Oslo", "days": 3}\n```', SCHEMA)
    assert fenced["errors"] == [] and fenced["text"] == '{"city": "Oslo", "days": 3}'


def test_an_answer_that_only_looks_like_json_is_judged_by_the_schema():
    wrong_type = _check('{"city": "Oslo", "days": "three"}', SCHEMA)["errors"]
    assert wrong_type and wrong_type[0].startswith("days:")
    assert "'days' is a required property" in " ".join(_check('{"city": "Oslo"}', SCHEMA)["errors"])
    assert _check('{"city": "Oslo", "days": 3, "note": "x"}', SCHEMA)["errors"], "additionalProperties ignored"


def test_only_a_fence_around_the_whole_answer_comes_off():
    strip = schema_worker.strip_whole_fence
    assert strip('```json\n{"a": 1}\n```') == '{"a": 1}'
    assert strip('```\n{"a": 1}\n```') == '{"a": 1}'
    for text in ('```json\n{"a": 1}\n```\nmore', 'see ```json\n{"a": 1}\n```', '```json {"a": 1}```',
                 '```not a tag!\n{"a": 1}\n```'):
        assert strip(text) == text, text


def test_prose_around_json_nan_and_json_mode():
    assert _check('Here you go: {"city": "Oslo", "days": 3}', SCHEMA)["errors"]
    assert _check('```json\n{"city": "Oslo", "days": 3}\n```\nHope that helps!', SCHEMA)["errors"]
    assert _check("", SCHEMA)["errors"] == [schema_worker.EMPTY_ANSWER] and _check(None, SCHEMA)["errors"]
    for text in ('{"a": NaN}', '{"a": Infinity}'):
        assert check_value(text, JSON_OBJECT, None)["errors"], text
    assert check_value('{"any": ["thing"]}', JSON_OBJECT, None)["errors"] == []
    assert check_value("[1, 2]", JSON_OBJECT, None)["errors"]


def test_the_answer_is_bounded_and_the_errors_reported_too():
    fmt = {"type": "array"}
    assert "characters; at most" in _check("[" + "1," * schema_worker.MAX_ANSWER_CHARS + "1]", fmt)["errors"][0]
    over = schema_worker.MAX_ANSWER_DEPTH + 1
    assert "nests deeper" in _check("[" * over + "]" * over, fmt)["errors"][0]
    assert _check("[" * schema_worker.MAX_ANSWER_DEPTH + "]" * schema_worker.MAX_ANSWER_DEPTH, fmt)["errors"] == []
    assert _check("[" * 200_000 + "]" * 200_000, fmt)["errors"]
    many = {"type": "object", "properties": {f"k{i}": {"type": "integer"} for i in range(20)}}
    errors = _check("{" + ", ".join(f'"k{i}": "x"' for i in range(20)) + "}", many)["errors"]
    assert len(errors) == 9 and errors[-1] == "... and 12 more"


@pytest.mark.timeout(10, method="signal")  # stock anyOf takes 2^depth: minutes at this depth
def test_a_recursive_union_is_checked_in_linear_time_valid_or_not():
    """pydantic writes ``child: A | B | None`` into A and B; the discriminating field comes after the
    recursive one. Stock anyOf checks every failing branch to its end: 2^depth, for a valid answer too."""
    from typing import Literal, Optional, Union

    from pydantic import BaseModel

    class A(BaseModel):
        child: Optional[Union["A", "B"]] = None
        kind: Literal["a"]

    class B(BaseModel):
        child: Optional[Union["A", "B"]] = None
        kind: Literal["b"]

    class Root(BaseModel):
        root: Union[A, B]

    schema = normalize_schema(Root.model_json_schema())
    node: dict = {"kind": "b"}
    for _ in range(40):
        node = {"child": node, "kind": "b"}
    started = time.monotonic()
    assert check_value(json.dumps({"root": node}), JSON_SCHEMA, schema)["errors"] == []
    broken = json.dumps({"root": node}).replace('"kind": "b"}', '"kind": "c"}', 1)
    assert check_value(broken, JSON_SCHEMA, schema)["errors"]
    assert time.monotonic() - started < 1.0


def test_an_answer_too_costly_to_check_fails_closed_with_its_own_message(monkeypatch):
    monkeypatch.setattr(schema_worker, "CHECK_BUDGET", 0.0)
    result = _check('"x"', {"anyOf": [{"type": "integer"}, {"type": "string"}]})
    assert result["schema_failed"] and "could not be checked against this schema within 0.0 s" in result["errors"][0]


def test_a_message_quotes_no_more_than_its_limit_of_the_value():
    errors = _check(json.dumps("x" * 900_000), {"type": "integer"})["errors"]
    assert errors and all(len(line) <= schema_worker.MAX_ERROR_CHARS for line in errors)
    errors = _check(json.dumps("x" * 900_000), {"anyOf": [{"type": "integer"}, {"type": "null"}]})["errors"]
    assert errors and all(len(line) <= schema_worker.MAX_ERROR_CHARS for line in errors)


@pytest.mark.timeout(5, method="signal")  # re holds the GIL: only a signal ends a runaway match
def test_a_catastrophic_pattern_is_a_mismatch_within_its_time_not_a_hang():
    started = time.monotonic()
    errors = _check(json.dumps(EVIL_TEXT), {"type": "string", "pattern": CATASTROPHIC})["errors"]
    assert time.monotonic() - started < 1.0
    assert "in the time allowed" in errors[0], errors


@pytest.mark.timeout(5, method="signal")
def test_the_time_limit_is_for_the_whole_answer_not_per_value():
    schema = {"type": "array", "items": {"type": "string", "pattern": CATASTROPHIC}}
    started = time.monotonic()
    errors = _check(json.dumps([EVIL_TEXT] * 40), schema)["errors"]
    assert time.monotonic() - started < 1.5
    assert errors[-1] == "... and 32 more"


def test_patterns_still_match_as_before():
    fmt = {"type": "object", "properties": {"code": {"type": "string", "pattern": "^[A-Z]{3}-\\d+$"}},
           "additionalProperties": False}
    assert _check('{"code": "ABC-12"}', fmt)["errors"] == []
    assert "does not match" in _check('{"code": "abc"}', fmt)["errors"][0]
    assert _check('{"code": "ABC-1", "b": 1, "c": 2}', fmt)["errors"] == [
        "<root>: Additional properties are not allowed ('b', 'c' were unexpected)"]


def test_jsonschema_matches_regular_expressions_only_where_the_subset_allows_the_timed_one():
    """The drift guard of the time limit: jsonschema calls re.search in these five places. The subset
    allows only ``pattern`` of their keywords, and that one is replaced (_TIMED). A new place in a
    jsonschema release turns this red."""
    import ast
    import inspect

    import jsonschema
    from jsonschema import _keywords, _legacy_keywords, _utils

    found = set()
    for module in (_keywords, _utils, _legacy_keywords, jsonschema.validators):
        for function in ast.walk(ast.parse(inspect.getsource(module))):
            if not isinstance(function, ast.FunctionDef):
                continue
            for call in ast.walk(function):
                if (isinstance(call, ast.Call) and isinstance(call.func, ast.Attribute)
                        and isinstance(call.func.value, ast.Name) and call.func.value.id == "re"
                        and call.func.attr in ("search", "match", "fullmatch", "findall", "finditer", "sub")):
                    found.add(f"{module.__name__.rsplit('.', 1)[-1]}.{function.name}")
    assert found == {"_keywords.pattern", "_keywords.patternProperties", "_utils.find_additional_properties",
                     "_utils.find_evaluated_property_keys_by_schema",
                     "_legacy_keywords.find_evaluated_property_keys_by_schema"}, found
    assert set(schema_worker._TIMED) == {"pattern", "anyOf", "multipleOf"}
    assert not {"patternProperties", "unevaluatedProperties", "dependentSchemas"} & set(schema_worker._ALLOWED)


def test_the_instruction_carries_the_schema_the_answer_is_checked_against():
    text = instruction_text(ResponseFormat(schema=SCHEMA, name="trip"))
    assert json.loads(text[text.index("{"):]) == SCHEMA


# ------------------------------------------------------------------ the process boundary

async def test_prepare_and_check_go_through_the_worker():
    fmt = await prepare_response_format(ResponseFormat(schema={**SCHEMA, "x-note": "gone"}, name="trip"))
    assert fmt.checked and "x-note" not in fmt.schema
    assert (await check_answer('{"city": "Oslo", "days": 3}', fmt)).ok
    wrong = await check_answer('{"city": "Oslo", "days": "3"}', fmt)
    assert not wrong.ok and not wrong.schema_failed
    with pytest.raises(InvalidResponseFormat, match="'allOf'") as refused:
        await prepare_response_format(ResponseFormat(schema={"allOf": [{}]}))
    assert refused.value.field == "schema"
    assert worker_pool().started == 1, "every request started a worker of its own"


#: Within the subset (fan-out 510, no pattern) and still minutes of work on a large answer: only the
#: process boundary ends it.
COSTLY = {"type": "array", "items": {"$ref": "#/$defs/a7"}, "$defs": _chain(7)["$defs"]}
COSTLY_ANSWER = "[" + ",".join(["1"] * 200_000) + "]"


@pytest.mark.timeout(10, method="signal")  # checked in this process instead, it would run for minutes
async def test_a_check_past_its_deadline_is_killed_fails_closed_and_the_next_gets_a_fresh_worker(monkeypatch):
    import psutil

    monkeypatch.setattr(structured_output, "CHECK_DEADLINE", 1.0)
    fmt = await prepare_response_format(ResponseFormat(schema=COSTLY))
    ticks: list[float] = []
    running = True

    async def ticker():
        while running:
            ticks.append(time.monotonic())
            await asyncio.sleep(0.01)

    rss_before = psutil.Process().memory_info().rss
    task = asyncio.ensure_future(ticker())
    started = time.monotonic()
    try:
        checked = await check_answer(COSTLY_ANSWER, fmt)
    finally:
        running = False
        await task
    assert time.monotonic() - started < 3.0
    assert not checked.ok and checked.schema_failed and "did not finish within 1 s" in checked.errors[0]
    gaps = [b - a for a, b in zip(ticks, ticks[1:])]
    assert max(gaps) < 0.25, f"the loop stood still for {max(gaps):.2f} s"
    assert psutil.Process().memory_info().rss - rss_before < 64 * 1024 * 1024
    pool = worker_pool()
    assert (pool.started, pool.killed) == (1, 1)
    assert (await check_answer("[]", fmt)).ok, "the pool did not recover"
    assert pool.started == 2


async def test_a_cancelled_check_takes_its_worker_with_it():
    fmt = await prepare_response_format(ResponseFormat(schema=COSTLY))
    task = asyncio.ensure_future(check_answer(COSTLY_ANSWER, fmt))
    await asyncio.sleep(0.5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    pool = worker_pool()
    assert pool.killed == 1 and not pool._all, "a worker was left behind"


@pytest.mark.timeout(15, method="signal")
async def test_no_more_workers_run_at_once_than_the_cap(monkeypatch):
    monkeypatch.setattr(structured_output, "CHECK_DEADLINE", 1.0)
    fmt = await prepare_response_format(ResponseFormat(schema=COSTLY))
    peak = 0
    pool = worker_pool()

    async def watch():
        nonlocal peak
        while True:
            peak = max(peak, len(pool._all))
            await asyncio.sleep(0.01)

    watcher = asyncio.ensure_future(watch())
    try:
        results = await asyncio.gather(*(check_answer(COSTLY_ANSWER, fmt, owner=f"user{i}") for i in range(4)))
    finally:
        watcher.cancel()
    assert all(result.schema_failed for result in results)
    assert peak == structured_output.WORKER_COUNT, peak



async def test_the_api_process_runs_no_schema_check_itself(monkeypatch):
    """The contract of the boundary: here the worker's functions are booby-trapped -- prepare and check still
    work, because they run in the worker's process, not in this one."""
    def trap(*args, **kwargs):
        raise AssertionError("a schema check ran in the API process")

    monkeypatch.setattr(schema_worker, "normalize_schema", trap)
    monkeypatch.setattr(schema_worker, "check_value", trap)
    monkeypatch.setattr(schema_worker, "handle", trap)

    fmt = await prepare_response_format(ResponseFormat(schema=SCHEMA))
    assert fmt.checked
    assert (await check_answer('{"city": "Oslo", "days": 3}', fmt)).ok
    assert (await check_answer('{"any": 1}', ResponseFormat(type=JSON_OBJECT))).ok  # json only, no schema


@pytest.mark.timeout(10, method="signal")
async def test_the_worker_applies_the_subset_to_whatever_it_is_sent():
    """A check request with a schema that never went through prepare: the worker refuses it itself, instead of
    matching its patternProperties with jsonschema's untimed re."""
    reply = await worker_pool().request({"op": "check", "kind": JSON_SCHEMA, "name": "x",
                                         "schema": {"type": "object", "patternProperties": {CATASTROPHIC: {}}},
                                         "text": json.dumps({EVIL_TEXT: 1})}, 5.0)
    assert reply["ok"] and reply["schema_failed"] and "'patternProperties'" in reply["errors"][0]


async def test_a_worker_that_retires_is_replaced(monkeypatch, tmp_path):
    """A worker whose memory grew answers with "retire" and ends: the pool takes the answer, lets it go and
    starts a fresh one for the next request."""
    script = tmp_path / "retiring_worker.py"
    script.write_text("import json, sys\nline = sys.stdin.readline()\n"
                      "sys.stdout.write(json.dumps({'ok': True, 'schema': {}, 'retire': True}) + '\\n')\n"
                      "sys.stdout.flush()\n")
    monkeypatch.setattr(structured_output, "_WORKER_PATH", script)
    pool = worker_pool()
    for _ in range(2):
        assert (await pool.request({"op": "prepare", "schema": {}}, 5.0)) == {"ok": True, "schema": {}}
    assert pool.started == 2 and pool.killed == 0 and not pool._all


def test_the_worker_loop_answers_every_line_purges_and_retires_past_its_memory(monkeypatch):
    import io
    import signal

    import regex

    alarms: list[int] = []
    monkeypatch.setattr(signal, "alarm", lambda seconds: alarms.append(seconds))
    monkeypatch.setattr("sys.argv", ["schema_worker.py", "0"])
    purged: list[bool] = []
    monkeypatch.setattr(regex, "purge", lambda: purged.append(True))
    monkeypatch.setattr(schema_worker, "_limit_memory", lambda: None)
    rss = iter([0, schema_worker.WORKER_MAX_RSS + 1])
    monkeypatch.setattr(schema_worker, "_rss", lambda: next(rss))
    requests = [{"op": "prepare", "schema": SCHEMA}, {"op": "nonsense"}, {"op": "prepare", "schema": SCHEMA}]
    monkeypatch.setattr("sys.stdin", io.StringIO("".join(json.dumps(r) + "\n" for r in requests)))
    out = io.StringIO()
    monkeypatch.setattr("sys.stdout", out)

    schema_worker.main()

    replies = [json.loads(line) for line in out.getvalue().splitlines()]
    assert len(replies) == 2, "the worker went on after retiring"
    assert replies[0]["ok"] and "retire" not in replies[0]
    assert replies[1] == {"ok": False, "field": "op", "message": "unknown op 'nonsense'", "retire": True}
    assert purged == [True, True]
    # Each request runs under the worker's own alarm, cleared once it is answered.
    assert alarms == [schema_worker.WORKER_REQUEST_SECONDS, 0] * 2


def test_on_linux_the_worker_limits_its_address_space(monkeypatch):
    import resource

    limits: list = []
    monkeypatch.setattr(resource, "setrlimit", lambda kind, value: limits.append((kind, value)))
    monkeypatch.setattr(schema_worker.sys, "platform", "linux")
    schema_worker._limit_memory()
    assert limits == [(resource.RLIMIT_AS, (schema_worker.WORKER_ADDRESS_SPACE,) * 2)]
    limits.clear()
    monkeypatch.setattr(schema_worker.sys, "platform", "darwin")
    schema_worker._limit_memory()
    assert limits == [], "macOS takes the limit without enforcing it: only the deadline bounds the worker there"



async def test_a_worker_that_answers_nonsense_is_a_failed_check_and_is_replaced(monkeypatch, tmp_path):
    script = tmp_path / "confused_worker.py"
    script.write_text("import sys\nfor line in sys.stdin:\n    sys.stdout.write('not json\\n')\n    sys.stdout.flush()\n")
    monkeypatch.setattr(structured_output, "_WORKER_PATH", script)
    fmt = ResponseFormat(schema=SCHEMA, checked=True)

    checked = await check_answer('{"city": "Oslo", "days": 3}', fmt)

    assert not checked.ok and checked.schema_failed and "answered no JSON" in checked.errors[0]
    pool = worker_pool()
    assert pool.killed == 1 and not pool._all



async def test_a_worker_that_ended_while_idle_is_replaced_and_the_request_goes_through():
    fmt = await prepare_response_format(ResponseFormat(schema=SCHEMA))
    pool = worker_pool()
    idle = pool._idle[0]
    idle.kill()  # the worker this pool started, by its own handle
    await idle.wait()

    assert (await check_answer('{"city": "Oslo", "days": 3}', fmt)).ok
    assert pool.started == 2


@pytest.mark.timeout(15, method="signal")
async def test_a_request_that_finds_no_free_worker_in_time_is_busy_not_the_schema_s_fault(monkeypatch):
    monkeypatch.setattr(structured_output, "WORKER_COUNT", 1)
    monkeypatch.setattr(structured_output, "QUEUE_DEADLINE", 0.3)
    monkeypatch.setattr(structured_output, "CHECK_DEADLINE", 2.0)
    fmt = await prepare_response_format(ResponseFormat(schema=COSTLY))

    slow, waiting = await asyncio.gather(check_answer(COSTLY_ANSWER, fmt), check_answer(COSTLY_ANSWER, fmt))

    assert "did not finish within 2 s" in slow.errors[0]
    assert waiting.schema_failed and "busy" in waiting.errors[0]


async def test_a_long_answer_s_verdict_stays_small():
    """The worker sends back the verdict only (the caller has the text), and its messages are bounded: a
    reply that grew with the answer broke the pipe's line limit."""
    fmt = await prepare_response_format(ResponseFormat(schema={"type": "integer"}))
    text = json.dumps("\U000e0001" * 990_000)
    checked = await check_answer(text, fmt)
    assert not checked.ok and not checked.schema_failed
    assert all(len(line) <= schema_worker.MAX_ERROR_CHARS for line in checked.errors)
    reply = await worker_pool().request({"op": "check", "kind": JSON_SCHEMA, "schema": fmt.schema, "name": "x",
                                         "text": text}, 5.0)
    assert len(json.dumps(reply)) < 2_000, "the reply carries more than the verdict"


@pytest.mark.parametrize("closes", ["0, 1", "1"], ids=["pipe broken", "no answer"])
async def test_a_worker_whose_pipe_broke_while_idle_is_replaced_once(monkeypatch, tmp_path, closes):
    """Alive but deaf (its pipes closed): the reused worker fails the write (both closed) or answers with EOF
    (only its stdout closed), and the request goes to a fresh one -- once."""
    script = tmp_path / "deaf_worker.py"
    script.write_text("import json, os, sys, time\n"
                      "sys.stdin.readline()\n"
                      "sys.stdout.write(json.dumps({'ok': True, 'schema': {}}) + '\\n')\n"
                      "sys.stdout.flush()\n"
                      f"for fd in ({closes},):\n    os.close(fd)\ntime.sleep(30)\n")
    monkeypatch.setattr(structured_output, "_WORKER_PATH", script)
    pool = worker_pool()
    assert (await pool.request({"op": "prepare", "schema": {}}, 5.0))["ok"]
    assert (await pool.request({"op": "prepare", "schema": {}}, 5.0))["ok"]
    assert pool.started == 2 and pool.killed == 1


async def test_the_workers_of_a_loop_that_ended_exit_by_themselves(monkeypatch):
    """The pool lives on its loop and goes with it; its idle workers exit after their idle time."""
    import psutil

    monkeypatch.setattr(structured_output, "WORKER_IDLE_SECONDS", 0.5)

    def a_run_of_its_own() -> int:
        async def prepare() -> int:
            await prepare_response_format(ResponseFormat(schema=SCHEMA))
            return len(worker_pool()._all)
        return asyncio.run(prepare())

    assert await asyncio.to_thread(a_run_of_its_own) == 1
    assert await asyncio.to_thread(a_run_of_its_own) == 1
    for _ in range(100):
        workers = [child for child in psutil.Process().children()
                   if any("schema_worker.py" in part for part in child.cmdline())]
        if not workers:
            break
        await asyncio.sleep(0.05)
    assert not workers, "the workers of the ended loops stayed"



async def test_a_fresh_worker_that_breaks_its_pipe_is_a_checker_error_not_a_crash(monkeypatch, tmp_path):
    """A worker that never reads: the (large) request cannot be written -- deterministically a broken pipe --
    and it comes back as SchemaCheckerError, which every caller turns into a failed check or a 503."""
    script = tmp_path / "closed_worker.py"
    script.write_text("import os, time\nos.close(0)\ntime.sleep(30)\n")
    monkeypatch.setattr(structured_output, "_WORKER_PATH", script)
    with pytest.raises(structured_output.SchemaCheckerError, match="the schema checker failed"):
        await worker_pool().request({"op": "check", "text": "x" * 1_000_000}, 5.0)
    assert worker_pool().killed == 1 and not worker_pool()._all



@pytest.mark.timeout(20, method="signal")
async def test_one_user_s_flood_does_not_hold_up_another_user(monkeypatch):
    """Two workers, one user sending six costly checks at once: that user's lane runs one at a time, the other
    worker stays free, and another user's prepare goes through at once."""
    monkeypatch.setattr(structured_output, "CHECK_DEADLINE", 1.5)
    fmt = await prepare_response_format(ResponseFormat(schema=COSTLY), owner="alice")
    flood = [asyncio.ensure_future(check_answer(COSTLY_ANSWER, fmt, owner="mallory")) for _ in range(6)]
    await asyncio.sleep(0.3)  # the flood has taken what it can

    started = time.monotonic()
    other = await prepare_response_format(ResponseFormat(schema=SCHEMA), owner="alice")
    waited = time.monotonic() - started

    for task in flood:
        task.cancel()
    await asyncio.gather(*flood, return_exceptions=True)
    assert other.checked
    assert waited < 1.0, f"another user's prepare waited {waited:.2f} s behind one user's flood"


async def test_a_user_s_requests_beyond_the_queue_time_are_busy_in_their_own_lane(monkeypatch):
    monkeypatch.setattr(structured_output, "CHECK_DEADLINE", 2.0)
    monkeypatch.setattr(structured_output, "QUEUE_DEADLINE", 0.5)
    fmt = await prepare_response_format(ResponseFormat(schema=COSTLY), owner="mallory")

    first, second = await asyncio.gather(check_answer(COSTLY_ANSWER, fmt, owner="mallory"),
                                         check_answer(COSTLY_ANSWER, fmt, owner="mallory"))

    assert "did not finish" in first.errors[0] and not first.checker_failed
    assert second.checker_failed and "this user's earlier requests" in second.errors[0]
    assert not worker_pool()._lanes, "a lane stayed behind"
