"""Registry entrypoint — the factories this plugin's manifest promises.

The LLM registry imports exactly this module when a config entry names one of
the providers declared in ``plugin.toml``, and takes over only the names the
manifest also declares. Nothing here serves ``chat()``: this plugin declares
``provides_decisions`` alone, so ``PROVIDERS`` is deliberately absent and the
plugin never appears as an LLM provider.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from agent_system.config.models import DecisionModelConfig


def build_openrouter_decisions(cfg: "DecisionModelConfig"):
    """Decisions factory (manifest key provides_decisions).

    Lazy import for the same reason as the TTS factories: a process that only
    chats never loads this client.
    """
    from .openrouter import DECISIONS_URL, DecisionsClient

    return DecisionsClient(
        model=cfg.model,
        api_key=cfg.api_key,
        # The client resolves the key against the host of THIS url, so a url
        # pointing at a proxy must not fall back to OpenRouter's default here
        # -- passing it through is what keeps the two in step.
        url=cfg.url or DECISIONS_URL,
        request_timeout=cfg.request_timeout,
        max_retries=cfg.max_retries,
    )


DECISION_PROVIDERS = {"openrouter_decisions": build_openrouter_decisions}
