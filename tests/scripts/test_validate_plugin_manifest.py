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
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from scripts.validate_plugin import (
    PluginValidator,
    find_plugin_directories,
    plugin_roots,
)

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

        The list comes from the SHIPPED collection path, not from a scan this
        test writes itself. The hand-rolled version was why this guard could
        not see that ``find_plugin_directories`` still demanded ``plugin.yaml``
        long after ``_check_file_structure`` had stopped: the sweep was green
        on 73 plugins while ``--all`` was finding 0.
        """
        plugins = find_plugin_directories(plugin_roots(REPO))

        assert len(plugins) > 60, f"only {len(plugins)} plugins found -- wrong roots?"
        broken = {p.name: _validate(p).errors for p in plugins if _validate(p).errors}

        assert broken == {}, broken

    def test_the_collection_path_finds_every_manifest_in_the_tree(self):
        """What the sweep above stands on, checked against the filesystem.

        Two things drifted apart here before: WHICH file marks a plugin
        (``plugin.toml`` won, the collector still asked for ``plugin.yaml``),
        and WHICH roots are searched (``--all`` named two of four, so
        plugins_trading and plugins_llm were unreachable).
        """
        found = {p.resolve() for p in find_plugin_directories(plugin_roots(REPO))}
        # The other side is derived from the FILESYSTEM, never from
        # ``plugin_roots`` -- the first version of this test asked the same
        # constant on both sides, so dropping two roots shrank both and the
        # mutation stayed green. Anything under src/ holding a plugin.toml is
        # a plugin, whatever its parent is called.
        on_disk = {
            manifest.parent.resolve()
            for manifest in (REPO / "src").glob("*/*/plugin.toml")
        }

        assert found == on_disk, {
            "missed": sorted(p.name for p in on_disk - found),
            "invented": sorted(p.name for p in found - on_disk),
        }
        assert len(on_disk) > 60, f"fixture: only {len(on_disk)} plugins on disk"

    def test_the_all_flag_reports_what_it_checked(self):
        """The production path, end to end, through argv.

        ``--all`` is the documented collective form -- pre-commit hook, CI
        step and make target all call it. It printed nothing and exited 0
        while checking zero plugins, which is the exact failure this whole
        file exists to prevent, one function deeper. A count in the output is
        what tells "all green" from "nothing looked at".
        """
        result = subprocess.run(
            [sys.executable, str(REPO / "src" / "scripts" / "validate_plugin.py"), "--all"],
            capture_output=True, text=True, cwd=REPO, timeout=900,
        )
        output = result.stdout + result.stderr

        match = re.search(r"Total:\s*(\d+)\s+plugins", output)
        assert match, f"--all named no total:\n{output[-2000:]}"
        assert int(match.group(1)) > 60, f"--all only checked {match.group(1)}"
        assert result.returncode == 0, output[-2000:]

    #: What ``validate_all_tool_schemas.py`` takes for a plugin: a directory one
    #: level under a root carrying EITHER marker. Derived from the filesystem,
    #: never from ``plugin_roots`` -- asking the same constant on both sides is
    #: how the sibling test below stayed green through a wrong root list.
    def _plugins_on_disk(self) -> set[Path]:
        found = {
            child.resolve()
            for child in (REPO / "src").glob("*/*")
            if child.is_dir()
            and ((child / "schema.yaml").exists() or (child / "plugin.toml").exists())
        }
        assert len(found) > 60, f"fixture: only {len(found)} plugins on disk"
        return found

    def test_the_sibling_validator_walks_the_same_roots(self):
        """``validate_all_tool_schemas.py`` kept its own copy of the root list.

        That copy named two roots while this one named four, so 9 plugin
        directories were out of reach -- the same drift, one script over. It
        imports ``plugin_roots`` now; what this pins is WHICH directories come
        back, not how many, so swapping a root for another of equal size is
        red too.
        """
        from scripts.validate_all_tool_schemas import (
            find_plugin_directories as collect,
        )

        found = {p.resolve() for p in collect(plugin_roots(REPO))}

        assert found == self._plugins_on_disk(), {
            "missed": sorted(p.name for p in self._plugins_on_disk() - found),
            "invented": sorted(p.name for p in found - self._plugins_on_disk()),
        }

    def test_the_sibling_validator_reports_what_it_checked(self):
        """The same script through argv, because that is how anyone runs it.

        "Plugins validated: 64" reads like a complete sweep, and that is what
        it printed while nine were unreachable. The number has to match the
        tree, or it is decoration.
        """
        result = subprocess.run(
            [sys.executable,
             str(REPO / "src" / "scripts" / "validate_all_tool_schemas.py")],
            capture_output=True, text=True, cwd=REPO, timeout=120,
        )
        output = result.stdout + result.stderr

        match = re.search(r"Plugins validated:\s*(\d+)", output)
        assert match, f"the validator named no count:\n{output[-2000:]}"
        assert int(match.group(1)) == len(self._plugins_on_disk()), output[-2000:]
        assert result.returncode == 0, output[-2000:]

    def test_a_tool_behind_a_template_flag_is_still_validated(self):
        """One render is one branch, and a branch can hold whole tools.

        tavily_search declares ``tools: []`` without an API key and two tools
        with one; web_scraper hides ``download_file`` the same way. Rendered
        once, those three sit in no document any check ever sees -- while the
        regex this replaced kept both branches at once and made the file
        unparseable instead. Neither is a check.
        """
        from agent_system.plugins.schema_loader import load_schema_from_dir
        from scripts.validate_all_tool_schemas import ToolSchemaValidator

        plugin = REPO / "src" / "plugins" / "tavily_search"
        one_branch = load_schema_from_dir(plugin, {"name": plugin.name})
        assert not (one_branch.get("tools") or []), (
            "fixture: this schema no longer hides its tools behind a flag -- "
            "the test would pass without rendering the other branch at all")

        validator = ToolSchemaValidator()

        assert validator.validate_plugin(plugin), validator.errors
        assert len(validator.all_schemas) >= 2, "the hidden branch was never rendered"

        # The other end of the same mechanism: where a plugin variable sits
        # INSIDE a tool, the two renders describe the same tool in different
        # words. Keyed by content instead of by name, six plugins' tools were
        # counted twice (170 reported for 164 in the tree).
        second = ToolSchemaValidator()
        subject = REPO / "src" / "plugins" / "basic_agent"
        renders = [
            {(t.get("function") or {}).get("name") or t.get("name"):
             json.dumps(t, sort_keys=True, default=str)
             for t in ((doc or {}).get("tools") or [])}
            for doc in ToolSchemaValidator()._render(subject)
        ]
        assert len(renders) == 2, "fixture: this schema takes no plugin variables"
        assert any(renders[0][n] != renders[1][n]
                   for n in set(renders[0]) & set(renders[1])), (
            "fixture: both renders describe every tool identically -- the "
            "duplicate this guards against could not arise here")

        assert second.validate_plugin(subject), second.errors
        names = [(t.get("function") or {}).get("name") or t.get("name")
                 for _, t, _ in second.all_schemas]

        assert len(names) == len(set(names)), names

    def test_a_schema_the_stand_in_cannot_serve_is_not_called_broken(self, tmp_path):
        """The stand-in has ONE shape, and a schema may want another.

        Here the flag guards a loop over pairs, which the filled-in list cannot
        provide -- the second render dies. The plugin's own default render still
        has to stand: reporting a healthy schema as unparseable is exactly the
        false alarm this change removed from tavily_search, and re-earning it
        one level up would be no progress.
        """
        from scripts.validate_all_tool_schemas import ToolSchemaValidator

        plugin = tmp_path / "probe"
        plugin.mkdir()
        (plugin / "schema.yaml").write_text(
            "tools:\n"
            "  - type: function\n"
            "    function:\n"
            '      name: "{{ name }}_probe"\n'
            '      description: "{% if pairs %}{% for k, v in pairs %}{{ k }}'
            '{% endfor %}{% endif %}"\n'
            "      parameters:\n"
            "        type: object\n",
            encoding="utf-8")

        validator = ToolSchemaValidator()

        assert validator.validate_plugin(plugin), validator.errors
        assert len(validator.all_schemas) == 1, validator.all_schemas
        assert [n for n in validator.one_state if n.startswith("probe")], \
            f"the failed second render was not reported: {validator.one_state}"

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


class TestToolMethodRouting:
    """Every tool routes to a method (``SchemaBasedToolMixin._get_method_name``)
    that has to exist. This was a script of its own that walked two plugin
    roots of four, stripped the Jinja with a regex (tavily_search "broken") and
    looked only into server.py (writer_issues keeps its handler in
    repair_pipeline.py) -- both findings false alarms."""

    TOOL = ("  - type: function\n"
            "    function:\n"
            '      name: "{tool}"\n'
            "      parameters:\n"
            "        type: object\n")

    def _plugin(self, tmp_path, tools, files):
        plugin = tmp_path / "probe"
        plugin.mkdir()
        (plugin / "schema.yaml").write_text(
            "tools:\n" + "".join(self.TOOL.format(tool=t) for t in tools),
            encoding="utf-8")
        for name, body in files.items():
            (plugin / name).write_text(body, encoding="utf-8")
        return plugin

    def _warnings(self, plugin):
        from scripts.validate_all_tool_schemas import ToolSchemaValidator
        validator = ToolSchemaValidator()
        assert validator.validate_plugin(plugin), validator.errors
        return validator.warnings

    def test_a_tool_without_its_method_is_reported(self, tmp_path):
        plugin = self._plugin(tmp_path, ["{{ name }}_list", "{{ name }}"], {
            "server.py": "class S:\n    async def tail(self, params): ...\n"})

        warnings = self._warnings(plugin)

        assert [w for w in warnings if "'list()'" in w], warnings
        assert [w for w in warnings if "'execute()'" in w], warnings

    def test_a_handler_in_another_module_counts(self, tmp_path):
        plugin = self._plugin(tmp_path, ["{{ name }}_execute_task", "search"], {
            "server.py": "class S(Mixin):\n    pass\n",
            "repair_pipeline.py": ("class Mixin:\n"
                                   "    async def execute_task(self, params): ...\n"
                                   "    def search(self, params): ...\n")})

        assert self._warnings(plugin) == []

    def test_a_wrapper_that_hands_on_is_still_checked(self, tmp_path):
        plugin = self._plugin(tmp_path, ["{{ name }}_list"], {
            "plugin.py": ("class P:\n"
                          "    async def call(self, tool, params):\n"
                          "        return await self.server.call(tool, params)\n")})

        assert self._warnings(plugin) != []

    def test_a_plugin_that_dispatches_itself_is_skipped(self, tmp_path):
        plugin = self._plugin(tmp_path, ["{{ name }}_list"], {
            "mcp_server.py": ("class S:\n"
                              "    async def call(self, tool, params):\n"
                              "        if tool.endswith('list'):\n"
                              "            return await self._list_log_files(params)\n")})

        assert self._warnings(plugin) == []

    def test_a_plugin_with_its_own_method_name_rule_is_skipped(self, tmp_path):
        plugin = self._plugin(tmp_path, ["{{ name }}_list"], {
            "server.py": ("class S:\n"
                          "    def _get_method_name(self, tool):\n"
                          "        return 'handle'\n"
                          "    async def handle(self, params): ...\n")})

        assert self._warnings(plugin) == []

    def test_neither_tests_nor_nested_functions_supply_a_handler(self, tmp_path):
        plugin = self._plugin(tmp_path, ["{{ name }}_list", "{{ name }}_tail"], {
            "server.py": ("def build():\n"
                          "    async def call(req):\n"
                          "        return {}\n"
                          "    def tail(params): ...\n")})
        (plugin / "tests").mkdir()
        (plugin / "tests" / "test_probe.py").write_text(
            "class Fake:\n    async def list(self, params): ...\n", encoding="utf-8")

        warnings = self._warnings(plugin)

        assert [w for w in warnings if "'list()'" in w], warnings
        assert [w for w in warnings if "'tail()'" in w], warnings

    def test_the_shipped_plugins_route_every_tool(self):
        from scripts import validate_all_tool_schemas as script

        validator = script.ToolSchemaValidator()
        for plugin in script.find_plugin_directories(plugin_roots(REPO)):
            validator.validate_plugin(plugin)

        # Counted where the lookup happens: "0 warnings" must not mean "every
        # plugin was skipped".
        assert validator.methods_checked > len(validator.all_schemas) // 2, (
            f"only {validator.methods_checked} of {len(validator.all_schemas)} "
            f"tools were checked; skipped: {validator.own_routing}")
        assert validator.warnings == []


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
