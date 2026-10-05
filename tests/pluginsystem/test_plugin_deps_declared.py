"""Every third-party import a plugin makes must be declared somewhere.

The failure this guards against reached production on 2026-08-20:
image_compose imported svglib/reportlab (lazy, at render time), declared
nothing, and the local venv happened to have both transitively — so every
test was green. A fresh server venv did not, and every SVG layer failed at
render time with a pip hint in the error message.

"Declared somewhere" means: in requirements/core.txt (framework-owned) or in
the plugin's own plugin.toml `dependencies`. Transitive availability does not
count — it is exactly what made the gap invisible.
"""
from __future__ import annotations

import ast
import importlib.util
import re
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).parents[2]
sys.path.insert(0, str(REPO_ROOT / "src"))

try:
    import tomllib
except ImportError:  # pragma: no cover
    import tomli as tomllib

#: import name -> pip distribution name, where they differ.
IMPORT_TO_DIST = {
    "pil": "pillow", "yaml": "pyyaml", "cv2": "opencv-python",
    "bs4": "beautifulsoup4", "dotenv": "python-dotenv",
    "dateutil": "python-dateutil", "fitz": "pymupdf",
    "tavily": "tavily-python", "ruamel": "ruamel.yaml",
    # Coarse: `google` is a namespace shared by many dists (google-cloud-*,
    # google-auth). Today the only google import in any plugin is genai; a
    # plugin importing e.g. google.cloud would slip past this mapping and
    # needs its own entry then.
    "google": "google-genai",
    # Same shape: `opentelemetry` is the namespace of the api, sdk and every
    # exporter dist. The otel plugin declares all it imports; the api stands
    # for the namespace here.
    "opentelemetry": "opentelemetry-api",
}

#: (plugin, import) pairs that are deliberately undeclared, each with a reason.
KNOWN_OPTIONAL = {
    # Dev-layout absolute import (`from src.plugins...`) in __main__/cli —
    # not a package.
    ("sqlite_query", "src"),
    # scripts/fix_comfyui_fp8.py repairs the transformers/kernels install on
    # the ComfyUI HOST. Both imports sit inside its functions and describe
    # that host's environment, not this plugin's runtime — declaring them
    # would put a remote repair tool's pins into every install.
    ("writer_jobs", "kernels"),
    ("writer_jobs", "transformers"),
}


def _norm(name: str) -> str:
    return name.lower().replace("-", "_").replace(".", "_")


def _core_dists() -> set[str]:
    out = set()
    for req in (REPO_ROOT / "requirements").glob("*.txt"):
        # all.txt is the aggregate itself; private.txt is the private
        # roots' own declarations, not something a public plugin may lean on.
        if req.name in ("all.txt", "private.txt"):
            continue
        for line in req.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.split("#")[0].strip()
            if line:
                out.add(_norm(re.split(r"[<>=\[\s]", line)[0]))
    return out


#: Every root scripts/aggregate_plugin_deps.py collects from — read FROM the
#: aggregator, not copied. A root the aggregator reads but this guard does not
#: is a package whose undeclared imports reach a fresh install unchecked, which
#: is how a whole plugin root stayed unscanned while its manifests fed
#: requirements/all.txt. A second hand-kept list would drift the same way.
def _aggregator_roots() -> tuple[str, ...]:
    spec = importlib.util.spec_from_file_location(
        "_aggregate_plugin_deps", REPO_ROOT / "scripts" / "aggregate_plugin_deps.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)  # import-safe: main() is behind __main__
    return tuple(p.name for p in (*module.PLUGIN_DIRS, *module.PRIVATE_PLUGIN_DIRS))


PLUGIN_ROOTS = _aggregator_roots()


def _plugin_dirs() -> list[Path]:
    """Every package under a plugin root — with or without a manifest.

    A missing plugin.toml is not a reason to skip: it means the package
    declares nothing, so its third-party imports are exactly the ones at risk
    (writer_publish imported numpy with no manifest at all). Scanning it with
    an empty declaration set is what makes "no manifest" safe rather than
    invisible.
    """
    roots = [REPO_ROOT / "src" / name for name in PLUGIN_ROOTS]
    return sorted(d for root in roots if root.is_dir()
                  for d in root.iterdir()
                  # Same skip rule as the aggregator (startswith "." or "_"):
                  # two lists that must agree about which packages exist may
                  # not disagree about which ones to ignore.
                  if d.is_dir() and not d.name.startswith((".", "_")))


def _declared(plugin_dir: Path) -> set[str]:
    manifest = plugin_dir / "plugin.toml"
    if not manifest.exists():
        return set()
    data = tomllib.loads(manifest.read_text(encoding="utf-8"))
    deps = data.get("plugin", {}).get("dependencies", [])
    reqs = data.get("plugin", {}).get("requires", {})
    names = {re.split(r"[<>=\[\s]", d)[0] for d in deps}
    names |= {k for k in reqs if k != "agent_system"}
    return {_norm(n) for n in names}


def _third_party_imports(plugin_dir: Path) -> set[str]:
    stdlib = set(sys.stdlib_module_names)
    found = set()
    for py in plugin_dir.rglob("*.py"):
        # tests/ and test_suite/ are dev tooling: they never ship to a server
        # venv, and their sibling imports (`from run import ...`) are local
        # modules that no pip name could satisfy. Measured 2026-09-01: the
        # three test_suite dirs import stdlib and siblings only, so excluding
        # them hides no real dependency.
        if "tests" in py.parts or "test_suite" in py.parts:
            continue
        # utf-8-sig: a BOM survives plain utf-8 reading as ﻿ and makes
        # ast.parse throw — which the old `except SyntaxError: continue`
        # swallowed, silently blinding this guard for the file (that is how
        # llm_openai/openai_client.py escaped the scan once). Parse failures
        # are now loud: a plugin file the guard cannot read is a finding,
        # not a skip.
        try:
            tree = ast.parse(py.read_text(encoding="utf-8-sig", errors="replace"))
        except SyntaxError as e:
            raise AssertionError(
                f"{py} is unparseable ({e}) — the dependency guard cannot "
                f"scan it, so its imports would go unchecked") from e
        for node in ast.walk(tree):
            mods = []
            if isinstance(node, ast.Import):
                mods = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
                mods = [node.module]
            for mod in mods:
                top = mod.split(".")[0]
                if top.lower() in stdlib:
                    continue
                # The framework, src/plugins and the plugin's own root. Another
                # root is NOT skipped: a public plugin must not import a
                # package the open-source checkout does not carry.
                if top in ("agent_system", "plugins", plugin_dir.parent.name):
                    continue
                found.add(top)
    return found


def test_an_import_from_another_plugin_root_is_a_finding(tmp_path):
    """Only a plugin's own root counts as internal: a public plugin importing a
    package the open-source checkout does not carry would break there."""
    plugin = tmp_path / "src" / "plugins" / "probe"
    plugin.mkdir(parents=True)
    (plugin / "mod.py").write_text(
        "import plugins.other\nimport plugins_extra.thing\nimport agent_system\n", encoding="utf-8")

    assert _third_party_imports(plugin) == {"plugins_extra"}


def test_scan_covers_packages_without_a_manifest():
    """A package with no plugin.toml must be scanned, not skipped.

    The enumeration used to require a manifest, so a package without one was
    invisible — writer_publish imported numpy at module level for years and no
    guard saw it. It has a manifest now, which is exactly why this needs its
    own test: with every offender declared, reverting the enumeration would
    leave the suite green and the hole open.
    """
    if not any(d.is_dir() and d.name.isidentifier() for d in (REPO_ROOT / "src").glob("plugins_*")):
        pytest.skip("every package under src/plugins has a manifest; the examples live in further roots")
    dirs = _plugin_dirs()
    manifestless = [d for d in dirs if not (d / "plugin.toml").exists()]
    assert manifestless, (
        "no manifest-less package left to prove the enumeration covers them — "
        "if that is really true, this test and the empty-set branch in "
        "_declared() can go")
    for d in manifestless:
        assert _declared(d) == set(), f"{d.name}: expected an empty declaration set"


def test_every_import_is_declared_in_core_or_the_plugins_toml():
    core = _core_dists()
    assert len(core) >= 20, "core requirements did not load — test would be vacuous"
    plugins = _plugin_dirs()
    assert len(plugins) >= 65, f"only {len(plugins)} plugins found — scan went blind"
    assert "plugins" in PLUGIN_ROOTS, (
        f"aggregator reports only {PLUGIN_ROOTS} — root list did not load")
    scanned_roots = {d.parent.name for d in plugins}
    present_roots = {r for r in PLUGIN_ROOTS if (REPO_ROOT / "src" / r).is_dir()}
    assert scanned_roots == present_roots, (
        f"scan covers {sorted(scanned_roots)}, aggregator collects from "
        f"{sorted(PLUGIN_ROOTS)} — a root only the aggregator sees is unguarded")

    offenders = []
    scanned_imports = 0
    for plugin_dir in plugins:
        declared = _declared(plugin_dir)
        for imp in sorted(_third_party_imports(plugin_dir)):
            scanned_imports += 1
            if (plugin_dir.name, imp) in KNOWN_OPTIONAL:
                continue
            dist = _norm(IMPORT_TO_DIST.get(imp.lower(), imp))
            if dist in core or dist in declared or _norm(imp) in declared:
                continue
            offenders.append(f"{plugin_dir.name}: imports {imp!r} "
                             f"(dist {dist!r}) — not in core.txt, not in its "
                             f"plugin.toml dependencies")

    assert scanned_imports >= 50, "no imports collected — scanner broken"
    assert not offenders, (
        "Undeclared third-party imports. They may work locally through "
        "TRANSITIVE installs and fail on a fresh server (that is how "
        "image_compose lost SVG rendering in production):\n  "
        + "\n  ".join(offenders))


#: Core llm/ imports that deliberately live on a PLUGIN-declared dist, each
#: with the reason. EMPTY since 2026-08-26: GeminiTTSClient (the last SDK
#: import, google-genai in tts.py) moved to plugins/llm_gemini — core
#: llm/ is fully SDK-free. Anything new here needs a written justification.
CORE_LLM_KNOWN_PLUGIN_DEPS: set = set()


def test_core_llm_imports_stay_provider_free():
    """The provider split's core invariant: src/agent_system/llm/ must not
    grow imports of plugin-owned SDKs. The SDKs left requirements/core.txt,
    so a new core import of one works locally (all.txt installs everything)
    and fails exactly on installs that trim plugins — invisible to every
    other test."""
    core = _core_dists()
    llm_dir = REPO_ROOT / "src" / "agent_system" / "llm"
    assert llm_dir.is_dir()

    imports = _third_party_imports(llm_dir)
    assert len(imports) >= 3, f"scanner collected only {imports} — went blind"

    offenders = [
        imp for imp in sorted(imports)
        if imp not in CORE_LLM_KNOWN_PLUGIN_DEPS
        and _norm(IMPORT_TO_DIST.get(imp.lower(), imp)) not in core
    ]
    assert not offenders, (
        f"core llm/ imports third-party packages that core.txt does not "
        f"declare: {offenders} — either the dep belongs back in core.txt or "
        f"the code belongs in a plugin (documented exceptions: "
        f"{sorted(CORE_LLM_KNOWN_PLUGIN_DEPS)})")


def test_plugin_modules_use_no_parent_relative_imports():
    """Review finding (moved code class): code moved from agent_system into a
    plugin keeps its `from ..hooks import ...` — which now resolves inside the
    plugin root, fails, and (in hook paths wrapped in except Exception) dies
    SILENTLY. Level-1 relative imports (same plugin package) are fine;
    anything above must be absolute.

    src/plugins only. A further plugin root may do the opposite on purpose:
    its plugins reach a shared package of that root through `from ..<shared>`,
    and discovery pre-registers that module for them. The guard used to sit on the LLM providers' own root; they
    live under src/plugins now, and the rule was never about them in
    particular."""
    root = REPO_ROOT / "src" / "plugins"
    offenders = []
    scanned = 0
    for py in root.rglob("*.py"):
        scanned += 1
        tree = ast.parse(py.read_text(encoding="utf-8-sig", errors="replace"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.level >= 2:
                offenders.append(
                    f"{py.relative_to(root)}:{node.lineno}: "
                    f"from {'.' * node.level}{node.module or ''} import ...")
    assert scanned >= 400, f"the scan walked only {scanned} modules — it went blind"
    assert not offenders, (
        "parent-relative imports inside src/plugins resolve against the "
        "plugins package, not agent_system — make them absolute:\n  "
        + "\n  ".join(offenders))
