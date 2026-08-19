"""Generate schemas/llm-config.schema.json from the Pydantic config models.

The schema is consumed by the VS Code YAML extension (.vscode/settings.json
maps it onto config/llm.yaml) — it exists so the editor flags mistakes while
editing the config. The previous, hand-written schema had drifted so far from
LLMSystemConfig that it actively misinformed (it knew neither the real
providers nor half the fields, while llm.yaml carried dead keys like
``ollama_url`` that no schema and no model ever read).

Derived instead of hand-written: ``LLMSystemConfig.model_json_schema()`` is
the single source of truth. Every object gets ``additionalProperties: false``
on top — the runtime ignores unknown keys (pydantic ``extra=ignore``), which
is exactly why a dead key survives silently; the editor is the place where it
should light up.

Run after every change to the LLM config models:

    .venv/Scripts/python.exe src/scripts/generate_llm_config_schema.py

tests/config/test_llm_config_schema.py fails when the file is stale.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
SCHEMA_PATH = REPO_ROOT / "schemas" / "llm-config.schema.json"


def _forbid_unknown_keys(node: Any) -> None:
    """Set additionalProperties: false on every object WITH declared properties.

    Objects without ``properties`` (free-form dicts like openrouter_routing or
    the models/profiles maps, which use additionalProperties themselves) are
    left alone.
    """
    if isinstance(node, dict):
        if node.get("type") == "object" and "properties" in node:
            node.setdefault("additionalProperties", False)
        for value in node.values():
            _forbid_unknown_keys(value)
    elif isinstance(node, list):
        for value in node:
            _forbid_unknown_keys(value)


def build_schema() -> dict:
    """The complete JSON schema for config/llm.yaml, derived from the models."""
    from agent_system.config.models import LLMSystemConfig

    inner = LLMSystemConfig.model_json_schema()
    _forbid_unknown_keys(inner)
    # $refs point at the document root (#/$defs/...), so the defs must live
    # there — not nested under properties/llm_system.
    defs = inner.pop("$defs", {})
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "title": "LLM Configuration Schema",
        "description": (
            "JSON Schema for AgentSystem LLM configuration (config/llm.yaml). "
            "GENERATED from agent_system.config.models.LLMSystemConfig by "
            "src/scripts/generate_llm_config_schema.py — do not edit by hand."
        ),
        "type": "object",
        "required": ["llm_system"],
        "additionalProperties": False,
        "properties": {"llm_system": inner},
        "$defs": defs,
    }


def main() -> None:
    schema = build_schema()
    SCHEMA_PATH.write_text(
        json.dumps(schema, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    print(f"wrote {SCHEMA_PATH}")


if __name__ == "__main__":
    main()
