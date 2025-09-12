from __future__ import annotations

from pathlib import Path
import os
from typing import Any, Dict, Optional

try:
    import yaml
except Exception:
    # PyYAML may not be installed in every test or runtime environment.
    yaml = None  # type: ignore[assignment]


# Module-level cached config
_config: Optional[Dict[str, Any]] = None


def _default_config() -> dict:
    return {
        'allowed_statuses': [
            'open', 'done', 'closed', 'complete', 'finished', 'resolved',
            'in progress', 'todo', 'reverted', 'rejected', 'cancelled', 'failed',
        ],
        'finish_statuses': ['done', 'closed', 'complete', 'finished', 'implemented', 'fixed'],
        'symbol_map': {
            '☐': 'open',
            '✅': 'done',
            '❌': ['failed', 'cancelled', 'rejected', 'reverted'],
            '⏳': 'in progress',
        },
        'word_map': {
            'done': 'done', 'implemented': 'done', 'finished': 'done', 'resolved': 'done', 'closed': 'done', 'completed': 'done',
            'open': 'open', 'in progress': 'in progress', 'started': 'in progress',
            'failed': 'failed', 'reverted': 'reverted', 'rejected': 'rejected',
            'cancelled': 'cancelled', 'canceled': 'cancelled',
        },
        'acceptable_terminal': ['done', 'reverted', 'rejected', 'cancelled', 'implemented', 'fixed', 'failed'],
    }


def load() -> Dict[str, Any]:
    """Load backlog value config from `config/backlog_values.yaml` if present.

    The location can be overridden by the environment variable `BACKLOG_VALUES`.
    If the file is missing or invalid, sensible defaults are returned.
    """
    global _config
    if _config is not None:
        return _config

    defaults = _default_config()

    # repo root is three parents up from this file: src/scripts/backlog_tool
    repo_root = Path(__file__).resolve().parents[3]
    cfg_path = Path(os.environ.get('BACKLOG_VALUES', repo_root / 'config' / 'backlog_values.yaml'))
    if not cfg_path.exists():
        _config = defaults
        return _config

    # Check if PyYAML import failed - this is reachable despite mypy's static analysis
    if yaml is None:
        # PyYAML not available in this environment; skip loading file
        _config = defaults
        return _config
    try:
        with open(cfg_path, 'r', encoding='utf-8') as fh:
            data = yaml.safe_load(fh) or {}
            cfg = defaults.copy()
            # shallow merge; lists/dicts from file replace defaults
            for k, v in data.items():
                cfg[k] = v
            _config = cfg
            return _config
    except Exception:
        _config = defaults
        return _config


def get(key: str, default: Any = None) -> Any:
    cfg = load()
    return cfg.get(key, default)
