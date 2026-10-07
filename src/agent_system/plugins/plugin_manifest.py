"""Read a plugin's manifest: ``plugin.toml``.

A plugin describes itself in ONE file: metadata (the ``[plugin]`` table) and
its Python requirements (``dependencies``, and ``optional_dependencies`` for
the ones an install may lack).

The ``plugin.yaml`` fallback this module used to carry is gone (2026-09-06).
The migration is finished -- 73 ``plugin.toml`` in the tree and 0
``plugin.yaml`` -- and a fallback nothing needs is not a safety net: it keeps
two formats alive for tools to disagree about, which is exactly how
``validate_plugin --all`` came to look for the file that no longer existed and
report success on the empty list it found.

Deliberately dependency-light (stdlib ``tomllib`` only, Python >=3.11) so the
build aggregator (``scripts/aggregate_plugin_deps.py``) can import this module
without pulling in the tool runtime.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

try:
    import tomllib  # Python >=3.11 stdlib
except ModuleNotFoundError:  # pragma: no cover - Repo verlangt >=3.11
    tomllib = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)

MANIFEST_TOML = "plugin.toml"


def load_plugin_metadata(plugin_dir: Path) -> dict[str, Any]:
    """A plugin's metadata dict (name/entrypoint/type/category/tags/...).

    The contents of the ``[plugin]`` table, falling back to the top level when
    there is no such table.

    Returns ``{}`` when no manifest exists or it cannot be read -- an
    unreadable manifest is reported as a WARNING, never swallowed.
    """
    toml_path = plugin_dir / MANIFEST_TOML
    if toml_path.exists() and tomllib is not None:
        try:
            with toml_path.open("rb") as fh:
                data = tomllib.load(fh)
            meta = data.get("plugin", data)
            return meta if isinstance(meta, dict) else {}
        except Exception as e:
            # Say so, do not swallow it: a broken manifest that reads as "no
            # metadata" is how a plugin goes missing without anyone noticing.
            logger.warning("Failed to parse %s: %s", toml_path, e)
    return {}


def plugin_pip_dependencies(plugin_dir: Path) -> list[str]:
    """A plugin's pip requirements from its ``plugin.toml``; ``[]`` otherwise.

    The framework constraint ``requires`` (e.g. ``agent_system``) is not a pip
    dependency and is never aggregated.
    """
    return _manifest_specs(plugin_dir, "dependencies")


def plugin_optional_pip_dependencies(plugin_dir: Path) -> list[str]:
    """A plugin's ``optional_dependencies``: pip specs whose install may fail
    without breaking the install -- each one switches a feature on.

    They go to ``requirements/optional.txt``, which ``pip install -e .`` does
    not read; the install scripts and the Dockerfile install it on their own.
    """
    return _manifest_specs(plugin_dir, "optional_dependencies")


def _manifest_specs(plugin_dir: Path, key: str) -> list[str]:
    toml_path = plugin_dir / MANIFEST_TOML
    if not (toml_path.exists() and tomllib is not None):
        return []
    try:
        with toml_path.open("rb") as fh:
            data = tomllib.load(fh)
    except Exception as e:
        logger.warning("Failed to parse %s for %s: %s", toml_path, key, e)
        return []
    meta = data.get("plugin", data)
    deps = meta.get(key) if isinstance(meta, dict) else None
    if isinstance(deps, list):
        return [str(d).strip() for d in deps if str(d).strip()]
    return []
