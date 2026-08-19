"""The derived schemas in schemas/ must track the Pydantic models.

The schemas are what the VS Code YAML extension validates the config files
against (.vscode/settings.json). The hand-written predecessors drifted for so
long that they knew neither the real providers nor the real fields — while
the configs carried dead keys (ollama_url, include_thinking) that nothing
read. These tests nail the class, per schema:

1. The file equals a fresh generation from the models (anti-drift: changing
   a config model without regenerating goes red).
2. The REAL config file validates against the schema FILE (production path:
   same artifacts the editor uses).
3. Strictness itself is asserted: an unknown key must fail validation —
   that is the property that makes dead keys visible in the editor.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml
from jsonschema import Draft202012Validator

from scripts.generate_config_schemas import SCHEMA_DIR, SCHEMAS

REPO_ROOT = Path(__file__).resolve().parents[2]

#: schema file -> (config file it validates, path to an object entry that must
#: reject an injected unknown key)
CONFIGS = {
    "llm-config.schema.json": ("config/llm.yaml", ("llm_system", "models")),
    "main-config.schema.json": ("config/config.yaml", ("network",)),
    "plugins-config.schema.json": ("config/plugins.yaml", ("plugins",)),
}


def _schema_from_file(filename: str) -> dict:
    return json.loads((SCHEMA_DIR / filename).read_text(encoding="utf-8"))


@pytest.mark.parametrize("filename", sorted(SCHEMAS))
def test_schema_file_matches_generated_schema(filename):
    """Regenerate with src/scripts/generate_config_schemas.py when red."""
    assert _schema_from_file(filename) == SCHEMAS[filename](), (
        f"schemas/{filename} is stale — run "
        ".venv/Scripts/python.exe src/scripts/generate_config_schemas.py"
    )


@pytest.mark.parametrize("filename", sorted(CONFIGS))
def test_real_config_validates_against_schema_file(filename):
    yaml_path, _ = CONFIGS[filename]
    data = yaml.safe_load((REPO_ROOT / yaml_path).read_text(encoding="utf-8"))
    assert data, f"fixture assertion: {yaml_path} is empty?"
    validator = Draft202012Validator(_schema_from_file(filename))
    errors = ["/".join(map(str, e.absolute_path)) + ": " + e.message
              for e in validator.iter_errors(data)]
    assert errors == [], "\n".join(errors[:20])


@pytest.mark.parametrize("filename", sorted(CONFIGS))
def test_unknown_key_fails_validation(filename):
    """The strictness IS the point: a dead key (the ollama_url class) must
    light up in the editor instead of being silently ignored."""
    yaml_path, inject_path = CONFIGS[filename]
    data = yaml.safe_load((REPO_ROOT / yaml_path).read_text(encoding="utf-8"))
    node = data
    for key in inject_path:
        node = node[key]
    if filename == "llm-config.schema.json":
        # inside the first model entry (an LLMModelConfig object)
        node = next(iter(node.values()))
    node["definitely_not_a_real_key"] = 1
    validator = Draft202012Validator(_schema_from_file(filename))
    errors = list(validator.iter_errors(data))
    assert errors, (
        f"an unknown key under {'/'.join(inject_path)} validated cleanly "
        f"against {filename} — strictness lost"
    )


def test_bare_skills_list_is_accepted_like_the_loader_accepts_it():
    """SkillsConfig turns a bare list into {"always": [...]} at load time
    (model_validator mode="before" — invisible to model_json_schema()). The
    schema must accept the same form, or the editor flags valid config."""
    data = yaml.safe_load(
        (REPO_ROOT / "config/plugins.yaml").read_text(encoding="utf-8"))
    server = next(iter(data["plugins"]["servers"].values()))
    server.setdefault("agent_config", {})["skills"] = ["house-style"]
    validator = Draft202012Validator(
        _schema_from_file("plugins-config.schema.json"))
    errors = ["/".join(map(str, e.absolute_path)) + ": " + e.message
              for e in validator.iter_errors(data)]
    assert errors == [], "\n".join(errors[:5])


def test_vscode_yaml_mappings_point_at_real_files():
    """The editor mapping is the schemas' only consumer, and it is the side
    no other test covers: a mapping onto a deleted/renamed schema (or a
    config that no longer exists) is exactly how mcp-config.schema.json
    rotted. Every side of every mapping must resolve."""
    settings = json.loads(
        (REPO_ROOT / ".vscode" / "settings.json").read_text(encoding="utf-8"))
    mappings = settings["yaml.schemas"]
    assert mappings, "fixture assertion: no yaml.schemas mappings at all"
    for schema_ref, targets in mappings.items():
        assert (REPO_ROOT / schema_ref).is_file(), (
            f"{schema_ref} is mapped in .vscode/settings.json but missing")
        for target in targets:
            assert list(REPO_ROOT.glob(target)), (
                f"mapping target {target!r} matches no file")


def test_schema_registry_covers_all_derived_schema_files():
    """Every *-config.schema.json in schemas/ that claims to be generated
    must be in the SCHEMAS registry (and thus under anti-drift)."""
    for path in SCHEMA_DIR.glob("*.schema.json"):
        text = path.read_text(encoding="utf-8")
        if "GENERATED from agent_system.config.models" in text:
            assert path.name in SCHEMAS, (
                f"{path.name} claims to be generated but is not in the "
                "SCHEMAS registry — it would silently drift"
            )
