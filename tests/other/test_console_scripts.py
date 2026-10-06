"""The commands `pip install` creates: console_scripts.cfg, continued by
console_scripts.private.cfg where further plugin roots are part of the checkout.

Read through setuptools' own pyproject reader, the one pip's build runs: it
joins the two files into one text, so the private file has to continue the
public file's last section, and it skips a file that does not exist.
"""
import configparser
import re
import shutil
import warnings
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
PUBLIC = ROOT / "console_scripts.cfg"
PRIVATE = ROOT / "console_scripts.private.cfg"


def _scripts(root: Path) -> dict[str, str]:
    from setuptools.config.pyprojecttoml import read_configuration

    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        return read_configuration(root / "pyproject.toml", expand=True)["project"]["scripts"]


def _entries(path: Path) -> dict[str, str]:
    """name -> target of every `name = target` line, whatever section it stands in."""
    return dict(re.findall(r"(?m)^([A-Za-z0-9_-]+) = (\S+)$", path.read_text(encoding="utf-8")))


def test_every_command_points_at_a_function_in_the_checkout():
    scripts = _scripts(ROOT)
    assert "agent-cli" in scripts, scripts
    for name, target in scripts.items():
        module, attr = target.split(":")
        base = ROOT / "src" / Path(*module.split("."))
        source = base.with_suffix(".py") if base.with_suffix(".py").is_file() else base / "__init__.py"
        assert source.is_file(), f"{name}: no module {module}"
        assert re.search(rf"(?m)^(async )?def {attr}\(", source.read_text(encoding="utf-8")), \
            f"{name}: {module} has no top-level {attr}()"


def test_the_public_file_names_no_further_plugin_root():
    parser = configparser.ConfigParser()
    parser.optionxform = str
    parser.read(PUBLIC, encoding="utf-8")
    assert parser.sections()[-1] == "console_scripts", "the private file continues the LAST section"
    leaked = {name: target for name, target in parser["console_scripts"].items() if target.startswith("plugins_")}
    assert not leaked, f"commands of a further plugin root belong in {PRIVATE.name}: {leaked}"


def test_the_private_commands_land_among_the_console_scripts():
    if not PRIVATE.exists():
        pytest.skip("no further plugin roots in this checkout")
    private = _entries(PRIVATE)
    assert private, f"{PRIVATE.name} declares no command"
    assert private.items() <= _scripts(ROOT).items()


def test_a_checkout_without_the_private_file_installs_the_public_commands(tmp_path):
    for name in ("pyproject.toml", "console_scripts.cfg", "README.md", "LICENSE"):
        shutil.copy(ROOT / name, tmp_path / name)
    (tmp_path / "requirements").mkdir()
    shutil.copy(ROOT / "requirements" / "all.txt", tmp_path / "requirements" / "all.txt")
    (tmp_path / "src").mkdir()

    assert _scripts(tmp_path) == _entries(PUBLIC)
