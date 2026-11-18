"""How layers.py reads its files: an include named by an ABSOLUTE glob (outside the config directory) loads -- it
used to fail on relative_to -- and the master, an include and the local layer are all read the same way
(read_config_text: UTF-8 with or without a BOM, or UTF-16 with one)."""
from __future__ import annotations

from pathlib import Path

from agent_system.config.settings import config_files, load_settings

SERVER = "plugins:\n  servers:\n    {name}:\n      type: basic_agent\n      description: {text}\n"


def test_an_absolute_glob_include_outside_the_config_directory_loads(tmp_path: Path):
    config_dir, elsewhere = tmp_path / "config", tmp_path / "elsewhere"
    config_dir.mkdir()
    elsewhere.mkdir()
    (elsewhere / "extra.yaml").write_text(SERVER.format(name="far", text="from afar"), encoding="utf-8")
    master = config_dir / "config.yaml"
    master.write_text(f"includes:\n  - '{(elsewhere / '*.yaml').as_posix()}'\n", encoding="utf-8")

    assert [path.resolve() for path in config_files(str(master))] == [
        master.resolve(), (elsewhere / "extra.yaml").resolve()]
    assert load_settings(str(master)).plugins.servers["far"].description == "from afar"


def test_a_relative_template_is_relative_to_the_file_that_writes_it_in_the_master_too(tmp_path: Path, monkeypatch):
    """`./prompts/x.md` in the master stayed as written and was opened from the process's start folder; in an
    include and in the local layer it was the file's own folder all along."""
    template = "      agent_config:\n        system_template: ./prompts/x.md\n"
    (tmp_path / "config/agents").mkdir(parents=True)
    master = tmp_path / "config/config.yaml"
    master.write_text("includes:\n  - agents/*.yaml\n" + SERVER.format(name="in_master", text="m") + template, encoding="utf-8")
    (tmp_path / "config/agents/extra.yaml").write_text(
        SERVER.format(name="in_include", text="i") + template, encoding="utf-8")
    monkeypatch.chdir(tmp_path)  # not the config folder: a path left relative would point here

    servers = load_settings(str(master)).plugins.servers

    assert Path(servers["in_master"].agent_config.system_template) == (tmp_path / "config/prompts/x.md").resolve()
    assert Path(servers["in_include"].agent_config.system_template) == (tmp_path / "config/agents/prompts/x.md").resolve()


def test_master_and_include_are_read_as_the_local_layer_is(tmp_path: Path):
    """A BOM in the master, UTF-16 in an include: what PowerShell 5.1 writes, read as the local layer always was."""
    master = tmp_path / "config.yaml"
    master.write_bytes(b"\xef\xbb\xbf" + "includes:\n  - extra.yaml\n".encode("utf-8"))
    (tmp_path / "extra.yaml").write_bytes(SERVER.format(name="wide", text="from utf16").encode("utf-16"))

    assert load_settings(str(master)).plugins.servers["wide"].description == "from utf16"
