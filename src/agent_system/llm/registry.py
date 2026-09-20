"""LLM provider registry — lazy dispatch to provider plugins.

Providers live as plugins under ``src/plugins/``, beside the tool
servers and told apart from them by ``type = ["llm-provider"]`` in the
manifest — they had a root of their own until 2026-09-20, and nothing
but the manifest says so now. Each one declares in its ``plugin.toml``
which provider names it serves (``provides``, plus the
optional ``provides_batch`` / ``provides_tts`` / ``provides_decisions``) and
exports the matching factories from its entrypoint module (``PROVIDERS`` /
``BATCH_BACKENDS`` / ``TTS_PROVIDERS`` / ``DECISION_PROVIDERS`` dicts). A
plugin may serve only the non-chat seams: llm_decisions declares no
``provides`` at all, and nothing there can be reached through ``chat()``.

The registry keeps the load order problem out of bootstrap entirely: the
first call for a name scans only the manifests (cheap, no imports) and then
imports exactly the plugin whose manifest declares that name — for the seam
being asked for, not just ``provides``. Unused providers — and their SDK
dependencies — are never imported.

``build_client`` is the ONE construction seam. The root conftest.py replaces
it with a fake so bootstrap never opens sockets; the original stays
available as ``_orig_build_client``. Everything above this seam
(``factory._build_client``) binds it late for exactly that reason.
"""
from __future__ import annotations

import importlib
import importlib.util
import logging
import sys
import threading
from pathlib import Path
from typing import Callable, Dict, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from agent_system.config.models import LLMModelConfig
    from agent_system.llm.batch.base import BatchProviderClient
    from agent_system.llm.models import LLMClient
    from agent_system.llm.tts import TTSClient

logger = logging.getLogger(__name__)

#: A provider factory builds a ready client from a RESOLVED model config
#: (profile lookup, llm_params overlay, and system defaults already applied
#: by the resolver in factory.py).
ProviderFactory = Callable[..., "LLMClient"]
#: A batch backend factory returns the queue backend for one batch provider,
#: or None when it cannot come up (e.g. no API key) — the caller logs skips.
BatchBackendFactory = Callable[..., Optional["BatchProviderClient"]]

PLUGIN_PACKAGE = "plugins"

#: Every seam this registry dispatches: the manifest key a plugin declares it
#: under, the dict its provider.py must export, and what a name collision is
#: called in the log. The scan, the loader and the reset all read THIS -- a
#: fifth seam is one row plus its own get_/build_/known_ trio, and no fourth
#: place that can be forgotten. It used to be four places: the reset forgot the
#: decisions seam for a day, and no test noticed.
SEAMS: Dict[str, tuple] = {
    "provides": ("PROVIDERS", "LLM provider"),
    "provides_batch": ("BATCH_BACKENDS", "Batch backend"),
    "provides_tts": ("TTS_PROVIDERS", "TTS provider"),
    "provides_decisions": ("DECISION_PROVIDERS", "Decisions provider"),
}

_lock = threading.RLock()
#: manifest key -> {served name: plugin dir name}, and the same shape for the
#: factories a loaded plugin exported. Both are filled IN PLACE and never
#: rebound: whoever holds one of these dicts (the tests do, and a table of
#: references would) must not end up reading a previous scan's state.
_owners: Dict[str, Dict[str, str]] = {key: {} for key in SEAMS}
_exports: Dict[str, Dict[str, Callable]] = {key: {} for key in SEAMS}
#: Whether the scan has run. Was `_provider_dirs is not None` -- which tied
#: "did we scan" to one particular seam's dict.
_scanned = False
#: provider name -> endpoint its factory defaults to (manifest
#: `default_base_url`). Only providers that HAVE a default appear.
_default_base_urls: Dict[str, str] = {}


class ProviderNotFoundError(ValueError):
    """No plugin's manifest declares the requested provider."""


def _plugins_root() -> Path:
    """Locate the plugins package directory.

    Preferred: wherever the import system finds it (installed package, or
    src/ already on sys.path). Fallbacks for running from a repo checkout
    whose src/ is not on the path yet: next to the agent_system package
    (editable install / checkout), then CWD-relative — the same convention
    plugin_dirs in config/plugins.yaml uses. The fallback's parent goes on
    sys.path so the later import_module resolves to the SAME module objects
    tests import directly (no duplicate class identities).
    """
    # sys.modules first, and deliberately. plugins/discovery.py registers a
    # hand-built types.ModuleType under this name when nothing has imported
    # the package yet; such a module carries __spec__ = None, and find_spec
    # on a module already in sys.modules reads exactly that attribute and
    # raises ValueError instead of searching. Its __path__ is the directory
    # we want anyway. Measured 2026-09-20 — until the providers moved out of
    # their own root this asked for a package discovery never touches, so
    # the collision could not happen.
    module = sys.modules.get(PLUGIN_PACKAGE)
    search = getattr(module, "__path__", None)
    if search is None:
        try:
            spec = importlib.util.find_spec(PLUGIN_PACKAGE)
        except ValueError:
            spec = None  # in sys.modules, but not a package
        search = spec.submodule_search_locations if spec else None
    if search:
        return Path(next(iter(search)))
    src = Path(__file__).resolve().parents[2]
    for cand in (src / PLUGIN_PACKAGE, Path.cwd() / "src" / PLUGIN_PACKAGE):
        if cand.is_dir():
            parent = str(cand.parent)
            if parent not in sys.path:
                sys.path.insert(0, parent)
            return cand
    raise ProviderNotFoundError(
        f"LLM provider plugin root '{PLUGIN_PACKAGE}' not found "
        f"(looked via import system, {src / PLUGIN_PACKAGE}, and CWD)")


def _read_manifest(plugin_dir: Path) -> Dict:
    """The [plugin] table of one plugin.toml; {} when absent/unreadable.

    Parsed with tomllib directly, NOT via agent_system.plugins.plugin_manifest:
    importing that submodule executes the plugins package __init__, which
    pulls the whole tool/Web runtime (fastapi, starlette, ...) — measured at
    ~0.4s and ~330 modules. Config validation calls into this scan, so it
    must stay light.
    """
    import tomllib
    toml_path = plugin_dir / "plugin.toml"
    if not toml_path.exists():
        return {}
    try:
        with toml_path.open("rb") as fh:
            data = tomllib.load(fh)
    except Exception as e:
        logger.warning("Failed to parse %s: %s", toml_path, e)
        return {}
    meta = data.get("plugin", data)
    if not isinstance(meta, dict):
        logger.warning(
            "%s: [plugin] is a %s, not a table — plugin ignored",
            toml_path, type(meta).__name__)
        return {}
    return meta


def _scan_manifests() -> None:
    """Read every plugins/*/plugin.toml once; imports nothing.

    Every plugin's, not just the providers': the manifest is what says
    which is which. 63 directories, 15 ms, once per process (measured
    2026-09-20) — and no import, which is the part that has to stay
    true, because config validation calls in here.
    """
    global _scanned
    if _scanned:
        return

    # Filled here, published at the end. The scan has to be ATOMIC: before
    # this table existed the loop filled locals and rebound the globals in one
    # step, so a manifest that threw mid-loop left nothing behind. Mutating the
    # published dicts directly would keep half a scan — and the next attempt
    # would then re-claim what the first one already claimed, warning about a
    # plugin colliding with ITSELF for every manifest read before the failure.
    owners: Dict[str, Dict[str, str]] = {key: {} for key in SEAMS}
    base_urls: Dict[str, str] = {}

    def claim(seam: Dict[str, str], names, kind: str, plugin: str) -> None:
        """First manifest wins a name — and says so.

        Silent first-wins on a collision makes the winner depend on directory
        order: whoever is alphabetically first owns the provider, and the
        loser looks like a plugin that simply does nothing. Warned for every
        kind, not just `provides` (batch/tts used to lose quietly).
        """
        for name in names or []:
            if name in seam:
                logger.warning(
                    "%s '%s' declared by both %s and %s — keeping %s",
                    kind, name, seam[name], plugin, seam[name])
                continue
            seam[name] = plugin

    for plugin_dir in sorted(_plugins_root().iterdir()):
        if not plugin_dir.is_dir():
            continue
        meta = _read_manifest(plugin_dir)
        if "llm-provider" not in (meta.get("type") or []):
            continue
        for key, (_attr, label) in SEAMS.items():
            claim(owners[key], meta.get(key), label, plugin_dir.name)
        mapping = meta.get("default_base_url")
        if isinstance(mapping, dict):
            base_urls.update({str(k): str(v) for k, v in mapping.items()})
        elif mapping is not None:
            logger.warning(
                "%s: default_base_url must be a table, got %s — ignored",
                plugin_dir.name, type(mapping).__name__)
    # Published in place, never rebound: a holder of one of these dicts (the
    # tests hold them, and a table of references would) must not end up
    # reading a previous scan's state.
    for key in SEAMS:
        _owners[key].clear()
        _owners[key].update(owners[key])
    _default_base_urls.clear()
    _default_base_urls.update(base_urls)
    _scanned = True
    logger.debug("LLM provider manifests: %s", _owners)


def _load_plugin(dir_name: str) -> None:
    """Import one plugin's entrypoint module and take over its factories.

    Only names this plugin's MANIFEST declares are taken over. Without that
    filter an undeclared export could claim a name another plugin owns by
    manifest, and which one wins would depend on load order — the manifest
    is the vocabulary (it is what config validation reads), so it decides
    here too. An export the manifest never declared is dead weight, and the
    manifest-agreement test names it.
    """
    module = importlib.import_module(f"{PLUGIN_PACKAGE}.{dir_name}.provider")
    for key, (attr, _label) in SEAMS.items():
        for name, factory in (getattr(module, attr, None) or {}).items():
            if _owners[key].get(name) != dir_name:
                continue
            _exports[key].setdefault(name, factory)


def get_provider(provider: str) -> ProviderFactory:
    with _lock:
        factory = _exports["provides"].get(provider)
        if factory is not None:
            return factory
        _scan_manifests()
        dir_name = _owners["provides"].get(provider)
        if dir_name is None:
            known = sorted(_owners["provides"])
            raise ProviderNotFoundError(
                f"Unknown LLM provider: {provider} (known: {', '.join(known)})")
        try:
            _load_plugin(dir_name)
        except ImportError as e:
            raise ImportError(
                f"LLM provider plugin '{dir_name}' failed to import for "
                f"provider '{provider}' — are its plugin.toml dependencies "
                f"installed? ({e})") from e
        factory = _exports["provides"].get(provider)
        if factory is None:
            raise ProviderNotFoundError(
                f"Plugin '{dir_name}' declares provider '{provider}' in its "
                f"manifest but its PROVIDERS dict does not export it")
        return factory


def get_batch_backend(batch_provider: str) -> Optional[BatchBackendFactory]:
    """Batch backend factory for one provider, or None if none is declared."""
    with _lock:
        factory = _exports["provides_batch"].get(batch_provider)
        if factory is not None:
            return factory
        _scan_manifests()
        dir_name = _owners["provides_batch"].get(batch_provider)
        if dir_name is None:
            return None
        try:
            _load_plugin(dir_name)
        except ImportError as e:
            raise ImportError(
                f"LLM provider plugin '{dir_name}' failed to import for "
                f"batch provider '{batch_provider}' — are its plugin.toml "
                f"dependencies installed? ({e})") from e
        factory = _exports["provides_batch"].get(batch_provider)
        if factory is None:
            # None means "nobody declares this" — but here somebody DID and
            # then failed to export it. Returning None made the caller report
            # "Unknown batch provider", which sends the operator looking for
            # a config typo that isn't there.
            raise ProviderNotFoundError(
                f"Plugin '{dir_name}' declares batch provider "
                f"'{batch_provider}' in its manifest but its BATCH_BACKENDS "
                f"dict does not export it")
        return factory


def get_tts_provider(tts_provider: str) -> Callable:
    """TTS factory for one provider name (manifest key ``provides_tts``)."""
    with _lock:
        factory = _exports["provides_tts"].get(tts_provider)
        if factory is not None:
            return factory
        _scan_manifests()
        dir_name = _owners["provides_tts"].get(tts_provider)
        if dir_name is None:
            known = sorted(_owners["provides_tts"])
            raise ProviderNotFoundError(
                f"Unknown TTS provider: {tts_provider} (known: {', '.join(known)})")
        try:
            _load_plugin(dir_name)
        except ImportError as e:
            raise ImportError(
                f"LLM provider plugin '{dir_name}' failed to import for "
                f"TTS provider '{tts_provider}' — are its plugin.toml "
                f"dependencies installed? ({e})") from e
        factory = _exports["provides_tts"].get(tts_provider)
        if factory is None:
            raise ProviderNotFoundError(
                f"Plugin '{dir_name}' declares TTS provider '{tts_provider}' "
                f"in its manifest but its TTS_PROVIDERS dict does not export it")
        return factory


def build_tts_client(cfg) -> "TTSClient":
    """Build the TTS client for a TTSModelConfig — the TTS construction seam.

    Same lazy-SDK caveat as build_client: a missing dependency surfaces at
    the factory call, so it gets the plugin-naming wrapper here too.
    """
    factory = get_tts_provider(cfg.provider)
    try:
        return factory(cfg)
    except ImportError as e:
        dir_name = _owners["provides_tts"].get(cfg.provider, "?")
        raise ImportError(
            f"LLM provider plugin '{dir_name}' failed while building TTS "
            f"provider '{cfg.provider}' — are its plugin.toml dependencies "
            f"installed? ({e})") from e


def known_tts_providers() -> frozenset:
    """TTS provider names any plugin manifest declares — no imports happen."""
    with _lock:
        _scan_manifests()
        return frozenset(_owners["provides_tts"])


def get_decisions_provider(decisions_provider: str) -> Callable:
    """Decisions factory for one provider (manifest key ``provides_decisions``)."""
    with _lock:
        factory = _exports["provides_decisions"].get(decisions_provider)
        if factory is not None:
            return factory
        _scan_manifests()
        dir_name = _owners["provides_decisions"].get(decisions_provider)
        if dir_name is None:
            known = sorted(_owners["provides_decisions"])
            raise ProviderNotFoundError(
                f"Unknown decisions provider: {decisions_provider} "
                f"(known: {', '.join(known)})")
        try:
            _load_plugin(dir_name)
        except ImportError as e:
            raise ImportError(
                f"LLM provider plugin '{dir_name}' failed to import for "
                f"decisions provider '{decisions_provider}' — are its "
                f"plugin.toml dependencies installed? ({e})") from e
        factory = _exports["provides_decisions"].get(decisions_provider)
        if factory is None:
            raise ProviderNotFoundError(
                f"Plugin '{dir_name}' declares decisions provider "
                f"'{decisions_provider}' in its manifest but its "
                f"DECISION_PROVIDERS dict does not export it")
        return factory


def build_decisions_client(cfg):
    """Build the client for a DecisionModelConfig — the decisions seam.

    Same lazy-dependency caveat as build_client: a missing dependency
    surfaces at the factory call, so it gets the plugin-naming wrapper here
    too.
    """
    factory = get_decisions_provider(cfg.provider)
    try:
        return factory(cfg)
    except ImportError as e:
        dir_name = _owners["provides_decisions"].get(cfg.provider, "?")
        raise ImportError(
            f"LLM provider plugin '{dir_name}' failed while building "
            f"decisions provider '{cfg.provider}' — are its plugin.toml "
            f"dependencies installed? ({e})") from e


def known_decisions_providers() -> frozenset:
    """Decisions provider names any manifest declares — no imports happen."""
    with _lock:
        _scan_manifests()
        return frozenset(_owners["provides_decisions"])


def known_batch_providers() -> frozenset:
    """Batch provider names any plugin manifest declares (``provides_batch``)."""
    with _lock:
        _scan_manifests()
        return frozenset(_owners["provides_batch"])


def default_base_url(provider: str) -> Optional[str]:
    """Endpoint a provider uses when the model entry names none.

    Declared per provider in the manifests::

        default_base_url = { openai_responses = "https://openrouter.ai/api/v1" }

    The resolver needs it to answer "does this model actually talk to
    OpenRouter?" for entries without a base_url — previously answered by
    naming the one provider that defaults there, in core code. The factory
    still applies its own default; the agreement between the two is pinned by
    tests/llm/test_llm_openrouter_routing_default.py.
    """
    with _lock:
        _scan_manifests()
        return _default_base_urls.get(provider)


def batch_client_provider(batch_provider: str) -> str:
    """Which PROVIDER builds the client for ``provider: batch`` models.

    Every LLM client brings its OWN batch backend under its own name, so the
    answer is always the name itself — ``batch_provider: openai_httpx`` pairs
    the httpx client with the httpx batch backend, ``batch_provider: gemini``
    pairs the Gemini client with the Gemini one. This function exists to
    VALIDATE the name and to keep the rule in one place; the core used to
    carry an if-chain over gemini/openai/anthropic that mapped one name onto
    another (openai -> openai_httpx), which is exactly the hardcoded provider
    list the plugin seam exists to avoid.
    """
    with _lock:
        _scan_manifests()
        if batch_provider not in _owners["provides_batch"]:
            known = sorted(_owners["provides_batch"])
            raise ProviderNotFoundError(
                f"Unknown batch_provider: {batch_provider} — no plugin under "
                f"src/plugins declares it via provides_batch "
                f"(known: {', '.join(known)})")
        return batch_provider


def known_providers() -> frozenset:
    """Provider names any plugin manifest declares — no imports happen.

    This is the vocabulary check config validation uses
    (LLMSystemConfig._providers_must_exist_as_plugins); the manifests are
    the single source of truth for what `provider:` may say.
    """
    with _lock:
        _scan_manifests()
        return frozenset(_owners["provides"])


def build_client(cfg: "LLMModelConfig", ssl_verify: Optional[bool] = None) -> "LLMClient":
    """Build the client for a RESOLVED model config — the one construction seam."""
    logger.debug(
        "build_client provider=%s model=%s api_key_set=%s base_url=%s",
        cfg.provider, cfg.model, bool(cfg.api_key), cfg.base_url)
    factory = get_provider(cfg.provider)
    try:
        return factory(cfg, ssl_verify=ssl_verify)
    except ImportError as e:
        # The SDK imports are LAZY inside the client constructors, so a
        # missing dependency surfaces here — at the factory call, not at
        # plugin import. Name the plugin, or the operator only sees a bare
        # "No module named 'anthropic'". ImportError, not just its
        # ModuleNotFoundError subclass: a broken compiled extension or a DLL
        # that won't load raises the parent, and that is the case where
        # knowing which plugin was being built matters most.
        dir_name = _owners["provides"].get(cfg.provider, "?")
        raise ImportError(
            f"LLM provider plugin '{dir_name}' failed while building "
            f"provider '{cfg.provider}' — are its plugin.toml dependencies "
            f"installed? ({e})") from e


def reset_for_tests() -> None:
    """Drop all cached scan/import state (test isolation)."""
    global _scanned
    with _lock:
        _scanned = False
        _default_base_urls.clear()
        for key in SEAMS:
            _owners[key].clear()
            _exports[key].clear()
