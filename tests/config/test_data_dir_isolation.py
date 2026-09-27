"""Tests run on the relative data directory, whatever the configuration says.

Twenty-one test files isolate their writes by chdir into a temporary
directory, which only holds while data paths are relative. A developer's
AGENT_DATA_DIR or a ``paths.data_dir`` in config/config.yaml would move them
into the real, configured directory. The root conftest pins it -- at import,
for loads outside any test (not measurable in-process: by the time a test
runs, the pin has long been set), and per test with ``relative_data_dir``,
which this file measures from a test that does NOT unsettle it.
"""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from agent_system import paths
from agent_system.config.settings import load_settings


@pytest.fixture(scope="module")
def leaked_configuration(tmp_path_factory):
    """What an earlier load could leave behind: a configured, unsettled data
    directory. Module-scoped, so it is in place before the per-test pin runs."""
    saved = paths._config_data_dir, paths._settled
    paths._config_data_dir = str(tmp_path_factory.mktemp("configured"))
    paths._settled = False
    yield
    paths._config_data_dir, paths._settled = saved


def test_a_leaked_configuration_does_not_reach_a_test(leaked_configuration):
    assert paths.configured_data_dir() is None
    assert paths.data_path("json_store") == Path("data", "json_store")


def test_a_load_inside_a_test_does_not_configure_it(leaked_configuration, tmp_path):
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "config.yaml").write_text(
        yaml.safe_dump({"paths": {"data_dir": str(tmp_path / "configured")}}), encoding="utf-8")

    load_settings(str(config_dir / "config.yaml"))

    assert paths.configured_data_dir() is None
