"""Read access to skill bundles.

A skill is a BUNDLE: ``SKILL.md`` is the index/instructions, and files next to
it (``references/…``) carry depth the agent loads only when a task needs it.
``always`` skills already sit in the system prompt (core renders them); these
tools are what turn a skill into a browsable knowledge base.

The registry itself lives in ``agent_system.skills`` (core) because the prompt
renderer needs it; this plugin is the tool surface over it — the same split as
``agent_system.hooks`` (core) vs. hook plugins.
"""
from __future__ import annotations

from typing import Any, TYPE_CHECKING

from agent_system.tools.schema_based import SchemaBasedToolServer
from agent_system.skills import get_skill_registry
from agent_system.skills.registry import DEFAULT_ENTRY, TEXT_ENCODING
from agent_system.utils.suggest import suggest_path

if TYPE_CHECKING:
    from agent_system.config.models import AgentSystemConfig, ToolServerConfig

#: Guard against a huge reference file blowing up the context in one call.
#: A longer file is read in pieces: the answer names ``next_offset``.
MAX_READ_CHARS = 100_000

#: One status row (tests/plugins/test_status_end_lines.py: MAX_LINE).
MAX_STATUS_LINE = 140


def _line(text: str) -> str:
    """Fit a status line into one row; a long file or skill list is cut."""
    return text if len(text) <= MAX_STATUS_LINE else text[:MAX_STATUS_LINE - 2] + " …"


class SkillsServer(SchemaBasedToolServer):
    """Expose discovered skills and their bundled files."""

    def __init__(self, name: str, system_config: "AgentSystemConfig", server_config: "ToolServerConfig") -> None:
        super().__init__(name, system_config, server_config)
        self._system_config = system_config

    def _registry(self):
        """Registry with the configured roots applied.

        ``ensure_discovered`` only touches the filesystem when the roots
        changed, so repeated tool calls stay cheap.
        """
        from agent_system.skills.registry import default_skill_dirs

        configured = list(
            getattr(getattr(self._system_config, "skills", None), "skill_dirs", []) or []
        )
        registry = get_skill_registry()
        registry.ensure_discovered(configured or list(default_skill_dirs()))
        return registry

    async def list(self, params: dict[str, Any]) -> dict[str, Any]:
        """List skills (name + description); with ``name`` one skill in detail."""
        status = params["_status"]  # Status is mandatory from framework
        registry = self._registry()
        wanted = str(params.get("name") or "").strip()

        if wanted:
            skill = registry.get(wanted)
            if skill is None:
                names = registry.names()
                hint = suggest_path(wanted, names)
                msg = (f"Unknown skill '{wanted}'"
                       + (f" — did you mean '{hint}'?" if hint else ""))
                await status.error(msg)
                return {
                    "status": "error",
                    "error": msg,
                    "did_you_mean": hint,
                    "available": names,
                }
            files = skill.list_files()
            await status.end(_line(
                f"{skill.name} v{skill.version}: {len(files)} file(s) — {', '.join(files)}"
            ))
            return {
                "status": "success",
                "skill": {
                    "name": skill.name,
                    "version": skill.version,
                    "description": skill.description,
                    "tags": list(skill.tags),
                    "files": files,
                },
            }

        skills = registry.list_skills()
        names = [s.name for s in skills]
        await status.end(_line(
            f"{len(names)} skill(s) available: {', '.join(names)}" if names
            else f"no skills found (searched {', '.join(registry.scanned_dirs) or 'no dirs'})"
        ))
        # Compact on purpose: with every file of every skill this answer was
        # ~25,700 chars for 42 skills. The files come with ``name``.
        return {
            "status": "success",
            "count": len(names),
            "skills": [{"name": s.name, "description": s.description} for s in skills],
        }

    async def read(self, params: dict[str, Any]) -> dict[str, Any]:
        """Read SKILL.md, or a bundled file when ``path`` is given."""
        status = params["_status"]  # Status is mandatory from framework
        registry = self._registry()
        name = str(params.get("name") or "").strip()
        if not name:
            await status.error("'name' is required")
            return {"status": "error", "error": "'name' is required",
                    "available": registry.names()}

        skill = registry.get(name)
        if skill is None:
            names = registry.names()
            hint = suggest_path(name, names)
            msg = (f"Unknown skill '{name}'"
                   + (f" — did you mean '{hint}'?" if hint else ""))
            await status.error(msg)
            return {"status": "error", "error": msg,
                    "did_you_mean": hint, "available": names}

        rel = str(params.get("path") or "").strip()
        raw_offset = params.get("offset")
        offset_text = "" if raw_offset is None else str(raw_offset).strip()
        # 18 digits: past any file, and below int()'s 4300-digit limit.
        if offset_text and not (offset_text.isascii() and offset_text.isdigit() and len(offset_text) <= 18):
            msg = f"'offset' must be a whole number >= 0, got {raw_offset!r}"
            await status.error(f"{name}: {msg}")
            return {"status": "error", "error": msg}
        offset = int(offset_text or 0)
        await status.progress(f"Reading {name}/{rel or DEFAULT_ENTRY}")
        try:
            target = skill.resolve(rel) if rel else skill.entry_path
        except (ValueError, FileNotFoundError) as e:
            # Path escapes the bundle, or simply is not there — both are the
            # caller's problem, not a server error: report and list what exists.
            files = skill.list_files()
            hint = suggest_path(rel, files)
            message = str(e) + (f" — did you mean '{hint}'?" if hint else "")
            await status.error(f"{name}: {message}")
            return {"status": "error", "error": message,
                    "did_you_mean": hint, "files": files}

        try:
            if not rel:
                # The entry: hand over the instructions, not the YAML header.
                # The frontmatter is plumbing for discovery -- the agent already
                # has name and description from the index.
                content = skill.body()
            else:
                # utf-8-sig: a bundled reference saved with a BOM would
                # otherwise start with a stray '﻿' in the agent's context.
                content = target.read_text(encoding=TEXT_ENCODING)
        except (OSError, UnicodeDecodeError) as e:
            await status.error(f"Cannot read '{rel or target.name}': {e}")
            return {"status": "error",
                    "error": f"Cannot read '{rel or target.name}': {e}"}

        total = len(content)
        if offset > total:
            msg = f"'offset' {offset} is past the end ({total} chars)"
            await status.error(f"{name}/{rel or DEFAULT_ENTRY}: {msg}")
            return {"status": "error", "error": msg}
        end = offset + MAX_READ_CHARS
        truncated = total > end
        content = content[offset:end]

        await status.end(_line(
            f"{name}/{rel or DEFAULT_ENTRY} — {len(content)} of {total} chars"
            + (f" from {offset}" if offset else "")
            + (f" (truncated, next_offset {end})" if truncated else "")
        ))
        result = {
            "status": "success",
            "skill": skill.name,
            "path": rel or skill.entry_path.name,
            "truncated": truncated,
            "content": content,
        }
        if truncated:
            result["next_offset"] = end
        return result
