"""Path validation and security for file operations.

Prevents path traversal attacks, validates paths within allowed directories,
and safely handles symlinks.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import List


logger = logging.getLogger(__name__)


class SecurityError(Exception):
    """Raised when a security violation is detected."""
    pass


class PathValidator:
    """Validates and sanitizes file paths for secure file operations."""
    
    def __init__(self, allowed_dirs: List[str]):
        """
        Initialize path validator with allowed directories.
        
        Args:
            allowed_dirs: List of directory paths that are allowed for file operations.
                          All paths are resolved to absolute paths.
        """
        self.allowed_dirs = [Path(d).resolve() for d in allowed_dirs]
        logger.info(f"PathValidator initialized with {len(self.allowed_dirs)} allowed directories")
        for d in self.allowed_dirs:
            logger.debug(f"Allowed directory: {d}")
    
    def validate_path(self, path: str, must_exist: bool = False) -> Path:
        """
        Validate that a path is safe and within allowed directories.
        
        Args:
            path: Path to validate (can be absolute or relative)
            must_exist: If True, raise error if path does not exist
        
        Returns:
            Resolved absolute Path object
        
        Raises:
            SecurityError: If path is unsafe or outside allowed directories
            FileNotFoundError: If must_exist=True and path does not exist
        """
        # Check for null bytes
        if '\x00' in path:
            raise SecurityError("Path contains null byte")
        
        # Convert to Path object
        p = Path(path)
        
        # Check for dangerous patterns in parts
        if ".." in p.parts:
            raise SecurityError("Path contains '..' traversal pattern")
        
        # Check for tilde expansion
        if "~" in str(p):
            raise SecurityError("Path contains '~' home directory pattern")
        
        # Resolve to absolute path (follows symlinks)
        try:
            abs_path = p.resolve(strict=must_exist)
        except OSError as e:
            if must_exist:
                raise FileNotFoundError(f"Path does not exist: {path}") from e
            # If not strict, resolve without following final component
            abs_path = p.absolute().resolve()
        
        # Check if within allowed directories
        is_allowed = False
        for allowed_dir in self.allowed_dirs:
            try:
                # Check if abs_path is relative to allowed_dir
                abs_path.relative_to(allowed_dir)
                is_allowed = True
                break
            except ValueError:
                # Not relative to this allowed_dir, try next
                continue
        
        if not is_allowed:
            raise SecurityError(
                f"Path outside allowed directories: {abs_path}\n"
                f"Allowed directories: {[str(d) for d in self.allowed_dirs]}"
            )
        
        # Additional symlink check: if path is a symlink, validate target
        if abs_path.is_symlink():
            target = abs_path.readlink()
            # Recursively validate symlink target
            if not target.is_absolute():
                target = abs_path.parent / target
            return self.validate_path(str(target), must_exist=must_exist)
        
        logger.debug(f"Path validated: {path} -> {abs_path}")
        return abs_path
    
    def is_allowed_directory(self, path: Path) -> bool:
        """Check if a path is one of the allowed directories."""
        abs_path = path.resolve()
        return abs_path in self.allowed_dirs
    
    def get_relative_path(self, path: Path) -> str:
        """Get path relative to closest allowed directory."""
        abs_path = path.resolve()
        
        for allowed_dir in self.allowed_dirs:
            try:
                rel_path = abs_path.relative_to(allowed_dir)
                return str(rel_path)
            except ValueError:
                continue
        
        # Fallback to absolute path
        return str(abs_path)
