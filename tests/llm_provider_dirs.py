"""Which plugin directories are LLM providers.

The providers used to live in a root of their own (``src/plugins_llm``), so
"is this an LLM provider" was answered by the parent folder's name. They moved
into ``src/plugins`` on 2026-09-20 and the folder no longer says it — but the
manifests always did, and it is the same predicate the registry's manifest scan
uses to decide whose ``provides`` it may claim.

Two anti-drift scans need this, and they need the SAME answer: one exempts the
providers from "no plugin builds a client by hand" (building clients is their
job), the other limits "every async httpx client passes verify=" to the LLM
path. Defined once, because a selector kept in two places drifts in one.
"""
from __future__ import annotations

import tomllib
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"

#: What a provider plugin's manifest says in `type`. agent_system.llm.registry
#: skips every manifest without it.
PROVIDER_TYPE = "llm-provider"


def llm_provider_dirs(root: Path | None = None) -> list[Path]:
    """Every plugin directory whose manifest declares the provider type.

    Sorted, so a caller's error message is stable. Raises when the root is
    gone: a scan over an empty list is green for the wrong reason, and both
    callers use this to *narrow* their own scan.
    """
    root = root or (SRC / "plugins")
    if not root.is_dir():
        raise AssertionError(f"{root} is not a directory — the scan would be vacuous")
    found = []
    for plugin_dir in sorted(root.iterdir()):
        manifest = plugin_dir / "plugin.toml"
        if not manifest.is_file():
            continue
        with manifest.open("rb") as fh:
            meta = tomllib.load(fh).get("plugin", {})
        if PROVIDER_TYPE in (meta.get("type") or []):
            found.append(plugin_dir)
    if not found:
        raise AssertionError(
            f"no plugin under {root} declares type = [\"{PROVIDER_TYPE}\"] — "
            f"either they moved again or the manifest key was renamed")
    return found
