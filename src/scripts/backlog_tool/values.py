from __future__ import annotations

from pathlib import Path
import os
import yaml

_config: dict | None = None


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
            '❌': 'failed',
            '⏳': 'in progress',
        },
        'word_map': {
            'done': 'done', 'implemented': 'done', 'finished': 'done', 'resolved': 'done', 'closed': 'done', 'completed': 'done',
            'open': 'open', 'in progress': 'in progress', 'started': 'in progress',
            'failed': 'failed', 'reverted': 'reverted', 'rejected': 'rejected',
            'cancelled': 'cancelled', 'canceled': 'cancelled',
        },
        'acceptable_terminal': ['done', 'reverted', 'rejected', 'cancelled', 'implemented', 'fixed'],
    }


def load() -> dict:
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


def get(key: str, default=None):
    cfg = load()
    return cfg.get(key, default)
