"""config/local.yaml: what one machine sets for itself, as the last include of
config.yaml. The server behind nginx and a desktop that opens its API for a
webhook need different networks from one tracked config.yaml -- so an include
sets any section, not only llm_system, plugins, external_servers and hooks."""
import logging
import subprocess
from pathlib import Path

import pytest
import yaml

from agent_system.config.settings import _expand_includes, load_settings

REPO = Path(__file__).resolve().parents[2]


def write(path, data):
    path.write_text(yaml.safe_dump(data), encoding="utf-8")


@pytest.fixture
def config_dir(tmp_path):
    folder = tmp_path / "config"
    folder.mkdir()
    write(folder / "config.yaml", {"includes": ["plugins.yaml", "local.yaml"],
                                   "network": {"host": "127.0.0.1", "port": 8123}})
    write(folder / "plugins.yaml", {"plugins": {"servers": {"forge": {"type": "forge", "enabled": True}}}})
    return folder


def test_an_include_sets_any_section_over_the_master_and_earlier_includes(config_dir):
    write(config_dir / "local.yaml", {"network": {"host": "0.0.0.0", "remote_paths": ["/hook"]},
                                      "logging": {"backup_count": 20},
                                      "plugins": {"servers": {"forge": {"enabled": False}}}})
    config = load_settings(str(config_dir / "config.yaml"))
    assert (config.network.host, config.network.port, config.network.remote_paths) == ("0.0.0.0", 8123, ["/hook"])
    assert config.logging.backup_count == 20
    assert (config.plugins.servers["forge"].type, config.plugins.servers["forge"].enabled) == ("forge", False)


def test_a_commented_out_section_sets_nothing_and_costs_nothing(config_dir):
    """`plugins:` with every line commented out used to fail the whole file in deep_merge."""
    (config_dir / "local.yaml").write_text("plugins:\nllm_system:\nnetwork:\n  host: 0.0.0.0\n", encoding="utf-8")
    config = load_settings(str(config_dir / "config.yaml"))
    assert (config.network.host, config.plugins.servers["forge"].enabled) == ("0.0.0.0", True)


def test_without_local_the_config_is_as_written(config_dir):
    config = load_settings(str(config_dir / "config.yaml"))
    assert (config.network.host, config.network.remote_paths) == ("127.0.0.1", None)


@pytest.mark.parametrize("section,unchanged", [
    # a process reads its data directory from the master alone (master_data_dir)
    ({"data_dir": "elsewhere"}, lambda config: config.paths.data_dir != "elsewhere"),
    # the setup panel reads the signing key a restart applies from the master alone
    ({"enabled": True, "secret_key": "from-an-include"}, lambda config: config.auth.enabled is False),
], ids=["paths", "auth"])
def test_the_masters_own_sections_stay_its_own(config_dir, caplog, section, unchanged):
    name = "paths" if "data_dir" in section else "auth"
    write(config_dir / "local.yaml", {name: section, "network": None})
    with caplog.at_level(logging.WARNING):
        config = load_settings(str(config_dir / "config.yaml"))
    assert unchanged(config) and f"'{name}' is read from config.yaml only" in caplog.text
    assert config.network.host == "127.0.0.1"  # a section with every line commented out sets nothing


def test_the_real_config_includes_local_last():
    master = REPO / "config" / "config.yaml"
    assert _expand_includes(yaml.safe_load(master.read_text(encoding="utf-8")), master)[-1] == "local.yaml"


def test_the_repository_never_takes_it():
    assert subprocess.run(["git", "check-ignore", "-q", "config/local.yaml"], cwd=REPO).returncode == 0
