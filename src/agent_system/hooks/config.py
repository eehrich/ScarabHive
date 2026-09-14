"""
Hook configuration and management.

Turns the global ``hooks:`` section of the loaded configuration
(AgentSystemConfig.hooks) into a HooksConfig and provides utilities for
applying its overrides to plugin hooks.
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Dict, List, Optional

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
    
    def get_hook_config(self, hook_name: str) -> Dict[str, Any]:
        """
        Get configuration for a specific hook.
        
        Args:
            hook_name: Full hook name (plugin.hook_name). An override keyed on
                just the plugin name applies to every hook of that plugin;
                the exact plugin.hook_name key wins.

        Returns:
            Dictionary with hook configuration (enabled, timeout, order)
        """
        # Start with defaults
        config = {
            "enabled": self.enabled,
            "timeout": self.default_timeout,
            "order": {"before": [], "after": []}
        }
        
        # Apply overrides if they exist (exact key wins over plugin-wide key)
        override = self.overrides.get(hook_name)
        if override is None and "." in hook_name:
            override = self.overrides.get(hook_name.split(".", 1)[0])
        if override is not None:
            if "enabled" in override:
                config["enabled"] = override["enabled"]
            if "timeout" in override:
                config["timeout"] = override["timeout"]
            if "order" in override:
                config["order"] = override["order"]
        
        return config
    
    def apply_to_hook_metadata(
        self,
        hook_name: str,
        metadata: Dict[str, Any]
    ) -> Dict[str, Any]:
        """
        Apply global hook configuration to plugin hook metadata.
        
        Args:
            hook_name: Name of the hook
            metadata: Hook metadata from plugin.toml
            
        Returns:
            Updated metadata with global overrides applied
        """
        # Create a copy to avoid modifying original
        updated = metadata.copy()
        
        # Apply overrides (global config has higher priority than plugin defaults)
        # But respect explicit plugin settings if no global override exists
        if hook_name in self.overrides:
            override = self.overrides[hook_name]
            
            # Only override if explicitly set in config
            if "enabled" in override:
                updated["enabled"] = override["enabled"]
            
            if "timeout" in override:
                updated["timeout"] = override["timeout"]
            
            if "order" in override:
                # Merge order specifications
                if "order" not in updated:
                    updated["order"] = {}
                
                order_override = override["order"]
                if "before" in order_override:
                    updated["order"]["before"] = order_override["before"]
                if "after" in order_override:
                    updated["order"]["after"] = order_override["after"]
        
        # Apply global enabled flag if hooks are globally disabled
        if not self.enabled:
            updated["enabled"] = False
        
        return updated
    
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


def validate_hook_references(
    hooks_config: HooksConfig,
    available_hooks: List[str]
) -> List[str]:
    """
    Validate that all hook references in order specs are valid.
    
    Args:
        hooks_config: Hooks configuration to validate
        available_hooks: List of available hook names
        
    Returns:
        List of validation error messages (empty if valid)
    """
    errors = []
    available_set = set(available_hooks)
    
    # Add virtual nodes
    available_set.add("begin")
    available_set.add("end")
    
    for hook_name, override in hooks_config.overrides.items():
        order = override.get("order", {})
        
        # Check before references
        for ref in order.get("before", []):
            if ref not in available_set and ref != hook_name:
                errors.append(
                    f"Hook '{hook_name}' references unknown hook '{ref}' in 'before' list"
                )
        
        # Check after references
        for ref in order.get("after", []):
            if ref not in available_set and ref != hook_name:
                errors.append(
                    f"Hook '{hook_name}' references unknown hook '{ref}' in 'after' list"
                )
        
        # Check for self-references
        if hook_name in order.get("before", []) or hook_name in order.get("after", []):
            errors.append(
                f"Hook '{hook_name}' references itself in order specification"
            )
    
    return errors
