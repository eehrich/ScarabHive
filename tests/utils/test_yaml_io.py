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
    # Every spelling that reaches PyYAML's loaders, not only the one this repo
    # happens to use today: the module under any alias (``yaml.``, ``_yaml.``)
    # and the from-import form. A guard that knows one spelling measures the
    # spelling, not the rule.
    pattern = re.compile(
        r"\b\w*yaml\.(?:safe_load|load|full_load|unsafe_load)\("
        r"|from\s+yaml\s+import\s+[^\n]*\b(?:safe_load|load|full_load|unsafe_load)\b"
    )
    # Check the instrument before believing its reading: a pattern that stops
    # matching reports a clean repo forever, and so does a scan over no files.
    for probe in ("x = yaml.safe_load(text)", "_yaml.load(text)",
                  "from yaml import safe_load"):
        assert pattern.search(probe), f"the scan pattern no longer matches {probe!r}"
    scanned = 0
    for pkg in ("agent_system", "plugins", "plugins_writer", "plugins_llm", "plugins_trading"):
        for py in (SRC / pkg).rglob("*.py"):
            if "tests" in py.parts or py.name == "yaml_io.py":
                continue
            scanned += 1
            for lineno, line in enumerate(py.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
                if pattern.search(line):
                    offenders.append(f"{py.relative_to(REPO)}:{lineno}: {line.strip()}")
    assert scanned, "no production file was scanned"
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


def test_a_syntax_error_keeps_the_pure_python_message():
    """What an operator reads when a config file breaks.

    libyaml reports line and column but not the offending line with a caret
    under it; the pure-Python parser does, and that message is the one this
    repo's config errors have always shown.
    """
    bad = "a: 1\n  b: [unclosed\n"
    with pytest.raises(yaml.YAMLError) as err:
        yaml_io.safe_load(bad)

    message = str(err.value)
    assert "line 2" in message, message
    assert "^" in message, f"the source line with the caret is gone:\n{message}"


def test_the_c_parser_is_an_accelerator_not_a_stricter_one():
    """Documents libyaml rejects and SafeLoader accepts must still load.

    Both shapes measured 2026-09-04: a ``%YAML`` directive for a version
    libyaml refuses, and a BOM in the middle of a document. Both went through
    ``yaml.safe_load`` before this module existed, so rejecting them now would
    be a regression nobody asked for -- a config file that loaded yesterday.
    """
    if yaml_io.loader() is yaml.SafeLoader:
        pytest.skip("no libyaml here: there is nothing to fall back from")

    for text, expected in (("%YAML 1.3\n---\na: 1\n", {"a": 1}),
                           ("a: 1\n﻿b: 2\n", {"a": 1, "﻿b": 2})):
        with pytest.raises(yaml.YAMLError):
            yaml.load(text, Loader=yaml_io.loader())  # fixture: the C parser really refuses
        assert yaml_io.safe_load(text) == expected, text


def test_a_lone_surrogate_raises_a_yaml_error():
    """libyaml raises UnicodeEncodeError on it -- not a YAMLError at all, so
    no caller that catches YAML errors would ever see it. The pure-Python
    loader makes it the ReaderError it has always been."""
    if yaml_io.loader() is yaml.SafeLoader:
        pytest.skip("no libyaml here: the C-only error cannot occur")

    with pytest.raises(UnicodeEncodeError):
        yaml.load("a: \ud800", Loader=yaml_io.loader())  # fixture: that is the C error
    with pytest.raises(yaml.YAMLError):
        yaml_io.safe_load("a: \ud800")


def test_a_broken_file_object_still_raises(tmp_path):
    """The re-parse must not swallow the error for a stream it cannot replay:
    a file object is already consumed when the C parser gives up."""
    path = tmp_path / "broken.yaml"
    path.write_text("a: 1\n  b: [unclosed\n", encoding="utf-8")
    with path.open(encoding="utf-8") as handle:
        with pytest.raises(yaml.YAMLError):
            yaml_io.safe_load(handle)
