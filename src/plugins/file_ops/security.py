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
import os
import re
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


#: Git Bash writes a Windows drive as its letter: `/e/Projects/x` is E:\Projects\x.
_GIT_BASH_DRIVE = re.compile(r"^/([a-zA-Z])(/.*)?$")


def from_git_bash(path: str) -> str:
    """On Windows, `/e/...` as the drive path it stands for; any other path as given.

    The coder's shell is Git Bash: every `pwd` and error message hands the model
    paths of that form, and the sandbox read them as a folder `e` on the root
    of the current drive -- outside every allowed directory.
    """
    if os.name != "nt" or not isinstance(path, str):
        return path
    match = _GIT_BASH_DRIVE.match(path)
    if match is None:
        return path
    return f"{match.group(1).upper()}:{match.group(2) or '/'}"


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
        path = from_git_bash(path)
        try:
            resolved = self._sandbox.resolve(path)
        except PathSandboxDenied as exc:
            raise SecurityError(str(exc)) from exc

        if must_exist and not resolved.exists():
            raise FileNotFoundError(f"Path does not exist: {path}")

        logger.debug("Path validated: %s -> %s", path, resolved)
        return resolved

    def validate_entry(self, path: str) -> Path:
        """The entry ``path`` names -- the link itself when it is a link.

        For delete, move and rename. ``validate_path`` resolves, so a link
        stood for its target: deleting ``link.txt`` deleted the file it points
        to, a recursive delete of a directory link emptied the directory it
        points to, and a move or rename moved the target and left the link
        dangling. A link is judged by the folder it lies in, which must be
        inside; what it points to is not touched. Anything else is
        ``validate_path(path, must_exist=True)``, as before.
        """
        path = from_git_bash(path)
        named = Path(path)
        if named.name not in ("", ".", ".."):
            try:
                folder = self.validate_path(str(named.parent))
            except SecurityError:
                folder = None  # judged below, like any other path
            if folder is not None:
                entry = folder / named.name
                if entry.is_symlink() or entry.is_junction():
                    return entry
        return self.validate_path(path, must_exist=True)

    def is_allowed_directory(self, path: Path) -> bool:
        """Check if a path is one of the allowed directories itself."""
        return path.resolve() in self._sandbox.roots

    def get_relative_path(self, path: Path) -> str:
        """Get path relative to closest allowed directory."""
        return self._sandbox.relative(path.resolve())
