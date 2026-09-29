"""Registry entrypoint — the factories this plugin's manifest promises.

The LLM registry imports exactly this module when a config entry names one of
the providers declared in ``plugin.toml``, and takes over only the names the
manifest also declares. Nothing here serves ``chat()``: this plugin declares
``provides_decisions`` alone, so ``PROVIDERS`` is deliberately absent and the
plugin never appears as an LLM provider.

Both providers build the same client -- one wire; system_one.py says what was
measured on which host -- and differ only in the ``Host`` they hand it.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from agent_system.config.models import DecisionModelConfig

    from .system_one import Host


def _build(cfg: "DecisionModelConfig", host: "Host"):
    """Lazy import for the same reason as the TTS factories: a process that only
    chats never loads this client."""
    from .system_one import DecisionsClient

    return DecisionsClient(
        model=cfg.model,
        api_key=cfg.api_key,
        # The client resolves the key against the host of THIS url, so a url
        # pointing at a proxy must reach it as configured -- None falls back to
        # the host's own endpoint inside the client, in one place.
        url=cfg.url,
        request_timeout=cfg.request_timeout,
        max_retries=cfg.max_retries,
        host=host,
    )


def build_openrouter_decisions(cfg: "DecisionModelConfig"):
    """OpenRouter's Decisions API (manifest key provides_decisions)."""
    from .system_one import OPENROUTER

    return _build(cfg, OPENROUTER)


def build_systemone_decisions(cfg: "DecisionModelConfig"):
    """TypeSafe's own ``/v1/systemone`` -- and a local laya-serve, given its url."""
    from .system_one import SYSTEM_ONE

    return _build(cfg, SYSTEM_ONE)


DECISION_PROVIDERS = {
    "openrouter_decisions": build_openrouter_decisions,
    "systemone_decisions": build_systemone_decisions,
}
