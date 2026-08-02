"""Discovery and lookup of skills.

A skill directory looks like::

    skills/
    └── my-skill/
        ├── skill.toml      # manifest, mirrors plugin.toml
        └── SKILL.md        # the knowledge (markdown, Jinja2-rendered)

Deliberately dependency-light: stdlib ``tomllib`` only, same as
``plugins.plugin_manifest``.
"""
from __future__ import annotations

import logging
import os
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Sequence

try:  # pragma: no cover - trivial import guard
    import tomllib  # Python >=3.11 stdlib
except ModuleNotFoundError:  # pragma: no cover
    tomllib = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)

#: Default roots scanned for skills, relative to the working directory.
DEFAULT_SKILL_DIRS: tuple[str, ...] = ("skills",)

#: Operator override, os.pathsep-separated (e.g. "skills:/opt/team-skills").
#: Keeps skill locations configurable without a config-schema change.
SKILL_DIRS_ENV = "AGENT_SKILL_DIRS"


def default_skill_dirs() -> tuple[str, ...]:
    """Roots to scan: ``$AGENT_SKILL_DIRS`` if set, else :data:`DEFAULT_SKILL_DIRS`."""
    raw = os.environ.get(SKILL_DIRS_ENV, "").strip()
    if not raw:
        return DEFAULT_SKILL_DIRS
    return tuple(p for p in (part.strip() for part in raw.split(os.pathsep)) if p)

MANIFEST_NAME = "skill.toml"
DEFAULT_ENTRY = "SKILL.md"


@dataclass(frozen=True)
class Skill:
    """One discovered skill."""

    name: str
    path: Path          # the skill directory
    entry_path: Path    # absolute path to the markdown body
    version: str = "0.0.0"
    description: str = ""
    tags: tuple[str, ...] = ()

    @property
    def entry(self) -> str:
        """Body path as a string (what the prompt renderer wants)."""
        return str(self.entry_path)

    def resolve(self, relative_path: str) -> Path:
        """Absolute path of a bundled file, confined to the skill directory.

        A skill is a BUNDLE: SKILL.md is the index, and files next to it
        (``reference/…``) carry the depth an agent loads only when it needs it.
        Reading those is what makes a skill a knowledge base rather than a
        prompt block — so the path handling here is the security boundary.

        Raises:
            ValueError: on an absolute path or one escaping the skill dir.
            FileNotFoundError: if no such file exists in the bundle.
        """
        candidate = Path(relative_path)
        if candidate.is_absolute():
            raise ValueError(f"'{relative_path}' must be relative to the skill directory")

        root = self.path.resolve()
        target = (root / candidate).resolve()
        # is_relative_to also rejects symlinks pointing outside the bundle,
        # because both sides are fully resolved first.
        if not target.is_relative_to(root):
            raise ValueError(f"'{relative_path}' escapes skill '{self.name}'")
        if not target.is_file():
            raise FileNotFoundError(f"'{relative_path}' not found in skill '{self.name}'")
        return target

    def list_files(self) -> List[str]:
        """Bundled files as skill-relative POSIX paths, sorted.

        The manifest itself is omitted — it is plumbing, not knowledge.
        """
        root = self.path.resolve()
        out = []
        for p in root.rglob("*"):
            if not p.is_file() or p.name == MANIFEST_NAME:
                continue
            try:
                out.append(p.resolve().relative_to(root).as_posix())
            except ValueError:  # pragma: no cover - symlink escaping the bundle
                continue
        # Sort the resulting strings, not the Path objects: Path ordering is
        # case-insensitive on Windows and case-sensitive elsewhere, which would
        # make this list platform-dependent.
        return sorted(out)


def _parse_manifest(skill_dir: Path) -> Optional[Skill]:
    """Build a Skill from ``<skill_dir>/skill.toml``; None if unusable.

    A malformed skill is skipped with a warning rather than raising: one broken
    directory must not stop the whole system from starting.
    """
    manifest = skill_dir / MANIFEST_NAME
    if not manifest.is_file():
        return None
    if tomllib is None:  # pragma: no cover - Python <3.11
        logger.warning("Cannot read %s: tomllib unavailable (needs Python 3.11+)", manifest)
        return None

    try:
        with open(manifest, "rb") as fh:
            data = tomllib.load(fh) or {}
    except Exception as e:  # noqa: BLE001 - one bad manifest must not break startup
        logger.warning("Skipping skill at %s: unreadable %s (%s)", skill_dir, MANIFEST_NAME, e)
        return None

    section = data.get("skill") or {}
    name = str(section.get("name") or skill_dir.name).strip()
    if not name:
        logger.warning("Skipping skill at %s: empty name", skill_dir)
        return None

    entry_path = skill_dir / str(section.get("entry") or DEFAULT_ENTRY)
    if not entry_path.is_file():
        logger.warning(
            "Skipping skill '%s' at %s: entry file '%s' not found",
            name, skill_dir, entry_path.name,
        )
        return None

    tags = section.get("tags") or []
    return Skill(
        name=name,
        path=skill_dir,
        entry_path=entry_path,
        version=str(section.get("version") or "0.0.0"),
        description=str(section.get("description") or "").strip(),
        tags=tuple(str(t) for t in tags) if isinstance(tags, (list, tuple)) else (),
    )


class SkillRegistry:
    """Discovered skills, keyed by name."""

    def __init__(self) -> None:
        self._skills: Dict[str, Skill] = {}
        self._scanned_dirs: List[str] = []
        self._requested_dirs: Optional[tuple[str, ...]] = None
        self._lock = threading.Lock()

    def discover(self, skill_dirs: Sequence[str]) -> None:
        """Scan the given roots; each immediate subdirectory may be a skill.

        Re-scanning replaces the previous contents, so an operator can add a
        skill and reload without a restart. First definition of a name wins, so
        the earlier root in the list takes precedence (mirrors plugin_dirs).
        """
        found: Dict[str, Skill] = {}
        scanned: List[str] = []

        for raw_root in skill_dirs:
            root = Path(raw_root)
            if not root.is_dir():
                logger.debug("Skill dir %s does not exist, skipping", root)
                continue
            scanned.append(str(root))
            for child in sorted(root.iterdir()):
                if not child.is_dir():
                    continue
                skill = _parse_manifest(child)
                if skill is None:
                    continue
                if skill.name in found:
                    logger.warning(
                        "Duplicate skill '%s': keeping %s, ignoring %s",
                        skill.name, found[skill.name].path, skill.path,
                    )
                    continue
                found[skill.name] = skill

        with self._lock:
            self._skills = found
            self._scanned_dirs = scanned
            self._requested_dirs = tuple(skill_dirs)
        logger.info("Discovered %d skill(s) in %s", len(found), scanned or "(no dirs)")

    def ensure_discovered(self, skill_dirs: Sequence[str]) -> None:
        """Scan only if these roots were not the last ones scanned.

        Prompts render on every LLM call, so the render path must not hit the
        filesystem each time. Use :meth:`discover` to force a re-scan (reload).
        """
        with self._lock:
            unchanged = self._requested_dirs == tuple(skill_dirs)
        if not unchanged:
            self.discover(skill_dirs)

    def get(self, name: str) -> Optional[Skill]:
        with self._lock:
            return self._skills.get(name)

    def list_skills(self) -> List[Skill]:
        with self._lock:
            return sorted(self._skills.values(), key=lambda s: s.name)

    def names(self) -> List[str]:
        with self._lock:
            return sorted(self._skills)

    @property
    def scanned_dirs(self) -> List[str]:
        with self._lock:
            return list(self._scanned_dirs)


_registry: Optional[SkillRegistry] = None
_registry_lock = threading.Lock()


def get_skill_registry(skill_dirs: Optional[Sequence[str]] = None) -> SkillRegistry:
    """Process-wide registry.

    Passing ``skill_dirs`` (re-)runs discovery; omitting it returns the current
    registry, discovering the defaults once on first use.
    """
    global _registry
    with _registry_lock:
        first_use = _registry is None
        if first_use:
            _registry = SkillRegistry()
        registry = _registry

    if skill_dirs is not None:
        registry.discover(skill_dirs)
    elif first_use:
        registry.discover(default_skill_dirs())
    return registry


def reset_skill_registry() -> None:
    """Drop the process-wide registry (used by tests for isolation)."""
    global _registry
    with _registry_lock:
        _registry = None
