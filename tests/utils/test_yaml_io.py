"""The YAML helper must actually use libyaml, fall back safely, agree with the
pure-Python parser on every document in the repo, and be the ONLY loader
production code calls -- one site reverting to ``yaml.safe_load`` silently
puts the 8.8x cost back.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

from agent_system.utils import yaml_io

REPO = Path(__file__).resolve().parents[2]
SRC = REPO / "src"


def _repo_yaml_files() -> list[Path]:
    files = set(REPO.glob("config/**/*.yaml"))
    for pkg in ("plugins", "plugins_writer", "plugins_llm"):
        files.update((SRC / pkg).rglob("schema.yaml"))
        files.update((SRC / pkg).rglob("plugin.yaml"))
    return sorted(files)


def test_uses_the_c_loader_when_libyaml_is_present():
    if not yaml.__with_libyaml__:
        pytest.skip("PyYAML built without libyaml on this interpreter")
    assert yaml_io.loader() is yaml.CSafeLoader


def test_falls_back_to_the_python_safe_loader_without_libyaml(monkeypatch):
    monkeypatch.delattr(yaml, "CSafeLoader", raising=False)
    assert yaml_io.loader() is yaml.SafeLoader
    assert yaml_io.safe_load("a: 1\nb: [x, y]\n") == {"a": 1, "b": ["x", "y"]}


def test_c_and_python_loaders_agree_on_every_repo_yaml():
    """Parity over the real documents, not a toy: config tree, every plugin
    schema and manifest. Jinja-templated schemas that are not valid YAML raw
    are skipped -- they are compared rendered, in the schema loader tests."""
    compared = 0
    for path in _repo_yaml_files():
        text = path.read_text(encoding="utf-8")
        try:
            expected = yaml.load(text, Loader=yaml.SafeLoader)
        except yaml.YAMLError:
            continue
        assert yaml_io.safe_load(text) == expected, str(path)
        compared += 1
    assert compared >= 50, f"only {compared} files compared -- the fixture is too small to mean anything"


def test_no_production_code_calls_the_pure_python_loader():
    """Anti-drift: every load in src/ (outside tests and the helper itself)
    goes through yaml_io. ``src/scripts`` is deliberately excluded -- dev
    tools that do not import agent_system."""
    offenders = []
    pattern = re.compile(r"\byaml\.(safe_load|load|full_load|unsafe_load)\(")
    for pkg in ("agent_system", "plugins", "plugins_writer", "plugins_llm"):
        for py in (SRC / pkg).rglob("*.py"):
            if "tests" in py.parts or py.name == "yaml_io.py":
                continue
            for lineno, line in enumerate(py.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
                if pattern.search(line):
                    offenders.append(f"{py.relative_to(REPO)}:{lineno}: {line.strip()}")
    assert not offenders, "\n".join(offenders)


def test_load_settings_parses_through_the_helper(monkeypatch):
    """The production path, not a rebuild: loading the real config tree must
    reach yaml_io.safe_load -- a site that keeps its own loader would pass
    the static scan only if it used a different spelling."""
    from agent_system.config import settings

    calls = []
    original = yaml_io.safe_load

    def counting(stream):
        calls.append(1)
        return original(stream)

    monkeypatch.setattr(yaml_io, "safe_load", counting)
    settings.load_settings(str(REPO / "config" / "config.yaml"))
    assert len(calls) > 1, "load_settings did not parse through yaml_io"
