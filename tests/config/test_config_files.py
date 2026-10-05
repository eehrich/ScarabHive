"""What load_settings reads: config_files lists the files in its order (the master config, then each include that
exists), and ${VAR} placeholders are expanded from the environment."""
from __future__ import annotations

from pathlib import Path

import pytest

from agent_system.config.settings import config_files, load_settings


def write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_config_files_follow_the_include_order_and_skip_missing_files(tmp_path: Path):
    master = tmp_path / "config.yaml"
    write(master, "includes:\n  - missing.yaml\n  - agents/*.yaml\n  - nomatch/*.yaml\n  - b.yaml\n")
    server = "plugins:\n  servers:\n    {name}:\n      type: basic_agent\n      description: {text}\n"
    write(tmp_path / "agents" / "z.yaml", server.format(name="shared", text="from z"))
    write(tmp_path / "agents" / "a.yaml", server.format(name="shared", text="from a"))
    write(tmp_path / "b.yaml", server.format(name="only_b", text="from b"))

    files = config_files(str(master))

    assert [path.resolve() for path in files] == [
        master.resolve(), (tmp_path / "agents" / "a.yaml").resolve(),
        (tmp_path / "agents" / "z.yaml").resolve(), (tmp_path / "b.yaml").resolve()]
    # a start does not skip a file the master names by its path: it may hold the route rules (config/security.yaml)
    with pytest.raises(ValueError, match="named in config.yaml but missing"):
        load_settings(str(master))
    write(master, "includes:\n  - agents/*.yaml\n  - nomatch/*.yaml\n  - b.yaml\n")
    loaded = load_settings(str(master))
    assert loaded.plugins.servers["shared"].description == "from z", "the later file wins, as config_files orders them"
    assert loaded.plugins.servers["only_b"].description == "from b"


def test_config_files_without_a_master_config_is_empty(tmp_path: Path):
    assert config_files(str(tmp_path / "config.yaml")) == []


def test_placeholders_are_expanded_and_unset_ones_named(tmp_path: Path, monkeypatch, caplog):
    monkeypatch.setenv("CFG_TEST_SERVER", "research_tools")
    monkeypatch.delenv("CFG_TEST_UNSET", raising=False)
    master = tmp_path / "config.yaml"
    write(master, "includes:\n  - agents.yaml\n")
    write(tmp_path / "agents.yaml", "plugins:\n  servers:\n    probe:\n      type: basic_agent\n"
          "      description: \"${CFG_TEST_SERVER} and ${CFG_TEST_UNSET}\"\n")

    with caplog.at_level("WARNING"):
        loaded = load_settings(str(master))

    assert loaded.plugins.servers["probe"].description == "research_tools and "
    assert "CFG_TEST_UNSET" in caplog.text
