"""Provider factories: ``claude_code`` and ``antigravity``, both a coding_cli instance named by the model entry's
``model`` -- alone, or as ``<instance>/<model>`` for a model of the CLI's the instance lists. The instance decides
the CLI, the subscription threshold and the workdir with its gateway tools, and the model and effort where the
entry names none (``thinking_level`` is the run's effort); the provider name only has to match the instance's
``cli``."""
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
        warn_unwired(cfg, provider=provider, wired=(), logger=logger)  # no dialect: the CLI talks to its model
        if not cfg.model or not cfg.model.partition("/")[0]:
            raise ValueError(f"provider {provider}: model names the coding_cli instance to run "
                             "(<instance>, or <instance>/<model> for a model the instance lists)")
        return CodingCliClient(provider, cfg.model, context_window=cfg.context_window, capabilities=cfg.capabilities,
                               thinking_level=cfg.thinking_level)

    return build


PROVIDERS = {"claude_code": _factory("claude_code"), "antigravity": _factory("antigravity")}
