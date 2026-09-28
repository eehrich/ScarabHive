"""What the semantic index stores: one document per symbol, not per file.

Measured on this repository (18.09.2026, all-MiniLM-L6-v2, the model behind
:mod:`agent_system.utils.vector_store`):

* the model reads **256 tokens** and drops the rest silently,
* a whole source file as one document therefore embeds the module head — which
  is why a search for a function used to answer with READMEs,
* a 50-line window is no better: 95 % of them are longer than 256 tokens, a
  30-line window still 81 %. Code is token-dense,
* a symbol — its signature plus the first lines of its docstring — has a median
  of 59 tokens (p90 241). What the index holds is what the model actually read.

So the unit here is a symbol: a function, a class, a heading section, and for
everything else a short window. Each document carries its path, because the
path is part of what a question is about ("the sub agent manager's cancel"),
and each knows its line, so a hit points at code instead of at a file.

The path is the one relative to the indexed directory. An absolute path puts
the same prefix -- home directory, checkout location, a temp dir -- in front of
every document: it tells them apart by nothing, dilutes each of them, and made
the ranking depend on where the tree lies (measured 28.09.2026: "end a login
session" ranked a method first under a Windows or Linux temp path and its
class first under the macOS one; relative, the method, on every machine).
"""

from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path
from typing import List

#: A generic window, for text that has neither symbols nor headings. Sized to
#: stay under the model's 256 tokens for prose and config; code lands there
#: only when its own parser fails.
WINDOW_LINES = 20
WINDOW_STEP = 16

#: How much of a docstring or a section body goes into the vector. Beyond this
#: the model reads nothing anyway.
MAX_BODY_CHARS = 700

#: Symbols are real structure, so the ceiling only guards against the absurd.
#: It used to be 400 for everything, which silently dropped the tail of large
#: test files -- 400 is a normal number of test functions in this repository.
MAX_SYMBOLS_PER_FILE = 2000

#: Windows are guesses, and a data file makes thousands of them. Measured on
#: the coder tree: one Blender .obj mesh produced 400 documents and 62 JSON
#: data files produced 3.521 -- noise that costs embedding time and competes
#: with real code for the top ten.
MAX_WINDOWS_PER_FILE = 150

#: What the documents look like. A store built from another format is rebuilt,
#: not mixed with new documents (search.py, the index state).
#: 2: the path in a document is relative to the indexed directory.
DOCUMENT_FORMAT = 2

PYTHON_SUFFIXES = {".py", ".pyi"}
HEADING_SUFFIXES = {".md", ".markdown", ".rst"}

#: Only these get cut into windows. Anything else a text sniffer lets through
#: -- a mesh, a lockfile, a dump, a fixture -- is worth exactly one document:
#: findable by its path, unable to flood the index.
WINDOW_SUFFIXES = {
    ".yaml", ".yml", ".toml", ".ini", ".cfg", ".conf", ".env", ".properties",
    ".js", ".mjs", ".cjs", ".ts", ".tsx", ".jsx", ".vue", ".svelte",
    ".c", ".h", ".cpp", ".hpp", ".cs", ".java", ".go", ".rs", ".rb", ".php",
    ".lua", ".gd", ".sh", ".bash", ".ps1", ".bat", ".sql", ".asm", ".s",
    ".html", ".htm", ".css", ".scss", ".xml", ".xsd", ".txt", ".tex",
}


@dataclass(frozen=True)
class SymbolDoc:
    """One unit of the semantic index."""

    line: int
    #: What a result list shows: "def Class.method(a, b)", "## Heading", "lines 40-60"
    label: str
    #: What is embedded.
    text: str


def documents(path: Path, text: str) -> List[SymbolDoc]:
    """Split *text* into the documents the index stores for *path*."""
    if not text.strip():
        return []
    suffix = path.suffix.lower()
    if suffix in PYTHON_SUFFIXES:
        docs = _python(path, text)
        # A file that does not parse (a template, Python 2, a fragment) still
        # has content worth finding, so it falls through to windows rather
        # than out of the index.
        if docs:
            return docs[:MAX_SYMBOLS_PER_FILE]
    elif suffix in HEADING_SUFFIXES:
        docs = _headings(path, text)
        if docs:
            return docs[:MAX_SYMBOLS_PER_FILE]
    # Source that failed its own parser still belongs in windows -- the
    # single-document treatment is for what was never code or prose.
    if suffix and suffix not in WINDOW_SUFFIXES | PYTHON_SUFFIXES | HEADING_SUFFIXES:
        return [_doc(path, 1, "file", text[:MAX_BODY_CHARS])]
    return _windows(path, text)[:MAX_WINDOWS_PER_FILE]


def _doc(path: Path, line: int, label: str, body: str) -> SymbolDoc:
    body = body.strip()[:MAX_BODY_CHARS]
    return SymbolDoc(line=line, label=label,
                     text=f"{path}\n{label}\n{body}" if body else f"{path}\n{label}")


def _python(path: Path, text: str) -> List[SymbolDoc]:
    """Module docstring plus every function and class, methods included."""
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError, RecursionError):
        return []

    docs: List[SymbolDoc] = []
    module_doc = ast.get_docstring(tree)
    if module_doc:
        docs.append(_doc(path, 1, "module", module_doc))

    def visit(node: ast.AST, prefix: str) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                args = ", ".join(a.arg for a in child.args.args)
                label = f"def {prefix}{child.name}({args})"
                docs.append(_doc(path, child.lineno, label,
                                 ast.get_docstring(child) or ""))
                # Nested helpers inside a function are implementation detail;
                # indexing them buries the function they belong to.
            elif isinstance(child, ast.ClassDef):
                bases = ", ".join(b.id for b in child.bases if isinstance(b, ast.Name))
                label = f"class {prefix}{child.name}({bases})"
                docs.append(_doc(path, child.lineno, label,
                                 ast.get_docstring(child) or ""))
                visit(child, f"{prefix}{child.name}.")

    visit(tree, "")
    return docs


def _headings(path: Path, text: str) -> List[SymbolDoc]:
    """One document per heading section, the heading itself as the label."""
    docs: List[SymbolDoc] = []
    body: List[str] = []
    heading, start = None, 1
    for number, line in enumerate(text.splitlines(), 1):
        if line.startswith("#") or line.startswith("=="):
            if heading is not None or body:
                docs.append(_doc(path, start, heading or "intro", "\n".join(body)))
            heading, start, body = line.strip("# ").strip() or "section", number, []
        else:
            body.append(line)
    if heading is not None or body:
        docs.append(_doc(path, start, heading or "intro", "\n".join(body)))
    return docs


def _windows(path: Path, text: str) -> List[SymbolDoc]:
    """Overlapping line windows — the fallback for YAML, JSON, JS, anything."""
    lines = text.splitlines()
    docs: List[SymbolDoc] = []
    for start in range(0, max(len(lines), 1), WINDOW_STEP):
        chunk = lines[start:start + WINDOW_LINES]
        body = "\n".join(chunk).strip()
        if body:
            docs.append(_doc(path, start + 1,
                             f"lines {start + 1}-{start + len(chunk)}", body))
        if start + WINDOW_LINES >= len(lines):
            break
    return docs
