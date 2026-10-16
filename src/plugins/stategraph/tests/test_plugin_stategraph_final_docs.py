"""The two format references keep up with the code: every diagnostic the validator emits and every error
type the engine raises has its row in docs/stategraph_design.md and in the skill's format.md."""

import re
from pathlib import Path

import pytest

from plugins.stategraph.model.spec import KNOWN_ERROR_TYPES

PLUGIN = Path(__file__).resolve().parents[1]
DESIGN = PLUGIN.parents[2] / "docs" / "stategraph_design.md"
SKILL_FORMAT = PLUGIN / "skills" / "stategraph-authoring" / "references" / "format.md"
DOCS = [DESIGN, SKILL_FORMAT]


def emitted_codes() -> set[str]:
    return {code for path in PLUGIN.rglob("*.py") if "tests" not in path.parts
            for code in re.findall(r"[\"'](SG\d{3})[\"']", path.read_text(encoding="utf-8"))}


def undocumented_codes(doc: str, codes: set[str]) -> set[str]:
    return {code for code in codes if not re.search(rf"^\| {code} \|", doc, re.MULTILINE)}


def undocumented_errors(doc: str, errors) -> set[str]:
    return {name for name in errors if f"`{name}`" not in doc}


@pytest.mark.parametrize("doc", DOCS, ids=lambda p: p.name)
def test_every_emitted_diagnostic_has_a_row(doc):
    codes = emitted_codes()
    assert {"SG001", "SG113"} <= codes, "the scan found no diagnostics -- this test would be vacuous"
    assert not undocumented_codes(doc.read_text(encoding="utf-8"), codes)


@pytest.mark.parametrize("doc", DOCS, ids=lambda p: p.name)
def test_every_engine_error_type_is_named(doc):
    assert not undocumented_errors(doc.read_text(encoding="utf-8"), KNOWN_ERROR_TYPES)
