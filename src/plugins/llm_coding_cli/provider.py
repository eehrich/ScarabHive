"""Provider factories: ``claude_code`` and ``antigravity``, both a coding_cli instance named by the model entry's
``model``. The instance decides the CLI, model, effort, subscription threshold and the workdir with its gateway
tools; the provider name only has to match the instance's ``cli``."""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Callable, Optional

from plugins.llm_common.model_dialects import warn_unwired

from .client import CodingCliClient

if TYPE_CHECKING:
    from agent_system.config.models import LLMModelConfig
    from agent_system.llm.models import LLMClient

logger = logging.getLogger(__name__)


def _factory(provider: str) -> Callable[..., "LLMClient"]:
    def build(cfg: "LLMModelConfig", ssl_verify: Optional[bool] = None) -> "LLMClient":
        warn_unwired(cfg, provider=provider, wired=(), logger=logger)  # effort and model are the instance's
        if not cfg.model:
            raise ValueError(f"provider {provider}: model names the coding_cli instance to run")
        return CodingCliClient(provider, cfg.model, context_window=cfg.context_window, capabilities=cfg.capabilities)

    return build


PROVIDERS = {"claude_code": _factory("claude_code"), "antigravity": _factory("antigravity")}
