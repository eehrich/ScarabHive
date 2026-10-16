"""The machine list's problem counts are kept while a machine's files stand: the list is shown on every panel load,
and validating every machine took 18 s there (and slowed the machine being opened meanwhile to 10 s and more)."""

from __future__ import annotations

import os

import pytest

from agent_system.config.models import AgentSystemConfig
from plugins.stategraph.server import StateGraphServer
from plugins.stategraph.tests.stategraph_testkit import tool_config
from plugins.stategraph.tests.test_plugin_stategraph_server import HELLO, HELLO_PY


@pytest.fixture
async def counted(tmp_path, monkeypatch):
    srv = StateGraphServer("stategraph", AgentSystemConfig(), tool_config(tmp_path))
    machines = tmp_path / "machines"
    (machines / "hello.yaml").write_text(HELLO, encoding="utf-8")
    (machines / "hello.py").write_text(HELLO_PY, encoding="utf-8")
    validated = []
    real = srv.service._validate
    monkeypatch.setattr(srv.service, "_validate", lambda tree, *a, **k: validated.append(tree.root) or real(tree, *a, **k))
    yield srv, machines, validated
    await srv.stop_plugin()


def _touch(path, text):
    stat = os.stat(path)
    path.write_text(text, encoding="utf-8")
    os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000_000))  # a coarse clock still sees a change


async def test_the_counts_of_an_unchanged_machine_are_not_validated_again(counted):
    srv, _, validated = counted

    first = srv.service.machine_checks()
    second = srv.service.machine_checks()

    assert first == second == {"hello": {"valid": True, "errors": 0, "warnings": 0}}, first
    assert len(validated) == 1, validated


@pytest.mark.parametrize("changed", ["hello.yaml", "hello.py"])
async def test_a_changed_file_of_the_tree_is_validated_again(counted, changed):
    srv, machines, validated = counted
    assert srv.service.machine_checks()["hello"]["valid"]

    broken = {"hello.yaml": HELLO.replace("target: done", "target: nowhere"),
              "hello.py": "def other(name):\n    return name\n"}[changed]
    _touch(machines / changed, broken)

    assert srv.service.machine_checks()["hello"]["errors"] >= 1, "the change was not seen"
    assert len(validated) == 2, validated
