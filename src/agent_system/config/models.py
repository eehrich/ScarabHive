from __future__ import annotations

from pydantic import BaseModel
from typing import Literal, Optional


class LLMConfig(BaseModel):
    provider: Literal["ollama", "openai"] = "ollama"
    model: str = "gpt-oss:20b"
    openai_api_key: Optional[str] = None


class MCPServerRef(BaseModel):
    type: str


class MCPConfig(BaseModel):
    enabled_servers: list[str] = []


class NetworkConfig(BaseModel):
    ssl_verify: bool = True


# In this scaffold we keep 'servers' as dict[str, dict] directly on AgentConfig.


class AgentConfig(BaseModel):
    llm: LLMConfig = LLMConfig()
    mcp: MCPConfig = MCPConfig()
    network: NetworkConfig = NetworkConfig()
    servers: dict[str, dict] = {}
