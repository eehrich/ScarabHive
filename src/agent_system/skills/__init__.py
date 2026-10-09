"""Skills — packaged, reusable agent knowledge.

Plugins distribute *capabilities* (tools an agent can call). Skills distribute
*knowledge* (how an agent should approach a task). A skill is a directory whose
``SKILL.md`` carries YAML frontmatter plus the instructions (the Agent Skills
standard), referenced by name from an agent's config — no prompt file has to be
edited to give an agent domain knowledge, and the same skill can be shared by
many agents, or with any other tool that speaks the standard.
"""
from .invocation import expand, invoke, split_arguments
from .registry import Skill, SkillRegistry, configured_skill_registry, get_skill_registry

__all__ = [
    "Skill", "SkillRegistry", "configured_skill_registry", "get_skill_registry",
    "expand", "invoke", "split_arguments",
]
