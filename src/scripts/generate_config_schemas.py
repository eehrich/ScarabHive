"""Generate the derived JSON schemas in schemas/ from the Pydantic config models.

The schemas are consumed by the VS Code YAML extension (.vscode/settings.json
maps them onto the config files) — they exist so the editor flags mistakes
while editing. The hand-written predecessors had drifted so far from the
models that they actively misinformed (wrong providers, invented fields),
while the configs carried dead keys (``ollama_url``, ``include_thinking``)
that no schema and no model ever read.

Derived instead of hand-written — the Pydantic models are the single source
of truth:

- ``llm-config.schema.json``      <- LLMSystemConfig     (config/llm.yaml)
- ``main-config.schema.json``     <- AgentSystemConfig   (config/config.yaml)
- ``plugins-config.schema.json``  <- PluginsConfig + the hooks: section
                                     (config/plugins.yaml)

Every object with declared properties gets ``additionalProperties: false``
on top — the runtime ignores unknown keys (pydantic ``extra="ignore"``),
which is exactly why a dead key survives silently; the editor is the place
where it should light up. Models that declare ``extra="allow"`` (MCPConfig:
plugin-specific keys) keep their permissiveness, pydantic emits
``additionalProperties: true`` for them explicitly.

NOT derived (no Pydantic model behind them): ``plugin-config.schema.json``
(legacy plugin.yaml manifests; the tree only carries plugin.toml today) and
``session-schema.json`` (SessionManager works on plain dicts).

Run after every change to the config models:

    .venv/Scripts/python.exe src/scripts/generate_config_schemas.py

tests/config/test_config_schemas.py fails when a file is stale.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable, Dict

REPO_ROOT = Path(__file__).resolve().parents[2]
SCHEMA_DIR = REPO_ROOT / "schemas"

_GENERATED_NOTE = (
    "GENERATED from agent_system.config.models by "
    "src/scripts/generate_config_schemas.py — do not edit by hand."
)


def _forbid_unknown_keys(node: Any) -> None:
    """Set additionalProperties: false on every object WITH declared properties.

    Objects without ``properties`` (free-form dicts like openrouter_routing or
    the models/profiles maps, which use additionalProperties themselves) are
    left alone; explicit ``additionalProperties: true`` from ``extra="allow"``
    models survives (setdefault).
    """
    if isinstance(node, dict):
        if node.get("type") == "object" and "properties" in node:
            node.setdefault("additionalProperties", False)
        for value in node.values():
            _forbid_unknown_keys(value)
    elif isinstance(node, list):
        for value in node:
            _forbid_unknown_keys(value)


def _hoist_defs(inner: dict) -> dict:
    """$refs point at the document root (#/$defs/...) — hoist them there."""
    defs = inner.pop("$defs", {})
    _allow_bare_skills_list(defs)
    return defs


def _allow_bare_skills_list(defs: dict) -> None:
    """Mirror SkillsConfig._accept_bare_list in the schema.

    The loader accepts ``skills: ["house-style"]`` (a model_validator with
    mode="before" turns it into {"always": [...]}) — invisible to
    model_json_schema(). Without this patch the editor flags a form the
    runtime happily accepts. The same class applies to any future
    before-validator that widens a field's accepted shape.
    """
    agent_config = defs.get("AgentConfig")
    if not agent_config:
        return
    skills = agent_config.get("properties", {}).get("skills")
    if isinstance(skills, dict) and "anyOf" in skills:
        skills["anyOf"].insert(
            0, {"type": "array", "items": {"type": "string"}})


def _add_model_inheritance(inner: dict, defs: dict) -> None:
    """Teach the schema about ``extends``.

    It lives ONLY in the YAML: settings._resolve_model_inheritance folds it
    away before validation, so LLMModelConfig deliberately has no ``extends``
    field — with extra="forbid" a leftover one has to be an error, not a
    silently ignored key. The editor still needs to know it, hence here.
    """
    model = defs.get("LLMModelConfig")
    if model is not None:
        model.setdefault("properties", {})["extends"] = {
            "type": "string",
            "title": "Extends",
            "description": "Name of another model entry to inherit from. Merged "
                           "field-wise; lists follow the +item/!pattern syntax of "
                           "the agent type: chains.",
        }
        # An inheriting entry need not repeat `model` — it comes from the parent.
        if isinstance(model.get("required"), list) and "model" in model["required"]:
            model["required"] = [r for r in model["required"] if r != "model"]


def build_llm_schema() -> dict:
    """Schema for config/llm.yaml, derived from LLMSystemConfig."""
    from agent_system.config.models import LLMSystemConfig

    inner = LLMSystemConfig.model_json_schema()
    _forbid_unknown_keys(inner)
    defs = _hoist_defs(inner)
    _add_model_inheritance(inner, defs)
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "title": "LLM Configuration Schema",
        "description": (
            "JSON Schema for AgentSystem LLM configuration (config/llm.yaml). "
            + _GENERATED_NOTE
        ),
        "type": "object",
        "required": ["llm_system"],
        "additionalProperties": False,
        "properties": {"llm_system": inner},
        "$defs": defs,
    }


def build_main_schema() -> dict:
    """Schema for config/config.yaml, derived from AgentSystemConfig.

    config.yaml is the PRE-include-merge file: the llm_system/plugins/
    external_servers sections live in the included files, so they are simply
    absent here — the schema only forbids UNKNOWN keys, absence is fine.
    """
    from agent_system.config.models import AgentSystemConfig

    schema = AgentSystemConfig.model_json_schema()
    _forbid_unknown_keys(schema)
    defs = _hoist_defs(schema)
    schema.update({
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "title": "Main Configuration Schema",
        "description": (
            "JSON Schema for the AgentSystem main configuration "
            "(config/config.yaml, pre-include-merge). " + _GENERATED_NOTE
        ),
        "$defs": defs,
    })
    return schema


def build_plugins_schema() -> dict:
    """Schema for config/plugins.yaml: plugins: (PluginsConfig) + hooks:.

    The hooks: section is loaded by hooks/config.py::HooksConfig — a plain
    class, not a Pydantic model, so its (three-field) shape is spelled out
    here by hand. If HooksConfig grows a field, add it here; the anti-drift
    test validates the REAL plugins.yaml, so a used-but-missing field goes
    red there.
    """
    from agent_system.config.models import PluginsConfig

    plugins_inner = PluginsConfig.model_json_schema()
    _forbid_unknown_keys(plugins_inner)
    defs = _hoist_defs(plugins_inner)

    hook_override = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "enabled": {"type": "boolean"},
            "timeout": {"type": "number", "exclusiveMinimum": 0},
            "order": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "before": {"type": "array", "items": {"type": "string"}},
                    "after": {"type": "array", "items": {"type": "string"}},
                },
            },
        },
    }
    hooks_section = {
        "type": "object",
        "description": (
            "Global hook settings (hooks/config.py::HooksConfig). Override "
            "keys: plugin_name.hook_name (exact, wins) or plugin_name "
            "(all hooks of that plugin)."
        ),
        "additionalProperties": False,
        "properties": {
            "enabled": {"type": "boolean", "default": True},
            "default_timeout": {"type": "number", "exclusiveMinimum": 0,
                                "default": 30.0},
            "overrides": {
                "type": "object",
                "additionalProperties": hook_override,
            },
        },
    }
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "title": "Plugins Configuration Schema",
        "description": (
            "JSON Schema for AgentSystem plugin configuration "
            "(config/plugins.yaml). " + _GENERATED_NOTE
        ),
        "type": "object",
        "required": ["plugins"],
        "additionalProperties": False,
        "properties": {"plugins": plugins_inner, "hooks": hooks_section},
        "$defs": defs,
    }


#: filename -> builder; the anti-drift test iterates this same registry.
SCHEMAS: Dict[str, Callable[[], dict]] = {
    "llm-config.schema.json": build_llm_schema,
    "main-config.schema.json": build_main_schema,
    "plugins-config.schema.json": build_plugins_schema,
}


def main() -> None:
    for filename, builder in SCHEMAS.items():
        path = SCHEMA_DIR / filename
        path.write_text(
            json.dumps(builder(), indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        print(f"wrote {path}")


if __name__ == "__main__":
    main()
