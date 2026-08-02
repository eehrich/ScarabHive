"""Skills — packaged, reusable agent knowledge.

Plugins distribute *capabilities* (tools an agent can call). Skills distribute
*knowledge* (how an agent should approach a task). A skill is a directory with a
``skill.toml`` manifest and a ``SKILL.md`` body, referenced by name from an
agent's config — no prompt file has to be edited to give an agent domain
knowledge, and the same skill can be shared by many agents.

See ``docs/skills_design.md`` for the concept and the trade-offs.
"""
from .registry import Skill, SkillRegistry, get_skill_registry

__all__ = ["Skill", "SkillRegistry", "get_skill_registry"]
