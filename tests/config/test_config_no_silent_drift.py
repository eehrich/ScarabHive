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
