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

    merged = {}
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
