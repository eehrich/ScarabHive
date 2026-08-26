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
import re
import sys
from pathlib import Path


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
    "duckduckgo_search": "duckduckgo-search",
    # Coarse: `google` is a namespace shared by many dists (google-cloud-*,
    # google-auth). Today the only google import in any plugin is genai; a
    # plugin importing e.g. google.cloud would slip past this mapping and
    # needs its own entry then.
    "google": "google-genai",
}

#: (plugin, import) pairs that are deliberately undeclared, each with a reason.
KNOWN_OPTIONAL = {
    # Guarded fallback import inside a function; the plugin works without it
    # and says so in the error path (errors.py).
    ("script_interpreter", "sandboxed_python"),
    # Dev-layout absolute import (`from src.plugins...`) in __main__/cli —
    # not a package.
    ("sqlite_query", "src"),
}


def _norm(name: str) -> str:
    return name.lower().replace("-", "_").replace(".", "_")


def _core_dists() -> set[str]:
    out = set()
    for req in (REPO_ROOT / "requirements").glob("*.txt"):
        if req.name == "all.txt":
            continue
        for line in req.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.split("#")[0].strip()
            if line:
                out.add(_norm(re.split(r"[<>=\[\s]", line)[0]))
    return out


def _plugin_dirs() -> list[Path]:
    roots = (REPO_ROOT / "src" / "plugins", REPO_ROOT / "src" / "plugins_llm")
    return sorted(d for root in roots for d in root.iterdir()
                  if (d / "plugin.toml").exists())


def _declared(plugin_dir: Path) -> set[str]:
    data = tomllib.loads((plugin_dir / "plugin.toml").read_text(encoding="utf-8"))
    deps = data.get("plugin", {}).get("dependencies", [])
    reqs = data.get("plugin", {}).get("requires", {})
    names = {re.split(r"[<>=\[\s]", d)[0] for d in deps}
    names |= {k for k in reqs if k != "agent_system"}
    return {_norm(n) for n in names}


def _third_party_imports(plugin_dir: Path) -> set[str]:
    stdlib = set(sys.stdlib_module_names)
    found = set()
    for py in plugin_dir.rglob("*.py"):
        if "tests" in py.parts:
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
                if top in ("agent_system", "plugins", "plugins_writer", "plugins_llm"):
                    continue
                found.add(top)
    return found


def test_every_import_is_declared_in_core_or_the_plugins_toml():
    core = _core_dists()
    assert len(core) >= 20, "core requirements did not load — test would be vacuous"
    plugins = _plugin_dirs()
    assert len(plugins) >= 30, f"only {len(plugins)} plugins found — scan went blind"

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
#: with the reason. Anything new here needs the same kind of justification.
CORE_LLM_KNOWN_PLUGIN_DEPS = {
    # tts.py (Gemini TTS, its own factory chain) imports google-genai lazily;
    # the dist is owned by plugins_llm/llm_gemini (see its plugin.toml) and
    # installed via requirements/all.txt. Documented last Gemini remnant in
    # core — moves to the plugin once writer_audio's import path can change.
    "google",
}


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
