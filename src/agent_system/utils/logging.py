from __future__ import annotations

import logging
import os
import re
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Optional

from concurrent_log_handler import ConcurrentRotatingFileHandler

# A plugin route whose URL carries a key -- whoever holds the URL may use it once (stategraph's callback URLs) --
# puts it right after /callback/ or in the token parameter of /callback?...; no log keeps it, percent-encoded
# neither (a URL handed on in a query; a path whose ? & = came encoded). The parameters before the token hold no
# / and no ?: each match ends at the next one, so a line full of callback URLs costs linear time, not quadratic.
_KEY_IN_PATH = re.compile(
    r"(/plugins/[^/\s?#]+/callback(?:/|/?(?:\?|%3F)(?:(?:(?!%26)[^/\s?#&\"'])*(?:&|%26))*token(?:=|%3D)))"
    r"(?:(?!%26)[^/\s?#&\"'])+"
    r"|(%2Fplugins%2F(?:(?!%2F)[^/\s?#&])+%2Fcallback(?:%2F|%3F(?:(?:(?!%26|%2F|%3F)[^/\s?#&\"'])*%26)*token%3D))"
    r"(?:(?!%2F|%26)[^/\s?#&\"'])+",
    re.IGNORECASE,
)


def loggable_path(text: str) -> str:
    """``text`` with the key of every callback URL in it masked (``/plugins/<plugin>/callback/***``,
    ``/plugins/<plugin>/callback?token=***``)."""
    return _KEY_IN_PATH.sub(lambda found: (found.group(1) or found.group(2)) + "***", text)


def _may_hold_a_key(value: Any) -> bool:
    text = str(value).lower()
    return any(mark in text for mark in ("/callback/", "/callback?", "/callback%3f", "%2fcallback%2f",
                                         "%2fcallback%3f"))


def _masked(value: Any) -> Any:
    # numbers stay numbers (%d); anything else that holds a key becomes its masked text
    try:
        if isinstance(value, (int, float)) or not _may_hold_a_key(value):
            return value
        return loggable_path(str(value))
    except Exception:  # noqa: BLE001 -- no text to show: the formatter fails on it the same way and reports it
        return value


class KeyInPathFilter(logging.Filter):
    """Masks callback keys in every record a handler writes: the access log, security.log, the app log.

    Each argument on its own: a formatter that takes the arguments apart (uvicorn's access log, five of them) gets
    them all. Message and arguments are merged only where the key stands in the message itself. A record without
    a key keeps its arguments as they were -- the very objects.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        if getattr(record, "_keys_masked", False):  # one record, several handlers: once is enough
            return True
        record._keys_masked = True
        try:
            args = record.args
            if isinstance(args, Mapping):  # logging's single-mapping form (any Mapping, not only a dict)
                masked = {name: _masked(value) for name, value in args.items()}
                if any(masked[name] is not args[name] for name in masked):
                    record.args = masked
            elif isinstance(args, tuple) and args:
                masked = tuple(_masked(value) for value in args)
                if any(new is not old for new, old in zip(masked, args)):
                    record.args = masked
            if _may_hold_a_key(record.msg):
                try:
                    merged = record.getMessage() if record.args else str(record.msg)
                    record.msg, record.args = loggable_path(merged), ()
                except Exception:  # noqa: BLE001 -- a broken call: its message masked, the handler reports it
                    record.msg = loggable_path(str(record.msg))
            if record.exc_info and not record.exc_text:  # the traceback as the formatter would cache it
                record.exc_text = logging.Formatter().formatException(record.exc_info)
            if record.exc_text and _may_hold_a_key(record.exc_text):
                record.exc_text = loggable_path(record.exc_text)
        except Exception:  # noqa: BLE001 -- a broken call is the handler's to report, not the filter's to raise
            pass
        return True


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
    # On the server's own loggers: started as `uvicorn ... --factory`, uvicorn writes the access log (and a refused
    # WebSocket's path) through handlers of its own, which the root's never see -- also with logging switched off.
    for name in ("uvicorn.access", "uvicorn.error"):
        server_logger = logging.getLogger(name)
        if not any(isinstance(f, KeyInPathFilter) for f in server_logger.filters):
            server_logger.addFilter(KeyInPathFilter())
    if not enabled:
        # what an earlier basicConfig left writes on; without any, logging's last resort (WARNING+ to stderr)
        if not any(isinstance(f, KeyInPathFilter) for f in logging.lastResort.filters):
            logging.lastResort.addFilter(KeyInPathFilter())
        for handler in logging.getLogger().handlers:
            if not any(isinstance(f, KeyInPathFilter) for f in handler.filters):
                handler.addFilter(KeyInPathFilter())
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
    file_handler.addFilter(KeyInPathFilter())
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
    console_handler.addFilter(KeyInPathFilter())
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


def setup_role_logging(logging_config: Any, role: str) -> Optional[str]:
    """setup_logging for one process role ("api", "cli") from the logging config.

    The file is the role's own ``file_<role>`` setting, else the shared
    ``file`` with the role in its name -- logs/agent.log becomes
    logs/agent-cli.log -- so the API and a CLI running next to it do not
    write into one file.
    """
    path = getattr(logging_config, f"file_{role}", None)
    if not path:
        base = Path(logging_config.file or "logs/agent.log")
        path = str(base.with_name(f"{base.stem or 'agent'}-{role}{''.join(base.suffixes) or '.log'}"))
    return setup_logging(logging_config.enabled, logging_config.level, path,
                         rotation_enabled=logging_config.rotation_enabled,
                         max_bytes=logging_config.max_bytes,
                         backup_count=logging_config.backup_count)
