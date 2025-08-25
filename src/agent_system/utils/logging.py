from __future__ import annotations

import logging
import os
import sys
from typing import Optional


class SafeUnicodeFormatter(logging.Formatter):
    """Formatter that safely handles Unicode characters by replacing problematic ones."""
    
    def format(self, record):
        # Get the formatted message
        formatted = super().format(record)
        # Replace problematic Unicode characters with safe alternatives
        try:
            # Try to encode with the target encoding and replace errors
            formatted = formatted.encode('utf-8', errors='replace').decode('utf-8')
        except Exception:
            # Fallback: replace any non-ASCII characters
            formatted = formatted.encode('ascii', errors='replace').decode('ascii')
        return formatted


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
    formatter = SafeUnicodeFormatter("%(asctime)s %(levelname)s %(name)s %(message)s")
    file_handler.setFormatter(formatter)
    root.addHandler(file_handler)

    # Console handler (level adjusted by CLI depending on --verbose)
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(lvl)
    console_handler.setFormatter(formatter)
    # Set encoding to handle Unicode characters properly
    if hasattr(console_handler.stream, 'reconfigure'):
        try:
            console_handler.stream.reconfigure(encoding='utf-8', errors='replace')
        except Exception:
            pass
    root.addHandler(console_handler)

    # Configure specific loggers to inherit from root but with appropriate levels
    loggers_to_configure = [
        "uvicorn",
        "uvicorn.access", 
        "uvicorn.error",
        "httpcore",
        "httpx",
        "asyncio"
    ]
    
    for logger_name in loggers_to_configure:
        logger = logging.getLogger(logger_name)
        logger.setLevel(lvl)
        # Don't add handlers, they will inherit from root
        logger.propagate = True

    return file_path
