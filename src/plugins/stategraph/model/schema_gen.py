"""Generate ``schemas/machine.schema.json`` from the format models and the registered activity kinds.

The schema serves editors (VS Code yaml-language-server) and the panel; the
models stay the one source of truth. Regenerate after changing a model or a
kind; ``tests/test_plugin_stategraph_schema.py`` fails when the file drifts::

    .venv/bin/python -m plugins.stategraph.model.schema_gen   (cwd: src)
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from plugins.stategraph.kinds import REGISTRY
from .spec import MachineSpec

SCHEMA_PATH = Path(__file__).resolve().parents[1] / "schemas" / "machine.schema.json"


def machine_schema() -> dict[str, Any]:
    schema = MachineSpec.model_json_schema(by_alias=True)
    defs = schema.setdefault("$defs", {})
    activities = []
    for key in sorted(REGISTRY):
        kind = REGISTRY[key]
        kind_schema = kind.spec_model.model_json_schema(by_alias=True)
        for name, definition in kind_schema.pop("$defs", {}).items():
            defs.setdefault(name, definition)
        kind_schema["title"] = f"{kind.title} activity ({key})"
        kind_schema.setdefault("required", [])
        if key not in kind_schema["required"]:
            kind_schema["required"].insert(0, key)
        defs[f"Activity_{key}"] = kind_schema
        activities.append({"$ref": f"#/$defs/Activity_{key}"})
    state = defs.get("StateSpec")
    if state is not None:
        state.setdefault("properties", {})["do"] = {"description": "the do-activity: exactly one kind key",
                                                    "oneOf": activities}
    # every other place that holds an activity is typed like do (the models keep them plain dicts: kinds are a
    # registry plugins add to)
    defs["Activity"] = {"description": "an activity: exactly one kind key", "oneOf": activities}
    for owner, name in (("MachineSpec", "finally"), ("LocalMachineSpec", "finally"), ("StateSpec", "finally"),
                        ("ResourceSpec", "open"), ("ResourceSpec", "fork"), ("ResourceSpec", "close")):
        properties = schema["properties"] if owner == "MachineSpec" else defs[owner]["properties"]
        _as_activity(properties[name])
    for key, kind in REGISTRY.items():
        properties = defs[f"Activity_{key}"]["properties"]
        for name in kind.nested_one:
            _as_activity(properties[name])
        for name in kind.nested_map:
            properties[name]["additionalProperties"] = dict(_ACTIVITY)
    schema["$schema"] = "https://json-schema.org/draft/2020-12/schema"
    schema["title"] = "stategraph machine (format 1)"
    return schema


_ACTIVITY = {"$ref": "#/$defs/Activity"}
_PLAIN_OBJECT = {"additionalProperties": True, "type": "object"}


def _as_activity(prop: dict[str, Any]) -> None:
    """A property pydantic types as a plain object (``dict[str, Any]``, optional or not) as an activity."""
    if "anyOf" in prop:
        prop["anyOf"] = [dict(_ACTIVITY) if member == _PLAIN_OBJECT else member for member in prop["anyOf"]]
    else:
        for key in _PLAIN_OBJECT:
            prop.pop(key, None)
        prop.update(_ACTIVITY)


def render() -> str:
    return json.dumps(machine_schema(), indent=2, sort_keys=True) + "\n"


def main() -> None:
    SCHEMA_PATH.parent.mkdir(parents=True, exist_ok=True)
    SCHEMA_PATH.write_text(render(), encoding="utf-8", newline="\n")  # the same file on every OS
    print(f"wrote {SCHEMA_PATH}")


if __name__ == "__main__":
    main()
