"""``validate_plugin.py`` on a tree that no longer has a single plugin.yaml.

The validator demanded ``plugin.yaml`` as a required file and returned at the
first check. Measured 2026-09-05: 73 ``plugin.toml`` in the tree and 0
``plugin.yaml``, so every plugin was refused with "Missing required file" and
nothing behind that line -- the JSON schema, the hook types, the schema.yaml
cross-checks -- had run in a long time.

What these tests pin is the reachability, not the rules: a toml-only plugin has
to get PAST the file check, far enough for the manifest schema to judge it.
"""
from __future__ import annotations

import contextlib
import io
import shutil
from pathlib import Path

import pytest

from scripts.validate_plugin import PluginValidator

REPO = Path(__file__).resolve().parents[2]
SCHEMAS = REPO / "schemas"
SHIPPED = REPO / "src" / "plugins" / "basic_agent"


def _validate(plugin_dir: Path) -> PluginValidator:
    validator = PluginValidator(plugin_dir, SCHEMAS)
    with contextlib.redirect_stdout(io.StringIO()):
        validator.validate()
    return validator


@pytest.fixture
def plugin_copy(tmp_path):
    """A real shipped plugin, copied so the manifest can be edited."""
    target = tmp_path / "probe_plugin"
    shutil.copytree(SHIPPED, target,
                    ignore=shutil.ignore_patterns("__pycache__", "tests"))
    assert (target / "plugin.toml").is_file(), "fixture: no manifest to validate"
    assert not (target / "plugin.yaml").exists(), "fixture: this must be toml-only"
    return target


def test_a_toml_only_plugin_passes(plugin_copy):
    """The shipped plugin is valid -- so a failure here is the validator's."""
    validator = _validate(plugin_copy)

    assert validator.errors == []


def test_the_manifest_schema_is_actually_applied(plugin_copy):
    """Reachability, measured through the one thing behind the file check that
    cannot be mistaken for something else: the schema forbids unknown keys.

    Without this the validator's remaining ~400 lines are unreachable code, and
    the manifest schema is a file nobody consults.
    """
    manifest = plugin_copy / "plugin.toml"
    manifest.write_text(manifest.read_text(encoding="utf-8") + '\nnonsense_key = 1\n',
                        encoding="utf-8")

    validator = _validate(plugin_copy)

    assert [e for e in validator.errors if "nonsense_key" in e], validator.errors


def test_the_lazy_key_is_known_to_the_schema(plugin_copy):
    """``lazy = true`` is what the runtime reads to allow building a server on
    first use. The schema forbids unknown keys, so a manifest key the runtime
    honours and the schema does not is a plugin the validator refuses."""
    manifest = plugin_copy / "plugin.toml"
    assert "lazy = true" in manifest.read_text(encoding="utf-8"), \
        "fixture: the shipped manifest no longer declares lazy"

    assert _validate(plugin_copy).errors == []


def test_a_plugin_without_any_manifest_is_still_refused(plugin_copy):
    """The other way: dropping the file check would let a plugin through that
    has nothing to validate."""
    (plugin_copy / "plugin.toml").unlink()

    validator = _validate(plugin_copy)

    assert [e for e in validator.errors if "Missing required file" in e], validator.errors
