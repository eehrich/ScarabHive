"""
Hook configuration and management.

Turns the global ``hooks:`` section of the loaded configuration
(AgentSystemConfig.hooks) into a HooksConfig. The overrides are applied
in plugins.discovery.register_plugin_hooks.
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Dict, Optional

if TYPE_CHECKING:
    from agent_system.config.models import AgentSystemConfig

logger = logging.getLogger(__name__)


class HooksConfig:
    """
    Global hook settings, as load_hooks_config reads them from the config.

    Provides global hook settings and per-hook overrides that can be applied
    during plugin bootstrap to customize hook behavior.
    """
    
    def __init__(
        self,
        enabled: bool = True,
        default_timeout: float = 30.0,
        overrides: Optional[Dict[str, Dict[str, Any]]] = None
    ):
        """
        Initialize hooks configuration.
        
        Args:
            enabled: Global enable/disable for all hooks
            default_timeout: Default timeout for hook execution in seconds
            overrides: Per-hook configuration overrides
        """
        self.enabled = enabled
        self.default_timeout = default_timeout
        self.overrides = overrides or {}


def load_hooks_config(settings: Optional["AgentSystemConfig"] = None) -> HooksConfig:
    """The global hook settings of an already loaded configuration.

    They come from the ``hooks:`` section that load_settings merged -- the
    config the caller runs on. This used to open config/plugins.yaml relative
    to the working directory instead, whatever --config or AGENT_CONFIG_PATH
    named. No file is read here: without ``settings`` the defaults apply.
    """
    if settings is None:
        return HooksConfig()
    section = settings.hooks
    return HooksConfig(
        enabled=section.enabled,
        default_timeout=section.default_timeout,
        # Unset fields stay absent: register_plugin_hooks asks
        # `'timeout' in override` -- a dumped None would count as set.
        overrides={name: override.model_dump(exclude_unset=True)
                   for name, override in section.overrides.items()},
    )
