from __future__ import annotations

from pydantic import BaseModel
from typing import Literal, Optional


class LLMConfig(BaseModel):
    provider: Literal["ollama", "openai"] = "ollama"
    model: str = "gpt-oss:20b"
    openai_api_key: Optional[str] = None
    ollama_url: Optional[str] = None  # e.g. http://remote-host:11434
    context_window: int = 32768  # default num_ctx for Ollama-compatible models
    ollama_mode: Literal["openai_compat", "native"] = "openai_compat"


class MCPServerRef(BaseModel):
    type: str


class MCPConfig(BaseModel):
    enabled_servers: list[str] = []


class NetworkConfig(BaseModel):
    ssl_verify: bool = True


# In this scaffold we keep 'servers' as dict[str, dict] directly on AgentConfig.


class LoggingConfig(BaseModel):
    enabled: bool = False
    level: str = "INFO"
    file: str = "logs/agent.log"
    as_json: bool = False


class PromptsConfig(BaseModel):
    system_template: str = "config/prompts/system_prompt.yaml"


class AgentConfig(BaseModel):
    llm: LLMConfig = LLMConfig()
    mcp: MCPConfig = MCPConfig()
    network: NetworkConfig = NetworkConfig()
    servers: dict[str, dict] = {}
    logging: LoggingConfig = LoggingConfig()
    prompts: PromptsConfig = PromptsConfig()
