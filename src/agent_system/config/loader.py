from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

import yaml

from .models import AgentConfig


_env_var_pattern = re.compile(r"\$\{([A-Z0-9_]+)\}")


def _expand_env(value: Any) -> Any:
    if isinstance(value, str):
        def repl(m: re.Match[str]) -> str:
            key = m.group(1)
            return os.environ.get(key, "")
        return _env_var_pattern.sub(repl, value)
    if isinstance(value, dict):
        return {k: _expand_env(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_expand_env(v) for v in value]
    return value


def load_config(path: str | Path) -> AgentConfig:
    p = Path(path)
    data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}

    # If the master manifest declares includes/files, load and merge them.
    includes = data.get("includes") or data.get("files") or []
    if isinstance(includes, str):
        includes = [includes]

    merged: dict[str, Any] = {}
    for inc in includes:
        try:
            inc_path = Path(inc)
            if not inc_path.is_absolute():
                inc_path = p.parent.joinpath(inc_path)
            if inc_path.exists():
                part = yaml.safe_load(inc_path.read_text(encoding="utf-8")) or {}
                # later includes override earlier merged keys
                merged.update(part)
        except Exception:
            # ignore errors reading individual includes
            pass

    # Merge master (manifest) with included content, giving included files precedence
    merged_final = dict(data)
    merged_final.update(merged)

    merged_final = _expand_env(merged_final)
    return AgentConfig.model_validate(merged_final)


def build_mcp_payload(config: AgentConfig) -> dict[str, Any]:
    """
    Build the MCP initialization payload from AgentConfig.
    
    Centralizes the logic for extracting and formatting configuration data
    needed by MCP integration, avoiding duplication between CLI and API.
    
    Args:
        config: The loaded AgentConfig instance
        
    Returns:
        Dictionary containing MCP, LLM system, agent profiles, and servers configuration
    """

    
    # Extract MCP configuration block
    try:
        if hasattr(config.mcp, "model_dump"):
            mcp_block = config.mcp.model_dump()
        elif isinstance(config.mcp, dict):
            mcp_block = dict(config.mcp)
        else:
            mcp_block = getattr(config.mcp, "__dict__", {})
    except Exception:
        mcp_block = {}

    # Build the payload with all required configuration sections
    payload = {"mcp": mcp_block}
    
    # Include LLM system configuration for plugin parent_llm injection
    if hasattr(config, 'llm_system') and config.llm_system:
        try:
            if hasattr(config.llm_system, 'model_dump'):
                payload['llm_system'] = config.llm_system.model_dump()
            else:
                payload['llm_system'] = config.llm_system
        except Exception:
            pass
    
    # Include agent LLM profile assignments
    if hasattr(config, 'agent_llm_profiles') and config.agent_llm_profiles:
        try:
            payload['agent_llm_profiles'] = config.agent_llm_profiles
        except Exception:
            pass
    
    # Include servers configuration for MCP plugin initialization
    if hasattr(config, 'servers') and config.servers:
        try:
            if hasattr(config.servers, 'model_dump'):
                payload['servers'] = config.servers.model_dump()
            elif isinstance(config.servers, dict):
                payload['servers'] = config.servers
            else:
                payload['servers'] = getattr(config.servers, '__dict__', {})
        except Exception:
            pass
    return payload
