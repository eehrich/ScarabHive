from __future__ import annotations

import logging
import os
import re
import sys
from typing import Optional


class SafeUnicodeFormatter(logging.Formatter):
    """Formatter that safely handles Unicode characters and preserves ANSI colors on TTY."""
    
    # ANSI escape sequence pattern
    ANSI_ESCAPE = re.compile(r'\x1b\[[0-9;]*[mG]')
    
    def __init__(self, fmt=None, datefmt=None, style='%', preserve_colors=True):
        super().__init__(fmt, datefmt, style)
        self.preserve_colors = preserve_colors
    
    def format(self, record):
        # Get the formatted message
        formatted = super().format(record)
        
        # Check if we should preserve ANSI colors (TTY output)
        is_tty = hasattr(sys.stdout, 'isatty') and sys.stdout.isatty()
        if self.preserve_colors and is_tty:
            # Preserve ANSI escape sequences but still handle Unicode issues
            try:
                # Only replace problematic characters, not ANSI codes
                # Split by ANSI codes, clean each part, then rejoin
                parts = self.ANSI_ESCAPE.split(formatted)
                cleaned_parts = []
                for part in parts:
                    if part:  # Skip empty parts from split
                        try:
                            cleaned_parts.append(part.encode('utf-8', errors='replace').decode('utf-8'))
                        except Exception:
                            cleaned_parts.append(part.encode('ascii', errors='replace').decode('ascii'))
                
                # Rejoin with ANSI codes (they're preserved in the split result)
                result = formatted
                for i, part in enumerate(parts):
                    if i < len(cleaned_parts):
                        result = result.replace(part, cleaned_parts[i], 1)
                return result
            except Exception:
                pass
        
        # For file output or when not preserving colors, strip ANSI codes
        formatted = self.ANSI_ESCAPE.sub('', formatted)
        
        # Handle Unicode characters
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

    # Initialize colorama on interactive TTYs so ANSI renders on Windows
    try:
        if sys.stdout.isatty():
            import colorama
            colorama.init()
    except Exception:
        pass

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

    # File handler (truncate on each start) - strip ANSI codes for files
    file_handler = logging.FileHandler(file_path, mode="w", encoding="utf-8")
    file_handler.setLevel(lvl)
    file_formatter = SafeUnicodeFormatter("%(asctime)s %(levelname)s %(name)s %(message)s", preserve_colors=False)
    file_handler.setFormatter(file_formatter)
    root.addHandler(file_handler)

    # Console handler (level adjusted by CLI depending on --verbose) - preserve ANSI codes for TTY
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(lvl)
    console_formatter = SafeUnicodeFormatter("%(asctime)s %(levelname)s %(name)s %(message)s", preserve_colors=True)
    console_handler.setFormatter(console_formatter)
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
