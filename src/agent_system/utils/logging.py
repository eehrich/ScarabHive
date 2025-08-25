from __future__ import annotations

import logging
import os
from typing import Optional


def setup_logging(enabled: bool, level: str, file_path: str) -> Optional[str]:
    """Configure root logging with explicit handlers.

    - File handler: always created when enabled is True, using the configured level, truncating on start.
    - Console handler: attached as well; CLI may adjust its level later (e.g., to WARNING when not verbose).

    Avoid logging.basicConfig to ensure we override any prior handlers reliably.
    """
    if not enabled:
        return None

    os.makedirs(os.path.dirname(file_path) or ".", exist_ok=True)
    lvl = getattr(logging, level.upper(), logging.INFO)

    root = logging.getLogger()
    # Remove existing handlers to prevent duplicates or inherited settings
    for h in list(root.handlers):
        try:
            root.removeHandler(h)
        except Exception:
            pass
    # Capture everything at root; handlers will filter by their levels
    root.setLevel(logging.DEBUG)

    # File handler (truncate on each start)
    file_handler = logging.FileHandler(file_path, mode="w", encoding="utf-8")
    file_handler.setLevel(lvl)
    formatter = logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s")
    file_handler.setFormatter(formatter)
    root.addHandler(file_handler)

    # Console handler (level adjusted by CLI depending on --verbose)
    console_handler = logging.StreamHandler()
    console_handler.setLevel(lvl)
    console_handler.setFormatter(formatter)
    root.addHandler(console_handler)

    return file_path
