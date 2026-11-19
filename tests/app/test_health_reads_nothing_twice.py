"""GET /health is asked by every open web UI tab every 30 s, and by monitors. It parsed the whole config for its name
and version and tried seven imports on every call; now the config is still read every time but parsed only when its
text changed, and the package versions are found once. What it answers is unchanged -- except that a config whose
top level is not a mapping answers the defaults instead of an error."""
from __future__ import annotations

import os
from types import SimpleNamespace

from agent_system.api import page_routes
from agent_system.utils import yaml_io


def _health(path):
    return page_routes.health(SimpleNamespace(config_path=path))


def _counted_parses(monkeypatch) -> list:
    parses = []
    parse = yaml_io.safe_load

    def counted(stream):
        parses.append(stream)
        return parse(stream)
    monkeypatch.setattr(yaml_io, "safe_load", counted)
    return parses


def test_an_unchanged_config_is_parsed_once(tmp_path, monkeypatch):
    parses = _counted_parses(monkeypatch)
    config = tmp_path / "config.yaml"
    config.write_text("name: probe-hive\nversion: 1.2.3\n", encoding="utf-8")
    for _ in range(3):
        answer = _health(config)
        assert (answer["status"], answer["name"], answer["version"]) == ("ok", "probe-hive", "1.2.3")
    assert len(parses) == 1


def test_an_edit_of_the_same_size_at_the_same_mtime_is_read_at_the_next_call(tmp_path):
    config = tmp_path / "config.yaml"
    config.write_text("name: hive\nversion: 1.2.3\n", encoding="utf-8")
    assert _health(config)["version"] == "1.2.3"
    stat = config.stat()
    config.write_text("name: hive\nversion: 1.2.4\n", encoding="utf-8")
    os.utime(config, ns=(stat.st_atime_ns, stat.st_mtime_ns))   # a coarse clock: nothing about the file tells
    assert config.stat().st_size == stat.st_size
    assert _health(config)["version"] == "1.2.4"


def test_a_parse_that_failed_is_tried_again_on_the_unchanged_file(tmp_path, monkeypatch):
    config = tmp_path / "config.yaml"
    config.write_text("name: steady-hive\n", encoding="utf-8")
    parse, failed = yaml_io.safe_load, []

    def fails_once(stream):
        if not failed:
            failed.append(True)
            raise OSError("the file was busy")
        return parse(stream)
    monkeypatch.setattr(yaml_io, "safe_load", fails_once)
    assert _health(config)["name"] == "AgentSystem"
    assert _health(config)["name"] == "steady-hive"


def test_a_config_that_is_not_a_mapping_answers_the_defaults(tmp_path):
    config = tmp_path / "config.yaml"
    config.write_text("- just\n- a list\n", encoding="utf-8")
    answer = _health(config)
    assert (answer["status"], answer["name"], answer["version"]) == ("ok", "AgentSystem", "unknown")


def test_a_broken_config_answers_the_defaults_and_is_read_again_once_fixed(tmp_path):
    config = tmp_path / "config.yaml"
    config.write_text("name: [unclosed\n", encoding="utf-8")
    assert (_health(config)["name"], _health(config)["version"]) == ("AgentSystem", "unknown")
    config.write_text("name: fixed-hive\n", encoding="utf-8")
    assert _health(config)["name"] == "fixed-hive"


def test_a_missing_config_answers_the_defaults(tmp_path):
    answer = _health(tmp_path / "gone.yaml")
    assert (answer["status"], answer["name"], answer["version"]) == ("ok", "AgentSystem", "unknown")


def test_package_versions_are_found_once_and_each_answer_has_its_own_copy(tmp_path):
    config = tmp_path / "config.yaml"
    config.write_text("name: x\n", encoding="utf-8")
    first = _health(config)["packages"]
    assert first["fastapi"] != "unknown" and first["pydantic"] != "unknown"
    assert page_routes._package_versions() is page_routes._package_versions()
    first["fastapi"] = "changed by a caller"
    assert _health(config)["packages"]["fastapi"] != "changed by a caller"
