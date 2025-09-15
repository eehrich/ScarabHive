"""
MCP Authentication and Security Configuration

Handles authentication, secrets, and security settings for MCP connections.
"""

from __future__ import annotations

import os
import logging
from typing import Any, Dict, Optional
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass
class MCPAuthConfig:
    """Configuration for MCP authentication"""
    auth_type: str = "none"  # none, bearer, api_key, basic
    api_key: Optional[str] = None
    api_key_header: str = "Authorization"
    bearer_token: Optional[str] = None
    username: Optional[str] = None
    password: Optional[str] = None
    ssl_verify: bool = True
    timeout: float = 30.0
    max_retries: int = 3
    retry_delay: float = 1.0


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
        auth_config = self.get_auth_config(server_name)
        if not auth_config:
            return {}
        
        headers = {}
        
        if auth_config.auth_type == "bearer" and auth_config.bearer_token:
            headers["Authorization"] = f"Bearer {auth_config.bearer_token}"
        
        elif auth_config.auth_type == "api_key" and auth_config.api_key:
            headers[auth_config.api_key_header] = auth_config.api_key
        
        elif auth_config.auth_type == "basic" and auth_config.username and auth_config.password:
            import base64
            credentials = f"{auth_config.username}:{auth_config.password}"
            encoded_credentials = base64.b64encode(credentials.encode()).decode()
            headers["Authorization"] = f"Basic {encoded_credentials}"
        
        return headers
    
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
    def from_config(cls, config: Dict[str, Any]) -> "MCPSecurityManager":
        """Create security manager from configuration"""
        manager = cls()
        
        servers_config = config.get("servers", {})
        for server_name, server_config in servers_config.items():
            auth_config = MCPAuthConfig()
            
            # Resolve authentication settings
            if "auth" in server_config:
                auth_section = server_config["auth"]
                auth_config.auth_type = auth_section.get("type", "none")
                
                # Resolve environment variables
                if "api_key" in auth_section:
                    auth_config.api_key = cls.resolve_env_vars(auth_section["api_key"])
                
                if "bearer_token" in auth_section:
                    auth_config.bearer_token = cls.resolve_env_vars(auth_section["bearer_token"])
                
                if "username" in auth_section:
                    auth_config.username = cls.resolve_env_vars(auth_section["username"])
                
                if "password" in auth_section:
                    auth_config.password = cls.resolve_env_vars(auth_section["password"])
                
                auth_config.api_key_header = auth_section.get("api_key_header", "Authorization")
            
            # Security settings
            auth_config.ssl_verify = server_config.get("ssl_verify", True)
            auth_config.timeout = server_config.get("timeout", 30.0)
            auth_config.max_retries = server_config.get("max_retries", 3)
            auth_config.retry_delay = server_config.get("retry_delay", 1.0)
            
            manager.add_auth_config(server_name, auth_config)
        
        return manager


# Global security manager
security_manager = MCPSecurityManager()


def get_security_manager() -> MCPSecurityManager:
    """Get the global security manager"""
    return security_manager


def configure_security(config: Dict[str, Any]) -> None:
    """Configure global security settings"""
    global security_manager
    security_manager = MCPSecurityManager.from_config(config)
    logger.info("MCP security configuration updated")