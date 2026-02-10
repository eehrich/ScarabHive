"""
Prompt builder for Lessons Learned injection.

Builds formatted lesson context blocks for injection into
the system prompt during pre_llm_call hook.
"""

from __future__ import annotations

import json
from typing import Any, Dict, List


def build_lesson_prompt(
    lessons: List[Dict[str, Any]],
    max_tokens: int = 1500,
) -> str:
    """
    Build a formatted lessons block for system prompt injection.

    Args:
        lessons: Active lessons sorted by priority/confidence
        max_tokens: Approximate token budget (chars * 0.25 heuristic)

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

            candidate = cat_header + "\n".join(cat_entries + [entry]) + "\n"
            if total_chars + len(candidate) > max_chars:
                break

            cat_entries.append(entry)
            total_chars += len(entry) + 1

        if cat_entries:
            body_parts.append(cat_header + "\n".join(cat_entries))

    if not body_parts:
        return ""

    return header + "\n".join(body_parts) + "\n"
