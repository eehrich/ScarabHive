"""
Lesson extraction from conversations using LLM analysis.

Extracts potential lessons from conversation history and
stores them after deduplication checks.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any, List, TYPE_CHECKING

from agent_system.llm.models import ChatMessage

from .models import ExtractionResult, LessonCandidate

if TYPE_CHECKING:
    from .server import LessonsLearnedServer

logger = logging.getLogger(__name__)

# ==============================================================================
# Extraction Prompt
# ==============================================================================

EXTRACTION_SYSTEM_PROMPT = """You are a lesson extraction assistant. Analyze the conversation and extract
reusable lessons that should be remembered for future sessions.

Focus on:
- Corrections the user made (the agent did X wrong, should have done Y)
- Explicit preferences stated by the user
- Patterns that led to successful outcomes
- Mistakes or anti-patterns to avoid
- Domain-specific knowledge shared by the user
- Tool usage patterns that worked well or poorly
- Workflow improvements discovered during the session

Do NOT extract:
- One-off factual queries (e.g., "What is the capital of France?")
- Lessons that are too specific to a single task and won't generalize
- Trivially obvious best practices

Output valid JSON:
```json
{
  "lessons": [
    {
      "title": "Short descriptive title (max 200 chars)",
      "content": "Detailed lesson content. What to do/avoid and why. (max 500 chars)",
      "category": "One of: style, workflow, error_pattern, domain_knowledge, tool_usage, communication, performance, quality",
      "priority": 5,
      "tags": ["tag1", "tag2"]
    }
  ]
}
```

If no meaningful lessons can be extracted, return: {"lessons": []}
Return ONLY the JSON, no other text."""

EXTRACTION_USER_TEMPLATE = """Analyze this conversation between a user and the agent "{agent_name}" and extract reusable lessons.

Conversation:
{conversation}"""


def _format_conversation(messages: list, max_chars: int = 8000) -> str:
    """Format conversation messages for the extraction prompt."""
    parts: List[str] = []
    total = 0

    for msg in messages:
        role = msg.role if hasattr(msg, "role") else msg.get("role", "unknown")
        content = msg.content if hasattr(msg, "content") else msg.get("content", "")

        if role == "system":
            continue
        if not content or not isinstance(content, str):
            continue

        # Skip tool results (they're too verbose)
        if role == "tool":
            continue

        line = f"[{role}]: {content}"
        if total + len(line) > max_chars:
            parts.append("... (conversation truncated) ...")
            break
        parts.append(line)
        total += len(line)

    return "\n\n".join(parts)


def _parse_extraction_response(response: str) -> List[LessonCandidate]:
    """Parse the LLM's JSON response into LessonCandidate objects."""
    try:
        # Strip markdown fences if present
        text = response.strip()
        if text.startswith("```"):
            lines = text.split("\n")
            text = "\n".join(lines[1:])
            if text.endswith("```"):
                text = text[:-3]
            text = text.strip()

        data = json.loads(text)
        lessons_data = data.get("lessons", [])

        candidates: List[LessonCandidate] = []
        for item in lessons_data:
            if not item.get("title") or not item.get("content"):
                continue
            candidates.append(LessonCandidate(
                title=str(item["title"])[:200],
                content=str(item["content"])[:2000],
                category=str(item.get("category", "general")),
                priority=int(item.get("priority", 5)),
                tags=item.get("tags", []),
            ))
        return candidates
    except (json.JSONDecodeError, KeyError, TypeError) as e:
        logger.warning(f"Failed to parse extraction response: {e}")
        return []


async def extract_lessons_from_conversation(
    messages: list,
    agent_name: str,
    session_id: str,
    server: "LessonsLearnedServer",
    max_lessons: int = 5,
    auto_approve: bool = False,
    llm_profile: str = "turbo",
    agent: Any = None,
) -> ExtractionResult:
    """
    Extract lessons from a conversation using LLM analysis.

    Args:
        messages: Conversation messages
        agent_name: Agent that had the conversation
        session_id: Session ID
        server: LessonsLearnedServer for storing results
        max_lessons: Max lessons to extract per session
        auto_approve: If True, set lessons to 'active' directly
        llm_profile: LLM profile to use for extraction
        agent: Agent object (for system_config access)

    Returns:
        ExtractionResult with counts of created/merged/confirmed lessons
    """
    result = ExtractionResult(session_id=session_id, agent_name=agent_name)

    try:
        # Format conversation
        conversation_text = _format_conversation(messages)
        if len(conversation_text) < 100:
            logger.debug("Conversation too short for extraction")
            return result

        # Create LLM client
        from agent_system.llm.factory import create_llm_from_profile

        system_config = None
        if agent and hasattr(agent, "system_config"):
            system_config = agent.system_config
        elif hasattr(server, "_system_config"):
            system_config = server._system_config

        if system_config is None:
            logger.warning("No system_config available for LLM extraction")
            return result

        llm = create_llm_from_profile(
            config=system_config,
            llm_profile=llm_profile,
        )

        # Build extraction messages
        extraction_messages = [
            ChatMessage(
                role="system",
                content=EXTRACTION_SYSTEM_PROMPT,
                timestamp=datetime.now(),
            ),
            ChatMessage(
                role="user",
                content=EXTRACTION_USER_TEMPLATE.format(
                    agent_name=agent_name,
                    conversation=conversation_text,
                ),
                timestamp=datetime.now(),
            ),
        ]

        # Call LLM
        response = await llm.chat(messages=extraction_messages)
        candidates = _parse_extraction_response(response)

        if not candidates:
            logger.debug(f"No lessons extracted for agent '{agent_name}' in session '{session_id}'")
            return result

        # Limit candidates
        candidates = candidates[:max_lessons]

        # Process each candidate with deduplication
        for candidate in candidates:
            dedup = await server.check_duplicate(agent_name, candidate.title, candidate.content)

            if dedup.is_duplicate:
                if dedup.action == "confirm":
                    # Exact duplicate → add evidence
                    await server.add_evidence(
                        lesson_id=dedup.existing_lesson_id or "",
                        session_id=session_id,
                        agent_name=agent_name,
                        evidence_type="confirm",
                        description=f"Re-extracted from session: {candidate.title}",
                    )
                    result.confirmed_count += 1
                elif dedup.action == "merge":
                    # High similarity → add evidence but also store as draft for review
                    await server.add_evidence(
                        lesson_id=dedup.existing_lesson_id or "",
                        session_id=session_id,
                        agent_name=agent_name,
                        evidence_type="confirm",
                        description=f"Similar lesson extracted: {candidate.title}",
                    )
                    result.merged_count += 1
            else:
                # New lesson → store
                status = "active" if auto_approve else "draft"
                await server.store_lesson(
                    agent_name=agent_name,
                    title=candidate.title,
                    content=candidate.content,
                    category=candidate.category,
                    priority=candidate.priority,
                    tags=candidate.tags,
                    source_type="auto",
                    source_session=session_id,
                    status=status,
                )
                result.created_count += 1

        result.skipped_count = len(candidates) - (
            result.created_count + result.merged_count + result.confirmed_count
        )

        # Log extraction in DB
        conn = server._get_connection()
        try:
            conn.execute(
                "INSERT INTO extraction_log (session_id, agent_name, extracted_count, llm_profile) VALUES (?, ?, ?, ?)",
                (session_id, agent_name, len(candidates), llm_profile),
            )
            conn.commit()
        finally:
            conn.close()

        return result

    except Exception as e:
        logger.error(f"Lesson extraction failed: {e}", exc_info=True)
        return result
