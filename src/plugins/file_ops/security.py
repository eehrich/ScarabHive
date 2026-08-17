"""Path validation for file operations.

The containment rules themselves live in
:mod:`agent_system.utils.path_sandbox` — one resolution point shared with the
other plugins that take a path from LLM arguments. This module keeps the
plugin-facing shape: ``SecurityError``, ``validate_path(path, must_exist)``,
and the ``must_exist`` contract that turns a missing file into
``FileNotFoundError`` rather than a security refusal.

Two lexical rules were dropped when the containment moved: paths containing
``..`` or ``~`` are no longer rejected on sight. Both are ineffective AFTER
canonicalization — ``a/../../etc/passwd`` resolves outside and is refused by
containment, ``a/../a/x`` resolves inside and IS the file that was named — so
the extra rules only rejected harmless paths. The escape tests keep passing
because they escape; see ``path_sandbox`` for why canonicalization must come
before any lexical judgement.

**Known gap — read_only lives in two places.** ``PathSandbox`` carries a
``read_only`` mode, but this plugin does NOT pass it: the server keeps
enforcing its own flag per operation (``server.py``). That is deliberate for
now. The server's check also covers operations that never resolve a path
through here, and routing it into the sandbox would mean deciding read-vs-write
at each of the eleven ``validate_path`` call sites — a bigger change than
moving the boundary, and one that belongs with the terminal work rather than
smuggled in beside it. Until then: the VOCABULARY is shared, the enforcement
for this plugin is not.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import List

from agent_system.utils.path_sandbox import PathSandbox, PathSandboxDenied

logger = logging.getLogger(__name__)


class SecurityError(PathSandboxDenied):
    """Raised when a security violation is detected.

    Subclass rather than alias: every catch site in this plugin reports
    ``error_type: "SecurityError"`` to the model, and that string is part of
    the model-facing contract.
    """


class PathValidator:
    """Validates and sanitizes file paths for secure file operations."""

    def __init__(self, allowed_dirs: List[str], base: Path | str | None = None):
        """
        Args:
            allowed_dirs: directories allowed for file operations. Relative
                entries resolve against ``base``.
            base: what a relative path counts against. Defaults to the current
                working directory, which is what this plugin used before the
                shared sandbox existed.
        """
        self._sandbox = PathSandbox.from_config(
            allowed_dirs, base=base if base is not None else Path.cwd())
        logger.info(
            "PathValidator initialized with %d allowed directories",
            len(self._sandbox.roots))

    @property
    def allowed_dirs(self) -> List[Path]:
        """The resolved allow-list (kept for callers and tests that read it)."""
        return list(self._sandbox.roots)

    def validate_path(self, path: str, must_exist: bool = False) -> Path:
        """
        Validate that a path is safe and within allowed directories.

        Returns:
            Resolved absolute Path object

        Raises:
            SecurityError: path is unsafe or outside allowed directories
            FileNotFoundError: ``must_exist`` and the path does not exist
        """
        try:
            resolved = self._sandbox.resolve(path)
        except PathSandboxDenied as exc:
            raise SecurityError(str(exc)) from exc

        if must_exist and not resolved.exists():
            raise FileNotFoundError(f"Path does not exist: {path}")

        logger.debug("Path validated: %s -> %s", path, resolved)
        return resolved

    def is_allowed_directory(self, path: Path) -> bool:
        """Check if a path is one of the allowed directories itself."""
        return path.resolve() in self._sandbox.roots

    def get_relative_path(self, path: Path) -> str:
        """Get path relative to closest allowed directory."""
        return self._sandbox.relative(path.resolve())
