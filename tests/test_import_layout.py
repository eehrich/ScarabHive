"""Guard against the pythonpath footgun of `pythonpath = src tests`.

pytest.ini puts tests/ on sys.path as a second import root (for shared
test helpers such as tool_execution_test_helpers.py). As a result every
tests/ subdirectory becomes importable as a top-level NAMESPACE package --
tests/config, tests/llm, tests/utils etc. collide by name with real
import roots (src packages; tests/mcp did so with the site-packages
package `mcp` until it was renamed on 2026-09-17). This holds today only
because REGULAR packages (with __init__.py) always beat namespace portions.

A single future __init__.py under tests/ (e.g. tests/config/__init__.py)
would silently shadow the real package of the same name and break imports
far from the cause. This meta test turns that into a loud, localized
error.
"""
from pathlib import Path

TESTS_DIR = Path(__file__).parent


def test_no_init_py_in_direct_test_subdirs():
    # Only DIRECT children of tests/ are dangerous: only a
    # tests/<name>/__init__.py makes <name> a regular package that can
    # shadow a real package of the same name. Deeper __init__.py files
    # (e.g. fixture packages under tests/fixtures/real_plugin/) are fine.
    offenders = [
        d.name for d in TESTS_DIR.iterdir()
        if d.is_dir() and d.name != "__pycache__" and (d / "__init__.py").exists()
    ]
    assert not offenders, (
        f"__init__.py found in direct tests/ subdirectories: "
        f"{offenders} -- forbidden, because tests/ is on the pythonpath and "
        f"such an __init__.py would silently shadow the REAL package of the "
        f"same name (site-packages `mcp`, src packages). "
        f"Shared helpers belong as flat modules directly in tests/ "
        f"(see tool_execution_test_helpers.py)."
    )
