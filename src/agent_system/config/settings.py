"""
Settings loader using pydantic BaseSettings semantics.

This provides a single, typed configuration entrypoint that prefers
environment variables and falls back to YAML file values. It keeps
behavior explicit and testable.
"""
from __future__ import annotations

from typing import Optional
from pathlib import Path
import os
import yaml

from .models import AgentConfig


def load_settings(config_path: Optional[str] = None) -> AgentConfig:
    """Load and return an `AgentConfig` using environment variables and
    optional YAML config file. Environment variables take precedence for
    any ${VAR} placeholders inside the YAML but do not override explicit
    keys unless the YAML uses that placeholder.

    Args:
        config_path: optional path to YAML config; if omitted, uses
                     the `config/agent.yaml` file if it exists or the
                     path from the AGENT_CONFIG_PATH environment variable.
    """
    # Allow overriding default config file via env var
    env_cfg = os.environ.get("AGENT_CONFIG_PATH")
    cfg_path = Path(config_path or env_cfg or "config/agent.yaml")
    data = {}
    if cfg_path.exists():
        data = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}

    # Apply simple env-variable expansion for ${VAR} patterns (keep existing loader behavior)
    def _expand_env(value):
        if isinstance(value, str):
            import re
            pattern = re.compile(r"\$\{([A-Z0-9_]+)\}")
            def repl(m):
                return os.environ.get(m.group(1), "")
            return pattern.sub(repl, value)
        if isinstance(value, dict):
            return {k: _expand_env(v) for k, v in value.items()}
        if isinstance(value, list):
            return [_expand_env(v) for v in value]
        return value

    data = _expand_env(data)

    return AgentConfig.model_validate(data)
