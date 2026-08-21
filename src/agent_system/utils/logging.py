from __future__ import annotations

import logging
import os
import re
import sys
from typing import Optional

from concurrent_log_handler import ConcurrentRotatingFileHandler


class ColorizedFormatter(logging.Formatter):
    """A colored log formatter that mimics uvicorn's styling."""
    
    # ANSI color codes
    COLORS = {
        'TRACE': '\033[34m',     # Blue
        'DEBUG': '\033[36m',     # Cyan  
        'INFO': '\033[32m',      # Green
        'WARNING': '\033[33m',   # Yellow
        'ERROR': '\033[31m',     # Red
        'CRITICAL': '\033[91m',  # Bright Red
        'RESET': '\033[0m',      # Reset
        'BOLD': '\033[1m',       # Bold
        'DIM': '\033[2m',        # Dim
    }
    
    def __init__(self, fmt=None, datefmt=None, style='%', use_colors=None):
        super().__init__(fmt, datefmt, style)
        if use_colors is None:
            self.use_colors = sys.stdout.isatty()
        else:
            self.use_colors = use_colors
    
    def color_level_name(self, level_name: str, level_no: int) -> str:
        if not self.use_colors:
            return level_name
            
        color_map = {
            logging.DEBUG: self.COLORS['DEBUG'],
            logging.INFO: self.COLORS['INFO'], 
            logging.WARNING: self.COLORS['WARNING'],
            logging.ERROR: self.COLORS['ERROR'],
            logging.CRITICAL: self.COLORS['CRITICAL'],
        }
        
        color = color_map.get(level_no, '')
        if color:
            return f"{color}{level_name}{self.COLORS['RESET']}"
        return level_name
    
    def format(self, record):
        # Create a copy to avoid modifying the original record
        record_copy = logging.makeLogRecord(record.__dict__)
        
        # Color the level name
        if self.use_colors:
            record_copy.levelname = self.color_level_name(record.levelname, record.levelno)
            
            # Also add some subtle coloring to the logger name
            if hasattr(record_copy, 'name'):
                # Make agent_system loggers slightly dimmed
                if record_copy.name.startswith('agent_system'):
                    record_copy.name = f"{self.COLORS['DIM']}{record.name}{self.COLORS['RESET']}"
        
        return super().format(record_copy)


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


def setup_logging(
    enabled: bool, 
    level: str, 
    file_path: str,
    rotation_enabled: bool = True,
    max_bytes: int = 10485760,  # 10 MB
    backup_count: int = 5
) -> Optional[str]:
    """Configure root logging with explicit handlers.

    - File handler: always created when enabled is True, using the configured level.
      If rotation_enabled=True, uses RotatingFileHandler to rotate logs when they exceed max_bytes.
      Otherwise uses standard FileHandler that truncates on start.
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
    # Remove existing handlers to prevent duplicates or inherited settings.
    # Close only handlers that own file-like resources to avoid closing
    # shared stdio streams (sys.stdout / sys.stderr) or handlers managed by
    # external frameworks (pytest, uvicorn). Closing shared streams can
    # break capturing and other tooling.
    for h in list(root.handlers):
        try:
            root.removeHandler(h)
        except Exception:
            pass

        # Best-effort: only close handlers that are file-based or that have
        # a stream that is not the global stdout/stderr. For rotating/file
        # handlers, `baseFilename` is typically present. Otherwise, if the
        # handler exposes a `stream`, only close it when it's not stdout/stderr.
        try:
            # FileHandler and its derivatives usually have `baseFilename`
            if getattr(h, 'baseFilename', None):
                try:
                    h.close()
                except Exception:
                    pass
                continue

            stream = getattr(h, 'stream', None)
            if stream and stream not in (sys.stdout, sys.stderr):
                try:
                    h.close()
                except Exception:
                    pass
        except Exception:
            # swallow any unexpected errors during best-effort close
            pass
    # Capture everything at root; handlers will filter by their levels
    root.setLevel(logging.DEBUG)

    # File handler - strip ANSI codes for files.
    # Use RotatingFileHandler if rotation is enabled, otherwise use standard FileHandler.
    # Use a context-aware approach: create the handler and rely on the
    # atexit/handler.close() behavior, but also keep it attached to root so
    # tests that inspect root handlers see it. We ensure earlier handlers
    # were closed above to avoid duplicate open descriptors.
    if rotation_enabled:
        # Multi-process-safe rotation via portalocker file locks (cross-platform).
        # Replaces the old FailTolerantRotatingFileHandler workaround which
        # silently lost backup files when several agent-cli processes hit the
        # rotation boundary at the same time on Windows (os.remove ran before
        # the os.rename failed -> cli.log.1 deleted, swallow -> data gone).
        file_handler = ConcurrentRotatingFileHandler(
            file_path,
            maxBytes=max_bytes,
            backupCount=backup_count,
            encoding="utf-8",
            use_gzip=False,
        )
    else:
        file_handler = logging.FileHandler(file_path, mode="w", encoding="utf-8")
    
    file_handler.setLevel(lvl)
    file_formatter = SafeUnicodeFormatter("%(asctime)s %(levelname)s %(name)s %(message)s", preserve_colors=False)
    file_handler.setFormatter(file_formatter)
    root.addHandler(file_handler)

    # Console handler (level adjusted by CLI depending on --verbose) - use colored formatter for TTY
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(lvl)
    
    # Use colored formatter for console output if it's a TTY
    if sys.stdout.isatty():
        console_formatter: logging.Formatter = ColorizedFormatter("%(asctime)s %(levelname)s %(name)s %(message)s")
    else:
        console_formatter = SafeUnicodeFormatter("%(asctime)s %(levelname)s %(message)s", preserve_colors=True)
    
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

    # Special handling for uvicorn.access to prevent duplicate logs
    # Uvicorn will create its own handlers, so we disable propagation
    access_logger = logging.getLogger("uvicorn.access")
    access_logger.setLevel(lvl)
    access_logger.propagate = False  # Prevent propagation to root to avoid duplicates

    # Config errors raised before this point had nowhere to go: every entry
    # point loads the config first and configures logging afterwards. Replay
    # them now, so the logfile carries them too. Local import: config.settings
    # is a heavier module and nothing here needs it at import time.
    try:
        from ..config.settings import flush_deferred_config_errors
        flush_deferred_config_errors()
    except Exception:  # pragma: no cover - logging must never break the start
        pass

    return file_path
