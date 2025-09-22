"""CLI utility functions and helpers."""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

# Global color mode: tests may monkeypatch this variable
color_mode: str = "auto"


def _atomic_write_text(path: Path, data: str) -> None:
    """Atomically write text to `path` by writing to a temp file in the
    same directory and renaming it into place. Ensures durable write where
    possible by flushing and syncing file content and directory."""
    dirpath = path.parent
    fd, tmp = tempfile.mkstemp(dir=dirpath)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(data)
            fh.flush()
            try:
                os.fsync(fh.fileno())
            except Exception:
                # Some platforms or filesystems may not support fsync; ignore
                pass
        try:
            os.replace(tmp, str(path))
        except Exception:
            # On some systems os.replace requires str paths
            os.replace(tmp, path)
        # Attempt to sync directory metadata
        try:
            # os.O_DIRECTORY is not available on all platforms (notably
            # Windows). Only attempt to open and fsync the directory when
            # the flag exists; otherwise skip directory fsync.
            if hasattr(os, "O_DIRECTORY"):
                dirfd = os.open(dirpath, os.O_DIRECTORY)
                try:
                    os.fsync(dirfd)
                finally:
                    os.close(dirfd)
        except Exception:
            pass
    finally:
        # Clean up tmp if still exists
        try:
            if os.path.exists(tmp):
                os.unlink(tmp)
        except Exception:
            pass


def _supports_color() -> bool:
    """Return whether ANSI color sequences should be used.

    Honors the global `color_mode` which tests may set to 'auto',
    'always' or 'never'. In 'auto' mode this checks stdout.isatty().
    """
    if color_mode == "never":
        return False
    if color_mode == "always":
        return True
    # auto
    try:
        return sys.stdout.isatty()
    except Exception:
        return False


def _colorize(text: str, color_code: str) -> str:
    """Wrap text in ANSI color codes when supported."""
    if not _supports_color():
        return text
    return f"\x1b[{color_code}m{text}\x1b[0m"