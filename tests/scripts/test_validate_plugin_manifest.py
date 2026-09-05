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


class TestTheShapesTheRuntimeSupports:
    """The validator refused three shapes the runtime has always run."""

    def test_every_shipped_plugin_validates(self):
        """The anti-drift guard, and the reason this file exists.

        Every finding this sweep produced on its first run was a validator or
        schema that had fallen behind the runtime -- a manifest key the code
        reads and the schema does not know (``lazy``, ``provides``,
        ``default_base_url``), a plugin type it never heard of (``library``,
        ``llm-provider``), a hook type missing from a hardcoded copy of the
        enum. None of them were broken plugins.

        Measured 2026-09-05: 73 plugins, 0.9 s.
        """
        roots = [REPO / "src" / d for d in
                 ("plugins", "plugins_writer", "plugins_trading", "plugins_llm")]
        plugins = [p for root in roots if root.is_dir()
                   for p in sorted(root.iterdir())
                   if p.is_dir() and (p / "plugin.toml").exists()]

        assert len(plugins) > 60, f"only {len(plugins)} plugins found -- wrong roots?"
        broken = {p.name: _validate(p).errors for p in plugins if _validate(p).errors}

        assert broken == {}, broken

    def test_a_library_plugin_needs_no_entrypoint_module(self):
        """`coder`, `amiga`, `research`, `writer_publish`: agents, skills and
        prompts, no code. The runtime discovers them; this refused them for a
        missing plugin.py."""
        validator = _validate(REPO / "src" / "plugins" / "coder")

        assert validator.errors == []

    def test_an_mcp_plugin_without_an_entrypoint_module_is_still_refused(self, plugin_copy):
        """The other way, or the exception above would be a hole: a plugin
        that DOES claim an entrypoint has to have one.

        Caught by _validate_entrypoint, against the module the manifest names.
        The file check used to ask the same question less precisely ("is there
        a plugin.py OR a server.py") and aborted the run on the answer, so a
        plugin got one finding instead of all of them."""
        (plugin_copy / "plugin.py").unlink()

        errors = _validate(plugin_copy).errors

        assert [e for e in errors if "entrypoint" in e.lower()], errors

    def test_a_code_plugin_without_an_entrypoint_key_is_refused(self, plugin_copy):
        """`entrypoint` left the schema's `required` list, because a library
        plugin and an llm-provider have none. This branch is what carries the
        requirement for everybody else now -- the only thing left that catches
        a manifest without one."""
        manifest = plugin_copy / "plugin.toml"
        kept = [line for line in manifest.read_text(encoding="utf-8").splitlines()
                if not line.startswith("entrypoint")]
        manifest.write_text(chr(10).join(kept) + chr(10), encoding="utf-8")

        errors = _validate(plugin_copy).errors

        assert [e for e in errors if "entrypoint" in e.lower()], errors

    def test_a_conditional_schema_is_rendered_not_stripped(self):
        """tavily_search offers no tools without an API key and a block
        sequence with one. Removing the `{% ... %}` lines leaves both standing,
        which is not YAML -- the runtime renders the template instead."""
        validator = _validate(REPO / "src" / "plugins" / "tavily_search")

        assert validator.errors == []
        assert validator.schema_yaml is not None, "the schema did not load at all"


class TestTheHookVocabulary:
    """The valid hook types were a hand-copied list that had fallen behind."""

    @pytest.fixture
    def hook_plugin(self, tmp_path):
        target = tmp_path / "hook_probe"
        shutil.copytree(REPO / "src" / "plugins" / "simple_prompt_inject", target,
                        ignore=shutil.ignore_patterns("__pycache__", "tests"))
        return target

    def _with_hook_type(self, plugin_dir: Path, hook_type: str) -> list[str]:
        """Always from the PRISTINE text: patching the file in place worked
        once and then silently did nothing, because the marker it looks for
        was already replaced -- a loop over nine types that measured one."""
        schema = plugin_dir / "schema.yaml"
        pristine = (REPO / "src" / "plugins" / "simple_prompt_inject"
                    / "schema.yaml").read_text(encoding="utf-8")
        assert "type: PRE_LLM_CALL" in pristine, "fixture: the marker is gone from the shipped schema"
        schema.write_text(pristine.replace("type: PRE_LLM_CALL", f"type: {hook_type}"),
                          encoding="utf-8")
        return [e for e in _validate(plugin_dir).errors if "invalid type" in e]

    def test_every_type_the_runtime_knows_is_accepted(self, hook_plugin):
        """From the enum, not from a copy. ``pre_llm_request`` and
        ``post_llm_response`` were missing, which made message_debugger -- a
        shipped, working plugin -- look broken."""
        from agent_system.hooks.plugin_hook import HookType

        rejected = {h.value: self._with_hook_type(hook_plugin, h.value)
                    for h in HookType}

        assert not any(rejected.values()), rejected

    def test_the_case_does_not_matter(self, hook_plugin):
        """SchemaBasedHookPlugin dispatches on ``hook["type"].upper()``, and
        its own docstring writes the type upper case. Both spellings reach the
        same hook, so both have to validate."""
        assert self._with_hook_type(hook_plugin, "PRE_LLM_CALL") == []

    def test_a_type_the_runtime_does_not_know_is_still_rejected(self, hook_plugin):
        """The other way: the list is derived, not abandoned."""
        assert self._with_hook_type(hook_plugin, "pre_llm_teatime") != []
