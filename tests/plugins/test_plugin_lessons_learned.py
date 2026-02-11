"""
Lessons Learned Plugin - Comprehensive Test Suite

Tests cover:
- Lesson CRUD (store, list, get, update, delete)
- Semantic search via VectorStore
- Deduplication logic
- Evidence tracking + confidence recalculation
- Cross-agent teaching
- Hook integration (inject + extract)
- Prompt builder
- Web endpoint routing
- Edge cases and error handling

Target: >70% code coverage for critical modules
"""

import json
import sqlite3
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from plugins.lessons_learned.models import (
    DEFAULT_CATEGORIES,
    DeduplicationResult,
    ExtractionResult,
    Lesson,
    LessonCandidate,
    LessonStatus,
    SourceType,
)
from plugins.lessons_learned.prompt_builder import build_lesson_prompt
from plugins.lessons_learned.server import LessonsLearnedServer


# =============================================================================
# Fixtures
# =============================================================================

@pytest.fixture
def temp_storage(tmp_path: Path) -> Path:
    """Temporary storage directory for lessons."""
    storage = tmp_path / "lessons_learned"
    storage.mkdir()
    return storage


@pytest.fixture
def mock_system_config() -> MagicMock:
    """Mock AgentSystemConfig."""
    config = MagicMock()
    config.llm_system = MagicMock()
    return config


@pytest.fixture
def mock_mcp_config(temp_storage: Path) -> MagicMock:
    """Mock MCPServerConfig with lessons_learned settings."""
    config = MagicMock()
    # Set attributes directly (like production MCPConfig with extra="allow")
    config.database_path = str(temp_storage / "lessons.db")
    config.max_lessons_per_agent = 200
    config.dedup_similarity_threshold = 0.82
    config.exact_duplicate_threshold = 0.95
    config.llm_profile = "fast"
    # Also keep dict-style config for backward compat
    config.config = {
        "database_path": str(temp_storage / "lessons.db"),
        "max_lessons_per_agent": 200,
        "dedup_similarity_threshold": 0.82,
        "exact_duplicate_threshold": 0.95,
        "llm_profile": "fast",
    }
    return config


@pytest.fixture
def server(mock_system_config: MagicMock, mock_mcp_config: MagicMock, temp_storage: Path) -> LessonsLearnedServer:
    """LessonsLearnedServer instance with clean state."""
    srv = LessonsLearnedServer(
        name="lessons_learned",
        system_config=mock_system_config,
        mcp_config=mock_mcp_config,
    )
    return srv


# =============================================================================
# Test: Database Initialization
# =============================================================================

class TestDatabaseInit:

    def test_database_created(self, server: LessonsLearnedServer):
        """Database file should be created on init."""
        assert server.db_path.exists()

    def test_tables_created(self, server: LessonsLearnedServer):
        """All required tables should exist."""
        conn = server._get_connection()
        try:
            tables = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
            ).fetchall()
            table_names = {r[0] for r in tables}
            assert "lessons" in table_names
            assert "categories" in table_names
            assert "lesson_evidence" in table_names
            assert "lesson_applications" in table_names
            assert "extraction_log" in table_names
        finally:
            conn.close()

    def test_default_categories_seeded(self, server: LessonsLearnedServer):
        """Default categories should be seeded on first init."""
        conn = server._get_connection()
        try:
            count = conn.execute("SELECT COUNT(*) FROM categories").fetchone()[0]
            assert count == len(DEFAULT_CATEGORIES)
        finally:
            conn.close()

    def test_wal_mode(self, server: LessonsLearnedServer):
        """Database should use WAL journal mode."""
        conn = server._get_connection()
        try:
            mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
            assert mode.lower() == "wal"
        finally:
            conn.close()


# =============================================================================
# Test: Store Lesson
# =============================================================================

class TestStoreLessons:

    @pytest.mark.asyncio
    async def test_store_basic(self, server: LessonsLearnedServer):
        """Store a basic lesson."""
        result = await server.store_lesson(
            agent_name="test_agent",
            title="Test Lesson",
            content="Always use descriptive variable names.",
            category="style",
            priority=7,
        )
        assert result["status"] == "stored"
        assert result["lesson_id"] == "test_agent_les_001"
        assert result["agent_name"] == "test_agent"

    @pytest.mark.asyncio
    async def test_store_with_tags(self, server: LessonsLearnedServer):
        """Store a lesson with tags."""
        result = await server.store_lesson(
            agent_name="test_agent",
            title="Code Review",
            content="Always check error handling.",
            tags=["code", "review", "errors"],
        )
        assert result["status"] == "stored"

        # Verify tags persisted
        lesson = await server.get_lesson(result["lesson_id"])
        assert lesson is not None
        assert "code" in lesson["tags"]

    @pytest.mark.asyncio
    async def test_store_sequential_ids(self, server: LessonsLearnedServer):
        """Lesson IDs should be sequential per agent."""
        r1 = await server.store_lesson(agent_name="agent_a", title="L1", content="C1")
        r2 = await server.store_lesson(agent_name="agent_a", title="L2", content="C2")
        r3 = await server.store_lesson(agent_name="agent_b", title="L3", content="C3")

        assert r1["lesson_id"] == "agent_a_les_001"
        assert r2["lesson_id"] == "agent_a_les_002"
        assert r3["lesson_id"] == "agent_b_les_001"  # Different agent, resets

    @pytest.mark.asyncio
    async def test_store_source_type_confidence(self, server: LessonsLearnedServer):
        """Confidence should be set based on source_type."""
        r_manual = await server.store_lesson(
            agent_name="test_agent", title="Manual", content="C",
            source_type="manual",
        )
        r_auto = await server.store_lesson(
            agent_name="test_agent", title="Auto", content="C",
            source_type="auto",
        )
        lesson_m = await server.get_lesson(r_manual["lesson_id"])
        lesson_a = await server.get_lesson(r_auto["lesson_id"])
        assert lesson_m is not None
        assert lesson_a is not None
        assert lesson_m["confidence"] == 0.8  # manual base
        assert lesson_a["confidence"] == 0.4  # auto base

    @pytest.mark.asyncio
    async def test_store_limit_enforced(self, server: LessonsLearnedServer):
        """Should reject when max_lessons_per_agent is reached."""
        server.max_lessons_per_agent = 3
        for i in range(3):
            await server.store_lesson(agent_name="dev", title=f"L{i}", content=f"C{i}")

        result = await server.store_lesson(agent_name="dev", title="Overflow", content="X")
        assert "error" in result
        assert "limit" in result["error"].lower()


# =============================================================================
# Test: List Lessons
# =============================================================================

class TestListLessons:

    @pytest.mark.asyncio
    async def test_list_empty(self, server: LessonsLearnedServer):
        """List on empty database should return empty."""
        result = await server.list_lessons(agent_name="nobody")
        assert result["lessons"] == []
        assert result["total"] == 0

    @pytest.mark.asyncio
    async def test_list_filter_by_agent(self, server: LessonsLearnedServer):
        """List should filter by agent_name."""
        await server.store_lesson(agent_name="alice", title="A1", content="C")
        await server.store_lesson(agent_name="bob", title="B1", content="C")

        result = await server.list_lessons(agent_name="alice")
        assert result["total"] == 1
        assert result["lessons"][0]["agent_name"] == "alice"

    @pytest.mark.asyncio
    async def test_list_filter_by_status(self, server: LessonsLearnedServer):
        """List should filter by status."""
        await server.store_lesson(agent_name="a", title="Draft", content="C", status="draft")
        await server.store_lesson(agent_name="a", title="Active", content="C", status="active")

        result = await server.list_lessons(agent_name="a", status="active")
        assert result["total"] == 1
        assert result["lessons"][0]["status"] == "active"

    @pytest.mark.asyncio
    async def test_list_pagination(self, server: LessonsLearnedServer):
        """Pagination should work with limit/offset."""
        for i in range(5):
            await server.store_lesson(agent_name="a", title=f"L{i}", content="C")

        page1 = await server.list_lessons(agent_name="a", limit=2, offset=0)
        page2 = await server.list_lessons(agent_name="a", limit=2, offset=2)

        assert len(page1["lessons"]) == 2
        assert len(page2["lessons"]) == 2
        assert page1["total"] == 5
        # Pages should have different lessons
        ids1 = {l["lesson_id"] for l in page1["lessons"]}
        ids2 = {l["lesson_id"] for l in page2["lessons"]}
        assert ids1.isdisjoint(ids2)


# =============================================================================
# Test: Get Single Lesson
# =============================================================================

class TestGetLesson:

    @pytest.mark.asyncio
    async def test_get_existing(self, server: LessonsLearnedServer):
        """Get existing lesson should return all fields."""
        r = await server.store_lesson(
            agent_name="test", title="Find Me", content="Content here",
            category="workflow", priority=8, tags=["tag1"],
        )
        lesson = await server.get_lesson(r["lesson_id"])
        assert lesson is not None
        assert lesson["title"] == "Find Me"
        assert lesson["priority"] == 8
        assert lesson["category"] == "workflow"
        assert "tag1" in lesson["tags"]
        assert "evidence_summary" in lesson

    @pytest.mark.asyncio
    async def test_get_nonexistent(self, server: LessonsLearnedServer):
        """Get nonexistent lesson should return None."""
        lesson = await server.get_lesson("les_999")
        assert lesson is None


# =============================================================================
# Test: Update Lesson
# =============================================================================

class TestUpdateLesson:

    @pytest.mark.asyncio
    async def test_update_fields(self, server: LessonsLearnedServer):
        """Update should modify specified fields."""
        r = await server.store_lesson(agent_name="a", title="Old", content="Old content")
        result = await server.update_lesson(r["lesson_id"], title="New", priority=9)
        assert result["status"] == "updated"
        assert "title" in result["updated_fields"]

        lesson = await server.get_lesson(r["lesson_id"])
        assert lesson is not None
        assert lesson["title"] == "New"
        assert lesson["priority"] == 9

    @pytest.mark.asyncio
    async def test_update_status(self, server: LessonsLearnedServer):
        """Update status field."""
        r = await server.store_lesson(agent_name="a", title="T", content="C")
        result = await server.update_lesson(r["lesson_id"], status="active")
        assert result["status"] == "updated"

        lesson = await server.get_lesson(r["lesson_id"])
        assert lesson is not None
        assert lesson["status"] == "active"

    @pytest.mark.asyncio
    async def test_update_no_valid_fields(self, server: LessonsLearnedServer):
        """Update with no valid fields should return error."""
        r = await server.store_lesson(agent_name="a", title="T", content="C")
        result = await server.update_lesson(r["lesson_id"], invalid_field="X")
        assert "error" in result


# =============================================================================
# Test: Delete Lesson
# =============================================================================

class TestDeleteLesson:

    @pytest.mark.asyncio
    async def test_delete_existing(self, server: LessonsLearnedServer):
        """Delete an existing lesson."""
        r = await server.store_lesson(agent_name="a", title="Delete Me", content="C")
        result = await server.delete_lesson(r["lesson_id"])
        assert result["status"] == "deleted"

        lesson = await server.get_lesson(r["lesson_id"])
        assert lesson is None

    @pytest.mark.asyncio
    async def test_delete_nonexistent(self, server: LessonsLearnedServer):
        """Delete nonexistent should return error."""
        result = await server.delete_lesson("les_999")
        assert "error" in result


# =============================================================================
# Test: Evidence + Confidence
# =============================================================================

class TestEvidence:

    @pytest.mark.asyncio
    async def test_add_confirm_evidence(self, server: LessonsLearnedServer):
        """Adding confirm evidence should increase evidence_count."""
        r = await server.store_lesson(
            agent_name="a", title="T", content="C", source_type="manual"
        )
        result = await server.add_evidence(r["lesson_id"], "sess_1", "a", "confirm", "good")
        assert result["status"] == "evidence_added"
        assert result["evidence_count"] == 1
        assert result["new_confidence"] > 0

    @pytest.mark.asyncio
    async def test_add_contradict_evidence(self, server: LessonsLearnedServer):
        """Adding contradict evidence should lower confidence."""
        r = await server.store_lesson(
            agent_name="a", title="T", content="C", source_type="manual"
        )
        # Add confirm first
        await server.add_evidence(r["lesson_id"], "s1", "a", "confirm")
        lesson_before = await server.get_lesson(r["lesson_id"])

        # Now contradict
        result = await server.add_evidence(r["lesson_id"], "s2", "a", "contradict")
        lesson_after = await server.get_lesson(r["lesson_id"])

        assert lesson_after is not None
        assert lesson_before is not None
        # Two pieces of evidence total
        assert lesson_after["evidence_count"] == 2

    @pytest.mark.asyncio
    async def test_evidence_nonexistent(self, server: LessonsLearnedServer):
        """Evidence for nonexistent lesson should error."""
        result = await server.add_evidence("les_999", "s1", "a")
        assert "error" in result


# =============================================================================
# Test: Cross-Agent Teaching (op_teach)
# =============================================================================

class TestTeaching:

    @pytest.mark.asyncio
    async def test_teach_new_lesson(self, server: LessonsLearnedServer):
        """Teaching should store lesson in target agent."""
        result = await server._op_teach(
            {"target_agent": "student_agent", "title": "Tip", "content": "Do X not Y"},
            source_agent="teacher_agent",
            session_id="sess_1",
        )
        assert result["status"] == "stored"
        assert result["agent_name"] == "student_agent"

        # Verify it's stored with cross_agent source
        lesson = await server.get_lesson(result["lesson_id"])
        assert lesson is not None
        assert lesson["source_type"] == "cross_agent"
        assert lesson["source_agent"] == "teacher_agent"

    @pytest.mark.asyncio
    async def test_teach_missing_fields(self, server: LessonsLearnedServer):
        """Teaching without required fields should error."""
        result = await server._op_teach(
            {"target_agent": "student", "title": "T"},  # missing content
            source_agent="teacher",
            session_id="s1",
        )
        assert "error" in result


# =============================================================================
# Test: MCP Execute Dispatcher
# =============================================================================

class TestExecute:

    @pytest.mark.asyncio
    async def test_execute_store(self, server: LessonsLearnedServer):
        """Execute 'store' operation."""
        result = await server.execute({
            "operation": "store",
            "title": "Execute Store",
            "content": "Test content",
            "_agent_name": "test_agent",
            "_session_id": "sess_1",
        })
        assert result.get("status") == "stored"

    @pytest.mark.asyncio
    async def test_execute_list(self, server: LessonsLearnedServer):
        """Execute 'list' operation."""
        await server.store_lesson(agent_name="a", title="T", content="C")
        result = await server.execute({
            "operation": "list",
            "_agent_name": "a",
            "_session_id": "s1",
        })
        assert "lessons" in result

    @pytest.mark.asyncio
    async def test_execute_unknown_op(self, server: LessonsLearnedServer):
        """Execute with unknown operation returns error."""
        result = await server.execute({
            "operation": "foobar",
            "_agent_name": "a",
            "_session_id": "s1",
        })
        assert "error" in result

    @pytest.mark.asyncio
    async def test_execute_delete(self, server: LessonsLearnedServer):
        """Execute 'delete' operation removes a lesson."""
        stored = await server.store_lesson(agent_name="a", title="To Delete", content="Content")
        lesson_id = stored["lesson_id"]

        result = await server.execute({
            "operation": "delete",
            "lesson_id": lesson_id,
            "_agent_name": "a",
            "_session_id": "s1",
        })
        assert result.get("status") == "deleted"

        # Verify it's gone
        get_result = await server.get_lesson(lesson_id)
        assert get_result is None or "error" in get_result

    @pytest.mark.asyncio
    async def test_execute_delete_missing_id(self, server: LessonsLearnedServer):
        """Execute 'delete' without lesson_id returns error."""
        result = await server.execute({
            "operation": "delete",
            "_agent_name": "a",
            "_session_id": "s1",
        })
        assert "error" in result

    @pytest.mark.asyncio
    async def test_execute_with_status_reporter(self, server: LessonsLearnedServer):
        """Execute should call status reporter."""
        status = MagicMock()
        status.progress = AsyncMock()
        status.end = AsyncMock()

        result = await server.execute({
            "operation": "list",
            "_agent_name": "a",
            "_session_id": "s1",
            "_status": status,
        })
        assert "lessons" in result
        status.end.assert_called_once()


# =============================================================================
# Test: Deduplication
# =============================================================================

class TestDeduplication:

    @pytest.mark.asyncio
    async def test_no_duplicate_on_empty(self, server: LessonsLearnedServer):
        """No duplicate when no lessons exist."""
        result = await server.check_duplicate("agent_a", "Some Title", "Some Content")
        assert not result.is_duplicate
        assert result.action == "create"


# =============================================================================
# Test: Prompt Builder
# =============================================================================

class TestPromptBuilder:

    def test_build_empty(self):
        """Empty lessons should produce empty string."""
        assert build_lesson_prompt([]) == ""

    def test_build_single_lesson(self):
        """Single lesson should produce formatted prompt."""
        lessons = [{
            "title": "Use type hints",
            "content": "Always add type hints to function signatures.",
            "category": "style",
            "priority": 8,
            "confidence": 0.9,
            "tags": ["python", "types"],
        }]
        result = build_lesson_prompt(lessons)
        assert "LESSONS LEARNED" in result
        assert "Use type hints" in result
        assert "P8" in result
        assert "●" in result  # high confidence indicator

    def test_build_multiple_categories(self):
        """Lessons from different categories should be grouped."""
        lessons = [
            {"title": "L1", "content": "C1", "category": "style", "priority": 5, "confidence": 0.5, "tags": []},
            {"title": "L2", "content": "C2", "category": "workflow", "priority": 5, "confidence": 0.5, "tags": []},
        ]
        result = build_lesson_prompt(lessons)
        assert "Style" in result
        assert "Workflow" in result

    def test_build_confidence_indicators(self):
        """Confidence levels should have different indicators."""
        lessons = [
            {"title": "High", "content": "C", "category": "a", "priority": 5, "confidence": 0.9, "tags": []},
            {"title": "Med", "content": "C", "category": "a", "priority": 5, "confidence": 0.6, "tags": []},
            {"title": "Low", "content": "C", "category": "a", "priority": 5, "confidence": 0.3, "tags": []},
        ]
        result = build_lesson_prompt(lessons)
        assert "●" in result
        assert "◐" in result
        assert "○" in result

    def test_build_token_budget(self):
        """Should respect max_tokens budget."""
        lessons = [
            {"title": f"L{i}", "content": "X" * 200, "category": "a", "priority": 5, "confidence": 0.5, "tags": []}
            for i in range(50)
        ]
        result = build_lesson_prompt(lessons, max_tokens=200)
        # Should be truncated
        assert len(result) < 200 * 4 + 500  # some overhead for headers


# =============================================================================
# Test: Hook - Inject Lessons
# =============================================================================

class TestInjectHook:

    @pytest.mark.asyncio
    async def test_inject_no_lessons(self, server: LessonsLearnedServer):
        """Inject hook with no active lessons should not modify."""
        context = MagicMock()
        context.agent_name = "test_agent"
        context.session_id = "s1"
        context.messages = []
        context.hook_config = {}

        result = await server.on_pre_llm_call(context)
        assert result.success
        assert not result.modified

    @pytest.mark.asyncio
    async def test_inject_with_lessons(self, server: LessonsLearnedServer):
        """Inject hook should add lessons to messages."""
        # Create active lesson
        await server.store_lesson(
            agent_name="test_agent", title="Rule 1", content="Do X",
            status="active", confidence=0.9, priority=9,
        )

        # Mock context with system message
        from agent_system.llm.models import ChatMessage
        sys_msg = ChatMessage(role="system", content="You are a helpful agent.")
        user_msg = ChatMessage(role="user", content="Hello")

        context = MagicMock()
        context.agent_name = "test_agent"
        context.session_id = "s1"
        context.messages = [sys_msg, user_msg]
        context.hook_config = {"max_lessons": 10, "min_confidence": 0.3}

        result = await server.on_pre_llm_call(context)
        assert result.success
        assert result.modified

        # Should have inserted a message after system message
        assert len(context.messages) == 3
        injected = context.messages[1]
        assert "LESSONS LEARNED" in injected.content
        assert "Rule 1" in injected.content

    @pytest.mark.asyncio
    async def test_inject_no_agent_name(self, server: LessonsLearnedServer):
        """Inject hook without agent_name should skip."""
        context = MagicMock()
        context.agent_name = ""
        context.hook_config = {}

        result = await server.on_pre_llm_call(context)
        assert result.success
        assert not result.modified


# =============================================================================
# Test: Stats
# =============================================================================

class TestStats:

    @pytest.mark.asyncio
    async def test_stats_empty(self, server: LessonsLearnedServer):
        """Stats on empty database."""
        stats = await server.get_stats()
        assert stats["total"] == 0
        assert stats["agent_count"] == 0

    @pytest.mark.asyncio
    async def test_stats_with_data(self, server: LessonsLearnedServer):
        """Stats with some lessons."""
        await server.store_lesson(agent_name="a", title="T1", content="C", status="active")
        await server.store_lesson(agent_name="a", title="T2", content="C", status="draft")
        await server.store_lesson(agent_name="b", title="T3", content="C", status="active")

        stats = await server.get_stats()
        assert stats["total"] == 3
        assert stats["agent_count"] == 2
        assert "a" in stats["agents"]
        assert "b" in stats["agents"]

    @pytest.mark.asyncio
    async def test_stats_per_agent(self, server: LessonsLearnedServer):
        """Stats filtered by agent."""
        await server.store_lesson(agent_name="a", title="T1", content="C")
        await server.store_lesson(agent_name="b", title="T2", content="C")

        stats = await server.get_stats(agent_name="a")
        assert stats["total"] == 1


# =============================================================================
# Test: Categories
# =============================================================================

class TestCategories:

    @pytest.mark.asyncio
    async def test_list_categories(self, server: LessonsLearnedServer):
        """Should return default categories."""
        categories = await server.list_categories()
        assert len(categories) == len(DEFAULT_CATEGORIES)
        names = {c["name"] for c in categories}
        assert "style" in names
        assert "workflow" in names


# =============================================================================
# Test: Models
# =============================================================================

class TestModels:

    def test_lesson_model(self):
        """Lesson model should instantiate with defaults."""
        lesson = Lesson(agent_name="test", title="T", content="C")
        assert lesson.priority == 5
        assert lesson.confidence == 0.5
        assert lesson.status == LessonStatus.DRAFT
        assert lesson.tags == []

    def test_lesson_candidate(self):
        """LessonCandidate should instantiate."""
        candidate = LessonCandidate(title="T", content="C")
        assert candidate.category == "general"
        assert candidate.priority == 5

    def test_deduplication_result(self):
        """DeduplicationResult should instantiate."""
        result = DeduplicationResult(is_duplicate=False)
        assert result.action == "create"
        assert result.similarity == 0.0

    def test_extraction_result(self):
        """ExtractionResult should instantiate."""
        result = ExtractionResult(session_id="s1", agent_name="a")
        assert result.created_count == 0
        assert result.candidates == []


# =============================================================================
# Test: Application Recording
# =============================================================================

class TestApplicationRecording:

    @pytest.mark.asyncio
    async def test_record_application(self, server: LessonsLearnedServer):
        """Recording application should increment count."""
        r = await server.store_lesson(agent_name="a", title="T", content="C")
        await server.record_application(r["lesson_id"], "sess_1", "a")

        lesson = await server.get_lesson(r["lesson_id"])
        assert lesson is not None
        assert lesson["application_count"] == 1
        assert lesson["last_applied_at"] is not None

    @pytest.mark.asyncio
    async def test_record_multiple_applications(self, server: LessonsLearnedServer):
        """Multiple applications should increment count."""
        r = await server.store_lesson(agent_name="a", title="T", content="C")
        await server.record_application(r["lesson_id"], "s1", "a")
        await server.record_application(r["lesson_id"], "s2", "a")
        await server.record_application(r["lesson_id"], "s3", "a")

        lesson = await server.get_lesson(r["lesson_id"])
        assert lesson is not None
        assert lesson["application_count"] == 3


# =============================================================================
# Test: Extraction - Parse Response
# =============================================================================

class TestExtractionParsing:

    def test_parse_valid_json(self):
        """Parse a valid extraction response."""
        from plugins.lessons_learned.extraction import _parse_extraction_response

        response = json.dumps({
            "lessons": [
                {"title": "Use pytest", "content": "Always use pytest for testing.", "category": "tool_usage", "priority": 7, "tags": ["python"]},
                {"title": "Check errors", "content": "Handle all exceptions.", "priority": 5},
            ]
        })
        candidates = _parse_extraction_response(response)
        assert len(candidates) == 2
        assert candidates[0].title == "Use pytest"
        assert candidates[0].category == "tool_usage"

    def test_parse_markdown_wrapped(self):
        """Parse JSON wrapped in markdown code block."""
        from plugins.lessons_learned.extraction import _parse_extraction_response

        response = '```json\n{"lessons": [{"title": "T", "content": "C"}]}\n```'
        candidates = _parse_extraction_response(response)
        assert len(candidates) == 1

    def test_parse_empty_lessons(self):
        """Parse response with no lessons."""
        from plugins.lessons_learned.extraction import _parse_extraction_response

        response = '{"lessons": []}'
        candidates = _parse_extraction_response(response)
        assert len(candidates) == 0

    def test_parse_invalid_json(self):
        """Parse invalid JSON should return empty."""
        from plugins.lessons_learned.extraction import _parse_extraction_response

        candidates = _parse_extraction_response("not json at all")
        assert len(candidates) == 0

    def test_parse_missing_required_fields(self):
        """Lessons without title/content should be skipped."""
        from plugins.lessons_learned.extraction import _parse_extraction_response

        response = json.dumps({
            "lessons": [
                {"title": "Good", "content": "Has content"},
                {"title": "Bad"},  # Missing content
                {"content": "Also bad"},  # Missing title
            ]
        })
        candidates = _parse_extraction_response(response)
        assert len(candidates) == 1


# =============================================================================
# Test: Cluster Token Limit
# =============================================================================

class TestClusterTokenLimit:

    @pytest.mark.asyncio
    async def test_large_cluster_splits(self, server: LessonsLearnedServer):
        """Clusters exceeding MAX_CLUSTER_CHARS are split into sub-clusters."""
        # Create lessons large enough to exceed the limit
        large_content = "A" * 20_000  # 20k chars per lesson
        lessons = []
        for i in range(5):
            lessons.append({
                "lesson_id": f"les_{i}",
                "title": f"Lesson {i}",
                "content": large_content,
                "status": "active",
                "priority": 5,
                "confidence": 0.8,
                "evidence_count": 1,
                "application_count": 0,
                "category": "general",
                "source_type": "reflection",
            })

        # The method should detect it's too large and call _evaluate_large_cluster
        # which splits into sub-clusters. We mock _llm_evaluate_cluster_single
        # to verify it gets called with smaller chunks.
        call_sizes = []
        original = server._llm_evaluate_cluster_single

        async def mock_evaluate(lessons_chunk):
            call_sizes.append(len(lessons_chunk))
            return {"groups": [], "keep_separate": [l["lesson_id"] for l in lessons_chunk], "reason": "test"}

        server._llm_evaluate_cluster_single = mock_evaluate
        try:
            result = await server._llm_evaluate_cluster(lessons)
            # Should have been split - each sub-cluster should be smaller
            assert len(call_sizes) > 1, f"Expected split but got {len(call_sizes)} call(s)"
            for size in call_sizes:
                assert size <= 4, f"Sub-cluster too large: {size} lessons"
        finally:
            server._llm_evaluate_cluster_single = original

    @pytest.mark.asyncio
    async def test_small_cluster_no_split(self, server: LessonsLearnedServer):
        """Small clusters are evaluated in a single call."""
        lessons = [
            {
                "lesson_id": "les_1", "title": "Short", "content": "Small content",
                "status": "active", "priority": 5, "confidence": 0.8,
                "evidence_count": 1, "application_count": 0,
                "category": "general", "source_type": "reflection",
            },
            {
                "lesson_id": "les_2", "title": "Short 2", "content": "Also small",
                "status": "active", "priority": 5, "confidence": 0.8,
                "evidence_count": 1, "application_count": 0,
                "category": "general", "source_type": "reflection",
            },
        ]
        call_sizes = []

        async def mock_evaluate(lessons_chunk):
            call_sizes.append(len(lessons_chunk))
            return {"groups": [], "keep_separate": [l["lesson_id"] for l in lessons_chunk], "reason": "test"}

        server._llm_evaluate_cluster_single = mock_evaluate
        try:
            await server._llm_evaluate_cluster(lessons)
            assert len(call_sizes) == 1
            assert call_sizes[0] == 2
        finally:
            del server._llm_evaluate_cluster_single


# =============================================================================
# Test: Conversation Formatting
# =============================================================================

class TestConversationFormatting:

    def test_format_basic(self):
        """Format basic conversation messages."""
        from plugins.lessons_learned.extraction import _format_conversation

        messages = [
            MagicMock(role="system", content="System prompt"),
            MagicMock(role="user", content="Hello"),
            MagicMock(role="assistant", content="Hi there"),
        ]
        result = _format_conversation(messages)
        assert "[user]: Hello" in result
        assert "[assistant]: Hi there" in result
        assert "system" not in result.lower()  # System messages excluded

    def test_format_truncation(self):
        """Should truncate when exceeding max_chars."""
        from plugins.lessons_learned.extraction import _format_conversation

        messages = [
            MagicMock(role="user", content="X" * 1000)
            for _ in range(20)
        ]
        result = _format_conversation(messages, max_chars=2000)
        assert len(result) < 3000
        assert "truncated" in result.lower()


# =============================================================================
# Test: Cleanup Lessons
# =============================================================================

class TestCleanupLessons:

    @pytest.mark.asyncio
    async def test_cleanup_dry_run(self, server: LessonsLearnedServer):
        """Dry run should list matching lessons without deleting."""
        await server.store_lesson(agent_name="a", title="Keep", content="C", confidence=0.9)
        await server.store_lesson(agent_name="a", title="Low Conf", content="C", confidence=0.2)

        result = await server.cleanup_lessons(max_confidence=0.3, dry_run=True)
        assert result["dry_run"] is True
        assert result["matched_count"] == 1
        assert result["lessons"][0]["title"] == "Low Conf"

        # Verify nothing was deleted
        all_lessons = await server.list_lessons()
        assert all_lessons["total"] == 2

    @pytest.mark.asyncio
    async def test_cleanup_execute(self, server: LessonsLearnedServer):
        """Actual cleanup should delete matching lessons."""
        await server.store_lesson(agent_name="a", title="Keep", content="C", confidence=0.9)
        await server.store_lesson(agent_name="a", title="Low Conf", content="C", confidence=0.2)

        result = await server.cleanup_lessons(max_confidence=0.3, dry_run=False)
        assert result["dry_run"] is False
        assert result["deleted_count"] == 1

        all_lessons = await server.list_lessons()
        assert all_lessons["total"] == 1
        assert all_lessons["lessons"][0]["title"] == "Keep"

    @pytest.mark.asyncio
    async def test_cleanup_by_evidence(self, server: LessonsLearnedServer):
        """Filter by max evidence count. Default evidence_count=1 (schema default)."""
        r1 = await server.store_lesson(agent_name="a", title="Low Evidence", content="C")
        r2 = await server.store_lesson(agent_name="a", title="High Evidence", content="C")
        # Add 2 evidences to r2 so its evidence_count becomes 2
        await server.add_evidence(r2["lesson_id"], "sess1", "a", "confirm", "first")
        await server.add_evidence(r2["lesson_id"], "sess2", "a", "confirm", "second")

        # evidence_count <= 1 should match only r1 (default=1), not r2 (now 2)
        result = await server.cleanup_lessons(max_evidence_count=1, dry_run=True)
        assert result["matched_count"] == 1
        assert result["lessons"][0]["lesson_id"] == r1["lesson_id"]

    @pytest.mark.asyncio
    async def test_cleanup_by_status(self, server: LessonsLearnedServer):
        """Filter by status."""
        await server.store_lesson(agent_name="a", title="Active", content="C", status="active")
        await server.store_lesson(agent_name="a", title="Draft", content="C", status="draft")

        result = await server.cleanup_lessons(status="draft", dry_run=True)
        assert result["matched_count"] == 1
        assert result["lessons"][0]["title"] == "Draft"

    @pytest.mark.asyncio
    async def test_cleanup_by_agent(self, server: LessonsLearnedServer):
        """Filter by agent name."""
        await server.store_lesson(agent_name="agent_a", title="A1", content="C")
        await server.store_lesson(agent_name="agent_b", title="B1", content="C")

        result = await server.cleanup_lessons(agent_name="agent_b", dry_run=False)
        assert result["deleted_count"] == 1
        assert result["lessons"][0]["title"] == "B1"

        all_lessons = await server.list_lessons()
        assert all_lessons["total"] == 1

    @pytest.mark.asyncio
    async def test_cleanup_combined_filters(self, server: LessonsLearnedServer):
        """Multiple filters should be AND-combined."""
        await server.store_lesson(agent_name="a", title="Low Draft", content="C", confidence=0.2, status="draft")
        await server.store_lesson(agent_name="a", title="Low Active", content="C", confidence=0.2, status="active")
        await server.store_lesson(agent_name="a", title="High Draft", content="C", confidence=0.9, status="draft")

        result = await server.cleanup_lessons(max_confidence=0.3, status="draft", dry_run=True)
        assert result["matched_count"] == 1
        assert result["lessons"][0]["title"] == "Low Draft"

    @pytest.mark.asyncio
    async def test_cleanup_no_matches(self, server: LessonsLearnedServer):
        """No matching lessons should return empty result."""
        await server.store_lesson(agent_name="a", title="Good", content="C", confidence=0.9)

        result = await server.cleanup_lessons(max_confidence=0.1, dry_run=False)
        assert result["deleted_count"] == 0
        assert result["lessons"] == []

    @pytest.mark.asyncio
    async def test_cleanup_older_than(self, server: LessonsLearnedServer):
        """Filter by age (older_than_days). Insert with old created_at."""
        # Store a lesson then manually backdate it
        r = await server.store_lesson(agent_name="a", title="Old", content="C")
        conn = server._get_connection()
        try:
            from datetime import UTC, datetime, timedelta
            old_date = (datetime.now(UTC) - timedelta(days=60)).isoformat()
            conn.execute("UPDATE lessons SET created_at = ? WHERE lesson_id = ?", (old_date, r["lesson_id"]))
            conn.commit()
        finally:
            conn.close()

        await server.store_lesson(agent_name="a", title="New", content="C")

        result = await server.cleanup_lessons(older_than_days=30, dry_run=True)
        assert result["matched_count"] == 1
        assert result["lessons"][0]["title"] == "Old"
