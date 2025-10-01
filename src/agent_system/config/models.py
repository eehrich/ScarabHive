"""
Configuration models for AgentSystem.

This module provides Pydantic models that match the new YAML configuration
structure with config.yaml as the master configuration and included files
for LLM and MCP configurations.
"""
from __future__ import annotations

from pydantic import BaseModel, Field
from typing import Literal, Optional, Dict, List, Any


# ===========================
# LLM Configuration Models
# ===========================

class HTTPXTimeoutConfig(BaseModel):
    """HTTPX timeout configuration"""
    connect: float = 10.0      # Connection establishment timeout
    read: float = 180.0        # Read timeout (waiting for response data)
    write: float = 10.0        # Write timeout (sending request data)  
    pool: float = 5.0          # Pool timeout (getting connection from pool)


class LLMModelConfig(BaseModel):
    """Individual LLM model configuration"""
    provider: Literal["ollama", "openai", "openai_httpx"] = "ollama"
    model: str
    openai_api_key: Optional[str] = None
    ollama_url: Optional[str] = None  # e.g. http://remote-host:11434
    context_window: int = 32768  # default num_ctx for Ollama-compatible models
    ollama_mode: Literal["openai_compat", "native"] = "openai_compat"
    request_timeout: int = 120  # seconds for LLM API calls
    httpx_timeouts: Optional[HTTPXTimeoutConfig] = None  # HTTPX-specific timeout overrides


class LLMProfile(BaseModel):
    """LLM usage profile that references a model"""
    model_ref: str  # Reference to model in models dict
    description: Optional[str] = None
    max_steps: Optional[int] = None


class LLMSystemConfig(BaseModel):
    """Complete LLM system configuration"""
    httpx_timeouts: Optional[HTTPXTimeoutConfig] = None  # Default HTTPX timeouts for all models
    models: Dict[str, LLMModelConfig] = {}
    profiles: Dict[str, LLMProfile] = {}


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


class TokenOptimizationConfig(BaseModel):
    """Token optimization configuration"""
    enable_compression: bool = False
    compress_tool_results: bool = False
    optimize_json: bool = False
    remove_verbose_patterns: bool = False


class ContextManagementConfig(BaseModel):
    """Context window management configuration"""
    enabled: bool = True
    summarizer_llm_profile: str = "turbo"
    strategy: Literal["TRUNCATE_OLDEST", "SUMMARIZE_OLDEST", "SLIDING_WINDOW", "SMART_COMPRESSION"] = "SUMMARIZE_OLDEST"
    preserve_recent_messages: int = 10
    prediction_threshold: float = 0.95
    summarization_threshold: float = 0.70
    max_summary_words: int = 5000
    tool_result_preview_chars: int = 500
    warning_levels: Dict[str, float] = Field(default_factory=lambda: {
        "yellow": 0.70,
        "orange": 0.85,
        "red": 0.95
    })
    token_optimization: Optional[TokenOptimizationConfig] = None


class AgentConfig(BaseModel):
    """Configuration for individual agent instances (matches type comment in mcp.yaml)"""
    llm_profile: str = "normal"  # LLM profile to use
    max_steps: int = 20  # maximum steps for agents that support multi-step reasoning
    tools: ToolConfig = Field(default_factory=ToolConfig)
    context_management: ContextManagementConfig = Field(default_factory=ContextManagementConfig)
    
    # Legacy field for backward compatibility - maps to tools.allowed
    allowed_tools: Optional[List[str]] = None
    
    def __init__(self, **data):
        # Handle legacy allowed_tools field
        if "allowed_tools" in data and data["allowed_tools"] is not None:
            if "tools" not in data:
                data["tools"] = {}
            if isinstance(data["tools"], dict):
                data["tools"]["allowed"] = data["allowed_tools"]
            # Remove the legacy field after processing
            data.pop("allowed_tools", None)
        super().__init__(**data)


class MCPConfig(BaseModel):
    """MCP configuration (matches type comment in mcp.yaml for default_config)"""
    type: str = "basic_agent"   # type of mcp-server/agent to use
    enabled: bool = False       # enable or disable this mcp-server/agent
    agent_config: Optional[AgentConfig] = None


class ExternalServerConnectionConfig(BaseModel):
    """Configuration for external server connections"""
    timeout: float = 5.0
    parallel_connect: bool = True


class ExternalServerCacheConfig(BaseModel):
    """Configuration for external server caching"""
    tool_list_ttl: float = 30.0


class MCPAuthConfig(BaseModel):
    """Authentication configuration for MCP servers"""
    type: str = "none"  # none, bearer, api_key, basic
    api_key: Optional[str] = None
    api_key_header: str = "Authorization"
    bearer_token: Optional[str] = None
    username: Optional[str] = None
    password: Optional[str] = None
    
    # Security settings
    ssl_verify: bool = True
    timeout: float = 30.0
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
    ssl_verify: Optional[bool] = True
    timeout: Optional[float] = 30.0
    max_retries: Optional[int] = 3
    retry_delay: Optional[float] = 1.0


class ExternalServersConfig(BaseModel):
    """Configuration for all external servers"""
    connection: ExternalServerConnectionConfig = Field(default_factory=ExternalServerConnectionConfig)
    cache: ExternalServerCacheConfig = Field(default_factory=ExternalServerCacheConfig)
    remote_servers: Dict[str, RemoteMCPConfig] = Field(default_factory=dict)


class MCPSystemConfig(BaseModel):
    """Complete MCP system configuration (matches type comment in mcp.yaml)"""
    plugin_dirs: List[str] = Field(default_factory=list)
    default_config: MCPConfig = Field(default_factory=MCPConfig)
    external_servers: ExternalServersConfig = Field(default_factory=ExternalServersConfig)
    servers: Dict[str, MCPConfig] = Field(default_factory=dict)  # Named MCP server configurations


# ===========================
# Core Configuration Models
# ===========================

class NetworkConfig(BaseModel):
    """Network configuration for the application"""
    ssl_verify: bool = False
    host: str = "127.0.0.1"
    port: int = 8000
    disable_cache: bool = True


class CancellationConfig(BaseModel):
    """Configuration for the cancellation system"""
    cleanup_timeout: float = 10.0  # Seconds to wait for graceful cleanup before forcing termination
    monitor_interval: float = 1.0  # Seconds between timeout checks


class LoggingConfig(BaseModel):
    """Logging configuration"""
    enabled: bool = True
    level: str = "DEBUG"
    file: str = "logs/agent.log"
    file_cli: str = "logs/cli.log"
    file_api: str = "logs/api.log"
    cancellation: Optional[CancellationConfig] = None


class ContextConfig(BaseModel):
    """Context information configuration"""
    auto_datetime: bool = True
    timezone: str = "Europe/Berlin"
    location: str = "Germany"


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
    network: NetworkConfig = Field(default_factory=NetworkConfig)
    default_agent: str = "basic_agent"
    logging: LoggingConfig = Field(default_factory=LoggingConfig)
    
    # Included configurations (will be populated from included files)
    llm_system: Optional[LLMSystemConfig] = None
    mcp_system: Optional[MCPSystemConfig] = None