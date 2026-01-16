"""
Configuration models for AgentSystem.

This module provides Pydantic models that match the new YAML configuration
structure with config.yaml as the master configuration and included files
for LLM and MCP configurations.
"""
from __future__ import annotations

import re
from pydantic import BaseModel, Field, field_validator
from typing import Literal, Optional, Dict, List, Any, Union


# ===========================
# LLM Configuration Models
# ===========================

class HTTPXTimeoutConfig(BaseModel):
    """HTTPX timeout configuration"""
    connect: float = 10.0      # Connection establishment timeout
    read: float = 180.0        # Read timeout (waiting for response data)
    write: float = 10.0        # Write timeout (sending request data)
    pool: float = 5.0          # Pool timeout (getting connection from pool)


class ModelCapabilitiesConfig(BaseModel):
    """Model capabilities configuration"""
    tools: bool = True
    function_calling: bool = True
    image_input: bool = False
    audio_input: bool = False
    video_input: bool = False
    streaming: bool = True
    json_mode: bool = False

    # API type support (OpenAI specific)
    supported_api_types: Optional[List[str]] = None  # e.g. ['chat_completions', 'realtime']
    default_api_type: Optional[str] = None  # e.g. 'realtime'

    # Image input limits
    max_image_size: Optional[int] = None  # bytes
    max_image_resolution: Optional[List[int]] = None  # [width, height]
    min_image_resolution: Optional[List[int]] = None  # [width, height]
    supported_image_formats: List[str] = Field(default_factory=list)
    image_detail_control: bool = False

    # Audio input limits
    max_audio_size: Optional[int] = None  # bytes
    max_audio_duration: Optional[int] = None  # seconds
    supported_audio_formats: List[str] = Field(default_factory=list)

    # Video input limits
    max_video_size: Optional[int] = None  # bytes
    max_video_duration: Optional[int] = None  # seconds
    supported_video_formats: List[str] = Field(default_factory=list)

    # Provider-specific features
    supports_files_api: bool = False
    supports_file_uploads: bool = False


class BatchProviderConfig(BaseModel):
    """Configuration for a specific batch provider (Gemini/OpenAI).
    
    Batch APIs provide 50% cost reduction and separate rate limits
    for non-time-critical workloads. Requests are collected, submitted
    as batch jobs, and results are polled asynchronously.
    """
    enabled: bool = True  # Enable this batch provider
    collection_window_seconds: float = 10.0  # Time to collect requests before submitting batch
    max_requests_per_batch: int = 100  # Max requests per batch (OpenAI: 50k, Gemini: 200k)
    poll_interval_seconds: float = 10.0  # Interval between status polls
    max_wait_hours: float = 24.0  # Max time to wait for batch completion
    max_retries: int = 3  # Max retries for server-side cancelled jobs
    cancel_on_startup: bool = True  # Cancel orphaned batches on app startup
    fallback_to_sync: bool = False  # Fallback to sync API on timeout/failure


class BatchProvidersConfig(BaseModel):
    """Configuration for all batch providers."""
    gemini: BatchProviderConfig = Field(default_factory=BatchProviderConfig)
    openai: BatchProviderConfig = Field(default_factory=BatchProviderConfig)
    anthropic: BatchProviderConfig = Field(default_factory=BatchProviderConfig)


class BatchSystemConfig(BaseModel):
    """Global batch system configuration.
    
    Centralized configuration for batch processing. Models with
    provider='batch' reference this config via their batch_provider field.
    """
    storage_path: str = "data/batch_jobs"  # Where to store batch job data
    providers: BatchProvidersConfig = Field(default_factory=BatchProvidersConfig)


class LLMModelConfig(BaseModel):
    """Individual LLM model configuration"""
    provider: Literal["ollama", "openai", "openai_httpx", "anthropic", "gemini", "gemini_sdk", "batch", "mock"] = "ollama"
    model: str
    api_key: Optional[str] = None
    base_url: Optional[str] = None  # Custom base URL for API endpoint (e.g. Gemini, Ollama, OpenAI-compatible)
    context_window: int = 32768  # default num_ctx for Ollama-compatible models
    ollama_mode: Literal["openai_compat", "native"] = "openai_compat"
    request_timeout: int = 120  # seconds for LLM API calls
    parallel_tool_calls: bool = True  # Enable parallel tool execution (set to False if LLM concatenates tool names/args)
    httpx_timeouts: Optional[HTTPXTimeoutConfig] = None  # HTTPX-specific timeout overrides
    capabilities: Optional[ModelCapabilitiesConfig] = None  # Model capabilities
    include_thoughts: Optional[bool] = None  # Enable thinking/reasoning output (Gemini, DeepSeek)
    thinking_budget: Optional[int] = None  # Token budget for thinking process (Gemini 2.5+, default: 8192)
    modalities: Optional[List[str]] = None  # Output modalities for audio models (e.g., ["text"] or ["text", "audio"])
    
    # Batch provider (only for provider="batch")
    batch_provider: Optional[Literal["gemini", "openai", "anthropic"]] = None  # Which batch API to use


class LLMProfile(BaseModel):
    """LLM usage profile that references a model"""
    model_ref: str  # Reference to model in models dict
    description: Optional[str] = None
    max_steps: Optional[int] = None


class LLMSystemConfig(BaseModel):
    """Complete LLM system configuration"""
    httpx_timeouts: Optional[HTTPXTimeoutConfig] = None  # Default HTTPX timeouts for all models
    batch: Optional[BatchSystemConfig] = None  # Global batch processing configuration
    models: Dict[str, LLMModelConfig] = {}
    profiles: Dict[str, LLMProfile] = {}
    default_profile: Optional[str] = "normal"  # Default LLM profile to use


# ===========================
# MCP Configuration Models
# ===========================

class ExternalServerConfig(BaseModel):
    """Configuration for external server connections and defaults"""
    # This matches the type comment in mcp.yaml for external_server
    pass  # Currently empty in YAML, can be extended


class ToolConfig(BaseModel):
    """Tool access control configuration"""
    allowed: Optional[List[str]] = Field(default_factory=list)  # list of allowed tools (use "*" to allow all tools)
    blocked: Optional[List[str]] = Field(default_factory=list)  # list of blocked tools

    def __init__(self, **data):
        # Convert None values to empty lists
        if data.get("allowed") is None:
            data["allowed"] = []
        if data.get("blocked") is None:
            data["blocked"] = []
        super().__init__(**data)


class HooksConfig(BaseModel):
    """Hook system configuration for individual agents."""
    enabled: bool = True  # Global switch: enable/disable all hooks for this agent
    overrides: Dict[str, Dict[str, Any]] = Field(default_factory=dict)  # Per-hook config (enabled, timeout, custom config, etc.)


class TimeoutConfig(BaseModel):
    """Timeout configuration for agent execution to prevent deadlocks."""
    # Queue handler timeouts
    status_queue_put_timeout: float = 5.0  # Timeout for status queue.put() operations (seconds)
    
    # LLM task polling timeout - safety net only!
    # For batch mode: The actual timeout is controlled by max_wait_hours in llm.yaml
    # For sync mode: The actual timeout is controlled by request_timeout in model config
    # This is just a safety net for truly stuck calls (default: 24 hours)
    llm_task_max_iterations: int = 864000  # 864000 * 0.1s = 24 hours safety net
    
    # Session lock timeout
    session_lock_timeout: float = 5.0  # Timeout for acquiring session lock (seconds)
    
    # Tool cleanup timeout
    tool_cleanup_timeout: float = 30.0  # Timeout for tool cleanup during cancellation (seconds)
    

class AgentConfig(BaseModel):
    """Configuration for individual agent instances (matches type comment in mcp.yaml)"""
    llm_profile: str | List[str] = "normal"  # LLM profile(s) to use. If list, first is default, others are available options
    llm_profile_fallbacks: Optional[List[str]] = None  # Fallback profiles on rate limit/errors (tried in order)
    max_steps: int = 20  # maximum steps for agents that support multi-step reasoning (default: 20, used if not set in config)
    tools: ToolConfig = Field(default_factory=ToolConfig)
    hooks: Optional[HooksConfig] = None  # Hook system configuration (optional)
    system_template: Optional[str] = None  # Path to system prompt template file
    system_prompt: Optional[str] = None  # Inline system prompt (alternative to system_template)
    template_vars: Optional[Dict[str, Any]] = None  # Custom variables for Jinja2 template rendering
    timeouts: TimeoutConfig = Field(default_factory=TimeoutConfig)  # Timeout configuration for deadlock prevention

    @property
    def default_llm_profile(self) -> str:
        """Get the default LLM profile (first in list if list, otherwise the string)."""
        if isinstance(self.llm_profile, list):
            return self.llm_profile[0] if self.llm_profile else "normal"
        return self.llm_profile

    @property
    def available_llm_profiles(self) -> List[str]:
        """Get all available LLM profiles."""
        if isinstance(self.llm_profile, list):
            return self.llm_profile
        return [self.llm_profile]
    
    @property
    def fallback_profiles(self) -> List[str]:
        """Get fallback profiles for rate limit/error recovery."""
        return self.llm_profile_fallbacks or []


class AgentMetadata(BaseModel):
    """Metadata for agent configuration.

    Provides additional information about agents for discoverability,
    categorization, and visibility control.
    """
    author: Optional[str] = None  # Author/creator of the agent
    version: Optional[str] = None  # Version string (e.g., "1.0.0")
    tags: Optional[List[str]] = None  # Tags for categorization/search
    category: Optional[str] = None  # Category (e.g., "financial", "research", "development")

    # Visibility control: determines where the agent appears
    # Default: "private" - agents must explicitly opt-in to visibility
    visibility: Literal["ui", "tool", "both", "private"] = "private"
    # - "ui": Visible in UI dropdown, NOT available as tool for other agents
    # - "tool": Available as tool for other agents, NOT in UI dropdown
    # - "both": Visible in UI AND available as tool
    # - "private": Neither UI nor tool (for testing/experimental agents)


class MCPConfig(BaseModel):
    """MCP configuration (matches type comment in mcp.yaml for default_config)"""
    model_config = {"extra": "allow"}  # Allow extra fields for plugin-specific config

    type: str = "basic_agent"   # type of mcp-server/agent to use
    enabled: bool = False       # enable or disable this mcp-server/agent
    description: Optional[str] = None  # Human-readable description of this instance
    self_tool_descriptions: Optional[Dict[str, str]] = Field(default_factory=dict)  # Custom descriptions for this server's own tools
    agent_config: Optional[AgentConfig] = None
    metadata: Optional[AgentMetadata] = None  # Instance metadata (author, version, visibility)


class ExternalServerConnectionConfig(BaseModel):
    """Configuration for external server connections"""
    timeout: float = 5.0
    parallel_connect: bool = True


class ExternalServerCacheConfig(BaseModel):
    """Configuration for external server caching"""
    enabled: bool = True  # Enable tool list caching
    tool_list_ttl: float = 30.0  # TTL for MCPClientManager internal cache
    max_size: Optional[int] = None  # Maximum cache entries (None = unlimited)


class MCPAuthConfig(BaseModel):
    """Authentication configuration for MCP servers"""
    type: str = "none"  # none, bearer, api_key, basic
    api_key: Optional[str] = None
    api_key_header: str = "Authorization"
    bearer_token: Optional[str] = None
    username: Optional[str] = None
    password: Optional[str] = None

    # Retry settings
    max_retries: int = 3
    retry_delay: float = 1.0


class RemoteMCPConfig(BaseModel):
    """Configuration for a remote MCP server"""
    url: str
    enabled: bool = False
    description: Optional[str] = None
    transport: str = "streaming"
    initialization_options: Optional[Dict[str, Any]] = None
    features: Optional[Dict[str, bool]] = None
    tools: Optional[ToolConfig] = None

    # Authentication and security
    auth: Optional[MCPAuthConfig] = None



class ExternalServersConfig(BaseModel):
    """Configuration for all external servers"""
    connection: ExternalServerConnectionConfig = Field(default_factory=ExternalServerConnectionConfig)
    cache: ExternalServerCacheConfig = Field(default_factory=ExternalServerCacheConfig)
    remote_servers: Dict[str, RemoteMCPConfig] = Field(default_factory=dict)


class MCPServerRateLimitConfig(BaseModel):
    """Rate limiting configuration for MCP server mode"""
    enabled: bool = True
    requests_per_minute: int = 60
    requests_per_hour: int = 1000
    burst_size: int = 10  # Allow bursts up to this many requests


class MCPServerAuthConfig(BaseModel):
    """Authentication configuration for MCP server mode"""
    required: bool = True
    methods: List[Literal["jwt", "api_key"]] = Field(default_factory=lambda: ["jwt", "api_key"])  # type: ignore[arg-type]  # Pydantic default_factory complexity


class MCPServerModeConfig(BaseModel):
    """Configuration for MCP server mode (exposing AgentSystem as remote MCP server)"""
    enabled: bool = False
    endpoint: str = "/mcp"  # Main JSON-RPC endpoint
    sse_endpoint: Optional[str] = "/mcp/sse"  # SSE stream endpoint (optional)

    # Plugin exposure configuration
    expose_plugins: List[str] = Field(default_factory=lambda: ["*"])  # ['*'] = all, or list specific plugins

    # Authentication and security
    authentication: MCPServerAuthConfig = Field(default_factory=MCPServerAuthConfig)
    rate_limit: MCPServerRateLimitConfig = Field(default_factory=MCPServerRateLimitConfig)

    # Session configuration
    session_ttl: float = 3600.0  # Session timeout in seconds (1 hour)
    max_concurrent_sessions: int = 100  # Maximum concurrent MCP client sessions
    
    # Timeout configuration
    default_timeout: float = 30.0  # Default HTTP request timeout for MCP streamable transport (seconds)
    sse_heartbeat_interval: float = 30.0  # Interval for SSE heartbeat messages (seconds)


class PluginsConfig(BaseModel):
    """Configuration for local plugins (matches config/plugins.yaml)"""
    plugin_dirs: List[str] = Field(default_factory=list)
    default_config: MCPConfig = Field(default_factory=MCPConfig)
    servers: Dict[str, MCPConfig] = Field(default_factory=dict)  # Named MCP server configurations


class MCPServersConfig(BaseModel):
    """Configuration for external MCP servers (matches config/mcp_servers.yaml)"""
    connection: ExternalServerConnectionConfig = Field(default_factory=ExternalServerConnectionConfig)
    cache: ExternalServerCacheConfig = Field(default_factory=ExternalServerCacheConfig)
    remote_servers: Dict[str, RemoteMCPConfig] = Field(default_factory=dict)


# Backward compatibility: Keep MCPSystemConfig for transition period
class MCPSystemConfig(BaseModel):
    """
    DEPRECATED: Old monolithic MCP system configuration.
    Use separate configs instead: PluginsConfig, MCPServersConfig, MCPServerModeConfig.
    This model is kept for backward compatibility during migration.
    """
    plugin_dirs: List[str] = Field(default_factory=list)
    default_config: MCPConfig = Field(default_factory=MCPConfig)
    external_servers: ExternalServersConfig = Field(default_factory=ExternalServersConfig)
    servers: Dict[str, MCPConfig] = Field(default_factory=dict)  # Named MCP server configurations
    server_mode: MCPServerModeConfig = Field(default_factory=MCPServerModeConfig)  # MCP server mode configuration


# ===========================
# Core Configuration Models
# ===========================

class NetworkConfig(BaseModel):
    """Network configuration for the application"""
    ssl_verify: bool = False
    host: str = "127.0.0.1"
    port: int = 8000
    disable_cache: bool = True
    
    # HTTP connection pooling settings
    http_connection_limit: int = 10  # Total HTTP connections for MCP streamable transport
    http_connection_limit_per_host: int = 5  # HTTP connections per host
    
    # CLI request timeout
    cli_request_timeout: float = 5.0  # Timeout for CLI HTTP requests (seconds)


class CancellationConfig(BaseModel):
    """Configuration for the cancellation system"""
    cleanup_timeout: float = 10.0  # Seconds to wait for graceful cleanup before forcing termination
    monitor_interval: float = 1.0  # Seconds between timeout checks


class LoggingConfig(BaseModel):
    """Logging configuration"""
    enabled: bool = True
    level: str = "DEBUG"
    file: str = "logs/agent.log"
    file_cli: Optional[str] = None
    file_api: Optional[str] = None
    cancellation: Optional[CancellationConfig] = None
    
    # Log rotation settings
    rotation_enabled: bool = True  # Enable log rotation
    max_bytes: Union[int, str] = Field(default="10MB")  # Max size per log file (int in bytes or string like "10MB", "100KB", "1GB")
    backup_count: int = 5  # Number of backup files to keep
    
    @field_validator('max_bytes', mode='before')
    @classmethod
    def parse_max_bytes(cls, v: Union[int, str]) -> int:
        """Parse max_bytes from human-readable format (e.g., '10MB') to bytes."""
        if isinstance(v, int):
            return v
        
        if isinstance(v, str):
            # Match number followed by optional unit (KB, MB, GB, case-insensitive)
            match = re.match(r'^(\d+(?:\.\d+)?)\s*(KB|MB|GB|K|M|G)?$', v.strip(), re.IGNORECASE)
            if not match:
                raise ValueError(
                    f"Invalid size format: '{v}'. "
                    "Expected format: number with optional unit (KB/MB/GB), e.g., '10MB', '100KB', '1GB'"
                )
            
            number = float(match.group(1))
            unit = (match.group(2) or '').upper()
            
            # Convert to bytes
            multipliers = {
                '': 1,
                'K': 1024,
                'KB': 1024,
                'M': 1024 * 1024,
                'MB': 1024 * 1024,
                'G': 1024 * 1024 * 1024,
                'GB': 1024 * 1024 * 1024,
            }
            
            return int(number * multipliers[unit])
        
        raise ValueError(f"max_bytes must be int or string, got {type(v).__name__}")
    
    def model_post_init(self, __context) -> None:
        """Post-init hook to parse default values through validator."""
        # Manually trigger validation for max_bytes if it's still a string
        if isinstance(self.max_bytes, str):
            self.max_bytes = self.parse_max_bytes(self.max_bytes)


class ContextConfig(BaseModel):
    """Context information configuration"""
    auto_datetime: bool = True
    timezone: str = "Europe/Berlin"
    location: str = "Germany"


class StatusConfig(BaseModel):
    """Status message system configuration"""
    queue_maxsize: int = 1000  # Max events per queue (prevents memory exhaustion)
    drop_oldest_when_full: bool = True  # Drop oldest events when queue is full
    
    # SSE connection keep-alive settings
    sse_keepalive_interval: float = 15.0  # Interval for SSE keep-alive comments (seconds)
    llm_heartbeat_interval: float = 5.0  # Interval for heartbeat events during LLM calls (seconds)


class VisionConfig(BaseModel):
    """Vision/image processing configuration"""
    image_warn_size_mb: float = 10.0  # Warn when images exceed this size (MB)
    image_max_size_mb: Optional[float] = None  # Maximum allowed image size (MB), None = no limit


# ===========================
# Security Configuration Models
# ===========================

class AnonymousAccessConfig(BaseModel):
    """Configuration for anonymous (unauthenticated) access.
    
    When auth.enabled=true but anonymous_access.enabled=true,
    unauthenticated users can access certain endpoints with
    a limited guest role.
    """
    enabled: bool = False  # Allow unauthenticated access to certain endpoints
    role: str = "guest"  # Role assigned to anonymous users
    allowed_endpoints: List[str] = Field(default_factory=lambda: [
        "GET /health",
        "GET /static/*",
        "GET /login",
        "POST /auth/login",
        "POST /auth/register",  # Allow self-registration when enabled
    ])
    rate_limit_multiplier: float = 0.5  # Stricter rate limit for anonymous (50% of normal)


class EndpointSecurityRule(BaseModel):
    """Security rule for an endpoint pattern.
    
    Patterns support:
    - Exact match: "GET /agents"
    - Wildcard: "/admin/*", "* /sessions/*"
    - Method prefix: "POST /run", "GET /events"
    """
    pattern: str  # e.g., "POST /run", "/admin/*", "GET /sessions/*"
    policy: Literal["require_auth", "allow_anonymous"] = "require_auth"
    min_role: Optional[str] = None  # "admin", "user", "guest"
    description: Optional[str] = None  # Human-readable description


class EndpointSecurityConfig(BaseModel):
    """Endpoint-level security configuration.
    
    When auth.enabled=true, this config controls which endpoints
    require authentication and what role is needed.
    """
    # Default policy when no specific rule matches
    default_policy: Literal["require_auth", "allow_anonymous"] = "require_auth"
    
    # Audit logging for all endpoint access (logs to security.log)
    audit_enabled: bool = True
    
    # Custom rules (processed in order, first match wins)
    rules: List[EndpointSecurityRule] = Field(default_factory=lambda: [
        # Admin endpoints always require admin role
        EndpointSecurityRule(
            pattern="/admin/*",
            policy="require_auth",
            min_role="admin",
            description="Admin endpoints require admin role"
        ),
        # Agent execution requires user role
        EndpointSecurityRule(
            pattern="POST /run",
            policy="require_auth",
            min_role="user",
            description="Agent execution requires authentication"
        ),
        # Events stream requires user role
        EndpointSecurityRule(
            pattern="GET /events",
            policy="require_auth",
            min_role="user",
            description="Event stream requires authentication"
        ),
        # Session management requires user role
        EndpointSecurityRule(
            pattern="* /sessions/*",
            policy="require_auth",
            min_role="user",
            description="Session management requires authentication"
        ),
    ])


class LLMSecurityConfig(BaseModel):
    """Security configuration for LLM API calls.
    
    These settings ensure that LLM requests (which cost money)
    are properly authorized and audited.
    """
    require_valid_user: bool = True  # LLM calls require authenticated user context
    validate_session_ownership: bool = True  # Users can only access their own sessions
    audit_llm_requests: bool = True  # Log all LLM requests with user info
    max_tokens_per_request_anonymous: Optional[int] = None  # Token limit for anonymous (None = blocked)
    max_requests_per_hour_anonymous: int = 0  # Hourly limit for anonymous users (0 = blocked)


class PluginSecurityConfig(BaseModel):
    """Security configuration for plugin web endpoints.
    
    Controls how plugin-provided HTTP endpoints are secured.
    By default, all plugin endpoints require authentication.
    """
    # Global default for all plugin endpoints
    default_policy: Literal["require_auth", "allow_anonymous"] = "require_auth"
    default_min_role: str = "user"  # Default minimum role for plugin endpoints
    
    # Plugin-specific overrides (plugin_name -> config)
    # Example: {"todo": {"policy": "allow_anonymous"}, "admin_tools": {"min_role": "admin"}}
    plugin_overrides: Dict[str, Dict[str, Any]] = Field(default_factory=dict)
    
    # Endpoint pattern overrides (same format as endpoint_security.rules)
    # These take precedence over plugin_overrides
    endpoint_rules: List[EndpointSecurityRule] = Field(default_factory=lambda: [
        # Example: Block all plugin admin endpoints for non-admins
        EndpointSecurityRule(
            pattern="/plugins/*/admin/*",
            policy="require_auth",
            min_role="admin",
            description="Plugin admin endpoints require admin role"
        ),
    ])


class AuthConfig(BaseModel):
    """Authentication and authorization configuration.
    
    Multi-layered security configuration for the AgentSystem.
    
    Security Layers:
    1. Transport: HTTPS (handled externally by reverse proxy)
    2. Rate Limiting: Per-IP request limiting
    3. Authentication: JWT tokens / API keys
    4. Authorization: Role-based access control
    5. Request Context: User context in agent execution
    """
    enabled: bool = False  # Master switch for authentication system
    secret_key: str = "CHANGE_THIS_SECRET_KEY_IN_PRODUCTION"
    algorithm: str = "HS256"
    access_token_expire_minutes: int = 30
    refresh_token_expire_days: int = 30  # Refresh token valid for 30 days

    # Database settings
    database_path: str = "data/users.db"

    # Security settings
    rate_limit_enabled: bool = True
    requests_per_minute: int = 60
    security_headers_enabled: bool = True

    # CORS settings
    cors_enabled: bool = True
    cors_origins: List[str] = Field(default_factory=lambda: ["*"])
    cors_credentials: bool = True
    cors_methods: List[str] = Field(default_factory=lambda: ["*"])
    cors_headers: List[str] = Field(default_factory=lambda: ["*"])

    # Trusted hosts (optional)
    trusted_hosts: Optional[List[str]] = None

    # Default admin user (created on first startup if no users exist)
    default_admin_username: str = "admin"
    default_admin_password: Optional[str] = None  # Generated randomly if not set
    default_admin_email: str = "admin@localhost"
    
    # NEW: Anonymous access configuration
    anonymous_access: AnonymousAccessConfig = Field(default_factory=AnonymousAccessConfig)
    
    # NEW: Endpoint-level security rules
    endpoint_security: EndpointSecurityConfig = Field(default_factory=EndpointSecurityConfig)
    
    # NEW: LLM request security
    llm_security: LLMSecurityConfig = Field(default_factory=LLMSecurityConfig)
    
    # NEW: Plugin endpoint security
    plugin_security: PluginSecurityConfig = Field(default_factory=PluginSecurityConfig)


class AgentSystemConfig(BaseModel):
    """Main configuration model for the entire AgentSystem"""
    # Basic metadata
    name: str = "AgentSystem"
    version: str = "0.0.0"
    description: str = "Scarab Flexible AI Agent System using MCP"

    # Include references (for documentation purposes)
    includes: Optional[List[str]] = None

    # Core configurations
    context: ContextConfig = Field(default_factory=ContextConfig)
    status: StatusConfig = Field(default_factory=StatusConfig)
    vision: VisionConfig = Field(default_factory=VisionConfig)
    network: NetworkConfig = Field(default_factory=NetworkConfig)
    default_agent: str = "basic_agent"
    logging: LoggingConfig = Field(default_factory=LoggingConfig)
    auth: AuthConfig = Field(default_factory=AuthConfig)

    # Included configurations (will be populated from included files)
    llm_system: Optional[LLMSystemConfig] = None

    # New structure (Epic 0044) - matches YAML keys
    plugins: Optional[PluginsConfig] = None  # From config/plugins.yaml -> plugins:
    external_servers: Optional[MCPServersConfig] = None  # From config/mcp_servers.yaml -> external_servers:
    server_mode: Optional[MCPServerModeConfig] = None  # From config/mcp_server_mode.yaml -> server_mode: