"""Does what the configuration says actually arrive?

The chain plugins.yaml -> server -> hook -> CompactionConfig consisted of
three hand-maintained lists that all had to name the same key. If it was
missing from one, the value vanished silently: ``config_dict`` is a bare
dict, nothing validates it. Measured against the shipped configuration, 5 of
25 settings never reached the plugin -- among them a byte limit that someone
had deliberately set to 29 MB to stay under a provider limit, and which ran
at 90 MB.

These tests run the REAL config/plugins.yaml through the REAL server. A test
against hand-built values would not see the gap -- it would set exactly the
keys the author happens to be thinking of.
"""

from __future__ import annotations

import tempfile
from dataclasses import fields
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from plugins.context_engineer.compaction import (
    PLUGIN_LEVEL_KEYS,
    CompactionConfig,
    unknown_config_keys,
)
from plugins.context_engineer.server import ContextEngineerServer

PLUGINS_YAML = Path("config/plugins.yaml")
PLUGIN_DIR = Path(__file__).parent.parent


def _shipped_config() -> dict:
    data = yaml.safe_load(PLUGINS_YAML.read_text(encoding="utf-8"))
    return data["plugins"]["servers"]["context_engineer"]["config"]


def _server(config: dict) -> ContextEngineerServer:
    mcp = SimpleNamespace(config=dict(config), hook_config={},
                          name="context_engineer")
    srv = ContextEngineerServer("context_engineer", SimpleNamespace(), mcp)
    srv._hooks_impl._storage_base = Path(tempfile.mkdtemp())
    return srv


def _effective(srv: ContextEngineerServer, session: str = "probe") -> dict:
    """What the plugin ends up REALLY using, not what it read."""
    parts = srv._hooks_impl._get_session_components(session)
    cfg = parts["strategy"].config
    values = {f.name: getattr(cfg, f.name) for f in fields(CompactionConfig)}
    values["enable_semantic_search"] = srv._hooks_impl.enable_semantic_search
    values["core_memory_max_tokens"] = parts["core_memory"].max_tokens
    values["session_ttl_seconds"] = srv._hooks_impl._session_ttl_seconds
    values["max_tracked_sessions"] = srv._hooks_impl._max_tracked_sessions
    values["storage_path"] = str(srv._hooks_impl._storage_base)
    values["session_data_ttl_days"] = srv._hooks_impl._session_data_ttl_days
    return values


class TestShippedConfigArrives:
    @pytest.mark.skipif(not PLUGINS_YAML.exists(), reason="no config/plugins.yaml")
    def test_every_shipped_setting_reaches_the_plugin(self):
        """The test that would have found the gap."""
        shipped = _shipped_config()
        assert shipped, "the context_engineer block is empty -- the test would be pointless"

        srv = _server(shipped)
        effective = _effective(srv)

        ignored = []
        for key, want in shipped.items():
            # storage_path is the only value the fixture overrides.
            if key == "storage_path":
                continue
            got = effective.get(key)
            # No float(): 20000.0 == 20000 holds anyway, and float() choked on
            # the first string -- a profile name in plugins.yaml would have
            # broken this test instead of saying whether the value arrives.
            if got is None or got != want:
                ignored.append(f"{key}: set {want!r}, effective {got!r}")

        assert not ignored, (
            "Values from config/plugins.yaml do not reach the plugin:\n  "
            + "\n  ".join(ignored))

    @pytest.mark.skipif(not PLUGINS_YAML.exists(), reason="no config/plugins.yaml")
    def test_the_shipped_config_has_no_dead_keys(self):
        """A key with the wrong NAME lands nowhere.

        `semantic_search` was set to true for months while the code reads
        `enable_semantic_search`. Mapping by field name does not catch that --
        a wrong name simply matches no field.
        """
        dead = unknown_config_keys(_shipped_config())
        assert not dead, (
            f"these keys in config/plugins.yaml reach nothing: {dead}. "
            f"Allowed are the fields of CompactionConfig and {sorted(PLUGIN_LEVEL_KEYS)}")

    def test_the_schema_defaults_have_no_dead_keys(self):
        """The same for schema.yaml -- it held 11 corpses from an earlier
        draft that would have triggered a warning on every start."""
        schema = yaml.safe_load((PLUGIN_DIR / "schema.yaml").read_text(encoding="utf-8"))
        dead = unknown_config_keys(schema.get("config", {}))
        assert not dead, f"dead keys in schema.yaml: {dead}"

    def test_a_misspelled_key_is_reported(self):
        """The guard itself: unknown keys must stand out."""
        assert unknown_config_keys({"semantic_search": True}) == ["semantic_search"]
        assert unknown_config_keys({"enable_semantic_search": True}) == []
        assert unknown_config_keys({"max_request_bytes": 1}) == []

    def test_agent_overrides_have_no_dead_keys(self):
        """The same for every agent's hooks.overrides block: mapped by field
        name, a misspelled override key reached nothing and said nothing."""
        def blocks(node):
            if isinstance(node, dict):
                for key, value in node.items():
                    if key == "context_engineer.engineer_context" and isinstance(value, dict):
                        yield value
                    else:
                        yield from blocks(value)
            elif isinstance(node, list):
                for value in node:
                    yield from blocks(value)

        found, dead = [], []
        for path in [*Path("config").rglob("*.yaml"), *Path("src").rglob("*.yaml")]:
            text = path.read_text(encoding="utf-8", errors="replace")
            if "context_engineer.engineer_context" not in text:
                continue
            try:
                data = yaml.safe_load(text)
            except yaml.YAMLError:
                continue
            for block in blocks(data):
                found.append(path.name)
                keys = set(block) - {"enabled", "timeout", "order"}
                # Plugin-level keys are known to plugins.yaml but dropped per agent.
                dead += [f"{path}: {key}" for key in unknown_config_keys(keys) + sorted(keys & PLUGIN_LEVEL_KEYS)]

        # One anchor per scanned root (config/, src/), both public, so they hold with and without the writer.
        assert {"context_browse_test_agent.yaml", "coder.yaml"} <= set(found), f"fixture: the scan found only {found}"
        assert not dead, f"override keys that reach no setting: {dead}"

    def test_a_misspelled_agent_override_is_logged(self, caplog):
        from plugins.context_engineer.hooks import ContextEngineerPlugin

        plugin = ContextEngineerPlugin(PLUGIN_DIR)
        with caplog.at_level("WARNING"):
            config = plugin._compaction_config({"always_compact_media_headrom": 2})

        assert config.always_compact_media_headroom == 0
        assert "always_compact_media_headrom" in caplog.text

    def test_a_plugin_level_key_in_an_agent_override_is_logged(self, caplog):
        from plugins.context_engineer.hooks import ContextEngineerPlugin

        key = sorted(PLUGIN_LEVEL_KEYS)[0]
        with caplog.at_level("WARNING"):
            ContextEngineerPlugin(PLUGIN_DIR)._compaction_config({key: True})

        assert key in caplog.text and "only plugins.yaml" in caplog.text

    def test_plugin_level_keys_are_actually_consumed(self):
        """PLUGIN_LEVEL_KEYS is an exception list -- it must not become a
        dumping ground. Every entry must actually pass a differing value
        through, otherwise it would merely be exempt from the warning."""
        probe = {
            "session_ttl_seconds": 1234,
            "max_tracked_sessions": 7,
            "enable_semantic_search": False,
            "core_memory_max_tokens": 4321,
            "session_data_ttl_days": 3,
        }
        srv = _server(probe)
        effective = _effective(srv)
        for key, want in probe.items():
            assert effective[key] == want, (
                f"{key} is in PLUGIN_LEVEL_KEYS but is not used "
                f"(set {want}, effective {effective[key]})")
        # storage_path is overridden by the fixture; checked separately.
        assert "storage_path" in PLUGIN_LEVEL_KEYS


class TestPerAgentOverrides:
    def test_an_override_works_for_every_field(self):
        """The replaced hand-kept list did not know three fields (store_media_*,
        media_store_ttl_seconds, media_store_max_files) -- an agent that
        set them was silently ignored."""
        srv = _server({})
        for f in fields(CompactionConfig):
            if f.type not in ("int", "float"):
                continue
            probe = int(getattr(CompactionConfig(), f.name)) + 17
            parts = srv._hooks_impl._get_session_components(
                f"ov-{f.name}", overrides={f.name: probe})
            got = getattr(parts["strategy"].config, f.name)
            assert got == probe, (
                f"per-agent override for {f.name} was swallowed "
                f"(set {probe}, effective {got})")


class TestSemanticSearchActuallyRuns:
    """Semantic search must actually search, not merely be expensive.

    ChromaDB answers PER QUERY: {"ids": [[id, ...]]}. Binding this nested
    shape to SQLite gives "Error binding parameter 1: type 'list' is not
    supported" -- and the surrounding except turned that into a silent
    fallback to text search. So semantic search never ran, while every
    archive write paid ~78 ms of embedding for an index nobody read.

    Invisible for months because the config key was misspelled
    (`semantic_search` instead of `enable_semantic_search`), so the feature
    never ran. A switch that turns nothing on hides a broken engine.
    """

    @pytest.fixture
    def archive(self, tmp_path):
        from plugins.context_engineer.archival_memory import ArchivalMemory

        a = ArchivalMemory(tmp_path / "a.db", session_id="s",
                           enable_semantic_search=True,
                           vector_store_path=tmp_path / "v")
        if not a.enable_semantic_search:
            pytest.skip("no vector backend available")
        for text in (
            "Der Blitter kopiert Speicherbloecke ohne die CPU zu belasten.",
            "Kapitel 3 handelt von der Reise durch die Wueste.",
            "Die Rechnung ueber 4711 Euro wurde am Dienstag bezahlt.",
            "Ein Grafikchip verschiebt Bilddaten parallel zum Hauptprozessor.",
            "Das Pferd stand am Brunnen und trank.",
        ):
            a.store({"role": "assistant", "content": text}, session_id="s")
        return a

    def test_it_finds_by_meaning_not_by_word(self, archive, caplog):
        """The graphics-chip sentence shares NO word with the query.

        The proof hangs on exactly that: were the fallback to text search
        still active, this hit could not show up.
        """
        import logging

        with caplog.at_level(logging.ERROR):
            hits = archive.search("Hardware die Speicher bewegt",
                                  session_id="s", limit=2, use_semantic=True)

        assert not [r for r in caplog.records if "falling back to text" in r.message], (
            "semantic search fell back to text search -- "
            "so it is not running at all")

        found = " ".join(m.content for m in hits)
        assert "Grafikchip" in found, (
            f"only literal hits found: {[m.content[:40] for m in hits]}")
        assert "Pferd" not in found and "Rechnung" not in found
