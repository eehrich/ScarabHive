"""Tests for multi-process-safe log rotation.

Background: with parallel agent-cli instances on Windows,
``os.rename(source, dest)`` fails during log rotation with
``PermissionError [WinError 32]`` because other processes hold the file
open. The earlier `FailTolerantRotatingFileHandler` solution swallowed
only the last rename, but had already deleted backup files via
`os.remove` beforehand -- so cli.log.1 vanished irrecoverably.

Current solution: `concurrent_log_handler.ConcurrentRotatingFileHandler`
uses file locks (portalocker, cross-platform) and serializes
rotations cleanly between processes.
"""
from __future__ import annotations

import logging
from pathlib import Path

from agent_system.utils.logging import setup_logging
from concurrent_log_handler import ConcurrentRotatingFileHandler


def test_rotation_enabled_uses_concurrent_handler(tmp_path: Path) -> None:
    """`setup_logging(rotation_enabled=True)` must attach a
    `ConcurrentRotatingFileHandler` to the root logger.
    """
    log_file = tmp_path / "rot.log"
    try:
        setup_logging(
            enabled=True,
            level="INFO",
            file_path=str(log_file),
            rotation_enabled=True,
            max_bytes=100,
            backup_count=2,
        )
        root = logging.getLogger()
        concurrent_handlers = [
            h for h in root.handlers if isinstance(h, ConcurrentRotatingFileHandler)
        ]
        assert len(concurrent_handlers) == 1
        h = concurrent_handlers[0]
        assert h.maxBytes == 100
        assert h.backupCount == 2
    finally:
        # Close cleanly -- otherwise the lock-file handle keeps the file open.
        for h in list(logging.getLogger().handlers):
            try:
                h.close()
            except Exception:
                pass


def test_rotation_actually_rotates_on_size_limit(tmp_path: Path) -> None:
    """Rotation really happens once maxBytes is exceeded."""
    log_file = tmp_path / "spam.log"
    try:
        setup_logging(
            enabled=True,
            level="INFO",
            file_path=str(log_file),
            rotation_enabled=True,
            max_bytes=200,
            backup_count=2,
        )
        logger = logging.getLogger("rotation_smoke")
        # write enough to trigger rotation
        for i in range(50):
            logger.info("line %d %s", i, "x" * 80)
        # Force handlers to flush
        for h in logging.getLogger().handlers:
            try:
                h.flush()
            except Exception:
                pass
        # At least one backup must exist
        backups = sorted(tmp_path.glob("spam.log.*"))
        assert backups, "Expected at least one rotated backup file"
    finally:
        for h in list(logging.getLogger().handlers):
            try:
                h.close()
            except Exception:
                pass
