from __future__ import annotations

import logging
import os
from typing import Optional


def setup_logging(enabled: bool, level: str, file_path: str) -> Optional[str]:
    if not enabled:
        return None
    os.makedirs(os.path.dirname(file_path) or ".", exist_ok=True)
    lvl = getattr(logging, level.upper(), logging.INFO)
    # Create a FileHandler that truncates the file on each start (mode='w')
    file_handler = logging.FileHandler(file_path, mode="w", encoding="utf-8")
    # StreamHandler for console output
    console_handler = logging.StreamHandler()
    logging.basicConfig(
        level=lvl,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        handlers=[file_handler, console_handler],
    )
    return file_path
