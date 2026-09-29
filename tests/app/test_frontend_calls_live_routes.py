"""Every endpoint the frontend calls has to exist in the app.

The bug this was written for: 57b55828 renamed ``/mcp/status`` to
``/tools/status`` and touched ``static/js/panels/system.js`` in the same
commit -- but only its first comment line, not the call on line 197. The
server list in the system panel would have stopped loading at the next
restart, and nothing anywhere said so: no Python imports that path, no
browser check opens that panel, and a 404 in a panel reads as "empty".

So this does not quote the paths, it EVALUATES them: it reads what the
frontend asks for and holds it against the routes the app really registers.
A rename that forgets one call site is red here whichever call site it is.

Scope, and why it is drawn this way: only string literals that start with
``/``. A path assembled from variables cannot be resolved without running the
page, and that is what the browser suite is for.

That exempts every plugin panel as things stand: they mount under a prefix
they compute (``${BASE}api/channels``), so not one of them contributes a
literal today. Their files are walked anyway -- the day a panel writes an
absolute path it is covered, and until then the floor below keeps "found
nothing there" from turning quietly into "looked nowhere".
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]

#: Where the frontend lives: the shell's own scripts and every plugin panel.
JS_ROOTS = ("static/js", "src/plugins")

#: ``api('/x')``, ``api("/x")``, ``api(`/x…`)`` and the same through fetch().
#: The literal is taken up to the first interpolation, so
#: ``/api/sessions/${id}`` contributes ``/api/sessions/``.
_CALL = re.compile(r"""\b(?:api|fetch)\(\s*(['"`])(/[^'"`$\s)]*)""")

#: A query string or a fragment is not part of the path, and a literal that
#: carries one (`/admin/security/audit?limit=...`) would otherwise look like a
#: route nobody serves. Found by this test on its first run, against itself.
_PATH_END = re.compile(r"[?#]")

#: Paths that are deliberately not routes of this app.
_NOT_OURS = (
    "//",        # protocol-relative URL
)


def _js_files(root: Path) -> list[Path]:
    """Every frontend script the scan below reads."""
    files = []
    for js_root in JS_ROOTS:
        base = root / js_root
        if not base.is_dir():
            continue
        files += [js for js in base.rglob("*.js") if "node_modules" not in js.parts]
    return files


def _called_paths(root: Path) -> dict[str, set[str]]:
    """path -> the files asking for it, over every .js under `root`."""
    found: dict[str, set[str]] = {}
    for js in _js_files(root):
        text = js.read_text(encoding="utf-8", errors="replace")
        for _quote, path in _CALL.findall(text):
            path = _PATH_END.split(path, 1)[0]
            if path.startswith(_NOT_OURS):
                continue
            found.setdefault(path, set()).add(str(js.relative_to(root)))
    return found


def _route_paths() -> set[str]:
    from agent_system.app import build_app

    return {getattr(route, "path", "") for route in build_app().routes}


def _matches(called: str, route: str) -> bool:
    """Does `route` answer `called`?

    Segment by segment, with two allowances: a ``{param}`` segment of the route
    takes anything, and the LAST segment of the called path may be a prefix of
    the route's -- the literal stops wherever the interpolation starts, and
    that is not always a segment boundary (``/tools/status${query}``).
    """
    want = [s for s in called.split("/") if s]
    have = [s for s in route.split("/") if s]
    if len(want) > len(have):
        return False
    for i, segment in enumerate(want):
        target = have[i]
        if target.startswith("{"):
            continue
        if segment == target:
            continue
        if i == len(want) - 1 and target.startswith(segment):
            continue
        return False
    return True


@pytest.fixture(scope="module")
def routes() -> set[str]:
    return _route_paths()


def test_every_path_the_frontend_asks_for_is_a_route_of_this_app(routes):
    called = _called_paths(REPO_ROOT)
    # A scan that finds nothing is green for the wrong reason, and so is a route
    # table that did not come up. All three floors sit far below what is there
    # (17 paths in 50+ files against 180+ routes, measured 2026-09-20).
    files = _js_files(REPO_ROOT)
    assert len(called) >= 8, f"the scan found only {sorted(called)} -- it went blind"
    assert len(routes) >= 20, f"the app registered only {len(routes)} routes"
    # Separately, because the plugin panels contribute no path at all today:
    # without this, dropping them from JS_ROOTS would change nothing visible.
    panels = [f for f in files if "plugins" in f.parts]
    assert len(files) >= 30 and len(panels) >= 10, (
        f"the scan walked {len(files)} scripts, {len(panels)} of them panels")

    orphans = {
        path: sorted(files) for path, files in called.items()
        if not any(_matches(path, route) for route in routes)
    }
    assert not orphans, (
        "the frontend calls paths this app does not serve -- a rename that "
        "missed a call site:\n  " + "\n  ".join(
            f"{path} ({', '.join(files)})" for path, files in sorted(orphans.items())))


def test_the_matcher_tells_a_renamed_prefix_from_a_parameter():
    """The matcher is the whole test; a lenient one passes on anything.

    The real rename is the first case: `/mcp/...` against a table that only has
    `/tools/...`. The rest are the allowances, each of which a stricter matcher
    would get wrong and turn into a false alarm.
    """
    assert not _matches("/mcp/status", "/tools/status")
    assert not _matches("/tools/status", "/tools")           # deeper than the route
    assert not _matches("/tools/cache/statistics", "/tools/cache/invalidate")
    assert _matches("/tools/status", "/tools/status")
    assert _matches("/api/sessions/", "/api/sessions/{session_id}")   # interpolated id
    assert _matches("/tools/status", "/tools/status/{name}")          # literal stops early
    assert _matches("/admin/system", "/admin/system")
