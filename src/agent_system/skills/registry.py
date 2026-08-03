"""Discovery and lookup of skills.

One layout, the Agent Skills standard (https://agentskills.io)::

    skills/
    └── my-skill/
        ├── SKILL.md        # YAML frontmatter (name, description) + markdown
        ├── references/     # optional: docs loaded on demand
        ├── scripts/        # optional: executable code
        └── assets/         # optional: templates, data

Skills in this layout work unchanged in Claude Code, Codex, Cursor, Copilot,
Gemini CLI and the rest of the ecosystem -- and theirs work here.

The body is used VERBATIM. The standard says nothing about templating, and our
Jinja environment renders unknown variables as empty: rendering a foreign body
would silently delete every literal ``{{ ... }}`` its author wrote. For our own
prompts the answer is ``{% include %}`` in the prompt TEMPLATES, not in skills
(docs/skills_design.md §8).

An earlier layout kept the metadata in a ``skill.toml`` beside the body. It is
gone -- a directory that still has one is reported, not silently skipped.
"""
from __future__ import annotations

import logging
import os
import re
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

import yaml

logger = logging.getLogger(__name__)

#: Default roots scanned for skills, relative to the working directory.
#: ``.claude/skills`` is where the ecosystem puts project-local skills (Claude
#: Code, Codex, Cursor …) — scanning it is what makes a downloaded skill usable
#: without any config. ``skills`` comes first, so our own names win a collision.
#: Discovery alone injects nothing: a skill only reaches a prompt when an agent
#: config names it under ``always``/``on_demand``.
DEFAULT_SKILL_DIRS: tuple[str, ...] = ("skills", ".claude/skills")

#: Operator override, os.pathsep-separated (e.g. "skills:/opt/team-skills").
#: Keeps skill locations configurable without a config-schema change.
SKILL_DIRS_ENV = "AGENT_SKILL_DIRS"


def default_skill_dirs() -> tuple[str, ...]:
    """Roots to scan: ``$AGENT_SKILL_DIRS`` if set, else :data:`DEFAULT_SKILL_DIRS`."""
    raw = os.environ.get(SKILL_DIRS_ENV, "").strip()
    if not raw:
        return DEFAULT_SKILL_DIRS
    return tuple(p for p in (part.strip() for part in raw.split(os.pathsep)) if p)

#: The retired manifest. Only still named so a leftover one can be reported
#: and kept out of the bundle listing.
LEGACY_MANIFEST_NAME = "skill.toml"
DEFAULT_ENTRY = "SKILL.md"

#: Skill files are decoded with ``utf-8-sig``, not ``utf-8``: editors on Windows
#: happily save a BOM, and a leading ``﻿`` would push the ``---`` off the
#: first column — the frontmatter would go undetected and the skill would vanish
#: from discovery entirely. ``utf-8-sig`` strips a BOM if present and is plain
#: utf-8 otherwise.
TEXT_ENCODING = "utf-8-sig"

#: Frontmatter delimiters per the Agent Skills spec: the file OPENS with ``---``.
_FRONTMATTER_RE = re.compile(r"\A---[ \t]*\r?\n(.*?)\r?\n---[ \t]*\r?\n?", re.DOTALL)

#: Spec: 1-64 chars, lowercase alphanumeric + hyphens, no leading/trailing
#: hyphen, no consecutive hyphens.
_VALID_NAME_RE = re.compile(r"\A[a-z0-9]+(-[a-z0-9]+)*\Z")

MAX_NAME_LEN = 64
MAX_DESCRIPTION_LEN = 1024
MAX_COMPATIBILITY_LEN = 500


def split_frontmatter(text: str) -> Tuple[Optional[Dict[str, Any]], str]:
    """``(frontmatter, body)``; ``(None, text)`` when there is no frontmatter.

    The body is returned without the frontmatter block -- otherwise the raw
    ``---name: …---`` header would be pasted into the system prompt.
    """
    match = _FRONTMATTER_RE.match(text)
    if not match:
        return None, text
    try:
        data = yaml.safe_load(match.group(1))
    except Exception:  # noqa: BLE001 - a broken header must not lose the body
        return None, text
    if not isinstance(data, dict):
        return None, text
    return data, text[match.end():]


@dataclass(frozen=True)
class Skill:
    """One discovered skill."""

    name: str
    path: Path          # the skill directory
    entry_path: Path    # absolute path to the markdown body
    version: str = "0.0.0"
    description: str = ""
    tags: tuple[str, ...] = ()
    #: Agent Skills spec fields (empty when the skill does not declare them).
    license: str = ""
    compatibility: str = ""
    #: hash=False because a dict is unhashable and this dataclass is frozen:
    #: without it every ``hash(skill)`` would raise. Still compared, so equal
    #: skills keep equal hashes.
    metadata: Mapping[str, str] = field(default_factory=dict, hash=False)
    #: Declared by the skill, NOT enforced here — see the warning at discovery.
    allowed_tools: tuple[str, ...] = ()

    @property
    def entry(self) -> str:
        """Body path as a string (what the prompt renderer wants)."""
        return str(self.entry_path)

    def body(self) -> str:
        """The instructions, with any frontmatter stripped.

        Leading blank lines go with it: the ``---`` block is conventionally
        followed by one, and it belongs to the header, not to the instructions.
        """
        text = self.entry_path.read_text(encoding=TEXT_ENCODING)
        _front, body = split_frontmatter(text)
        return body.lstrip("\n")

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

        A leftover legacy manifest is omitted — it is plumbing, not knowledge.
        """
        root = self.path.resolve()
        out = []
        for p in root.rglob("*"):
            if not p.is_file() or p.name == LEGACY_MANIFEST_NAME:
                continue
            try:
                out.append(p.resolve().relative_to(root).as_posix())
            except ValueError:  # pragma: no cover - symlink escaping the bundle
                continue
        # Sort the resulting strings, not the Path objects: Path ordering is
        # case-insensitive on Windows and case-sensitive elsewhere, which would
        # make this list platform-dependent.
        return sorted(out)


def _clean_name(raw: str, skill_dir: Path) -> str:
    """A usable skill name, preferring the directory when the field is unfit.

    The spec requires ``name`` to match the parent directory, so the directory
    is the safe fallback -- and the goal here is to LOAD a foreign skill, not
    to grade it. Violations are reported, never fatal.
    """
    candidate = (raw or "").strip()
    if not candidate:
        return skill_dir.name
    if len(candidate) > MAX_NAME_LEN or not _VALID_NAME_RE.match(candidate):
        logger.warning(
            "Skill at %s: name %r violates the Agent Skills naming rules "
            "(1-%d chars, lowercase a-z/0-9 and single hyphens) - using the "
            "directory name %r instead",
            skill_dir, candidate, MAX_NAME_LEN, skill_dir.name,
        )
        return skill_dir.name
    if candidate != skill_dir.name:
        logger.warning(
            "Skill at %s: name %r does not match the directory name - the spec "
            "requires them to be equal; using %r",
            skill_dir, candidate, candidate,
        )
    return candidate


def _first_paragraph(body: str) -> str:
    """First prose paragraph of a body, for skills without a description.

    Same fallback Claude Code applies. Without SOME description an on-demand
    skill is invisible: the index line is all the agent ever sees of it.
    """
    for block in body.split("\n\n"):
        text = " ".join(
            line.strip() for line in block.strip().splitlines()
            if line.strip() and not line.lstrip().startswith(("#", "---", "```"))
        ).strip()
        if text:
            return text[:MAX_DESCRIPTION_LEN]
    return ""


def _truncate(value: str, limit: int, field_name: str, skill_dir: Path) -> str:
    text = (value or "").strip()
    if len(text) > limit:
        logger.warning("Skill at %s: %s exceeds %d characters - truncating",
                       skill_dir, field_name, limit)
        return text[:limit]
    return text


def _parse_frontmatter(skill_dir: Path) -> Optional[Skill]:
    """Build a Skill from ``SKILL.md`` YAML frontmatter (Agent Skills standard).

    Returns None when there is no readable SKILL.md with frontmatter, so the
    caller can fall back to the legacy manifest.
    """
    entry_path = skill_dir / DEFAULT_ENTRY
    if not entry_path.is_file():
        return None
    try:
        text = entry_path.read_text(encoding=TEXT_ENCODING)
    except Exception as e:  # noqa: BLE001 - one unreadable file must not stop startup
        logger.warning("Skipping skill at %s: unreadable %s (%s)",
                       skill_dir, DEFAULT_ENTRY, e)
        return None

    front, body = split_frontmatter(text)
    if front is None:
        return None

    name = _clean_name(str(front.get("name") or ""), skill_dir)
    description = _truncate(str(front.get("description") or ""),
                            MAX_DESCRIPTION_LEN, "description", skill_dir)
    if not description:
        description = _first_paragraph(body)
        logger.debug("Skill '%s': no description field, using the first paragraph", name)

    # metadata is a free-form string map; version/tags are ours and live there
    # by convention, so a round-trip through our own format keeps working.
    raw_meta = front.get("metadata")
    meta: Dict[str, str] = {}
    if isinstance(raw_meta, dict):
        meta = {str(k): str(v) for k, v in raw_meta.items()}

    raw_tools = front.get("allowed-tools") or front.get("allowed_tools") or ""
    if isinstance(raw_tools, (list, tuple)):
        tools = tuple(str(t) for t in raw_tools if str(t).strip())
    else:
        tools = tuple(str(raw_tools).split())
    if tools:
        # We read the field but have no per-skill tool gating: an agent's tools
        # come from its own config. Silently dropping a declared RESTRICTION
        # would widen it, so say so rather than let it pass unnoticed.
        logger.warning(
            "Skill at %s declares allowed-tools %s - this system does not gate "
            "tools per skill; the agent's own tool config applies unchanged",
            skill_dir, " ".join(tools),
        )

    raw_tags = front.get("tags") or meta.get("tags") or ()
    if isinstance(raw_tags, str):
        raw_tags = [t.strip() for t in raw_tags.split(",") if t.strip()]

    return Skill(
        name=name,
        path=skill_dir,
        entry_path=entry_path,
        version=str(front.get("version") or meta.get("version") or "0.0.0"),
        description=description,
        tags=tuple(str(t) for t in raw_tags) if isinstance(raw_tags, (list, tuple)) else (),
        license=str(front.get("license") or "").strip(),
        compatibility=_truncate(str(front.get("compatibility") or ""),
                                MAX_COMPATIBILITY_LEN, "compatibility", skill_dir),
        metadata=meta,
        allowed_tools=tools,
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
                skill = _parse_frontmatter(child)
                if skill is None:
                    if (child / LEGACY_MANIFEST_NAME).is_file():
                        # It used to define a skill. Vanishing without a word is
                        # how a directory turns into a mystery -- name the fix.
                        logger.warning(
                            "Skill at %s has a %s but no YAML frontmatter in %s "
                            "- the manifest layout is gone; move name/description "
                            "into the %s header to make it discoverable again",
                            child, LEGACY_MANIFEST_NAME, DEFAULT_ENTRY, DEFAULT_ENTRY,
                        )
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
