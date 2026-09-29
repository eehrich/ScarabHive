"""config/local.yaml and config/local.env: what one machine sets for itself. The server behind nginx and a desktop
that opens its API for a webhook need different networks from one tracked config.yaml, and an installation's own
keys and signing key must never land in a tracked file -- so the local layer is read after every include, sets any
section (auth and paths too, which an include cannot), and its secrets file is read before config/secrets.env."""
import logging
import os
import subprocess
from pathlib import Path

import pytest
import yaml

from agent_system.config import settings
from agent_system.config.settings import (_expand_includes, config_files, environment_at_restart, load_settings,
                                          master_data_dir)

REPO = Path(__file__).resolve().parents[2]


def write(path, data):
    path.write_text(yaml.safe_dump(data), encoding="utf-8")


@pytest.fixture
def config_dir(tmp_path):
    folder = tmp_path / "config"
    folder.mkdir()
    write(folder / "config.yaml", {"includes": ["plugins.yaml", "extra.yaml"],
                                   "network": {"host": "127.0.0.1", "port": 8123}})
    write(folder / "plugins.yaml", {"plugins": {"servers": {"forge": {"type": "forge", "enabled": True}}}})
    write(folder / "extra.yaml", {})  # named, so it must be there; a test writes what it sets
    return folder


def test_the_local_layer_sets_any_section_over_the_master_and_every_include(config_dir):
    write(config_dir / "extra.yaml", {"network": {"host": "10.0.0.1"}, "logging": {"backup_count": 5}})
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


def test_the_local_layer_sets_auth_and_paths_which_an_include_cannot(config_dir, caplog):
    """The install scripts and the Setup panel name the machine's own signing key here; a process that never loads
    settings reads its data directory from the master and this layer (master_data_dir)."""
    write(config_dir / "extra.yaml", {"auth": {"secret_key": "from-an-include-" * 3}, "paths": {"data_dir": "inc"}})
    write(config_dir / "local.yaml", {"auth": {"enabled": True, "secret_key": "from-the-local-layer-" * 2},
                                      "paths": {"data_dir": "mine"}})
    with caplog.at_level(logging.WARNING):
        config = load_settings(str(config_dir / "config.yaml"))
    assert (config.auth.enabled, config.auth.secret_key) == (True, "from-the-local-layer-" * 2)
    assert master_data_dir(str(config_dir / "config.yaml")) == "mine"
    # extra.yaml is named by its path: it may set the route rules, and nothing else of auth
    assert "extra.yaml: auth.secret_key is read from config.yaml only" in caplog.text, caplog.text
    assert "extra.yaml: 'paths' is read from config.yaml only" in caplog.text, caplog.text


def test_a_master_that_still_names_it_reads_it_once_and_last(config_dir, caplog):
    """The tracked config.yaml named it as its last include until the loader read it itself. A server that has
    not pulled yet, or a copy of the config, still does: no warning for its auth, and it still wins."""
    write(config_dir / "config.yaml", {"includes": ["local.yaml", "extra.yaml"]})
    write(config_dir / "extra.yaml", {"network": {"host": "10.0.0.1"}})
    write(config_dir / "local.yaml", {"network": {"host": "0.0.0.0"}, "auth": {"secret_key": "local-" * 6}})
    with caplog.at_level(logging.WARNING):
        config = load_settings(str(config_dir / "config.yaml"))
    assert (config.network.host, config.auth.secret_key) == ("0.0.0.0", "local-" * 6)
    assert "read from config.yaml only" not in caplog.text, caplog.text
    assert [path.name for path in config_files(str(config_dir / "config.yaml"))] == [
        "config.yaml", "extra.yaml", "local.yaml"]


def test_a_utf16_local_layer_loads(config_dir):
    """PowerShell 5.1's `>` writes UTF-16, and the install guide has the reader write this file: a start died on it
    with a UnicodeDecodeError, where an include of the same bytes was only skipped."""
    (config_dir / "local.yaml").write_bytes("network:\n  host: 0.0.0.0\n".encode("utf-16"))
    assert load_settings(str(config_dir / "config.yaml")).network.host == "0.0.0.0"


@pytest.mark.parametrize("data", [b"network: [unclosed\n", "network:\n  host: wert-mit-\u00fc\n".encode("cp1252")],
                         ids=["broken-yaml", "no-text"])
def test_a_local_layer_that_does_not_load_fails_the_start(config_dir, data):
    """It names this machine's own signing key: skipped, the start signed every login with the public one."""
    (config_dir / "local.yaml").write_bytes(data)
    with pytest.raises(ValueError, match="local.yaml does not load"):
        load_settings(str(config_dir / "config.yaml"))
    assert master_data_dir(str(config_dir / "config.yaml")) is None  # a process without settings: no crash either


def test_an_empty_hooks_key_in_the_local_layer_sets_nothing(config_dir):
    """As in an include: `max_hooks:` with its value commented out must not overwrite the master's with null."""
    write(config_dir / "config.yaml", {"includes": ["plugins.yaml"], "hooks": {"enabled": True, "default_timeout": 7}})
    (config_dir / "local.yaml").write_text("hooks:\n  default_timeout:\n  enabled: false\n", encoding="utf-8")
    config = load_settings(str(config_dir / "config.yaml"))
    assert (config.hooks.enabled, config.hooks.default_timeout) == (False, 7)


def test_a_local_layer_that_is_no_mapping_fails_the_start(config_dir):
    (config_dir / "local.yaml").write_text("- a\n- list\n", encoding="utf-8")
    with pytest.raises(ValueError, match="not a mapping"):
        load_settings(str(config_dir / "config.yaml"))


@pytest.fixture
def own_process(monkeypatch):
    """What this test takes from secrets files goes with it."""
    monkeypatch.setattr(settings, "_secrets_from_file", {})
    monkeypatch.setenv(settings.SECRETS_FROM_FILE_ENV, os.environ.get(settings.SECRETS_FROM_FILE_ENV, ""))
    for name in ("LOCAL_TEST_KEY", "LOCAL_TEST_OTHER"):
        monkeypatch.setenv(name, "")
        monkeypatch.delenv(name)


def test_local_env_is_read_before_secrets_env(config_dir, own_process):
    """Its first line of a name wins: a key the panel wrote replaces the one the tracked file holds."""
    (config_dir / "secrets.env").write_text("LOCAL_TEST_KEY=from-secrets\nLOCAL_TEST_OTHER=only-there\n",
                                            encoding="utf-8")
    (config_dir / "local.env").write_text("LOCAL_TEST_KEY=from-local\n", encoding="utf-8")
    write(config_dir / "local.yaml", {"network": {"host": "${LOCAL_TEST_KEY}"}})
    master = str(config_dir / "config.yaml")

    at_restart = environment_at_restart(master)
    config = load_settings(master)

    assert (at_restart["LOCAL_TEST_KEY"], at_restart["LOCAL_TEST_OTHER"]) == ("from-local", "only-there")
    assert config.network.host == "from-local"
    assert (os.environ["LOCAL_TEST_KEY"], os.environ["LOCAL_TEST_OTHER"]) == ("from-local", "only-there")


def test_a_secret_taken_at_runtime_is_the_files_not_the_environments(own_process):
    settings.take_secret("LOCAL_TEST_KEY", "written-now")

    assert os.environ["LOCAL_TEST_KEY"] == "written-now"
    assert not settings.set_by_the_environment("LOCAL_TEST_KEY")
    assert "LOCAL_TEST_KEY:" in os.environ[settings.SECRETS_FROM_FILE_ENV]


def test_a_variable_of_the_real_environment_is_told_apart(own_process, monkeypatch):
    monkeypatch.setenv("LOCAL_TEST_KEY", "set-by-the-shell")

    assert settings.set_by_the_environment("LOCAL_TEST_KEY")
    assert not settings.set_by_the_environment("LOCAL_TEST_OTHER")


def test_the_real_config_leaves_the_local_layer_to_the_loader():
    master = REPO / "config" / "config.yaml"
    assert "local.yaml" not in _expand_includes(yaml.safe_load(master.read_text(encoding="utf-8")), master)


@pytest.mark.parametrize("name", ["config/local.yaml", "config/local.env"])
def test_the_repository_never_takes_it(name):
    assert subprocess.run(["git", "check-ignore", "-q", name], cwd=REPO).returncode == 0


@pytest.mark.parametrize("name", ["config/local.yaml", "config/local.env", "config/secrets.env"])
def test_no_image_takes_it(name):
    """The Dockerfile copies the build context (COPY . /app): the keys would sit in an image layer."""
    lines = {line.strip() for line in (REPO / ".dockerignore").read_text(encoding="utf-8").splitlines()}
    assert name in lines, f"{name} is not in .dockerignore"


def test_one_secrets_file_under_two_spellings_is_read_once(config_dir, own_process, monkeypatch, caplog):
    """The key was the raw relative path, but the resolved absolute one: one file, read twice."""
    (config_dir / "local.env").write_text("LOCAL_TEST_KEY=once\n", encoding="utf-8")
    monkeypatch.chdir(config_dir.parent)
    with caplog.at_level(logging.INFO, logger=settings.logger.name):
        settings._load_secrets_file(Path("config") / "local.env")
        settings._load_secrets_file(config_dir / "local.env")
    assert caplog.text.count("credential(s) from") == 1, caplog.text
