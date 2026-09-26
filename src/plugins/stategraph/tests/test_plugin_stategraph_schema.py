"""schemas/machine.schema.json is generated from the format models; it must not drift from them.

Mutation checks run (2026-09-26): adding a field to StateSpec without
regenerating made test_committed_schema_matches_the_models red; dropping the
Activity_* oneOf injection in schema_gen made test_activities_are_validated_by_kind red.
"""

from __future__ import annotations

import json

import jsonschema

from plugins.stategraph.model.schema_gen import SCHEMA_PATH, machine_schema, render


def test_committed_schema_matches_the_models():
    assert SCHEMA_PATH.read_text(encoding="utf-8") == render(), (
        "schemas/machine.schema.json is stale: run `.venv/bin/python -m plugins.stategraph.model.schema_gen` "
        "(cwd src) and commit the result")


def test_schema_is_a_valid_json_schema():
    jsonschema.Draft202012Validator.check_schema(json.loads(SCHEMA_PATH.read_text(encoding="utf-8")))


def test_activities_are_validated_by_kind():
    validator = jsonschema.Draft202012Validator(machine_schema())
    machine = {"stategraph": 1, "id": "m", "initial": "a",
               "states": {"a": {"do": {"agent": "w", "task": "t"}, "transitions": [{"target": "b"}]},
                          "b": {"type": "final"}}}
    assert not list(validator.iter_errors(machine))
    machine["states"]["a"]["do"] = {"agent": "w", "tsak": "t"}  # a typo is not silently accepted
    assert list(validator.iter_errors(machine))
