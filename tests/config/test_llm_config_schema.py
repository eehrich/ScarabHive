"""schemas/llm-config.schema.json must track the Pydantic models.

The schema is what the VS Code YAML extension validates config/llm.yaml
against (.vscode/settings.json). The hand-written predecessor drifted for so
long that it knew neither the real providers nor the real fields — while the
config carried dead keys (ollama_url, include_thinking) that nothing read.
These tests nail the class:

1. The file equals a fresh generation from LLMSystemConfig (anti-drift:
   changing a config model without regenerating the schema goes red).
2. The REAL config/llm.yaml validates against the schema FILE (production
   path: same artifacts the editor uses).
3. Strictness itself is asserted: an unknown key inside a model entry must
   fail validation — that is the property that makes dead keys visible.
"""
from __future__ import annotations

import json
from pathlib import Path

import yaml
from jsonschema import Draft202012Validator

from scripts.generate_llm_config_schema import SCHEMA_PATH, build_schema

REPO_ROOT = Path(__file__).resolve().parents[2]
LLM_YAML = REPO_ROOT / "config" / "llm.yaml"


def _schema_from_file() -> dict:
    return json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))


def test_schema_file_matches_generated_schema():
    """Regenerate with src/scripts/generate_llm_config_schema.py when red."""
    assert _schema_from_file() == build_schema(), (
        "schemas/llm-config.schema.json is stale — run "
        ".venv/Scripts/python.exe src/scripts/generate_llm_config_schema.py"
    )


def test_real_llm_yaml_validates_against_schema_file():
    data = yaml.safe_load(LLM_YAML.read_text(encoding="utf-8"))
    assert data, "fixture assertion: config/llm.yaml is empty?"
    validator = Draft202012Validator(_schema_from_file())
    errors = ["/".join(map(str, e.absolute_path)) + ": " + e.message
              for e in validator.iter_errors(data)]
    assert errors == [], "\n".join(errors[:20])


def test_unknown_key_in_a_model_entry_fails_validation():
    """The strictness IS the point: a dead key (the ollama_url class) must
    light up in the editor instead of being silently ignored."""
    data = yaml.safe_load(LLM_YAML.read_text(encoding="utf-8"))
    first_model = next(iter(data["llm_system"]["models"].values()))
    first_model["ollama_url"] = "http://example.invalid:11434"
    validator = Draft202012Validator(_schema_from_file())
    errors = list(validator.iter_errors(data))
    assert errors, "an unknown model key validated cleanly — strictness lost"
