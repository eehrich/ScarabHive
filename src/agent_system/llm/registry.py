"""LLM provider registry — lazy dispatch to provider plugins.

Providers live as plugins under ``src/plugins_llm/``; each one declares in
its ``plugin.toml`` which provider names it serves (``provides``, plus
optional ``provides_batch``) and exports the matching factories from its
entrypoint module (``PROVIDERS`` / ``BATCH_BACKENDS`` dicts).

The registry keeps the load order problem out of bootstrap entirely: the
first ``build_client()`` call scans only the manifests (cheap, no imports)
and then imports exactly the plugin whose ``provides`` contains the
requested provider. Unused providers — and their SDK dependencies — are
never imported.

``build_client`` is the ONE construction seam. tests/conftest.py replaces
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

PLUGIN_PACKAGE = "plugins_llm"

_lock = threading.RLock()
_provider_dirs: Optional[Dict[str, str]] = None  # provider name -> plugin dir name
_batch_dirs: Optional[Dict[str, str]] = None  # batch provider name -> plugin dir name
_tts_dirs: Optional[Dict[str, str]] = None  # tts provider name -> plugin dir name
_factories: Dict[str, ProviderFactory] = {}
_batch_backends: Dict[str, BatchBackendFactory] = {}
_tts_factories: Dict[str, Callable] = {}


class ProviderNotFoundError(ValueError):
    """No plugin under plugins_llm declares the requested provider."""


def _plugins_root() -> Path:
    """Locate the plugins_llm package directory.

    Preferred: wherever the import system finds it (installed package, or
    src/ already on sys.path). Fallbacks for running from a repo checkout
    whose src/ is not on the path yet: next to the agent_system package
    (editable install / checkout), then CWD-relative — the same convention
    plugin_dirs in config/plugins.yaml uses. The fallback's parent goes on
    sys.path so the later import_module resolves to the SAME module objects
    tests import directly (no duplicate class identities).
    """
    spec = importlib.util.find_spec(PLUGIN_PACKAGE)
    if spec and spec.submodule_search_locations:
        return Path(next(iter(spec.submodule_search_locations)))
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
    pulls the whole MCP/Web runtime (fastapi, starlette, ...) — measured at
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
    return meta if isinstance(meta, dict) else {}


def _scan_manifests() -> None:
    """Read every plugins_llm/*/plugin.toml once; imports nothing."""
    global _provider_dirs, _batch_dirs, _tts_dirs
    if _provider_dirs is not None:
        return

    providers: Dict[str, str] = {}
    batches: Dict[str, str] = {}
    tts: Dict[str, str] = {}
    for plugin_dir in sorted(_plugins_root().iterdir()):
        if not plugin_dir.is_dir():
            continue
        meta = _read_manifest(plugin_dir)
        if "llm-provider" not in (meta.get("type") or []):
            continue
        for name in meta.get("provides") or []:
            if name in providers:
                logger.warning(
                    "LLM provider '%s' declared by both %s and %s — keeping %s",
                    name, providers[name], plugin_dir.name, providers[name])
                continue
            providers[name] = plugin_dir.name
        for name in meta.get("provides_batch") or []:
            if name not in batches:
                batches[name] = plugin_dir.name
        for name in meta.get("provides_tts") or []:
            if name not in tts:
                tts[name] = plugin_dir.name
    _provider_dirs = providers
    _batch_dirs = batches
    _tts_dirs = tts
    logger.debug("LLM provider manifests: %s (batch: %s, tts: %s)",
                 providers, batches, tts)


def _load_plugin(dir_name: str) -> None:
    """Import one plugin's entrypoint module and take over its factories."""
    module = importlib.import_module(f"{PLUGIN_PACKAGE}.{dir_name}.provider")
    for name, factory in (getattr(module, "PROVIDERS", None) or {}).items():
        _factories.setdefault(name, factory)
    for name, factory in (getattr(module, "BATCH_BACKENDS", None) or {}).items():
        _batch_backends.setdefault(name, factory)
    for name, factory in (getattr(module, "TTS_PROVIDERS", None) or {}).items():
        _tts_factories.setdefault(name, factory)


def get_provider(provider: str) -> ProviderFactory:
    with _lock:
        factory = _factories.get(provider)
        if factory is not None:
            return factory
        _scan_manifests()
        dir_name = (_provider_dirs or {}).get(provider)
        if dir_name is None:
            known = sorted(_provider_dirs or {})
            raise ProviderNotFoundError(
                f"Unknown LLM provider: {provider} (known: {', '.join(known)})")
        try:
            _load_plugin(dir_name)
        except ImportError as e:
            raise ImportError(
                f"LLM provider plugin '{dir_name}' failed to import for "
                f"provider '{provider}' — are its plugin.toml dependencies "
                f"installed? ({e})") from e
        factory = _factories.get(provider)
        if factory is None:
            raise ProviderNotFoundError(
                f"Plugin '{dir_name}' declares provider '{provider}' in its "
                f"manifest but its PROVIDERS dict does not export it")
        return factory


def get_batch_backend(batch_provider: str) -> Optional[BatchBackendFactory]:
    """Batch backend factory for one provider, or None if none is declared."""
    with _lock:
        factory = _batch_backends.get(batch_provider)
        if factory is not None:
            return factory
        _scan_manifests()
        dir_name = (_batch_dirs or {}).get(batch_provider)
        if dir_name is None:
            return None
        try:
            _load_plugin(dir_name)
        except ImportError as e:
            raise ImportError(
                f"LLM provider plugin '{dir_name}' failed to import for "
                f"batch provider '{batch_provider}' — are its plugin.toml "
                f"dependencies installed? ({e})") from e
        return _batch_backends.get(batch_provider)


def get_tts_provider(tts_provider: str) -> Callable:
    """TTS factory for one provider name (manifest key ``provides_tts``)."""
    with _lock:
        factory = _tts_factories.get(tts_provider)
        if factory is not None:
            return factory
        _scan_manifests()
        dir_name = (_tts_dirs or {}).get(tts_provider)
        if dir_name is None:
            known = sorted(_tts_dirs or {})
            raise ProviderNotFoundError(
                f"Unknown TTS provider: {tts_provider} (known: {', '.join(known)})")
        try:
            _load_plugin(dir_name)
        except ImportError as e:
            raise ImportError(
                f"LLM provider plugin '{dir_name}' failed to import for "
                f"TTS provider '{tts_provider}' — are its plugin.toml "
                f"dependencies installed? ({e})") from e
        factory = _tts_factories.get(tts_provider)
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
    except ModuleNotFoundError as e:
        dir_name = (_tts_dirs or {}).get(cfg.provider, "?")
        raise ImportError(
            f"LLM provider plugin '{dir_name}' failed while building TTS "
            f"provider '{cfg.provider}' — are its plugin.toml dependencies "
            f"installed? ({e})") from e


def known_tts_providers() -> frozenset:
    """TTS provider names any plugin manifest declares — no imports happen."""
    with _lock:
        _scan_manifests()
        return frozenset(_tts_dirs or {})


def known_providers() -> frozenset:
    """Provider names any plugin manifest declares — no imports happen.

    This is the vocabulary check config validation uses
    (LLMSystemConfig._providers_must_exist_as_plugins); the manifests are
    the single source of truth for what `provider:` may say.
    """
    with _lock:
        _scan_manifests()
        return frozenset(_provider_dirs or {})


def build_client(cfg: "LLMModelConfig", ssl_verify: Optional[bool] = None) -> "LLMClient":
    """Build the client for a RESOLVED model config — the one construction seam."""
    logger.debug(
        "build_client provider=%s model=%s api_key_set=%s base_url=%s",
        cfg.provider, cfg.model, bool(cfg.api_key), cfg.base_url)
    factory = get_provider(cfg.provider)
    try:
        return factory(cfg, ssl_verify=ssl_verify)
    except ModuleNotFoundError as e:
        # The SDK imports are LAZY inside the client constructors, so a
        # missing dependency surfaces here — at the factory call, not at
        # plugin import. Name the plugin, or the operator only sees a bare
        # "No module named 'anthropic'".
        dir_name = (_provider_dirs or {}).get(cfg.provider, "?")
        raise ImportError(
            f"LLM provider plugin '{dir_name}' failed while building "
            f"provider '{cfg.provider}' — are its plugin.toml dependencies "
            f"installed? ({e})") from e


def reset_for_tests() -> None:
    """Drop all cached scan/import state (test isolation)."""
    global _provider_dirs, _batch_dirs, _tts_dirs
    with _lock:
        _provider_dirs = None
        _batch_dirs = None
        _tts_dirs = None
        _factories.clear()
        _batch_backends.clear()
        _tts_factories.clear()
