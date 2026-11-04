"""The small shared helpers: shorten, field_of, dumps_lenient."""
from __future__ import annotations

import datetime
from pathlib import Path
from types import SimpleNamespace

from agent_system.utils.fields import field_of
from agent_system.utils.json_utils import dumps_lenient
from agent_system.utils.text import shorten


def test_shorten_keeps_what_fits_and_cuts_to_the_limit():
    assert shorten("abc", 3) == "abc"
    assert shorten("abcd", 3) == "ab…"
    assert shorten("x" * 81, 80, ellipsis="...") == "x" * 77 + "..."
    assert len(shorten("y" * 200, 60)) == 60


def test_field_of_reads_dicts_and_objects():
    assert field_of({"role": "user"}, "role") == "user"
    assert field_of(SimpleNamespace(role="tool"), "role") == "tool"
    assert field_of({}, "role") is None and field_of(object(), "role", "?") == "?"


def test_dumps_lenient_never_fails_and_keeps_non_ascii():
    text = dumps_lenient({"when": datetime.date(2026, 1, 2), "path": Path("a"), "word": "Grüße"})
    assert '"2026-01-02"' in text and '"Grüße"' in text
    assert dumps_lenient([1], indent=2) == "[\n  1\n]"
