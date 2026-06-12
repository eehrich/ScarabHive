"""
MCP Authentication and Security Configuration

Handles authentication, secrets, and security settings for MCP connections.
"""

from __future__ import annotations

import os
import logging
from typing import Dict, Optional

from ..config.models import AgentSystemConfig, MCPAuthConfig

logger = logging.getLogger(__name__)


def build_auth_headers(auth_config: Optional[MCPAuthConfig]) -> Dict[str, str]:
    """Build HTTP auth headers from an MCPAuthConfig (bearer/api_key/basic).

    Returns an empty dict when there is no auth config or type 'none'. Shared by
    MCPSecurityManager.get_auth_headers and MCPClientFactory.create_client_from_config
    so outbound MCP connections actually carry the configured credentials.
    """
    if not auth_config:
        return {}

    headers: Dict[str, str] = {}

    if auth_config.type == "bearer" and auth_config.bearer_token:
        headers["Authorization"] = f"Bearer {auth_config.bearer_token}"

    elif auth_config.type == "api_key" and auth_config.api_key:
        headers[auth_config.api_key_header] = auth_config.api_key

    elif auth_config.type == "basic" and auth_config.username and auth_config.password:
        import base64
        credentials = f"{auth_config.username}:{auth_config.password}"
        encoded_credentials = base64.b64encode(credentials.encode()).decode()
        headers["Authorization"] = f"Basic {encoded_credentials}"

    return headers


class MCPSecurityManager:
    """Manages security settings and authentication for MCP connections"""

    def __init__(self):
        self.auth_configs: Dict[str, MCPAuthConfig] = {}

    def add_auth_config(self, server_name: str, config: MCPAuthConfig) -> None:
        """Add authentication configuration for a server"""
        self.auth_configs[server_name] = config
        logger.debug(f"Added auth config for server: {server_name}")

    def get_auth_config(self, server_name: str) -> Optional[MCPAuthConfig]:
        """Get authentication configuration for a server"""
        return self.auth_configs.get(server_name)

    def get_auth_headers(self, server_name: str) -> Dict[str, str]:
        """Get authentication headers for a server"""
        return build_auth_headers(self.get_auth_config(server_name))

    @staticmethod
    def resolve_env_vars(value: str) -> str:
        """Resolve environment variables in configuration values"""
        if isinstance(value, str) and value.startswith("${") and value.endswith("}"):
            env_var = value[2:-1]
            resolved = os.getenv(env_var)
            if resolved is None:
                logger.warning(f"Environment variable {env_var} not found")
                return value
            return resolved
        return value

    @classmethod
    def from_config(cls, config: AgentSystemConfig) -> MCPSecurityManager:
        """Create security manager from AgentSystemConfig"""
        manager = cls()

        # Access MCP servers config (new structure)
        if not config.external_servers:
            return manager
            
        if not config.external_servers.remote_servers:
            return manager
            
        # Process each remote server configuration
        for server_name, server_config in config.external_servers.remote_servers.items():
            # Build auth config data dictionary (only auth-related fields for MCPAuthConfig)
            auth_data = {
                "type": "none",
                "api_key": None,
                "api_key_header": "Authorization",
                "bearer_token": None,
                "username": None,
                "password": None,
                "max_retries": 3,
                "retry_delay": 1.0
            }

            # Apply authentication settings from RemoteMCPConfig.auth
            if server_config.auth:
                auth_section = server_config.auth
                auth_data["type"] = auth_section.type or "none"

                # Resolve environment variables
                if auth_section.api_key:
                    auth_data["api_key"] = cls.resolve_env_vars(auth_section.api_key)

                if auth_section.bearer_token:
                    auth_data["bearer_token"] = cls.resolve_env_vars(auth_section.bearer_token)

                if auth_section.username:
                    auth_data["username"] = cls.resolve_env_vars(auth_section.username)

                if auth_section.password:
                    auth_data["password"] = cls.resolve_env_vars(auth_section.password)

                if auth_section.api_key_header:
                    auth_data["api_key_header"] = auth_section.api_key_header
                    
                # Override with auth-specific settings if present
                if auth_section.max_retries is not None:
                    auth_data["max_retries"] = auth_section.max_retries
                if auth_section.retry_delay is not None:
                    auth_data["retry_delay"] = auth_section.retry_delay

            # Note: ssl_verify and timeout now come from centralized config above
            # RemoteMCPConfig no longer has these attributes

            # Create Pydantic model instance
            auth_config = MCPAuthConfig(**auth_data)
            manager.add_auth_config(server_name, auth_config)

        return manager


# Global security manager
security_manager = MCPSecurityManager()


def get_security_manager() -> MCPSecurityManager:
    """Get the global security manager"""
    return security_manager


def configure_security(config: AgentSystemConfig) -> None:
    """Configure global security settings.
    
    Args:
        config: AgentSystemConfig object
    """
    global security_manager
    security_manager = MCPSecurityManager.from_config(config)
    logger.info("MCP security configuration updated")