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

from agent_system.mcp.schema_based import SchemaBasedMCPServer
from agent_system.skills import get_skill_registry
from agent_system.skills.registry import DEFAULT_ENTRY, TEXT_ENCODING
from agent_system.utils.suggest import suggest_path

if TYPE_CHECKING:
    from agent_system.config.models import AgentSystemConfig, MCPConfig

#: Guard against a huge reference file blowing up the context in one call.
MAX_READ_CHARS = 100_000


class SkillsServer(SchemaBasedMCPServer):
    """Expose discovered skills and their bundled files."""

    def __init__(self, name: str, system_config: "AgentSystemConfig", mcp_config: "MCPConfig") -> None:
        super().__init__(name, system_config, mcp_config)
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
        """List skills; with ``name`` restrict to one and show its files."""
        status = params["_status"]  # Status is mandatory from framework
        registry = self._registry()
        wanted = (params.get("name") or "").strip()

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
            await status.end(
                f"{skill.name} v{skill.version}: {len(files)} file(s) — {', '.join(files)}"
            )
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
        await status.end(
            f"{len(names)} skill(s) available: {', '.join(names)}" if names
            else f"no skills found (searched {', '.join(registry.scanned_dirs) or 'no dirs'})"
        )
        return {
            "status": "success",
            "count": len(names),
            "skills": [
                {
                    "name": s.name,
                    "version": s.version,
                    "description": s.description,
                    "tags": list(s.tags),
                    "files": s.list_files(),
                }
                for s in skills
            ],
        }

    async def read(self, params: dict[str, Any]) -> dict[str, Any]:
        """Read SKILL.md, or a bundled file when ``path`` is given."""
        status = params["_status"]  # Status is mandatory from framework
        registry = self._registry()
        name = (params.get("name") or "").strip()
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

        rel = (params.get("path") or "").strip()
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

        truncated = len(content) > MAX_READ_CHARS
        if truncated:
            content = content[:MAX_READ_CHARS]

        await status.end(
            f"{name}/{rel or DEFAULT_ENTRY} — {len(content)} chars"
            + (f" (truncated at {MAX_READ_CHARS})" if truncated else "")
        )
        return {
            "status": "success",
            "skill": skill.name,
            "path": rel or skill.entry_path.name,
            "truncated": truncated,
            "content": content,
        }
