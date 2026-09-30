"""No code names the data directory itself -- it goes through agent_system.paths.

The data directory is configurable (AGENT_DATA_DIR, paths.data_dir). A new
``"data/writer/x.db"`` default, a ``ROOT / "data" / ...`` join or a
``Path("data")`` would quietly keep writing to the old place once it is moved:
an empty database next to the real one, every query reading zero rows. This
scans the source (AST, not grep: docstrings and comments may mention the
path) and fails on each such spelling. Write ``data_path("writer", "x.db")``
for a default, ``resolve_data_path(value)`` for a value from configuration,
the environment or a database row.
"""
from __future__ import annotations

import ast
import re

from agent_system.paths import PROJECT_ROOT

SRC = PROJECT_ROOT / "src"

#: Files allowed to spell a data path, each with the reason.
ALLOWED = {
    # the one place that defines the default
    "agent_system/paths.py",
    # config-form defaults of the models, shown in the JSON schemas; their
    # consumers resolve them (UserDatabase, BatchQueueManager)
    "agent_system/config/models.py",
    # the portable form stored in books.db and in exchange bundles, not a
    # location; the exporter and importer resolve it
    "plugins_writer/writer_admin/exchange/spec.py",
    # the package's own data folder next to the module, not the data directory
    "plugins_writer/writer_core/cross_scene_checker.py",
}

PREFIX = re.compile(r"^(\./)?data[/\\]")
CALLS = {"Path", "PurePath", "PurePosixPath", "joinpath", "join"}


def _is_data(node: ast.AST) -> bool:
    return isinstance(node, ast.Constant) and node.value == "data"


def spellings(source: str) -> list[tuple[int, str]]:
    """(line, what) for every spelling of the data directory in ``source``."""
    tree = ast.parse(source)
    bare = {id(node.value) for node in ast.walk(tree)
            if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant)}
    found = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            if id(node) not in bare and PREFIX.match(node.value):
                found.append((node.lineno, repr(node.value)))
        elif isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
            if _is_data(node.left) or _is_data(node.right):
                found.append((node.lineno, '/ "data"'))
        elif isinstance(node, ast.Call):
            name = getattr(node.func, "id", None) or getattr(node.func, "attr", None)
            if name in CALLS and any(_is_data(arg) for arg in node.args):
                found.append((node.lineno, f'{name}("data")'))
        elif isinstance(node, (ast.List, ast.Tuple)):
            # a directory list: allowed_directories or ["data"]. Only a list
            # of that one element -- ("type", "data", "cause") names fields.
            # Not a set: {"data"} is a membership test on keys (the keys a
            # JSON body may carry), never a list of directories.
            if len(node.elts) == 1 and _is_data(node.elts[0]):
                found.append((node.lineno, '["data"]'))
        elif isinstance(node, ast.BoolOp):
            # a fallback: value or "data"
            if any(_is_data(value) for value in node.values):
                found.append((node.lineno, 'or "data"'))
        elif isinstance(node, ast.JoinedStr):
            # f"{ROOT}/data/x"
            if any(isinstance(part, ast.Constant) and isinstance(part.value, str)
                   and "/data/" in part.value for part in node.values[1:]):
                found.append((node.lineno, 'f".../data/..."'))
    return found


def test_the_scanner_sees_every_spelling():
    """A scanner that finds nothing would pass this whole file vacuously."""
    source = '''
"""data/in/a/docstring is fine"""
a = "data/writer/books.db"
b = ROOT / "data" / "x"
c = Path("data")
d = os.path.join(root, "data", "x")
e = f"data/okf/{arm}"
f = payload["data"]
g = "metadata/x"
h = cfg.get("allowed_directories") or ["data"]
i = Path(value or "data")
j = f"{ROOT}/data/x"
k = {"kind": "data"}
m = set(parsed) - {"data"}
'''
    assert sorted(line for line, _ in spellings(source)) == [3, 4, 5, 6, 7, 10, 11, 12]


def test_no_code_spells_the_data_directory():
    offenders = []
    for path in sorted(SRC.rglob("*.py")):
        rel = path.relative_to(SRC).as_posix()
        if "tests" in path.parts or rel in ALLOWED:
            continue
        for line, what in spellings(path.read_text(encoding="utf-8")):
            offenders.append(f"src/{rel}:{line}: {what}")
    assert not offenders, (
        "the data directory is configurable -- use agent_system.paths "
        "(data_path for a default, resolve_data_path for a configured value):\n"
        + "\n".join(offenders))


def test_every_allowed_file_still_needs_its_exception():
    """An exception whose file no longer spells the path is dead weight -- and
    the next spelling in that file would slip through unseen."""
    # An entry under a package root this checkout does not have (the writer is
    # not in the open-source one) cannot be judged here; a missing file under a
    # root that is there still fails.
    stale = [rel for rel in sorted(ALLOWED)
             if (SRC / rel.split("/")[0]).is_dir()
             and not spellings((SRC / rel).read_text(encoding="utf-8"))]
    assert not stale, f"no longer needed in ALLOWED: {stale}"
