"""File operations for backlog management.

This module handles all file I/O operations including reading, writing,
backup creation, restoration, and pruning.
"""
import logging
import os
import shutil
import time
from typing import List, Optional

# Set up logger
logger = logging.getLogger(__name__)


def read_file(path: str) -> List[str]:
    """Read a file and return its lines with robust error handling.

    Args:
        path: Path to the file to read

    Returns:
        List of lines from the file

    Raises:
        FileNotFoundError: If file doesn't exist
        IOError: If reading fails
        UnicodeDecodeError: If file encoding is invalid
    """
    if not isinstance(path, str) or not path.strip():
        raise ValueError("Invalid file path provided")

    logger.debug(f"Reading file: {path}")

    try:
        with open(path, 'r', encoding='utf-8') as f:
            lines = f.readlines()

        # Ensure all lines end with newlines for consistency
        processed_lines = []
        for line in lines:
            if not line.endswith('\n'):
                line += '\n'
            processed_lines.append(line)

        logger.info(f"Successfully read {len(processed_lines)} lines from {path}")
        return processed_lines

    except FileNotFoundError:
        logger.error(f"File not found: {path}")
        raise
    except UnicodeDecodeError as e:
        logger.error(f"Encoding error reading {path}: {e}")
        raise
    except IOError as e:
        logger.error(f"IO error reading {path}: {e}")
        raise
    except Exception as e:
        logger.error(f"Unexpected error reading {path}: {e}")
        raise IOError(f"Failed to read file {path}: {e}") from e


def safe_write(path: str, text: str) -> None:
    """Atomically write text to file with backup and error handling.

    Args:
        path: File path to write to
        text: Content to write

    Raises:
        IOError: If writing fails
        OSError: If file system operations fail
    """
    if not isinstance(path, str) or not path.strip():
        raise ValueError("Invalid file path provided")

    if not isinstance(text, str):
        raise ValueError("Text content must be a string")

    tmp = path + ".tmp"
    logger.debug(f"Starting atomic write to {path}")

    try:
        # Ensure the directory exists (only if path contains directories)
        dir_path = os.path.dirname(path)
        if dir_path and dir_path != '.':
            os.makedirs(dir_path, exist_ok=True)

        # Write to temporary file first
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(text)

        # Atomic replace
        os.replace(tmp, path)
        logger.info(f"Successfully wrote {len(text)} characters to {path}")

    except Exception as e:
        # Clean up temp file if it exists
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except Exception:
            pass  # Ignore cleanup errors

        logger.error(f"Failed to write to {path}: {e}")
        raise IOError(f"Failed to write file {path}: {e}") from e


def make_backup(path: str) -> str:
    """Create a timestamped backup of `path` under a .backups directory.

    Args:
        path: File path to backup

    Returns:
        Path to the created backup file

    Raises:
        IOError: If backup creation fails
        FileNotFoundError: If source file doesn't exist
    """
    if not isinstance(path, str) or not path.strip():
        raise ValueError("Invalid file path provided")

    if not os.path.exists(path):
        raise FileNotFoundError(f"Source file does not exist: {path}")

    try:
        p = os.path.abspath(path)
        d = os.path.dirname(p)
        backups_dir = os.path.join(d, '.backups')
        os.makedirs(backups_dir, exist_ok=True)

        ts = time.strftime('%Y%m%d_%H%M%S')
        base = os.path.basename(p)
        bak = f"{base}.{ts}.bak"
        dest = os.path.join(backups_dir, bak)

        shutil.copy2(p, dest)
        logger.info(f"Created backup: {dest}")
        return dest

    except Exception as e:
        logger.error(f"Failed to create backup for {path}: {e}")
        raise IOError(f"Failed to create backup: {e}") from e


def list_backups(path: str) -> list[str]:
    """List all backup files for the given path.

    Args:
        path: File path to list backups for

    Returns:
        List of backup file paths, sorted by modification time (oldest first)
    """
    p = os.path.abspath(path)
    d = os.path.dirname(p)
    backups_dir = os.path.join(d, '.backups')
    if not os.path.isdir(backups_dir):
        return []
    files = [os.path.join(backups_dir, f) for f in os.listdir(backups_dir)
             if f.startswith(os.path.basename(p) + '.') and f.endswith('.bak')]
    # sort by modification time ascending (oldest first)
    files.sort(key=lambda f: os.path.getmtime(f))
    return files


def restore_backup(path: str, backup_path: str) -> None:
    """Restore backup_path over path atomically.

    Args:
        path: Target file path to restore to
        backup_path: Path to the backup file to restore from

    Raises:
        FileNotFoundError: If backup file doesn't exist
        IOError: If restoration fails
    """
    if not os.path.exists(backup_path):
        raise FileNotFoundError(backup_path)
    # atomic replace
    tmp = path + '.restore.tmp'
    shutil.copy2(backup_path, tmp)
    os.replace(tmp, path)


def prune_backups(path: str, keep: Optional[int] = None, older_than_days: Optional[int] = None) -> List[str]:
    """Prune backups for `path` by keeping the newest `keep` files and/or removing files older than `older_than_days`.

    Args:
        path: File path to prune backups for
        keep: Number of newest backups to keep (optional)
        older_than_days: Remove backups older than this many days (optional)

    Returns:
        List of removed file paths
    """
    files = list_backups(path)
    if not files:
        return []

    # build list of candidates with mtimes
    files_with_mtime = [(f, os.path.getmtime(f)) for f in files]
    # newest first
    files_with_mtime.sort(key=lambda t: t[1], reverse=True)

    to_remove = set()
    if keep is not None and keep >= 0:
        # keep newest `keep` files
        for f, _ in files_with_mtime[keep:]:
            to_remove.add(f)

    if older_than_days is not None and older_than_days > 0:
        cutoff = time.time() - (older_than_days * 86400)
        for f, m in files_with_mtime:
            if m < cutoff:
                to_remove.add(f)

    # If no criteria given, default to keep 10
    if keep is None and older_than_days is None:
        default_keep = 10
        for f, _ in files_with_mtime[default_keep:]:
            to_remove.add(f)

    removed = []
    for f in sorted(to_remove):
        try:
            os.remove(f)
            removed.append(f)
        except Exception:
            # on failure, skip and continue
            continue
    return removed
