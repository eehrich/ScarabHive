"""
Hook configuration loading and management.

Loads hook configuration from config/plugins.yaml and provides
utilities for applying global overrides to plugin hooks.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

logger = logging.getLogger(__name__)


class HooksConfig:
    """
    Configuration for plugin hooks loaded from config/plugins.yaml.
    
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
            hook_name: Name of the hook (can be simple name or plugin.hook_name)
            
        Returns:
            Dictionary with hook configuration (enabled, timeout, order)
        """
        # Start with defaults
        config = {
            "enabled": self.enabled,
            "timeout": self.default_timeout,
            "order": {"before": [], "after": []}
        }
        
        # Apply overrides if they exist
        if hook_name in self.overrides:
            override = self.overrides[hook_name]
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
            metadata: Hook metadata from plugin.yaml
            
        Returns:
            Updated metadata with global overrides applied
        """
        config = self.get_hook_config(hook_name)
        
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
    
    @classmethod
    def from_yaml(cls, config_path: str | Path) -> "HooksConfig":
        """
        Load hooks configuration from YAML file.
        
        Args:
            config_path: Path to config/plugins.yaml
            
        Returns:
            HooksConfig instance
            
        Raises:
            FileNotFoundError: If config file doesn't exist
            ValueError: If hooks section is invalid
        """
        config_path = Path(config_path)
        
        if not config_path.exists():
            logger.warning(f"Hooks config not found at {config_path}, using defaults")
            return cls()
        
        try:
            with open(config_path, "r", encoding="utf-8") as f:
                data = yaml.safe_load(f) or {}
            
            hooks_section = data.get("hooks", {})
            
            return cls(
                enabled=hooks_section.get("enabled", True),
                default_timeout=hooks_section.get("default_timeout", 30.0),
                overrides=hooks_section.get("overrides", {})
            )
            
        except Exception as e:
            logger.error(f"Failed to load hooks config from {config_path}: {e}", exc_info=True)
            return cls()  # Return defaults on error
    
    @classmethod
    def from_plugins_config(cls, plugins_config_path: str | Path = "config/plugins.yaml") -> "HooksConfig":
        """
        Convenience method to load from default plugins config location.
        
        Args:
            plugins_config_path: Path to plugins.yaml (default: config/plugins.yaml)
            
        Returns:
            HooksConfig instance
        """
        return cls.from_yaml(plugins_config_path)


def load_hooks_config(config_path: Optional[str | Path] = None) -> HooksConfig:
    """
    Load hooks configuration from file.
    
    Args:
        config_path: Optional path to config file (default: config/plugins.yaml)
        
    Returns:
        HooksConfig instance
    """
    if config_path is None:
        config_path = Path("config/plugins.yaml")
    
    return HooksConfig.from_yaml(config_path)


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
