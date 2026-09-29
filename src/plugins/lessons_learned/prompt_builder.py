"""
Prompt builder for Lessons Learned injection.

Builds formatted lesson context blocks for injection into
the system prompt during pre_llm_call hook.
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional


def build_lesson_prompt(
    lessons: List[Dict[str, Any]],
    max_tokens: int = 1500,
    shown: Optional[List[Dict[str, Any]]] = None,
) -> str:
    """
    Build a formatted lessons block for system prompt injection.

    Args:
        lessons: Active lessons sorted by priority/confidence
        max_tokens: Approximate token budget (chars * 0.25 heuristic)
        shown: If given, the lessons that made it into the block are appended
            to it -- the budget may leave some out.

    Returns:
        Formatted prompt injection string, or empty string if no lessons
    """
    if not lessons:
        return ""

    max_chars = max_tokens * 4  # rough token-to-char ratio

    header = (
        "## LESSONS LEARNED\n"
        "The following are verified lessons from past sessions. "
        "Apply them where relevant to improve quality and consistency.\n\n"
    )

    # Group by category
    by_category: Dict[str, List[Dict[str, Any]]] = {}
    for lesson in lessons:
        cat = lesson.get("category", "general")
        by_category.setdefault(cat, []).append(lesson)

    body_parts: List[str] = []
    total_chars = len(header)

    for category, cat_lessons in by_category.items():
        cat_header = f"### {category.replace('_', ' ').title()}\n"
        cat_entries: List[str] = []

        for lesson in cat_lessons:
            confidence = lesson.get("confidence", 0.5)
            priority = lesson.get("priority", 5)
            tags_raw = lesson.get("tags", [])
            if isinstance(tags_raw, str):
                try:
                    tags_raw = json.loads(tags_raw)
                except (json.JSONDecodeError, TypeError):
                    tags_raw = []

            # Confidence indicator
            if confidence >= 0.8:
                indicator = "●"
            elif confidence >= 0.5:
                indicator = "◐"
            else:
                indicator = "○"

            # Build entry
            entry = f"- {indicator} **{lesson['title']}** [P{priority}]"
            content = lesson.get("content", "")
            if content:
                # Truncate long content
                if len(content) > 300:
                    content = content[:297] + "..."
                entry += f"\n  {content}"

            if tags_raw:
                entry += f"\n  Tags: {', '.join(tags_raw)}"

            # What this entry adds to the block: itself and its line break, and
            # the category heading with the first entry. (Adding the category's
            # earlier entries again filled only half the budget.)
            cost = len(entry) + 1 + (0 if cat_entries else len(cat_header))
            if total_chars + cost > max_chars:
                break

            cat_entries.append(entry)
            total_chars += cost
            if shown is not None:
                shown.append(lesson)

        if cat_entries:
            body_parts.append(cat_header + "\n".join(cat_entries))

    if not body_parts:
        return ""

    return header + "\n".join(body_parts) + "\n"
