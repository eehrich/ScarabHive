"""
LLM factory helpers.

Provides a factory that creates LLM clients from AgentSystemConfig and AgentConfig.
This keeps LLM creation explicit and testable (dependency injection).
Uses the new profile-based configuration system.
"""
from __future__ import annotations

import copy
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional, TYPE_CHECKING

from ..config.models import (
    AgentSystemConfig, AgentConfig, LLMModelConfig, LLMSystemConfig,
    resolve_llm_params,
)
from .models import LLMClient

if TYPE_CHECKING:
    from .batch.queue_manager import BatchQueueManager

logger = logging.getLogger(__name__)


# Global batch queue manager - lazy initialized on first use
_batch_queue_manager: Optional["BatchQueueManager"] = None
_batch_manager_config: Optional[AgentSystemConfig] = None  # Config for lazy init


def set_batch_queue_manager(manager: Optional["BatchQueueManager"]) -> None:
    """Set the global batch queue manager."""
    global _batch_queue_manager
    _batch_queue_manager = manager
    if manager:
        logger.info("Batch queue manager registered globally")


def set_batch_config(config: AgentSystemConfig) -> None:
    """Store config for lazy batch manager initialization."""
    global _batch_manager_config
    _batch_manager_config = config


def get_batch_queue_manager() -> Optional["BatchQueueManager"]:
    """Get or create the global batch queue manager (lazy init)."""
    global _batch_queue_manager
    
    if _batch_queue_manager is not None:
        return _batch_queue_manager
    
    # Lazy init if we have config
    if _batch_manager_config is not None:
        _batch_queue_manager = _create_batch_queue_manager_sync(_batch_manager_config)
        
    return _batch_queue_manager


def _create_batch_queue_manager_sync(config: AgentSystemConfig) -> Optional["BatchQueueManager"]:
    """Create batch queue manager synchronously (without starting async tasks)."""
    if not config.llm_system or not config.llm_system.models:
        return None
    
    # Check for global batch config
    batch_system_config = config.llm_system.batch
    if not batch_system_config:
        return None
    
    # Check if any provider is enabled
    if not any(p.enabled for p in batch_system_config.providers.values()):
        return None
    
    # Check if any model uses batch provider
    has_batch_model = any(
        m.provider == "batch" for m in config.llm_system.models.values()
    )
    if not has_batch_model:
        return None
    
    from .batch.queue_manager import BatchQueueManager
    
    storage_path = Path(batch_system_config.storage_path)
    manager = BatchQueueManager(
        batch_system_config=batch_system_config,
        storage_path=storage_path
    )
    logger.info("Batch queue manager created (lazy init)")
    return manager


def agent_params_for_profile(agent_config: Any, llm_profile: str,
                            extra: Optional[Dict[str, Any]] = None) -> Optional[Dict[str, Any]]:
    """The llm_params an override of *llm_profile* runs with, caller's last.

    An override picks another MODEL, not another agent: what the agent says
    about every model it runs on ("*" or flat) has to reach the override the
    same way _create_fallback_llm carries it into a fallback. Dropped, the
    agent's own settings were silently gone for that run -- measured on the
    coder, whose context_window 200000 and prompt_cache_mode never reached a
    profile picked in the panel, and whose calls were then counted against
    the model's 272000 instead.
    """
    own = resolve_llm_params(getattr(agent_config, "llm_params", None), llm_profile) or {}
    merged = {**own, **(extra or {})}
    return merged or None


def create_llm_from_profile(
    config: AgentSystemConfig,
    llm_profile: str,
    ssl_verify: Optional[bool] = None,
    llm_params: Optional[dict] = None,
) -> LLMClient:
    """Create an LLM client from a profile name.

    This is the recommended way to create LLM clients when you need to
    override the default profile. It properly handles batch mode wrapping.

    Args:
        config: The system configuration
        llm_profile: Name of the LLM profile to use
        ssl_verify: Optional SSL verification override
        llm_params: Optional per-agent LLM parameter overrides
            (agent_config.llm_params, flat ODER profil-gekeyt) — applied over
            the resolved model config, see resolve_llm_config_for_agent().

    Returns:
        LLMClient (possibly wrapped with BatchLLMClient if batch mode enabled)
    """
    # Create temporary agent config with override profile (+ optional per-agent
    # llm_params so callers with agent context propagate their overrides).
    # Profil-gekeyte Params werden HIER auf das Zielprofil aufgeloest — die
    # temp-Config kennt die Ketten des Original-Agents nicht und wuerde
    # fremde Profil-Keys sonst (zu Recht) ablehnen.
    temp_agent_config = AgentConfig(
        llm_profile=llm_profile,
        llm_params=resolve_llm_params(llm_params, llm_profile),
    )
    return _build_client(config, temp_agent_config, ssl_verify)



@dataclass
class ResolvedLLM:
    """Result of profile resolution: the ONE vocabulary handed to providers.

    ``spec`` is the model config with everything already applied that used
    to be re-assembled downstream: per-agent llm_params overlay, the system
    httpx_timeouts default, the merged OpenRouter provider_routing, and —
    for batch models — the underlying provider. Provider factories read it
    directly; there is no flattened kwargs dict anymore.
    """
    spec: LLMModelConfig
    profile_name: str
    model_ref: str
    is_batch: bool = False
    batch_provider: Optional[str] = None


def _stamp_profile(client: Any, profile: str) -> None:
    """Which profile *client* runs: a run switched to it hands that to its
    sub-agents (llm/caller_llm.py). A client that takes no attributes (a
    provider plugin's slotted object) still runs -- it just passes nothing on.
    """
    try:
        client.profile_name = profile
    except AttributeError:
        logger.debug("LLM client %s takes no profile_name; sub-agents will not inherit it",
                     type(client).__name__)


def _build_client(
    config: AgentSystemConfig,
    agent_config: AgentConfig,
    ssl_verify: Optional[bool],
) -> LLMClient:
    """Resolve, build, and batch-wrap — the ONE construction path.

    Everything after profile resolution is identical for every caller, so it
    lives exactly once.
    """
    # Imported HERE, not at module level: late binding is what lets
    # conftest/tests swap registry.build_client for a fake (a module-level
    # `from .registry import build_client` would freeze the original).
    from . import registry

    resolved = resolve_llm_config_for_agent(config, agent_config)

    if ssl_verify is None:
        ssl_verify = getattr(config.network, "ssl_verify", None) if config.network else None

    underlying_client = registry.build_client(resolved.spec, ssl_verify=ssl_verify)
    _stamp_profile(underlying_client, resolved.profile_name)

    if resolved.is_batch and resolved.batch_provider:
        queue_manager = get_batch_queue_manager()
        if queue_manager:
            batch_system_config = config.llm_system.batch if config.llm_system else None
            if batch_system_config:
                provider_config = batch_system_config.providers.get(
                    resolved.batch_provider)
                if provider_config and provider_config.enabled:
                    from .batch.batch_client import BatchLLMClient
                    logger.info("Wrapping LLM client with batch support: model=%s, provider=%s",
                                resolved.model_ref, resolved.batch_provider)
                    batch_client = BatchLLMClient(
                        underlying_client=underlying_client,
                        queue_manager=queue_manager,
                        batch_provider_config=provider_config,
                        model_name=resolved.spec.model,
                        batch_provider=resolved.batch_provider,
                    )
                    _stamp_profile(batch_client, resolved.profile_name)
                    return batch_client
        logger.warning(
            "Batch mode requested for model %s but batch system not available. "
            "Falling back to sync mode.",
            resolved.model_ref
        )

    return underlying_client


def _targets_openrouter(model_config: LLMModelConfig) -> bool:
    """Does this model talk to OpenRouter?

    The EFFECTIVE base_url decides, not the configured one: a provider whose
    factory defaults to OpenRouter lands there with an empty base_url, so
    looking only at the config field would let those entries slip through.
    WHICH provider does that is plugin knowledge — the manifests declare it
    as `default_base_url`; this used to be a provider name spelled out here.
    Guarded against drift by tests/llm/test_llm_openrouter_routing_default.py,
    which builds a real client and asserts its default base_url matches.
    """
    from . import registry as _registry

    base_url = model_config.base_url or _registry.default_base_url(
        model_config.provider)
    return "openrouter.ai" in (base_url or "").lower()


def _resolve_provider_routing(
    llm_system: LLMSystemConfig, model_config: LLMModelConfig
) -> Optional[Dict[str, Any]]:
    """System-Default und Modell-Eintrag zum OpenRouter-"provider"-Objekt mischen.

    Der System-Default (``llm_system.openrouter_routing``, z.B. ``{sort: price}``
    fuer den jeweils guenstigsten Anbieter) greift nur an OpenRouter-Endpunkten:
    ``provider`` ist ein OpenRouter-Body-Feld, ein fremder Endpunkt bekaeme einen
    unbekannten Key.

    Gemischt wird FLACH und nur auf oberster Ebene: ein Schluessel, den der
    Modell-Eintrag setzt, ersetzt den Default-Wert **ganz**. Bei verschachtelten
    Werten heisst das, dass Unter-Schluessel des Defaults verschwinden —
    ``max_price: {prompt: 1, completion: 2}`` + Modell ``max_price: {prompt: 5}``
    ergibt ``{prompt: 5}``, der completion-Deckel ist weg. Absicht: ein
    Deep-Merge auf einem freien ``Dict[str, Any]`` waere die groessere
    Ueberraschung.

    Der Modell-Eintrag kann den Default pro Schluessel ueberstimmen, ihn aber
    nicht abschalten — ``provider_routing: {}`` heisst "nichts eigenes", nicht
    "kein Routing". Wer ein einzelnes Modell herausnehmen will, setzt dort einen
    Gegenwert (z.B. ``sort: throughput``).

    Achtung auf die Semantik, nicht nur auf die Schluessel: ein Modell mit
    ``order`` behaelt sein ``order``, sendet ab jetzt aber ``{order: [...],
    sort: ...}`` — das ist ein anderes Routing als vorher.
    """
    per_model = model_config.provider_routing
    # getattr, not attribute access: callers hand in partial config objects
    # and test doubles, and an absent optional field means "no system
    # default" — not a crash that takes the client build with it. Two
    # basic_agent tests died on exactly that when the field was added.
    defaults = getattr(llm_system, "openrouter_routing", None)
    if not defaults or not _targets_openrouter(model_config):
        return per_model
    return {**defaults, **(per_model or {})}


def resolve_llm_config_for_agent(
    config: AgentSystemConfig, agent_config: AgentConfig
) -> ResolvedLLM:
    """
    Resolve LLM configuration for a specific agent using the profile system.

    Args:
        config: The main system configuration containing LLM system
        agent_config: The agent-specific configuration

    Returns:
        ResolvedLLM: fully resolved model config plus batch metadata,
        ready for registry.build_client()
    """
    if not config.llm_system:
        raise ValueError("LLM system configuration is missing from AgentSystemConfig")

    # Get the default profile name from agent config
    profile_name = agent_config.default_llm_profile

    # Resolve profile to model config
    if profile_name not in config.llm_system.profiles:
        raise ValueError(f"Profile '{profile_name}' not found in LLM system profiles")

    profile = config.llm_system.profiles[profile_name]
    model_ref = profile.model_ref

    if model_ref not in config.llm_system.models:
        raise ValueError(f"Model reference '{model_ref}' not found in LLM system models")

    model_config = config.llm_system.models[model_ref]

    # Per-Agent LLM-Parameter-Overrides (agent_config.llm_params): über den
    # referenzierten Model-Config-Eintrag legen, statt für jede Kombination
    # (Modell × thinking_level × max_tokens …) einen eigenen models-Eintrag in
    # llm.yaml anzulegen. Re-Validierung über LLMModelConfig hält die
    # Typ-Garantien; Identitäts-Felder sind durch den AgentConfig-Validator
    # gesperrt. Greift für ALLE Profile, die über diese agent_config aufgelöst
    # werden (default/advanced/escalation) — Fallback-Profile laufen bewusst
    # ohne (deren Call-Sites reichen keine llm_params durch).
    # Profil-gekeyte Form wird auf das hier aufgelöste Profil reduziert
    # (merge("*", params[profil])); Flat-Form gilt unverändert.
    llm_params = resolve_llm_params(
        getattr(agent_config, "llm_params", None), profile_name
    )
    if llm_params:
        model_config = LLMModelConfig.model_validate(
            {**model_config.model_dump(), **llm_params}
        )
        logger.debug("Applied agent llm_params on model_ref=%s: %s", model_ref, llm_params)

    # If provider is "batch", batch_provider names the underlying provider
    provider = model_config.provider
    batch_provider = model_config.batch_provider
    is_batch_model = provider == "batch"

    if is_batch_model:
        if not batch_provider:
            raise ValueError(f"Model '{model_ref}' has provider='batch' but no batch_provider specified")
        # Which client a batch model uses is declared by the plugin that
        # serves that batch provider, not listed here.
        from . import registry as _registry
        provider = _registry.batch_client_provider(batch_provider)
        logger.debug("Batch model detected: %s (batch_provider=%s)", model_ref, batch_provider)

    # Stamp resolved values into the spec so provider factories read ONE
    # object: the mapped provider, the system httpx_timeouts default, and
    # the merged OpenRouter routing.
    updates: Dict[str, Any] = {}
    if provider != model_config.provider:
        updates["provider"] = provider
    if is_batch_model:
        # The spec is fully resolved; the batch origin lives on ResolvedLLM.
        updates["batch_provider"] = None
    if not model_config.httpx_timeouts and config.llm_system.httpx_timeouts:
        # A copy, not the shared instance: LLMModelConfig is not frozen, so a
        # factory mutating spec.httpx_timeouts must not edit the system config.
        # (model_copy(update=...) applies update values AS-IS, so the deep
        # copy below does not cover this one.)
        updates["httpx_timeouts"] = config.llm_system.httpx_timeouts.model_copy()
    provider_routing = _resolve_provider_routing(config.llm_system, model_config)
    if provider_routing != model_config.provider_routing:
        # deepcopy for the same reason as httpx_timeouts above: update values
        # are applied AS-IS, and the merged dict is only shallow-fresh — its
        # nested values (`order: [...]`) are still the system config's own
        # objects, so an in-place edit would travel back into it.
        updates["provider_routing"] = copy.deepcopy(provider_routing)
    # ALWAYS a deep copy, updates or not: without it the spec aliases the
    # shared registry entry (config.llm_system.models[...]) including its
    # capabilities/provider_routing objects, and any factory that ever
    # normalizes a field in place would edit the system config process-wide.
    spec = model_config.model_copy(update=updates, deep=True)

    logger.debug("Resolved LLM config: profile=%s, model_ref=%s, provider=%s, model=%s",
                 profile_name, model_ref, provider, spec.model)

    return ResolvedLLM(
        spec=spec,
        profile_name=profile_name,
        model_ref=model_ref,
        is_batch=is_batch_model,
        batch_provider=batch_provider if is_batch_model else None,
    )


class LLMFactory:
    """Create LLM clients from configuration objects.

    This wrapper centralizes the logic for creating an LLM client so that
    callers (for example bootstrap code) can explicitly create an LLM
    without relying on import-time side effects.
    
    When provider='batch' is set in the model config, the factory wraps
    the underlying LLM client with a BatchLLMClient that routes requests
    through the global BatchQueueManager for 50% cost reduction.
    """

    def __init__(self, config: Optional[AgentSystemConfig] = None, agent_config: Optional[AgentConfig] = None):
        self.config = config
        self.agent_config = agent_config

    def create(self) -> Optional[LLMClient]:
        """Create an LLM client from the provided configurations.

        Delegates to the same construction path as create_llm_from_profile —
        this method used to carry its own copy of the forwarding list, and the
        two had already drifted once.
        """
        if not self.config or not self.agent_config:
            return None

        return _build_client(self.config, self.agent_config, ssl_verify=None)
