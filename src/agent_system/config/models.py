from __future__ import annotations

from pydantic import BaseModel, Field
from typing import Literal, Optional


class LLMConfig(BaseModel):
    provider: Literal["ollama", "openai"] = "ollama"
    model: str = "gpt-oss:20b"
    openai_api_key: Optional[str] = None
    ollama_url: Optional[str] = None  # e.g. http://remote-host:11434
    context_window: int = 32768  # default num_ctx for Ollama-compatible models
    ollama_mode: Literal["openai_compat", "native"] = "openai_compat"
    request_timeout: int = 120  # seconds for LLM API calls


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


class AgentConfig(BaseModel):
    llm: LLMConfig = LLMConfig()
    mcp: MCPConfig = MCPConfig()
    network: NetworkConfig = NetworkConfig()
    servers: dict[str, dict] = {}
    logging: LoggingConfig = LoggingConfig()
    prompts: PromptsConfig = PromptsConfig()
    context: ContextConfig = ContextConfig()
    # Maximum planning/tool-calling steps before stopping
    max_steps: int = Field(default=6, ge=1)
