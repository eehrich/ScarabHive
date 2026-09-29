"""
Configuration Service

Centralized configuration loading and management. This service eliminates
duplicated config loading code between CLI and API, and provides a clean
interface for configuration access.
"""
from __future__ import annotations

import logging
from typing import Optional
from pathlib import Path

from agent_system.config.settings import load_settings
from agent_system.config.models import AgentSystemConfig, ToolServerConfig


logger = logging.getLogger(__name__)


class ConfigService:
    """Centralized configuration loading and validation service.
    
    This service provides a single source of truth for configuration
    management across CLI and API interfaces.
    """

    def __init__(self):
        """Initialize the ConfigService."""
        self._config: Optional[AgentSystemConfig] = None
        self._config_path: Optional[Path] = None

    def load_config(
        self,
        config_path: Optional[str] = None,
        force_reload: bool = False
    ) -> AgentSystemConfig:
        """Load and validate configuration from YAML file.
        
        This method caches the loaded configuration to avoid repeated
        file I/O. Use force_reload=True to reload from disk.
        
        Args:
            config_path: Optional path to YAML config file. If None, uses
                        default config path or AGENT_CONFIG_PATH env var.
            force_reload: If True, reload config even if already cached.
        
        Returns:
            Validated AgentSystemConfig instance.
        
        Raises:
            ValidationError: If configuration is invalid.
            FileNotFoundError: If config file doesn't exist and is required.
        """
        path_obj = Path(config_path) if config_path else None
        
        # Return cached config if available and path hasn't changed
        if (
            not force_reload
            and self._config is not None
            and path_obj == self._config_path
        ):
            logger.debug(f"Using cached config from {self._config_path}")
            return self._config
        
        logger.info(f"Loading config from {config_path or 'default location'}")
        
        try:
            config = load_settings(config_path)
            self._config = config
            self._config_path = path_obj
            
            # Count all configured servers
            plugin_count = len(config.plugins.servers) if config.plugins else 0
            mcp_remote_count = len(config.external_servers.remote_servers) if config.external_servers else 0
            logger.debug(f"Config loaded: {plugin_count} plugins, {mcp_remote_count} remote MCP servers")
            return config
        except Exception as e:
            logger.error(f"Failed to load configuration: {e}")
            raise

    def get_config(self) -> Optional[AgentSystemConfig]:
        """Get the currently loaded configuration.
        
        Returns:
            The cached configuration, or None if not loaded yet.
        """
        return self._config

    def setup_logging(
        self,
        config: Optional[AgentSystemConfig] = None,
        verbose: bool = False,
        log_level: Optional[str] = None
    ) -> None:
        """Setup logging configuration.
        
        Configures Python's logging system based on the provided configuration
        and verbosity settings.
        
        Args:
            config: AgentSystemConfig instance. If None, uses cached config.
            verbose: If True, enable verbose (DEBUG) logging.
            log_level: Override log level (DEBUG, INFO, WARNING, ERROR, CRITICAL).
        """
        cfg = config or self._config
        
        # Determine effective log level
        if log_level:
            level = getattr(logging, log_level.upper(), logging.INFO)
        elif verbose:
            level = logging.DEBUG
        elif cfg and hasattr(cfg, 'log_level'):
            level = getattr(logging, cfg.log_level.upper(), logging.INFO)
        else:
            level = logging.INFO
        
        # Configure root logger
        logging.basicConfig(
            level=level,
            format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
            force=True  # Override any existing configuration
        )
        
        # Downgrade ChromaDB's noisy "n_results" warning to INFO
        # This is normal behavior when query requests more results than available
        chromadb_logger = logging.getLogger('chromadb.segment.impl.vector.local_persistent_hnsw')
        chromadb_logger.setLevel(logging.ERROR)  # Only show actual errors, not informational warnings
        
        logger.info(f"Logging configured: level={logging.getLevelName(level)}, verbose={verbose}")

    def get_tool_server_config(
        self,
        server_name: str,
        config: Optional[AgentSystemConfig] = None
    ) -> Optional[ToolServerConfig]:
        """Get tool server configuration by name.
        
        Args:
            server_name: Name of the tool server.
            config: Optional config instance. If None, uses cached config.
        
        Returns:
            ToolServerConfig if found, None otherwise.
        """
        cfg = config or self._config
        if not cfg:
            logger.warning("No configuration loaded")
            return None
        
        # Check plugins first, then external_servers (remote_servers)
        if cfg.plugins and server_name in cfg.plugins.servers:
            return cfg.plugins.servers[server_name]
        
        if cfg.external_servers and server_name in cfg.external_servers.remote_servers:
            # remote_servers are RemoteMCPConfig, not ToolServerConfig
            # For now, return None - this needs proper handling
            logger.warning(f"Server '{server_name}' found in remote_servers, not local plugins")
            return None
        
        logger.warning(f"tool server '{server_name}' not found in configuration")
        return None

    def list_tool_servers(
        self,
        config: Optional[AgentSystemConfig] = None,
        enabled_only: bool = False
    ) -> dict[str, ToolServerConfig]:
        """List all configured tool servers (local plugins only).
        
        Args:
            config: Optional config instance. If None, uses cached config.
            enabled_only: If True, return only enabled servers.
        
        Returns:
            Dictionary mapping server names to ToolServerConfig instances.
        """
        cfg = config or self._config
        if not cfg:
            logger.warning("No configuration loaded")
            return {}
        
        # Only return local plugin servers
        servers: dict[str, ToolServerConfig] = {}
        
        if cfg.plugins:
            servers.update(cfg.plugins.servers)
        
        # mcp_servers has remote_servers (RemoteMCPConfig), not local servers
        # Don't include them here as they're different types
        
        if enabled_only:
            servers = {
                name: server
                for name, server in servers.items()
                if server.enabled
            }
        
        return servers

    def get_agent_config(
        self,
        agent_name: str,
        config: Optional[AgentSystemConfig] = None
    ) -> Optional[dict]:
        """Get configuration-based agent definition.
        
        Args:
            agent_name: Name of the config-based agent.
            config: Optional config instance. If None, uses cached config.
        
        Returns:
            Agent definition dict if found, None otherwise.
        """
        cfg = config or self._config
        if not cfg or not cfg.agents:
            return None
        
        return cfg.agents.get(agent_name)

    def list_agents(
        self,
        config: Optional[AgentSystemConfig] = None
    ) -> dict[str, dict]:
        """List all configuration-based agents.
        
        Args:
            config: Optional config instance. If None, uses cached config.
        
        Returns:
            Dictionary mapping agent names to agent definitions.
        """
        cfg = config or self._config
        if not cfg or not cfg.agents:
            return {}
        
        return dict(cfg.agents)

    def get_default_server_config(
        self,
        config: Optional[AgentSystemConfig] = None
    ) -> Optional[ToolServerConfig]:
        """Get default tool server configuration for agents.
        
        Args:
            config: Optional config instance. If None, uses cached config.
        
        Returns:
            Default ToolServerConfig if found, None otherwise.
        """
        cfg = config or self._config
        if not cfg:
            return None
        
        # Try plugins.default_config
        if cfg.plugins and cfg.plugins.default_config:
            return cfg.plugins.default_config
        
        return None

    def get_plugin_dirs(
        self,
        config: Optional[AgentSystemConfig] = None
    ) -> list[Path]:
        """Get configured plugin directories.
        
        Args:
            config: Optional config instance. If None, uses cached config.
        
        Returns:
            List of Path objects for plugin directories.
        """
        cfg = config or self._config
        if not cfg:
            return []
        
        if not cfg.plugins or not cfg.plugins.plugin_dirs:
            return []
        
        return [Path(p) for p in cfg.plugins.plugin_dirs if p]

    def clear_cache(self) -> None:
        """Clear cached configuration.
        
        This forces the next load_config() call to reload from disk.
        """
        logger.debug("Clearing configuration cache")
        self._config = None
        self._config_path = None
