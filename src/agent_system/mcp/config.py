"""
Configuration schema for MCP external servers

Defines the configuration structure for external MCP server connections.
"""

from __future__ import annotations

import yaml
import logging
from typing import Any, Dict, List, Optional, Union
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)


@dataclass
class MCPServerConfig:
    """Configuration for an external MCP server"""
    name: str
    url: str
    enabled: bool = True
    description: Optional[str] = None

    # Authentication
    auth_type: str = "none"  # none, bearer, api_key, basic
    api_key: Optional[str] = None
    api_key_header: str = "Authorization"
    bearer_token: Optional[str] = None
    username: Optional[str] = None
    password: Optional[str] = None

    # Connection settings
    ssl_verify: bool = True
    timeout: float = 30.0
    max_retries: int = 3
    retry_delay: float = 1.0
    transport_type: str = "http"  # http, smithery

    # Initialization options for MCP server
    initialization_options: Dict[str, Any] = field(default_factory=dict)

    # Feature configuration
    tools: bool = True
    resources: bool = True
    prompts: bool = True

    # Tool filtering
    tool_prefix: Optional[str] = None
    allowed_tools: List[str] = field(default_factory=list)
    blocked_tools: List[str] = field(default_factory=list)

    # Resource filtering
    resource_prefix: Optional[str] = None
    allowed_resources: List[str] = field(default_factory=list)
    blocked_resources: List[str] = field(default_factory=list)

    # Prompt filtering
    prompt_prefix: Optional[str] = None
    allowed_prompts: List[str] = field(default_factory=list)
    blocked_prompts: List[str] = field(default_factory=list)

    # Metadata
    tags: List[str] = field(default_factory=list)
    priority: int = 100  # Lower number = higher priority


@dataclass
class MCPConfig:
    """Complete MCP configuration"""
    enabled: bool = True

    # Server settings
    expose_local_server: bool = True
    local_server_port: int = 8000
    local_server_host: str = "localhost"

    # External servers
    servers: Dict[str, MCPServerConfig] = field(default_factory=dict)

    # Global settings
    default_timeout: float = 30.0
    max_concurrent_requests: int = 10
    enable_health_checks: bool = True
    health_check_interval: float = 300.0  # 5 minutes

    # Security
    require_auth: bool = False
    allowed_origins: List[str] = field(default_factory=list)
    rate_limit_requests: int = 1000
    rate_limit_window: int = 3600  # 1 hour


class MCPConfigManager:
    """Manages MCP configuration loading and validation"""

    def __init__(self, config_path: Optional[Union[str, Path]] = None):
        self.config_path = Path(config_path) if config_path else None
        self.config: Optional[MCPConfig] = None

    def load_config(self, config_data: Optional[Dict[str, Any]] = None) -> MCPConfig:
        """Load MCP configuration from data or file"""
        if config_data is None:
            if not self.config_path or not self.config_path.exists():
                logger.info("No MCP configuration found, using defaults")
                return MCPConfig()

            with open(self.config_path, 'r', encoding='utf-8') as f:
                config_data = yaml.safe_load(f)

        if not config_data:
            return MCPConfig()

        mcp_data = config_data.get("mcp", {})

        # Parse main config
        config = MCPConfig(
            enabled=mcp_data.get("enabled", True),
            expose_local_server=mcp_data.get("expose_local_server", True),
            local_server_port=mcp_data.get("local_server_port", 8000),
            local_server_host=mcp_data.get("local_server_host", "localhost"),
            default_timeout=mcp_data.get("default_timeout", 30.0),
            max_concurrent_requests=mcp_data.get("max_concurrent_requests", 10),
            enable_health_checks=mcp_data.get("enable_health_checks", True),
            health_check_interval=mcp_data.get("health_check_interval", 300.0),
            require_auth=mcp_data.get("require_auth", False),
            allowed_origins=mcp_data.get("allowed_origins", []),
            rate_limit_requests=mcp_data.get("rate_limit_requests", 1000),
            rate_limit_window=mcp_data.get("rate_limit_window", 3600)
        )

        # Parse external servers
        servers_data = mcp_data.get("external_servers", {})
        for server_name, server_data in servers_data.items():
            server_config = self._parse_server_config(server_name, server_data)
            config.servers[server_name] = server_config

        self.config = config
        logger.info(f"Loaded MCP configuration with {len(config.servers)} external servers")
        return config

    def _parse_server_config(self, name: str, data: Dict[str, Any]) -> MCPServerConfig:
        """Parse individual server configuration"""
        config = MCPServerConfig(
            name=name,
            url=data["url"],
            enabled=data.get("enabled", True),
            description=data.get("description")
        )

        # Authentication
        auth_data = data.get("auth", {})
        config.auth_type = auth_data.get("type", "none")
        config.api_key = auth_data.get("api_key")
        config.api_key_header = auth_data.get("api_key_header", "Authorization")
        config.bearer_token = auth_data.get("bearer_token")
        config.username = auth_data.get("username")
        config.password = auth_data.get("password")

        # Connection settings
        config.ssl_verify = data.get("ssl_verify", True)
        config.timeout = data.get("timeout", 30.0)
        config.max_retries = data.get("max_retries", 3)
        config.retry_delay = data.get("retry_delay", 1.0)
        config.transport_type = data.get("transport_type", "http")

        # Initialization options
        config.initialization_options = data.get("initialization_options", {})

        # Features
        features = data.get("features", {})
        config.tools = features.get("tools", True)
        config.resources = features.get("resources", True)
        config.prompts = features.get("prompts", True)

        # Tool filtering
        tool_config = data.get("tools", {})
        config.tool_prefix = tool_config.get("prefix")
        config.allowed_tools = tool_config.get("allowed", [])
        config.blocked_tools = tool_config.get("blocked", [])

        # Resource filtering
        resource_config = data.get("resources", {})
        config.resource_prefix = resource_config.get("prefix")
        config.allowed_resources = resource_config.get("allowed", [])
        config.blocked_resources = resource_config.get("blocked", [])

        # Prompt filtering
        prompt_config = data.get("prompts", {})
        config.prompt_prefix = prompt_config.get("prefix")
        config.allowed_prompts = prompt_config.get("allowed", [])
        config.blocked_prompts = prompt_config.get("blocked", [])

        # Metadata
        config.tags = data.get("tags", [])
        config.priority = data.get("priority", 100)

        return config

    def save_config(self, config: MCPConfig, path: Optional[Union[str, Path]] = None) -> None:
        """Save MCP configuration to file"""
        save_path = Path(path) if path else self.config_path
        if not save_path:
            raise ValueError("No config path specified")

        # Convert to dict format
        config_data: Dict[str, Any] = {
            "mcp": {
                "enabled": config.enabled,
                "expose_local_server": config.expose_local_server,
                "local_server_port": config.local_server_port,
                "local_server_host": config.local_server_host,
                "default_timeout": config.default_timeout,
                "max_concurrent_requests": config.max_concurrent_requests,
                "enable_health_checks": config.enable_health_checks,
                "health_check_interval": config.health_check_interval,
                "require_auth": config.require_auth,
                "allowed_origins": config.allowed_origins,
                "rate_limit_requests": config.rate_limit_requests,
                "rate_limit_window": config.rate_limit_window,
                "external_servers": {}
            }
        }

        # Add external servers
        for server_name, server_config in config.servers.items():
            server_data = {
                "url": server_config.url,
                "enabled": server_config.enabled,
                "description": server_config.description,
                "ssl_verify": server_config.ssl_verify,
                "timeout": server_config.timeout,
                "max_retries": server_config.max_retries,
                "retry_delay": server_config.retry_delay,
                "features": {
                    "tools": server_config.tools,
                    "resources": server_config.resources,
                    "prompts": server_config.prompts
                },
                "tags": server_config.tags,
                "priority": server_config.priority
            }

            # Add auth if configured
            if server_config.auth_type != "none":
                auth_dict: Dict[str, str] = {"type": server_config.auth_type}
                if server_config.api_key:
                    auth_dict["api_key"] = server_config.api_key
                if server_config.api_key_header != "Authorization":
                    auth_dict["api_key_header"] = server_config.api_key_header
                if server_config.bearer_token:
                    auth_dict["bearer_token"] = server_config.bearer_token
                if server_config.username:
                    auth_dict["username"] = server_config.username
                if server_config.password:
                    auth_dict["password"] = server_config.password
                server_data["auth"] = auth_dict

            # Add filters if configured
            if server_config.tool_prefix or server_config.allowed_tools or server_config.blocked_tools:
                tools_dict: Dict[str, Any] = {}
                if server_config.tool_prefix:
                    tools_dict["prefix"] = server_config.tool_prefix
                if server_config.allowed_tools:
                    tools_dict["allowed"] = server_config.allowed_tools
                if server_config.blocked_tools:
                    tools_dict["blocked"] = server_config.blocked_tools
                server_data["tools"] = tools_dict

            # Similar for resources and prompts...

            config_data["mcp"]["external_servers"][server_name] = server_data

        # Save to file
        with open(save_path, 'w', encoding='utf-8') as f:
            yaml.dump(config_data, f, default_flow_style=False, sort_keys=False)

        logger.info(f"Saved MCP configuration to {save_path}")

    def validate_config(self, config: MCPConfig) -> List[str]:
        """Validate MCP configuration and return list of issues"""
        issues = []

        # Validate port range
        if not (1 <= config.local_server_port <= 65535):
            issues.append(f"Invalid local_server_port: {config.local_server_port}")

        # Validate timeouts
        if config.default_timeout <= 0:
            issues.append(f"Invalid default_timeout: {config.default_timeout}")

        if config.health_check_interval <= 0:
            issues.append(f"Invalid health_check_interval: {config.health_check_interval}")

        # Validate external servers
        for server_name, server_config in config.servers.items():
            server_issues = self._validate_server_config(server_name, server_config)
            issues.extend(server_issues)

        return issues

    def _validate_server_config(self, name: str, config: MCPServerConfig) -> List[str]:
        """Validate individual server configuration"""
        issues = []

        # Validate URL
        if not config.url:
            issues.append(f"Server {name}: URL is required")
        elif not config.url.startswith(("http://", "https://")):
            issues.append(f"Server {name}: URL must start with http:// or https://")

        # Validate auth configuration
        if config.auth_type not in ["none", "bearer", "api_key", "basic"]:
            issues.append(f"Server {name}: Invalid auth_type: {config.auth_type}")

        if config.auth_type == "bearer" and not config.bearer_token:
            issues.append(f"Server {name}: bearer_token required for bearer auth")

        if config.auth_type == "api_key" and not config.api_key:
            issues.append(f"Server {name}: api_key required for api_key auth")

        if config.auth_type == "basic" and (not config.username or not config.password):
            issues.append(f"Server {name}: username and password required for basic auth")

        # Validate timeouts
        if config.timeout <= 0:
            issues.append(f"Server {name}: Invalid timeout: {config.timeout}")

        if config.retry_delay < 0:
            issues.append(f"Server {name}: Invalid retry_delay: {config.retry_delay}")

        if config.max_retries < 0:
            issues.append(f"Server {name}: Invalid max_retries: {config.max_retries}")

        return issues


def create_example_config() -> Dict[str, Any]:
    """Create an example MCP configuration"""
    return {
        "mcp": {
            "enabled": True,
            "expose_local_server": True,
            "local_server_port": 8000,
            "local_server_host": "localhost",
            "default_timeout": 30.0,
            "max_concurrent_requests": 10,
            "enable_health_checks": True,
            "health_check_interval": 300.0,
            "require_auth": False,
            "allowed_origins": ["http://localhost:3000"],
            "rate_limit_requests": 1000,
            "rate_limit_window": 3600,
            "external_servers": {
                "example_server": {
                    "url": "https://api.example.com/mcp",
                    "enabled": True,
                    "description": "Example external MCP server",
                    "auth": {
                        "type": "api_key",
                        "api_key": "${EXAMPLE_API_KEY}",
                        "api_key_header": "X-API-Key"
                    },
                    "ssl_verify": True,
                    "timeout": 30.0,
                    "max_retries": 3,
                    "retry_delay": 1.0,
                    "features": {
                        "tools": True,
                        "resources": True,
                        "prompts": True
                    },
                    "tools": {
                        "prefix": "example_",
                        "allowed": ["search", "analyze"],
                        "blocked": ["delete", "admin"]
                    },
                    "tags": ["search", "analysis"],
                    "priority": 50
                }
            }
        }
    }