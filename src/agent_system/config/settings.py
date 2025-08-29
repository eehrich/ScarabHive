"""
Settings loader using pydantic BaseSettings semantics.

This provides a single, typed configuration entrypoint that prefers
environment variables and falls back to YAML file values. It keeps
behavior explicit and testable.
"""
from __future__ import annotations

from typing import Optional, Any
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
    data: dict = {}

    # If the master config (manifest) exists, load it and then load any
    # included files listed under `includes` or `files`. The master config
    # acts as a manifest and should not be overwritten by CLI actions; only
    # the included files (for example `mcp.yaml`) will be written by the CLI.
    if cfg_path.exists():
        master = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
        # Determine includes: accept either `includes` (list) or `files`
        includes = master.get("includes") or master.get("files") or []
        # If includes is a single string, make it a list
        if isinstance(includes, str):
            includes = [includes]

        # Start with the master config as base
        data = dict(master)

        # Load each included file (relative paths are resolved against master)
        merged: dict[str, Any] = {}
        for inc in includes:
            inc_path = Path(inc)
            if not inc_path.is_absolute():
                inc_path = cfg_path.parent.joinpath(inc_path)
            if inc_path.exists():
                try:
                    part = yaml.safe_load(inc_path.read_text(encoding="utf-8")) or {}
                    # shallow merge: later included files override previous keys
                    merged.update(part)
                except Exception:
                    # ignore parse errors for now and continue
                    pass

        # Merge master (manifest) with included content, giving included files precedence
        merged_final = dict(data)
        merged_final.update(merged)
        data = merged_final

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

    # Resolve any `mcp.plugin_dirs` entries. We attempt to preserve user
    # intent when paths are relative: first resolve relative to the
    # configuration file directory (so includes relative to the managed file
    # keep local paths), but if that resolution yields a non-existing path and
    # the same relative path exists relative to the repository root (parent of
    # the config directory), prefer that. This allows common entries like
    # `src/plugins` in `mcp.yaml` to refer to the repository `src` tree while
    # keeping resolution predictable.
    if cfg_path.exists():
        base_dir = cfg_path.parent
        # repository root is assumed to be parent of the config dir when
        # config lives in a `config/` subdirectory; fall back to base_dir if
        # the parent is not meaningful.
        repo_root = cfg_path.parent.parent if cfg_path.parent.parent.exists() else base_dir
        try:
            mcp_block = data.get("mcp") if isinstance(data, dict) else None
            if isinstance(mcp_block, dict):
                pdirs = mcp_block.get("plugin_dirs")
                if isinstance(pdirs, list):
                    resolved = []
                    for p in pdirs:
                        if isinstance(p, str) and p:
                            ppath = Path(p)
                            if not ppath.is_absolute():
                                # Try config-folder-relative first
                                try:
                                    candidate = (base_dir.joinpath(ppath)).resolve()
                                except Exception:
                                    candidate = base_dir.joinpath(ppath)
                                # If that candidate doesn't exist but an equivalent
                                # path exists relative to the repo root, prefer the
                                # repo-root-relative path (handles `src/...`).
                                if not candidate.exists():
                                    try:
                                        repo_candidate = (repo_root.joinpath(ppath)).resolve()
                                    except Exception:
                                        repo_candidate = repo_root.joinpath(ppath)
                                    if repo_candidate.exists():
                                        p = str(repo_candidate)
                                    else:
                                        p = str(candidate)
                                else:
                                    p = str(candidate)
                        resolved.append(p)
                    data["mcp"]["plugin_dirs"] = resolved
        except Exception:
            # Conservative: if resolution fails for any reason, keep original values
            pass

    return AgentConfig.model_validate(data)
