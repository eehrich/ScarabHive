"""Kommt an, was in der Konfiguration steht?

Die Kette plugins.yaml -> Server -> Hook -> CompactionConfig bestand aus drei
handgefuehrten Listen, die alle denselben Schluessel nennen mussten. Fehlte er
in einer, verschwand der Wert wortlos: ``config_dict`` ist ein blankes Dict,
nichts validiert es. Gemessen an der ausgelieferten Konfiguration erreichten
5 von 25 Einstellungen den Plugin nie -- darunter eine Byte-Grenze, die jemand
bewusst auf 29 MB gesetzt hatte, um unter einem Provider-Limit zu bleiben, und
die mit 90 MB lief.

Diese Tests fahren die ECHTE config/plugins.yaml durch den ECHTEN Server. Ein
Test gegen selbstgebaute Werte wuerde die Luecke nicht sehen -- er wuerde
genau die Schluessel setzen, an die der Autor gerade denkt.
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
    """Was der Plugin am Ende WIRKLICH benutzt, nicht was er gelesen hat."""
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
    @pytest.mark.skipif(not PLUGINS_YAML.exists(), reason="kein config/plugins.yaml")
    def test_every_shipped_setting_reaches_the_plugin(self):
        """Der Test, der die Luecke gefunden haette."""
        shipped = _shipped_config()
        assert shipped, "der context_engineer-Block ist leer — Test waere gegenstandslos"

        srv = _server(shipped)
        effective = _effective(srv)

        ignored = []
        for key, want in shipped.items():
            # storage_path ist der einzige Wert, den die Fixture ueberschreibt.
            if key == "storage_path":
                continue
            got = effective.get(key)
            # Kein float(): 20000.0 == 20000 gilt ohnehin, und float() brach an
            # der ersten Zeichenkette ab -- ein Profilname in plugins.yaml haette
            # diesen Test umgeworfen, statt zu sagen, ob der Wert ankommt.
            if got is None or got != want:
                ignored.append(f"{key}: gesetzt {want!r}, wirksam {got!r}")

        assert not ignored, (
            "Werte aus config/plugins.yaml erreichen den Plugin nicht:\n  "
            + "\n  ".join(ignored))

    @pytest.mark.skipif(not PLUGINS_YAML.exists(), reason="kein config/plugins.yaml")
    def test_the_shipped_config_has_no_dead_keys(self):
        """Ein Schluessel mit falschem NAMEN landet nirgends.

        `semantic_search` stand monatelang auf true, waehrend der Code
        `enable_semantic_search` liest. Die Zuordnung ueber Feldnamen faengt
        das nicht -- ein falscher Name passt eben auf kein Feld.
        """
        dead = unknown_config_keys(_shipped_config())
        assert not dead, (
            f"diese Schluessel in config/plugins.yaml erreichen nichts: {dead}. "
            f"Erlaubt sind die Felder von CompactionConfig und {sorted(PLUGIN_LEVEL_KEYS)}")

    def test_the_schema_defaults_have_no_dead_keys(self):
        """Dasselbe fuer schema.yaml — dort lagen 11 Leichen aus einem
        frueheren Entwurf, die bei jedem Start eine Warnung ausgeloest haetten."""
        schema = yaml.safe_load((PLUGIN_DIR / "schema.yaml").read_text(encoding="utf-8"))
        dead = unknown_config_keys(schema.get("config", {}))
        assert not dead, f"tote Schluessel in schema.yaml: {dead}"

    def test_a_misspelled_key_is_reported(self):
        """Der Riegel selbst: unbekannte Schluessel muessen auffallen."""
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

        assert "sub_agent_cover_artist.yaml" in found, f"fixture: the scan found only {found}"
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
        """PLUGIN_LEVEL_KEYS ist eine Ausnahmeliste — sie darf nicht zur
        Muellhalde werden. Jeder Eintrag muss einen abweichenden Wert auch
        wirklich durchreichen, sonst waere er nur von der Warnung befreit."""
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
                f"{key} steht in PLUGIN_LEVEL_KEYS, wird aber nicht benutzt "
                f"(gesetzt {want}, wirksam {effective[key]})")
        # storage_path wird von der Fixture ueberschrieben; separat geprueft.
        assert "storage_path" in PLUGIN_LEVEL_KEYS


class TestPerAgentOverrides:
    def test_an_override_works_for_every_field(self):
        """Die ersetzte Handliste kannte drei Felder nicht (store_media_*,
        media_store_ttl_seconds, media_store_max_files) — ein Agent, der die
        setzte, wurde stillschweigend ignoriert."""
        srv = _server({})
        for f in fields(CompactionConfig):
            if f.type not in ("int", "float"):
                continue
            probe = int(getattr(CompactionConfig(), f.name)) + 17
            parts = srv._hooks_impl._get_session_components(
                f"ov-{f.name}", overrides={f.name: probe})
            got = getattr(parts["strategy"].config, f.name)
            assert got == probe, (
                f"per-Agent-Override fuer {f.name} wurde verschluckt "
                f"(gesetzt {probe}, wirksam {got})")


class TestSemanticSearchActuallyRuns:
    """Semantische Suche muss suchen, nicht nur teuer sein.

    ChromaDB antwortet PRO ANFRAGE: {"ids": [[id, ...]]}. Diese verschachtelte
    Form an SQLite zu binden gibt "Error binding parameter 1: type 'list' is
    not supported" — und der umschliessende except machte daraus einen stillen
    Rueckfall auf die Textsuche. Die semantische Suche lief also nie, waehrend
    jeder Archiv-Schreibvorgang ~78 ms Einbettung fuer einen Index bezahlte,
    den niemand las.

    Monatelang unsichtbar, weil der Config-Schluessel falsch geschrieben war
    (`semantic_search` statt `enable_semantic_search`) und die Funktion damit
    nie lief. Ein Schalter, der nichts einschaltet, verdeckt einen kaputten
    Motor.
    """

    @pytest.fixture
    def archive(self, tmp_path):
        from plugins.context_engineer.archival_memory import ArchivalMemory

        a = ArchivalMemory(tmp_path / "a.db", session_id="s",
                           enable_semantic_search=True,
                           vector_store_path=tmp_path / "v")
        if not a.enable_semantic_search:
            pytest.skip("kein Vektor-Backend verfuegbar")
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
        """Der Grafikchip-Satz teilt KEIN Wort mit der Anfrage.

        Genau daran haengt der Beweis: waere der Rueckfall auf die Textsuche
        noch aktiv, koennte dieser Treffer nicht auftauchen.
        """
        import logging

        with caplog.at_level(logging.ERROR):
            hits = archive.search("Hardware die Speicher bewegt",
                                  session_id="s", limit=2, use_semantic=True)

        assert not [r for r in caplog.records if "falling back to text" in r.message], (
            "die semantische Suche ist auf die Textsuche zurueckgefallen — "
            "sie laeuft also gar nicht")

        found = " ".join(m.content for m in hits)
        assert "Grafikchip" in found, (
            f"nur woertliche Treffer gefunden: {[m.content[:40] for m in hits]}")
        assert "Pferd" not in found and "Rechnung" not in found
