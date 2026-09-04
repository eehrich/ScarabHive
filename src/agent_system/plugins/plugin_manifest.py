"""Plugin-Manifest laden — ``plugin.toml`` (bevorzugt) mit Fallback auf ``plugin.yaml``.

Ein Plugin beschreibt sich in EINER Datei ``plugin.toml``: Metadaten
(``[plugin]``-Tabelle) und seine Python-Requirements (``dependencies``). Waehrend
der schrittweisen Migration wird ``plugin.yaml`` weiter als Fallback gelesen,
damit noch nicht umgestellte Plugins unveraendert funktionieren.

Bewusst abhaengigkeitsarm (nur stdlib ``tomllib`` ab Python 3.11 + ``pyyaml``),
damit auch der Build-Aggregator (``scripts/aggregate_plugin_deps.py``) dieses
Modul importieren kann, ohne die MCP-Runtime zu laden.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

try:
    import tomllib  # Python >=3.11 stdlib
except ModuleNotFoundError:  # pragma: no cover - Repo verlangt >=3.11
    tomllib = None  # type: ignore[assignment]

from agent_system.utils import yaml_io

logger = logging.getLogger(__name__)

MANIFEST_TOML = "plugin.toml"
MANIFEST_YAML = "plugin.yaml"


def load_plugin_metadata(plugin_dir: Path) -> dict[str, Any]:
    """Metadaten-Dict eines Plugins (name/entrypoint/type/category/tags/...).

    - ``plugin.toml`` (bevorzugt): Inhalt der ``[plugin]``-Tabelle. Faellt auf
      den Top-Level zurueck, falls keine ``[plugin]``-Tabelle existiert.
    - ``plugin.yaml`` (Fallback): das Top-Level-Mapping.

    Gibt ``{}`` zurueck, wenn kein Manifest existiert oder es unlesbar ist. Die
    zurueckgegebene Struktur ist formatunabhaengig identisch (dieselben Keys wie
    das alte ``plugin.yaml``), sodass die Aufrufer unveraendert bleiben.
    """
    toml_path = plugin_dir / MANIFEST_TOML
    if toml_path.exists() and tomllib is not None:
        try:
            with toml_path.open("rb") as fh:
                data = tomllib.load(fh)
            meta = data.get("plugin", data)
            return meta if isinstance(meta, dict) else {}
        except Exception as e:
            # Sichtbar machen, nicht still schlucken (kein stiller Drift):
            # bei defekter toml fallen wir auf yaml zurueck, falls vorhanden.
            logger.warning("Failed to parse %s: %s", toml_path, e)

    yaml_path = plugin_dir / MANIFEST_YAML
    if yaml_path.exists():
        try:
            with yaml_path.open("r", encoding="utf-8") as fh:
                data = yaml_io.safe_load(fh) or {}
            return data if isinstance(data, dict) else {}
        except Exception as e:
            logger.warning("Failed to parse %s: %s", yaml_path, e)
            return {}
    return {}


def plugin_pip_dependencies(plugin_dir: Path) -> list[str]:
    """Python pip-Requirements aus dem ``plugin.toml`` eines Plugins; ``[]`` sonst.

    Bewusst NUR ``plugin.toml`` (nicht das Legacy-``plugin.yaml``): das
    ``dependencies``-Feld in den alten yaml-Manifesten ist ueberladen — manche
    Plugins listen dort pip-Pakete, andere Inter-Plugin-Namen (z.B.
    ``writer_content``). Es ist damit KEINE sichere Build-Eingabe. Die pip-Deps
    eines Plugins werden erst aggregiert, nachdem es auf ``plugin.toml``
    migriert wurde; bis dahin bleiben sie in ``requirements/core.txt``.

    Der Framework-Constraint ``requires`` (z.B. ``agent_system``) gehoert nicht
    zu den pip-Deps und wird nie aggregiert.
    """
    toml_path = plugin_dir / MANIFEST_TOML
    if not (toml_path.exists() and tomllib is not None):
        return []
    try:
        with toml_path.open("rb") as fh:
            data = tomllib.load(fh)
    except Exception as e:
        logger.warning("Failed to parse %s for dependencies: %s", toml_path, e)
        return []
    meta = data.get("plugin", data)
    deps = meta.get("dependencies") if isinstance(meta, dict) else None
    if isinstance(deps, list):
        return [str(d).strip() for d in deps if str(d).strip()]
    return []
