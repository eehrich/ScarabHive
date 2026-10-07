"""Drift guard for the per-plugin dependency aggregation.

Plugin pip requirements live in each plugin's ``plugin.toml``;
``scripts/aggregate_plugin_deps.py`` merges them with ``requirements/core.txt``
into ``requirements/all.txt``, which the root ``pyproject.toml`` reads via
``[tool.setuptools.dynamic]``. If a plugin's deps change without re-running the
script, the install metadata silently goes stale — these tests catch that.
A plugin's ``optional_dependencies`` go to ``requirements/optional.txt``, which
the install scripts install best effort and pyproject.toml does not read.
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


def test_the_aggregator_knows_every_plugin_root_of_the_checkout():
    """A root the aggregator misses would skip the test below instead of failing it."""
    agg = _load_aggregator()
    on_disk = {d.name for d in (ROOT / "src").iterdir() if d.is_dir() and d.name.startswith("plugins") and d.name.isidentifier()}
    assert {d.name for d in (*agg.PLUGIN_DIRS, *agg.PRIVATE_PLUGIN_DIRS)} == on_disk


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


def test_optional_txt_is_not_stale():
    """requirements/optional.txt must equal the aggregator's current output."""
    agg = _load_aggregator()
    actual = (ROOT / "requirements" / "optional.txt").read_text(encoding="utf-8")
    assert actual == agg.render_optional(), (
        "requirements/optional.txt is stale — re-run:\n"
        "    python scripts/aggregate_plugin_deps.py"
    )


def test_an_optional_dependency_reaches_optional_txt_and_not_all_txt(tmp_path, monkeypatch):
    """The two lists of a manifest go to two files: what may fail to install
    must not reach the set `pip install -e .` installs, or it stops it again."""
    agg = _load_aggregator()
    plugin = tmp_path / "plugins" / "probe"
    plugin.mkdir(parents=True)
    (plugin / "plugin.toml").write_text(
        '[plugin]\nname = "probe"\n'
        'dependencies = ["probe-hard>=1"]\n'
        'optional_dependencies = ["probe-soft[extra]>=2", "probe-hard>=1"]\n',
        encoding="utf-8")
    private = tmp_path / "plugins_extra" / "secret"
    private.mkdir(parents=True)
    (private / "plugin.toml").write_text(
        '[plugin]\nname = "secret"\noptional_dependencies = ["secret-soft"]\n', encoding="utf-8")
    core = tmp_path / "core.txt"
    core.write_text("probe-core>=3\n", encoding="utf-8")
    monkeypatch.setattr(agg, "PLUGIN_DIRS", [tmp_path / "plugins"])
    monkeypatch.setattr(agg, "PRIVATE_PLUGIN_DIRS", [tmp_path / "plugins_extra"])
    monkeypatch.setattr(agg, "CORE_FILE", core)

    assert agg.collect_requirements() == ["probe-core>=3", "probe-hard>=1"]
    # probe-hard is in all.txt already; optional.txt does not repeat it.
    assert agg.collect_optional_requirements() == ["probe-soft[extra]>=2"]
    assert agg.render_optional().endswith("\nprobe-soft[extra]>=2\n")
    # A private root's optional dependency stays out of the public file, and
    # the guard below sees it.
    assert agg.private_optional_requirements() == ["secret-soft"]
    assert agg.collect_private_requirements() == []


def test_pycairo_is_not_a_hard_requirement():
    """pycairo has wheels for Windows only; as a hard dependency (through
    reportlab[pycairo]) it stopped `pip install -e .` on a fresh Mac
    (2026-10). It belongs in optional_dependencies. (Whether the hard set as a
    whole resolves to wheels only needs the network: `pip install --dry-run
    --only-binary=:all: -r requirements/all.txt`.)"""
    agg = _load_aggregator()
    hard = agg.collect_requirements()
    assert any(r.lower().startswith("reportlab") for r in hard), "fixture: reportlab is no longer aggregated"
    assert not [r for r in hard if "cairo" in r.lower()], hard
    assert any("pycairo" in r.lower() for r in agg.collect_optional_requirements())


def test_no_private_root_declares_optional_dependencies():
    """optional.txt ships with the open-source release, so it is built from
    src/plugins only. A private root's optional dependency would be dropped
    without a word -- declare it under `dependencies` there, or give the
    private roots an optional file of their own first."""
    agg = _load_aggregator()
    if not agg.has_private_roots():
        pytest.skip("no private plugin roots in this checkout")
    assert agg.private_optional_requirements() == []


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


@pytest.mark.parametrize("argv, code", [(["--help"], 0), (["--frobnicate"], 2)])
def test_the_aggregator_writes_nothing_for_help_or_an_unknown_argument(tmp_path, monkeypatch, capsys, argv, code):
    agg = _load_aggregator()
    for name in ("OUT_FILE", "PRIVATE_OUT_FILE", "OPTIONAL_OUT_FILE"):
        monkeypatch.setattr(agg, name, tmp_path / f"{name}.txt")
    with pytest.raises(SystemExit) as exited:
        agg.main(argv)
    assert exited.value.code == code
    assert not list(tmp_path.iterdir()), "it wrote files anyway"


def test_the_aggregator_writes_without_arguments(tmp_path, monkeypatch):
    """The other side: no argument still writes (the guard above is not a
    script that never writes)."""
    agg = _load_aggregator()
    monkeypatch.setattr(agg, "ROOT", tmp_path)
    for name in ("OUT_FILE", "PRIVATE_OUT_FILE", "OPTIONAL_OUT_FILE"):
        monkeypatch.setattr(agg, name, tmp_path / f"{name}.txt")
    assert agg.main([]) == 0
    assert (tmp_path / "OUT_FILE.txt").read_text(encoding="utf-8") == agg.render()
