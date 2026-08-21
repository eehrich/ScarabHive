"""Zwei Zusicherungen gegen still verschluckte Konfiguration.

Beide Prüfungen fanden je zwei Defekte, die keiner der bestehenden Tests
bemerkt hat — weil ein verschluckter Wert nichts kaputt macht, das ein Test
ansieht: die Anwendung startet, die Tabelle ist da, nur der Schalter fehlt.

**Doppelte YAML-Schlüssel.** ``yaml.safe_load`` behält wortlos den letzten.
So verschwand ``mode: pipeline`` aus ``repair_pipeline.yaml`` (der Agent kam
als reiner Tool-Server hoch statt als Orchestrator), ein kompletter
MCP-Server aus ``mcp_servers.yaml`` (sein Block landete in ``localhost`` und
überschrieb dessen URL), und eine Parameterbeschreibung aus
``json_store/schema.yaml``.

**Profilreferenzen ins Leere.** Ein Profil, das es nicht gibt, ist kein
Fehler beim Laden — es fällt erst zur Laufzeit auf, und als Fallback-Glied
womöglich nie. So blieb ``or-gmini-flash-unlimited`` (ein fehlendes „e")
unbemerkt und nahm zwei Panel-Agenten ihren Fallback.

Beide Tests sichern ihre GRUNDMENGE ab: eine leere Menge erfüllt jede
All-Aussage, ein Scanner der nichts mehr findet wäre sonst von einem
funktionierenden nicht zu unterscheiden.
"""
from __future__ import annotations

import logging
import sys
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).parents[2]
sys.path.insert(0, str(REPO_ROOT / "src"))

#: Wo Konfiguration lebt. `docs` ist ausgenommen — dort stehen Beispiele.
SCAN_ROOTS = ("config", "src")

#: Verzeichnisse, die nie Betriebs-Konfiguration enthalten.
SKIP_PARTS = frozenset({"docs", "node_modules", "__pycache__", ".venv"})


class _DuplicateKeyLoader(yaml.SafeLoader):
    """SafeLoader, der doppelte Mapping-Schluessel meldet statt sie zu schlucken."""


def _mapping_without_duplicates(loader, node, deep=False):
    seen: dict = {}
    for key_node, _ in node.value:
        key = loader.construct_object(key_node, deep=deep)
        try:
            hash(key)
        except TypeError:
            # YAML erlaubt zusammengesetzte Schluessel (`? {a: b}`). Selten,
            # aber sie kollidieren genauso — ueber ihre Darstellung vergleichen.
            key = repr(key)
        if key in seen:
            raise ValueError(
                f"doppelter Schluessel {key!r} in Zeile "
                f"{key_node.start_mark.line + 1} (zuerst in Zeile {seen[key]})")
        seen[key] = key_node.start_mark.line + 1
    return yaml.SafeLoader.construct_mapping(loader, node, deep)


_DuplicateKeyLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _mapping_without_duplicates)


def _config_files() -> list[Path]:
    out = []
    for root in SCAN_ROOTS:
        for path in (REPO_ROOT / root).rglob("*.y*ml"):
            if path.is_file() and not SKIP_PARTS & set(path.parts):
                out.append(path)
    return sorted(out)


def test_no_yaml_file_has_a_duplicate_key():
    files = _config_files()
    assert len(files) >= 100, (
        f"nur {len(files)} yaml-Dateien gefunden — der Scanner sieht den Baum "
        f"nicht mehr, der Test waere gegenstandslos")

    duplicates, parsed = [], 0
    for path in files:
        try:
            yaml.load(path.read_text(encoding="utf-8"), Loader=_DuplicateKeyLoader)
            parsed += 1
        except ValueError as exc:
            duplicates.append(f"{path.relative_to(REPO_ROOT)}: {exc}")
        except yaml.YAMLError:
            # Nicht jede .yaml im Baum ist gueltiges YAML (Fixtures, Vorlagen
            # mit Platzhaltern). Die zaehlen nicht als geprueft.
            pass

    assert parsed >= 100, (
        f"nur {parsed} von {len(files)} Dateien liessen sich parsen — "
        f"der Test misst nicht mehr, was er soll")
    assert not duplicates, (
        "doppelte YAML-Schluessel — der letzte gewinnt, der erste ist "
        "wortlos weg:\n  " + "\n  ".join(duplicates))


def _profile_references(node, where: str, out: list[tuple[str, str]]) -> None:
    """Jede Stelle einsammeln, die einen llm_profile-NAMEN nennt.

    Nur Strings und String-Listen zaehlen. In den ``schema.yaml`` der Plugins
    ist ``llm_profile`` ein deklariertes Config-FELD — sein Wert ist dort ein
    Mapping (``type``/``default``/``enum``), kein Profilname.
    """
    if isinstance(node, dict):
        for key, value in node.items():
            if key in ("llm_profile", "llm_profile_advanced",
                       "llm_profile_escalation"):
                if isinstance(value, str):
                    out.append((value, f"{where}.{key}"))
                elif isinstance(value, list):
                    out.extend((v, f"{where}.{key}")
                               for v in value if isinstance(v, str))
                else:
                    _profile_references(value, f"{where}.{key}", out)
            elif key == "llm_params" and isinstance(value, dict):
                # Profil-gekeyte Overrides: der SCHLUESSEL ist der Profilname.
                out.extend((k, f"{where}.llm_params[{k!r}]")
                           for k in value if k != "*")
            else:
                _profile_references(value, f"{where}.{key}", out)
    elif isinstance(node, list):
        for i, value in enumerate(node):
            _profile_references(value, f"{where}[{i}]", out)


def test_every_profile_reference_resolves():
    from agent_system.config.settings import load_settings

    known = set(load_settings().llm_system.profiles)
    assert len(known) >= 20, (
        f"nur {len(known)} Profile geladen — die echte Konfiguration kam "
        f"nicht an, der Test waere gegenstandslos")

    references: list[tuple[str, str]] = []
    for path in _config_files():
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8"))
        except yaml.YAMLError:
            continue
        _profile_references(data, str(path.relative_to(REPO_ROOT)), references)

    assert len(references) >= 200, (
        f"nur {len(references)} Profilreferenzen gefunden — der Sammler "
        f"greift nicht mehr, der Test waere gegenstandslos")

    dangling = sorted({(name, where) for name, where in references
                       if name not in known})
    assert not dangling, (
        "diese Profilnamen gibt es nicht (Tippfehler oder umbenannt) — als "
        "Fallback-Glied faellt das im Betrieb womoeglich nie auf:\n  "
        + "\n  ".join(f"{name!r} <- {where}" for name, where in dangling))


@pytest.mark.parametrize("bad_yaml,expected", [
    ("a: 1\na: 2\n", "doppelter Schluessel"),
    ("top:\n  x: 1\n  x: 2\n", "doppelter Schluessel"),
])
def test_the_duplicate_detector_actually_detects(bad_yaml, expected):
    """Der Detektor selbst — sonst gruent der Scanner, weil er nichts sieht."""
    with pytest.raises(ValueError, match=expected):
        yaml.load(bad_yaml, Loader=_DuplicateKeyLoader)


class TestStaleLlmParamKeysAreDroppedLoudly:
    """A key whose profile is in none of the agent's chains configures nothing.
    It must not become silently ineffective — and it must not cost EVERY server
    its start either, which is what it did until 2026-08-22: one role rename in
    one file, and no service came up."""

    STALE = {"llm_profile": ["a", "b"],
             "llm_params": {"*": {"max_tokens": 100},
                            "gone": {"thinking_level": "high"}}}

    def test_direct_construction_still_raises(self):
        from pydantic import ValidationError

        from agent_system.config.models import AgentConfig
        with pytest.raises(ValidationError, match="profile keys"):
            AgentConfig.model_validate(self.STALE)

    def test_the_config_load_drops_it_and_keeps_the_rest(self):
        from agent_system.config.models import AgentConfig

        cfg = AgentConfig.model_validate(
            self.STALE, context={"drop_stale_llm_params": True})
        assert "gone" not in cfg.llm_params, "stale key survived"
        assert cfg.llm_params["*"] == {"max_tokens": 100}, "valid params taken down with it"

    def test_load_settings_survives_it_and_names_the_agent(self, tmp_path, caplog):
        """The wiring: without the context in the real load path, the same key
        would abort the whole start again."""
        from agent_system.config.settings import load_settings

        (tmp_path / "config.yaml").write_text(
            "plugins:\n"
            "  servers:\n"
            "    kaputt_agent:\n"
            "      type: agent\n"
            "      enabled: true\n"
            "      agent_config:\n"
            "        llm_profile: [a, b]\n"
            "        llm_params:\n"
            "          \"weg\":\n"
            "            thinking_level: high\n",
            encoding="utf-8")

        with caplog.at_level(logging.ERROR):
            cfg = load_settings(str(tmp_path / "config.yaml"))

        assert cfg.plugins.servers["kaputt_agent"].agent_config.llm_params == {}
        assert any("kaputt_agent" in r.getMessage() for r in caplog.records), \
            "the loss went unreported — which is exactly what makes it silent"

    def test_inheritance_does_not_move_the_abort_to_agent_creation(self, tmp_path, caplog):
        """The second validation. A child that overrides the chains inherits
        the parent's keyed params, so the key only turns stale AFTER the merge
        — in get_mcp_config_by_name, not at load time. Tolerating it only at
        load would move the abort from startup to the first spawn."""
        from agent_system.config.settings import (
            _reported_stale_llm_params,
            get_mcp_config_by_name,
            load_settings,
        )

        (tmp_path / "config.yaml").write_text(
            "plugins:\n"
            "  servers:\n"
            "    parent_agent:\n"
            "      type: agent\n"
            "      enabled: true\n"
            "      agent_config:\n"
            "        llm_profile: [a, b]\n"
            "        llm_params:\n"
            "          \"b\":\n"
            "            thinking_level: high\n"
            "    child_agent:\n"
            "      type: parent_agent\n"
            "      enabled: true\n"
            "      agent_config:\n"
            "        llm_profile: [c, d]\n",
            encoding="utf-8")

        cfg = load_settings(str(tmp_path / "config.yaml"))
        _reported_stale_llm_params.clear()   # the load pass may have reported already
        with caplog.at_level(logging.ERROR):
            merged = get_mcp_config_by_name("child_agent", cfg)

        assert merged is not None, "agent creation died on an inherited stale key"
        assert merged.agent_config.llm_params == {}
        assert any("child_agent" in r.getMessage() for r in caplog.records), \
            "dropped without a word at the very place the message warns about"

    def test_it_reaches_the_logfile_even_though_the_config_loads_first(self, tmp_path):
        """Every entry point loads the config BEFORE configuring logging, so an
        error raised during the load has no handler to go to and never reaches
        the file the operator reads. setup_logging replays it."""
        import agent_system.config.settings as settings_mod
        from agent_system.utils.logging import setup_logging

        root = logging.getLogger()
        saved = root.handlers[:]
        for handler in saved:
            root.removeHandler(handler)
        settings_mod._deferred_config_errors.clear()
        settings_mod._reported_stale_llm_params.clear()
        log_file = tmp_path / "agent.log"
        try:
            (tmp_path / "config.yaml").write_text(
                "plugins:\n"
                "  servers:\n"
                "    late_agent:\n"
                "      type: agent\n"
                "      enabled: true\n"
                "      agent_config:\n"
                "        llm_profile: [a, b]\n"
                "        llm_params:\n"
                "          \"gone\":\n"
                "            thinking_level: high\n",
                encoding="utf-8")
            settings_mod.load_settings(str(tmp_path / "config.yaml"))
            assert settings_mod._deferred_config_errors, \
                "nothing was kept for replay — the error is lost with the handlers"
            setup_logging(enabled=True, level="INFO", file_path=str(log_file),
                          rotation_enabled=False)
        finally:
            for handler in root.handlers[:]:
                handler.close()
                root.removeHandler(handler)
            for handler in saved:
                root.addHandler(handler)

        assert "late_agent" in log_file.read_text(encoding="utf-8"), \
            "the error never made it into the logfile"
