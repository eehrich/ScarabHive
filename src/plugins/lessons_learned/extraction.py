"""
Lesson extraction from conversations using LLM analysis.

Extracts potential lessons from conversation history and
stores them after deduplication checks.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from datetime import datetime
from typing import Any, List, Optional, Tuple, TYPE_CHECKING

from agent_system.llm.models import ChatMessage
from agent_system.utils.json_utils import repair_json as _repair_json

from .models import ExtractionResult, LessonCandidate

if TYPE_CHECKING:
    from .server import LessonsLearnedServer

logger = logging.getLogger(__name__)

#: A comma before a closing bracket, which JSON does not allow and models write.
_TRAILING_COMMA = re.compile(r",\s*([\]}])")

# ==============================================================================
# Extraction Prompt
# ==============================================================================

EXTRACTION_SYSTEM_PROMPT = """You are a lesson extraction assistant. Analyze the conversation and extract
reusable lessons that should be remembered for future sessions.

Focus on:
- Corrections the user made (the agent did X wrong, should have done Y)
- Explicit preferences stated by the user
- Domain-specific knowledge or insights shared by the user that generalize across projects
- Content quality patterns (what makes good/bad output in this domain)
- Error patterns that should be avoided in the future
- Style preferences or standards that emerged
- Be concise, but keep the core meaning and insight of the lesson

Do NOT extract:
- One-off factual queries (e.g., "What is the capital of France?")
- Lessons that are too specific to a single task/project and won't generalize to other projects
- Project-specific content (e.g., specific magic systems, character names, plot details of one book)
- Trivially obvious best practices
- Tool usage instructions (e.g., "Use tool X for task Y", "Pass parameter Z to tool")
- Operational workflow steps (e.g., "Check for existing items before creating new ones")
- Communication patterns (e.g., "Provide clear next steps", "Return the ID after creation")
- Anything that describes HOW to use the system rather than domain knowledge
- Success confirmations or status reports disguised as lessons

Output valid JSON:
```json
{
  "lessons": [
    {
      "title": "Short descriptive title (max 200 chars)",
      "content": "Detailed lesson content. What to do/avoid and why. (max 500 chars)",
      "category": "One of: style, error_pattern, domain_knowledge, quality",
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


def written(msg: Any) -> Optional[Tuple[str, str]]:
    """Role and text of a message a person or the assistant wrote, else None.

    Not the system prompt, tool results or developer turns, and nothing a
    plugin or the loop injected (``injected_by``) -- among them this plugin's
    own lessons block, which read back in confirmed the lessons against
    themselves."""
    field = msg.get if isinstance(msg, dict) else (lambda name: getattr(msg, name, None))
    role, content = field("role"), field("content")
    if role not in ("user", "assistant") or field("injected_by") or not isinstance(content, str) or not content:
        return None
    return role, content


def fingerprint(previous: Any, last: Any) -> str:
    """Names the last message an extraction read, by itself and the one before
    it (and their timestamps, where they have one): a repeated "ok" alone
    would match the wrong one."""
    parts = []
    for msg in (previous, last):
        if msg is None:
            parts.append("")
            continue
        stamp = msg.get("timestamp") if isinstance(msg, dict) else getattr(msg, "timestamp", None)
        role, content = written(msg) or ("", "")
        parts.append(f"{role}\x1f{content}\x1f{'' if stamp is None else stamp}")
    return hashlib.sha256("\x1e".join(parts).encode("utf-8")).hexdigest()[:32]


def unread_part(messages: list, anchor: Optional[str]) -> Tuple[Any, List[Any]]:
    """The written messages after the one ``anchor`` names, and the message before them.

    Found by what was read, not by position: the list a request hands the
    session end hooks holds the system prompt and the notes hooks appended in
    this request, which are gone from the next one, and a compaction shifts
    everything -- a position logged from it pointed past messages never read.
    Searched from the end. Not found (compacted away), or no extraction yet:
    every written message; reading one twice beats skipping it, and a second
    evidence from the same session is not counted."""
    mine = [msg for msg in messages if written(msg)]
    if anchor:
        for index in range(len(mine) - 1, -1, -1):
            if fingerprint(mine[index - 1] if index else None, mine[index]) == anchor:
                return mine[index], mine[index + 1:]
    return None, mine


def read_conversation(messages: list, max_chars: int = 8000) -> Tuple[str, List[Any]]:
    """The extraction prompt's conversation text, and the messages it holds.

    Up to ``max_chars``; the messages behind the cut are not in the list, so
    the next extraction reads them. A single message longer than that is
    shortened, not dropped: dropped, nothing was read, nothing logged, and the
    extraction of the session stood still for good."""
    parts: List[str] = []
    read: List[Any] = []
    total = 0

    for msg in messages:
        if not (message := written(msg)):
            continue
        role, content = message

        line = f"[{role}]: {content}"
        if total + len(line) > max_chars:
            if read:
                parts.append("... (conversation truncated) ...")
                break
            line = line[:max_chars] + " ... (message shortened)"
        parts.append(line)
        read.append(msg)
        total += len(line)

    return "\n\n".join(parts), read


def _format_conversation(messages: list, max_chars: int = 8000) -> str:
    """Format conversation messages for the extraction prompt."""
    return read_conversation(messages, max_chars)[0]


def _candidates(data: Any) -> Optional[List[LessonCandidate]]:
    """The lessons of a parsed answer, None when it is no answer at all (no
    object with a "lessons" list). An item the model got wrong (a priority
    "high", tags as one string) is skipped: it used to raise out of the parse
    and take every other lesson of the session with it."""
    if not isinstance(data, dict) or not isinstance(data.get("lessons"), list):
        return None
    candidates: List[LessonCandidate] = []
    for item in data["lessons"]:
        if not isinstance(item, dict) or not item.get("title") or not item.get("content"):
            continue
        try:
            candidates.append(LessonCandidate(
                title=str(item["title"])[:200],
                content=str(item["content"])[:2000],
                category=str(item.get("category", "general")),
                priority=int(item.get("priority", 5)),
                tags=item.get("tags", []),
            ))
        except (TypeError, ValueError) as e:  # a pydantic ValidationError is a ValueError
            logger.warning(f"Extracted lesson '{item.get('title')}' skipped: {e}")
    return candidates


def _parse_extraction_response(response: str) -> Optional[List[LessonCandidate]]:
    """Parse the LLM's JSON response into LessonCandidate objects; None when it could not be read."""
    return _parse(response)[0]


def _parse(response: Any) -> Tuple[Optional[List[LessonCandidate]], bool]:
    """The candidates of an answer, and whether the answer was whole.

    None: no answer (empty, a refusal, cut off before its list) -- the
    messages were not judged, and must not be logged as read. Whole: the
    first JSON object in it parses complete, whatever text stands around it
    (a sentence before, a fence and a closing remark after) -- or does so
    once trailing commas are dropped. Counted as not whole, those everyday
    answers were never logged, and the same messages went to the LLM at
    every request. Only an answer repair_json had to mend is used but not
    whole: a cut-off tail may have held more lessons, so the messages are
    read again (what was stored from it comes back as a duplicate of its own
    session, which adds nothing)."""
    try:
        text = response.strip()
        start = text.find("{")
        if start >= 0:
            for attempt in (text[start:], _TRAILING_COMMA.sub(r"\1", text[start:])):
                try:
                    return _candidates(json.JSONDecoder().raw_decode(attempt)[0]), True
                except json.JSONDecodeError:
                    pass
        return _candidates(json.loads(text)), True
    except json.JSONDecodeError:
        # Try repair_json as fallback
        logger.debug("Standard JSON parse failed in extraction, trying repair_json...")
        candidates = _candidates(_repair_json(text))
        if candidates is None:
            logger.warning("Failed to parse extraction response even with repair")
        return candidates, False
    except (AttributeError, KeyError, TypeError) as e:  # no text at all
        logger.warning(f"Failed to parse extraction response: {e}")
        return None, False


async def extract_lessons_from_conversation(
    messages: list,
    agent_name: str,
    session_id: str,
    server: "LessonsLearnedServer",
    max_lessons: int = 5,
    auto_approve: bool = False,
    llm_profile: str = "turbo",
    agent: Any = None,
    previous: Any = None,
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
        previous: The written message before ``messages`` in the conversation,
            None at its start; with the last message read, it names where the
            next extraction starts (logged once the LLM has answered)

    Returns:
        ExtractionResult with counts of created/merged/confirmed lessons
    """
    result = ExtractionResult(session_id=session_id, agent_name=agent_name)

    try:
        # Format conversation
        conversation_text, read = read_conversation(messages)
        if len(conversation_text) < 100:  # waits for more; nothing logged, nothing skipped
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
        candidates, whole = _parse(response)
        if candidates is None:  # not judged: read again next time, nothing logged
            return result
        if not candidates:
            logger.debug(f"No lessons extracted for agent '{agent_name}' in session '{session_id}'")

        # Limit candidates
        candidates = candidates[:max_lessons]
        unchecked = False

        # Process each candidate with deduplication
        for candidate in candidates:
            dedup = await server.check_duplicate(agent_name, candidate.title, candidate.content)

            if dedup.error:
                # Stored unchecked, the same lesson came back as a new copy at
                # every session end until the agent's limit was full. Skipped,
                # it is extracted again once the check works.
                logger.warning(f"Lesson '{candidate.title}' not stored: no duplicate check ({dedup.error})")
                unchecked = True
                continue
            if dedup.is_duplicate:
                if dedup.action == "confirm":
                    # Exact duplicate → add evidence
                    added = await server.add_evidence(
                        lesson_id=dedup.existing_lesson_id or "",
                        session_id=session_id,
                        agent_name=agent_name,
                        evidence_type="confirm",
                        description=f"Re-extracted from session: {candidate.title}",
                    )
                    # counted only when it is new evidence, not one this session gave already
                    result.confirmed_count += added.get("status") == "evidence_added"
                elif dedup.action == "merge":
                    # High similarity → evidence for the existing lesson, nothing stored
                    added = await server.add_evidence(
                        lesson_id=dedup.existing_lesson_id or "",
                        session_id=session_id,
                        agent_name=agent_name,
                        evidence_type="confirm",
                        description=f"Similar lesson extracted: {candidate.title}",
                    )
                    result.merged_count += added.get("status") == "evidence_added"
            else:
                # New lesson → store
                status = "active" if auto_approve else "draft"
                stored = await server.store_lesson(
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
                if "error" in stored:  # the agent's lesson limit, most likely: counted as skipped
                    logger.warning(f"Lesson '{candidate.title}' not stored: {stored['error']}")
                    continue
                result.created_count += 1

        result.skipped_count = len(candidates) - (
            result.created_count + result.merged_count + result.confirmed_count
        )

        # Log extraction in DB -- also an answer without lessons: the log is
        # where the next run starts. Not when a lesson went unchecked: those
        # messages are read again once the duplicate check works.
        if whole and not unchecked:
            result.anchor = fingerprint(read[-2] if len(read) > 1 else previous, read[-1])
            server.log_extraction(session_id, agent_name, len(candidates), result.anchor, llm_profile)

        return result

    except Exception as e:
        logger.error(f"Lesson extraction failed: {e}", exc_info=True)
        return result
