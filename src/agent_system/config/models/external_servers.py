"""The ``external_servers:`` section (config/mcp_servers.yaml): the MCP servers this system
connects to -- remote ones it dials, local ones (transport: stdio) it starts -- and the
connection and cache settings they share.
"""
from __future__ import annotations

from pydantic import BaseModel, Field
from typing import Literal, Optional, Dict, List, Any

from .agent import ToolConfig


class ExternalServerConfig(BaseModel):
    """Configuration for external server connections and defaults"""
    # This matches the type comment in mcp.yaml for external_server
    pass  # Currently empty in YAML, can be extended


class ExternalServerConnectionConfig(BaseModel):
    """Configuration for external server connections"""
    timeout: float = 5.0
    parallel_connect: bool = True


class ExternalServerCacheConfig(BaseModel):
    """Configuration for external server caching"""
    enabled: bool = True  # Enable tool list caching
    tool_list_ttl: float = 30.0  # TTL for the mcp_client plugin's tool-list cache
    max_size: Optional[int] = None  # Maximum cache entries (None = unlimited)


class MCPAuthConfig(BaseModel):
    """Authentication configuration for tool servers"""
    type: str = "none"  # none, bearer, api_key, basic, oauth
    api_key: Optional[str] = None
    api_key_header: str = "Authorization"
    bearer_token: Optional[str] = None
    username: Optional[str] = None
    password: Optional[str] = None

    # oauth: the authorization-code flow with PKCE (mcp_client/oauth.py). A server
    # with dynamic client registration needs none of these; one without (GitHub)
    # needs the client_id (and client_secret) of an OAuth app made for it.
    client_id: Optional[str] = None
    client_secret: Optional[str] = None
    # Where the authorization server sends the browser back: this app's
    # /plugins/<mcp_client instance>/oauth/callback, at the address the browser
    # reaches the app on. None: http://<network.host>:<network.port>/...
    redirect_uri: Optional[str] = None

    # Retry settings
    max_retries: int = 3
    retry_delay: float = 1.0


class RemoteMCPConfig(BaseModel):
    """Configuration for a remote MCP server"""
    url: str = ""
    enabled: bool = False
    description: Optional[str] = None
    transport: str = "streaming"
    initialization_options: Optional[Dict[str, Any]] = None
    features: Optional[Dict[str, bool]] = None
    tools: Optional[ToolConfig] = None

    # Local servers (transport: stdio) are launched instead of dialled, so they
    # need a command rather than a url. Without these fields pydantic dropped
    # them silently (extra="ignore") and the stdio transport was unreachable
    # from a real config, however correctly it was written.
    command: Optional[str] = None
    args: Optional[List[str]] = None
    env: Optional[Dict[str, str]] = None

    # Per-server answer timeout in seconds; None falls back to the global
    # external_servers.connection.timeout. A Blender render needs minutes,
    # the everything demo answers in milliseconds -- one global value
    # cannot serve both.
    timeout: Optional[float] = None

    # When the connection is made. "startup": every process that boots the
    # plugins connects (and, for stdio, STARTS) the server. "on_demand": only
    # a process running an agent whose tools.allowed names this server
    # (``<name>.*`` or ``<name>.<tool>``) -- a browser server otherwise ran
    # once per CLI worker, for nobody.
    connect: Literal["startup", "on_demand"] = "startup"

    # Offer the server's resources and prompts to agents as tools of the server
    # (<name>.mcp_read_resource ...). Off by default: on, <name>.* admits them,
    # and a resource can carry what tools.blocked keeps a tool from reading.
    resources_and_prompts: bool = False

    # Authentication and security
    auth: Optional[MCPAuthConfig] = None



class ExternalServersConfig(BaseModel):
    """Configuration for all external servers"""
    connection: ExternalServerConnectionConfig = Field(default_factory=ExternalServerConnectionConfig)
    cache: ExternalServerCacheConfig = Field(default_factory=ExternalServerCacheConfig)
    remote_servers: Dict[str, RemoteMCPConfig] = Field(default_factory=dict)


class MCPServersConfig(BaseModel):
    """Configuration for external MCP servers (matches config/mcp_servers.yaml)"""
    connection: ExternalServerConnectionConfig = Field(default_factory=ExternalServerConnectionConfig)
    cache: ExternalServerCacheConfig = Field(default_factory=ExternalServerCacheConfig)
    remote_servers: Dict[str, RemoteMCPConfig] = Field(default_factory=dict)
