"""Drift guard for the per-plugin dependency aggregation.

Plugin pip requirements live in each plugin's ``plugin.toml``;
``scripts/aggregate_plugin_deps.py`` merges them with ``requirements/core.txt``
into ``requirements/all.txt``, which the root ``pyproject.toml`` reads via
``[tool.setuptools.dynamic]``. If a plugin's deps change without re-running the
script, the install metadata silently goes stale — these tests catch that.
"""
import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


def _load_aggregator():
    spec = importlib.util.spec_from_file_location(
        "aggregate_plugin_deps", ROOT / "scripts" / "aggregate_plugin_deps.py"
    )
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_all_txt_is_not_stale():
    """requirements/all.txt must equal the aggregator's current output."""
    agg = _load_aggregator()
    expected = agg.render()
    actual = (ROOT / "requirements" / "all.txt").read_text(encoding="utf-8")
    assert actual == expected, (
        "requirements/all.txt is stale — re-run:\n"
        "    python scripts/aggregate_plugin_deps.py"
    )


def test_private_txt_is_not_stale():
    """requirements/private.txt must equal the aggregator's output wherever the
    private roots exist; the open-source checkout has neither."""
    agg = _load_aggregator()
    if not agg.has_private_roots():
        pytest.skip("no private plugin roots in this checkout")
    actual = (ROOT / "requirements" / "private.txt").read_text(encoding="utf-8")
    assert actual == agg.render_private(), (
        "requirements/private.txt is stale — re-run:\n"
        "    python scripts/aggregate_plugin_deps.py"
    )


def test_private_txt_repeats_nothing_all_txt_has():
    agg = _load_aggregator()
    public = {r.lower().replace(" ", "") for r in agg.collect_requirements()}
    assert not public & {r.lower().replace(" ", "") for r in agg.collect_private_requirements()}


def test_migrated_plugin_dep_is_aggregated():
    """A migrated plugin's plugin.toml dep flows into the aggregate (okf pilot)."""
    agg = _load_aggregator()
    reqs = agg.collect_requirements()
    assert any(r.lower().startswith("ruamel") for r in reqs), (
        "okf's plugin.toml dependency (ruamel.yaml) not found in the aggregate"
    )


def test_aggregate_covers_core():
    """Core requirements are always included in the aggregate."""
    agg = _load_aggregator()
    reqs_norm = {r.lower().replace(" ", "") for r in agg.collect_requirements()}
    core_norm = {
        line.strip().lower().replace(" ", "")
        for line in (ROOT / "requirements" / "core.txt").read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.strip().startswith("#")
    }
    assert core_norm <= reqs_norm, "aggregate is missing core requirements"
