from __future__ import annotations

import logging
import os
from typing import Optional


def setup_logging(enabled: bool, level: str, file_path: str) -> Optional[str]:
    if not enabled:
        return None
    os.makedirs(os.path.dirname(file_path) or ".", exist_ok=True)
    lvl = getattr(logging, level.upper(), logging.INFO)
    logging.basicConfig(
        level=lvl,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        handlers=[logging.FileHandler(file_path, encoding="utf-8"), logging.StreamHandler()]
    )
    return file_path
