from __future__ import annotations

from pydantic import BaseModel, Field
from typing import Literal, Optional, Dict


class LLMModelConfig(BaseModel):
    """Individual LLM model configuration"""
    provider: Literal["ollama", "openai"] = "ollama"
    model: str
    openai_api_key: Optional[str] = None
    ollama_url: Optional[str] = None  # e.g. http://remote-host:11434
    context_window: int = 32768  # default num_ctx for Ollama-compatible models
    ollama_mode: Literal["openai_compat", "native"] = "openai_compat"
    request_timeout: int = 120  # seconds for LLM API calls


class LLMProfile(BaseModel):
    """LLM usage profile that references a model"""
    model_ref: str  # Reference to model in models dict
    description: Optional[str] = None
    max_steps: Optional[int] = None
    # Could add profile-specific overrides here if needed


class LLMSystemConfig(BaseModel):
    """Complete LLM system configuration with models and profiles"""
    models: Dict[str, LLMModelConfig] = {}
    profiles: Dict[str, LLMProfile] = {}
    default_profile: str = "fast"


class LLMConfig(BaseModel):
    """Legacy LLM config - kept for backwards compatibility"""
    provider: Literal["ollama", "openai"] = "ollama"
    model: str = "gpt-oss:20b"
    openai_api_key: Optional[str] = None
    ollama_url: Optional[str] = None  # e.g. http://remote-host:11434
    context_window: int = 32768  # default num_ctx for Ollama-compatible models
    ollama_mode: Literal["openai_compat", "native"] = "openai_compat"
    request_timeout: int = 120  # seconds for LLM API calls
    
    # New field for profile-based configuration
    profile: Optional[str] = None  # Reference to profile name


class MCPServerRef(BaseModel):
    type: str


class MCPConfig(BaseModel):
    enabled_servers: list[str] = []
    # Optional list of plugin directories to discover MCP server plugins from
    plugin_dirs: list[str] = []
    # Suffix to use for config backups when enabling/disabling plugins
    backup_suffix: str = ".bak"
    # How many backup rotations to keep (1 = keep only .bak, 0 = no backups)
    backup_rotate: int = 1
    # External MCP servers configuration
    external_servers: dict[str, dict] = {}


class NetworkConfig(BaseModel):
    ssl_verify: bool = True
    # Host and port for the FastAPI/Uvicorn server
    host: str = "127.0.0.1"
    port: int = 8000
    # Disable browser caching for web assets (useful for development)
    disable_cache: bool = False


# In this scaffold we keep 'servers' as dict[str, dict] directly on AgentConfig.


class LoggingConfig(BaseModel):
    enabled: bool = False
    level: str = "INFO"
    file: str = "logs/agent.log"
    # Optional explicit per-role log files. If provided, these override the
    # role-derived naming logic used by the CLI and API startup code.
    file_cli: Optional[str] = None
    file_api: Optional[str] = None
    as_json: bool = False


class PromptsConfig(BaseModel):
    system_template: str = "config/prompts/system_prompt.yaml"


class ContextConfig(BaseModel):
    auto_datetime: bool = True
    timezone: str = "Europe/Berlin"
    location: str = "Germany"


class ContextManagementConfig(BaseModel):
    enabled: bool = True
    strategy: Literal["TRUNCATE_OLDEST", "SUMMARIZE_OLDEST", "SLIDING_WINDOW", "SMART_COMPRESSION"] = "SUMMARIZE_OLDEST"
    preserve_recent_messages: int = 5
    prediction_threshold: float = 0.90
    summarization_threshold: float | int = 0.80
    max_summary_words: int = 500
    tool_result_preview_chars: int = 200
    warning_levels: dict[str, float] = {"yellow": 0.7, "orange": 0.85, "red": 0.95}
    token_optimization: dict | None = None


class CancellationConfig(BaseModel):
    """Configuration for the cancellation system"""
    cleanup_timeout: float = 10.0  # Seconds to wait for graceful cleanup before forcing termination
    monitor_interval: float = 1.0  # Seconds between timeout checks


class AgentConfig(BaseModel):
    llm: LLMConfig = LLMConfig()
    llm_system: Optional[LLMSystemConfig] = None  # New LLM system configuration
    agent_llm_profiles: Optional[Dict[str, str]] = None  # Agent-specific LLM profile assignments
    mcp: MCPConfig = MCPConfig()
    network: NetworkConfig = NetworkConfig()
    servers: dict[str, dict] = {}
    context_management: ContextManagementConfig | None = None
    logging: LoggingConfig = LoggingConfig()
    prompts: PromptsConfig = PromptsConfig()
    context: ContextConfig = ContextConfig()
    cancellation: CancellationConfig = CancellationConfig()
    # Maximum planning/tool-calling steps before stopping
    max_steps: int = Field(default=6, ge=1)
