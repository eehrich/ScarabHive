"""Every search test runs on both engines: ripgrep, and the walker behind it.

grep_search and search_files answer through ripgrep when one is found and
through the Python walker when not -- which one depends on the machine, not
on the test. So both have to give the same answers, and the tests of this
plugin hold each of them to it: the modules below run twice, once per engine.
Without a ripgrep on PATH or in data/cache/bin the ripgrep run is skipped, and
says so.
"""
from __future__ import annotations

import pytest

from plugins.file_ops import ripgrep

#: Modules whose tests do not search text (the semantic index walks in Python
#: either way): running them twice would only double the slowest tests.
_ONE_ENGINE = {
    "test_plugin_file_ops_semantic_index",
    "test_plugin_file_ops_index_across_processes",
    "test_plugin_file_ops_symbols",
    "test_plugin_file_ops_shutdown_hook",
    "test_plugin_file_ops_ripgrep",
    "test_plugin_file_ops_index_checkpoints",
    "test_plugin_file_ops_index_freshness",
}


def pytest_generate_tests(metafunc):
    if "search_engine" in metafunc.fixturenames:
        module = metafunc.module.__name__.rsplit(".", 1)[-1]
        kinds = ["python"] if module in _ONE_ENGINE else ["ripgrep", "python"]
        metafunc.parametrize("search_engine", kinds, indirect=True)


@pytest.fixture(autouse=True)
def search_engine(request, monkeypatch):
    """The engine this run searches with -- ``ripgrep.locate`` answers accordingly."""
    kind = getattr(request, "param", "python")
    binary = ripgrep.locate(None, False) if kind == "ripgrep" else None
    if kind == "ripgrep" and binary is None:
        pytest.skip("no ripgrep on PATH or in data/cache/bin -- only the walker is tested here")
    monkeypatch.setattr(ripgrep, "locate", lambda *args, **kwargs: binary)
    return kind
