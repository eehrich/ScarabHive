"""What the semantic index stores, and what it must never store.

The measured reason these tests exist (18.09.2026): the embedding model reads
256 tokens and drops the rest without a word. Every assertion here is about
that boundary -- a document that is too long is not an error anywhere, it is
just a file half of which cannot be found.
"""
from __future__ import annotations

from pathlib import Path

from plugins.file_ops import symbols

PY = '''\
"""Module about tea."""
import os


def brew(leaves, water):
    """Pour water over leaves."""
    return leaves + water


class Pot:
    """A vessel."""

    def pour(self, cup):
        """Fill the cup."""
        return cup

    class Lid:
        """Nested on purpose."""

        def close(self):
            return True
'''


def labels(docs):
    return [d.label for d in docs]


def test_a_python_file_is_split_into_its_symbols():
    docs = symbols.documents(Path("tea.py"), PY)
    assert labels(docs) == [
        "module",
        "def brew(leaves, water)",
        "class Pot()",
        "def Pot.pour(self, cup)",
        "class Pot.Lid()",
        "def Pot.Lid.close(self)",
    ]


def test_every_symbol_knows_the_line_it_starts_on():
    """A hit that cannot say where it is costs a full file read."""
    lines = {d.label: d.line for d in symbols.documents(Path("tea.py"), PY)}
    body = PY.splitlines()
    assert body[lines["def brew(leaves, water)"] - 1].startswith("def brew")
    assert body[lines["def Pot.pour(self, cup)"] - 1].strip().startswith("def pour")


def test_the_document_carries_path_signature_and_docstring():
    """All three are what the query is compared against: a question names the
    module, the function or the words of its description."""
    doc = next(d for d in symbols.documents(Path("a/tea.py"), PY)
               if d.label.startswith("def brew"))
    assert "a/tea.py" in doc.text or "a\\tea.py" in doc.text
    assert "def brew(leaves, water)" in doc.text
    assert "Pour water over leaves." in doc.text


def test_a_body_is_cut_to_what_the_model_reads():
    """A docstring of 40k characters would be embedded up to its first 256
    tokens either way -- but it would also be stored, queried and shipped."""
    long_doc = 'def f():\n    """' + ("word " * 20000) + '"""\n    return 1\n'
    doc = symbols.documents(Path("long.py"), long_doc)[0]
    assert len(doc.text) <= symbols.MAX_BODY_CHARS + 200


def test_a_file_that_does_not_parse_still_lands_in_the_index():
    """Python 2, a template, a fragment: unparsable is not uninteresting."""
    docs = symbols.documents(Path("broken.py"), "def f(:\n  print 'hi'\n")
    assert docs and docs[0].label.startswith("lines ")


def test_markdown_is_split_at_its_headings():
    text = "# Title\nintro\n\n## Setup\nrun it\n\n## Usage\ncall it\n"
    docs = symbols.documents(Path("r.md"), text)
    assert labels(docs) == ["Title", "Setup", "Usage"]
    assert "run it" in docs[1].text
    assert docs[1].line == 4


def test_anything_else_becomes_overlapping_windows():
    """YAML, JSON, JS: no parser, but the content still has to be findable,
    and the overlap is what keeps a hit from being cut in half."""
    text = "\n".join(f"key_{i}: value" for i in range(50))
    docs = symbols.documents(Path("c.yaml"), text)
    assert len(docs) > 1
    assert docs[0].line == 1 and docs[1].line == symbols.WINDOW_STEP + 1
    assert "key_17: value" in docs[0].text and "key_17: value" in docs[1].text


def test_one_data_file_cannot_fill_the_whole_index():
    """Measured on the coder tree: one Blender mesh made 400 documents and the
    JSON fixtures 3.521 -- embedding time spent on noise that then competes
    with real code for the top ten."""
    text = "\n".join(f"line {i}" for i in range(50_000))
    assert len(symbols.documents(Path("huge.yaml"), text)) == \
        symbols.MAX_WINDOWS_PER_FILE


def test_a_file_that_is_not_code_or_prose_is_one_document():
    """A mesh, a lockfile, a dump: findable by its path, unable to flood."""
    mesh = "\n".join(f"v {i}.0 {i}.5 {i}.2" for i in range(5000))
    docs = symbols.documents(Path("ball.obj"), mesh)
    assert len(docs) == 1 and docs[0].label == "file"


def test_a_large_source_file_keeps_all_of_its_symbols():
    """The old ceiling of 400 cut the tail off large test files silently --
    and 400 test functions is a normal size in this repository."""
    text = "\n\n".join(f'def test_{i}():\n    """Check {i}."""\n    return {i}'
                       for i in range(600))
    docs = symbols.documents(Path("test_big.py"), text)
    assert len(docs) == 600


def test_an_empty_file_is_no_document():
    assert symbols.documents(Path("e.py"), "   \n\n") == []


def test_a_heading_with_no_body_is_still_one_document_and_no_more():
    """An empty vector matches everything a little and nothing well, so blank
    stretches must not become documents of their own.

    (This replaces an assertion that every document has non-empty text -- it
    could not fail, because a document always carries its path and label.)
    """
    docs = symbols.documents(Path("b.md"), "# One\n\n\n\n## Two\n\n\n")
    assert labels(docs) == ["One", "Two"]
