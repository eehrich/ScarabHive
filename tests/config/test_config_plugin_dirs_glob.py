"""A wildcard in plugins.plugin_dirs becomes the package roots it matches.

config/plugins.yaml ships ``src/plugins*``: the open-source checkout has only
src/plugins, a private one further src/plugins_<name>/ roots, and no file
names them. Every reader of plugin_dirs (discovery, CLI, runtime, help,
agent_editor) takes the entries as directories, so the pattern has to be
expanded where the config is loaded, not later.
"""
from pathlib import Path

from agent_system.config.settings import load_settings


def test_a_wildcard_entry_becomes_the_matching_package_roots(tmp_path):
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "config.yaml").write_text(
        "plugins:\n  plugin_dirs:\n    - src/plugins*\n", encoding="utf-8")
    for name in ("plugins", "plugins_extra", "plugins.egg-info"):
        (tmp_path / "src" / name).mkdir(parents=True)
    (tmp_path / "src" / "plugins_notes.txt").write_text("", encoding="utf-8")

    dirs = load_settings(str(tmp_path / "config" / "config.yaml")).plugins.plugin_dirs

    # sorted, so src/plugins comes first; no file, no name Python cannot import
    assert [Path(d).name for d in dirs] == ["plugins", "plugins_extra"]
    assert all(Path(d).is_absolute() for d in dirs)


def test_an_absolute_wildcard_is_expanded_too(tmp_path):
    for name in ("plugins", "plugins_extra"):
        (tmp_path / "elsewhere" / name).mkdir(parents=True)
    pattern = (tmp_path / "elsewhere" / "plugins*").as_posix()
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "config.yaml").write_text(
        f"plugins:\n  plugin_dirs:\n    - '{pattern}'\n", encoding="utf-8")

    dirs = load_settings(str(tmp_path / "config" / "config.yaml")).plugins.plugin_dirs

    assert [Path(d).name for d in dirs] == ["plugins", "plugins_extra"]


def test_a_plain_entry_stays_one_directory(tmp_path):
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "config.yaml").write_text(
        "plugins:\n  plugin_dirs:\n    - src/plugins\n", encoding="utf-8")
    (tmp_path / "src" / "plugins").mkdir(parents=True)
    (tmp_path / "src" / "plugins_extra").mkdir()

    dirs = load_settings(str(tmp_path / "config" / "config.yaml")).plugins.plugin_dirs

    assert [Path(d).name for d in dirs] == ["plugins"]
