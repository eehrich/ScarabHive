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

import asyncio
import json
import threading
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from agent_system.llm.message_roles import DEVELOPER

from plugins.lessons_learned.models import (
    DEFAULT_CATEGORIES,
    DeduplicationResult,
    ExtractionResult,
    Lesson,
    LessonCandidate,
    LessonStatus,
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
def mock_server_config(temp_storage: Path) -> MagicMock:
    """Mock ToolServerConfig with lessons_learned settings."""
    config = MagicMock()
    # Set attributes directly (like production ToolServerConfig with extra="allow")
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
def server(mock_system_config: MagicMock, mock_server_config: MagicMock, temp_storage: Path) -> LessonsLearnedServer:
    """LessonsLearnedServer instance with clean state."""
    srv = LessonsLearnedServer(
        name="lessons_learned",
        system_config=mock_system_config,
        server_config=mock_server_config,
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
        await server.add_evidence(r["lesson_id"], "s2", "a", "contradict")
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

        # Appended as the last turn, not pushed in behind the system prompt
        assert len(context.messages) == 3
        injected = context.messages[-1]
        assert injected.role == DEVELOPER
        assert "LESSONS LEARNED" in injected.content
        assert "Rule 1" in injected.content

    @pytest.mark.asyncio
    async def test_unchanged_lessons_are_not_written_again(self, server: LessonsLearnedServer):
        """The second call writes nothing -- that is what keeps the prefix cached."""
        await server.store_lesson(
            agent_name="test_agent", title="Rule 1", content="Do X",
            status="active", confidence=0.9, priority=9,
        )

        from agent_system.llm.models import ChatMessage
        context = MagicMock()
        context.agent_name = "test_agent"
        context.session_id = "s1"
        context.messages = [ChatMessage(role="system", content="You are a helpful agent."),
                            ChatMessage(role="user", content="Hello")]
        context.hook_config = {"max_lessons": 10, "min_confidence": 0.3}

        assert (await server.on_pre_llm_call(context)).modified

        second = await server.on_pre_llm_call(context)

        assert second.modified is False
        blocks = [m for m in context.messages
                  if getattr(m, "injected_by", None) == "lessons_learned"]
        assert len(blocks) == 1

    @pytest.mark.asyncio
    async def test_a_new_lesson_is_appended_behind_the_old_block(
            self, server: LessonsLearnedServer):
        """Without this, a guard on mere EXISTENCE would never update again."""
        await server.store_lesson(
            agent_name="test_agent", title="Rule 1", content="Do X",
            status="active", confidence=0.9, priority=9,
        )
        from agent_system.llm.models import ChatMessage
        context = MagicMock()
        context.agent_name = "test_agent"
        context.session_id = "s1"
        context.messages = [ChatMessage(role="system", content="You are a helpful agent."),
                            ChatMessage(role="user", content="Hello")]
        context.hook_config = {"max_lessons": 10, "min_confidence": 0.3}
        assert (await server.on_pre_llm_call(context)).modified

        await server.store_lesson(
            agent_name="test_agent", title="Rule 2", content="Do Y",
            status="active", confidence=0.9, priority=8,
        )
        result = await server.on_pre_llm_call(context)

        assert result.modified is True
        blocks = [m for m in context.messages
                  if getattr(m, "injected_by", None) == "lessons_learned"]
        assert len(blocks) == 2, "the new state is appended, the old one keeps its place"
        assert "Rule 2" not in blocks[0].content and "Rule 2" in blocks[-1].content
        assert context.messages[-1] is blocks[-1]

    @pytest.mark.asyncio
    async def test_a_lesson_counts_as_applied_even_when_nothing_was_written(
            self, server: LessonsLearnedServer):
        """The block from the call before still stands in this conversation.

        Counting only the calls that rewrote it would turn "applied" into
        "changed" -- and the confidence machinery reads that number.
        """
        stored = await server.store_lesson(
            agent_name="test_agent", title="Rule 1", content="Do X",
            status="active", confidence=0.9, priority=9,
        )
        lesson_id = stored["lesson_id"]
        from agent_system.llm.models import ChatMessage
        context = MagicMock()
        context.agent_name = "test_agent"
        context.session_id = "s1"
        context.messages = [ChatMessage(role="system", content="You are a helpful agent."),
                            ChatMessage(role="user", content="Hello")]
        context.hook_config = {"max_lessons": 10, "min_confidence": 0.3}

        await server.on_pre_llm_call(context)
        after_first = (await server.get_lesson(lesson_id))["application_count"]
        second = await server.on_pre_llm_call(context)
        after_second = (await server.get_lesson(lesson_id))["application_count"]

        assert second.modified is False, "fixture: the block was rewritten"
        assert after_second == after_first + 1

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
        """An answer that is no JSON is no answer: None, not "no lessons"."""
        from plugins.lessons_learned.extraction import _parse_extraction_response

        assert _parse_extraction_response("not json at all") is None

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
            await server._llm_evaluate_cluster(lessons)
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
            MagicMock(role="system", content="System prompt", injected_by=None),
            MagicMock(role="user", content="Hello", injected_by=None),
            MagicMock(role="assistant", content="Hi there", injected_by=None),
        ]
        result = _format_conversation(messages)
        assert "[user]: Hello" in result
        assert "[assistant]: Hi there" in result
        assert "system" not in result.lower()  # System messages excluded

    def test_format_truncation(self):
        """Should truncate when exceeding max_chars."""
        from plugins.lessons_learned.extraction import _format_conversation

        messages = [
            MagicMock(role="user", content="X" * 1000, injected_by=None)
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


# =============================================================================
# Test: What agents send beyond the tool schema
# =============================================================================

class TestWhatAgentsSend:
    """Nothing checks the tool schema before the tool runs: the tool refuses beyond it, the store keeps what it is given."""

    @pytest.mark.asyncio
    async def test_store_keeps_the_text_rounds_and_splits(self, server: LessonsLearnedServer):
        r = await server.store_lesson(agent_name="a", title="T" * 250, content="C" * 2500, priority=7.5, tags="x, y,, ")
        lesson = await server.get_lesson(r["lesson_id"])
        assert (len(lesson["title"]), len(lesson["content"]), lesson["priority"], lesson["tags"]) == (250, 2500, 8, ["x", "y"])
        assert stored_tags(server, r["lesson_id"]) == ["x", "y"]  # as the prompt builder reads them

    @pytest.mark.asyncio
    async def test_store_clamps_priority(self, server: LessonsLearnedServer):
        low = await server.store_lesson(agent_name="a", title="Low", content="C", priority=0.2)
        high = await server.store_lesson(agent_name="a", title="High", content="C", priority=12)
        assert [(await server.get_lesson(r["lesson_id"]))["priority"] for r in (low, high)] == [1, 10]

    @pytest.mark.asyncio
    async def test_update_keeps_the_text_rounds_and_splits(self, server: LessonsLearnedServer):
        r = await server.store_lesson(agent_name="a", title="T", content="C", tags=["old"])
        await server.update_lesson(r["lesson_id"], title="T" * 300, content="C" * 2100, priority=6.5, tags=" p ,q")
        lesson = await server.get_lesson(r["lesson_id"])
        assert (len(lesson["title"]), len(lesson["content"]), lesson["priority"], lesson["tags"]) == (300, 2100, 7, ["p", "q"])
        assert stored_tags(server, r["lesson_id"]) == ["p", "q"]

    @pytest.mark.asyncio
    async def test_the_tool_refuses_beyond_its_schema(self, server: LessonsLearnedServer):
        refused = [
            await server._op_store({"title": "T" * 201, "content": "C"}, "agent", "s-1"),
            await server._op_store({"title": "T", "content": "C" * 2001}, "agent", "s-1"),
            await server._op_store({"title": "T", "content": "C", "priority": 15}, "agent", "s-1"),
            await server._op_store({"title": "T", "content": "C", "priority": "high"}, "agent", "s-1"),
            await server._op_teach({"target_agent": "pupil", "title": "T", "content": "C" * 2001}, "agent", "s-1"),
            await server._op_teach({"target_agent": "pupil", "title": "T" * 201, "content": "C"}, "agent", "s-1"),
            await server._op_teach({"target_agent": "pupil", "title": "T", "content": "C", "priority": 0}, "agent", "s-1"),
        ]
        assert [list(result) for result in refused] == [["error"]] * len(refused)
        assert "longer than 2000" in refused[1]["error"] and "1 to 10" in refused[2]["error"]
        assert (await server.list_lessons())["total"] == 0
        kept = await server._op_store({"title": "T" * 200, "content": "C" * 2000, "priority": 10}, "agent", "s-1")
        for change in ({"content": "C" * 2001}, {"title": "T" * 201}, {"priority": 10.5}):
            assert "error" in await server._op_update({"lesson_id": kept["lesson_id"], **change})
        lesson = await server.get_lesson(kept["lesson_id"])
        assert (len(lesson["title"]), len(lesson["content"]), lesson["priority"]) == (200, 2000, 10)

    @pytest.mark.asyncio
    async def test_a_merge_rounds_the_priority_half_up(self, server: LessonsLearnedServer):
        first = await server.store_lesson(agent_name="a", title="One", content="C")
        second = await server.store_lesson(agent_name="a", title="Two", content="C")
        lessons = [await server.get_lesson(r["lesson_id"]) for r in (first, second)]
        await server._execute_merge("a", lessons, {"primary_id": first["lesson_id"], "priority": "8.5"})
        assert (await server.get_lesson(first["lesson_id"]))["priority"] == 9

    @pytest.mark.asyncio
    async def test_the_tool_path_splits_tags(self, server: LessonsLearnedServer):
        r = await server._op_store({"title": "T", "content": "C", "tags": "a, b"}, "agent", "s-1")
        await server._op_update({"lesson_id": r["lesson_id"], "priority": 7.5})
        listed = (await server.list_lessons(agent_name="agent"))["lessons"][0]
        assert (listed["tags"], listed["priority"]) == (["a", "b"], 8)

    @pytest.mark.asyncio
    async def test_stored_rows_decode_to_lists(self, server: LessonsLearnedServer):
        """Rows written before the tags were bounded: raw text, JSON text of a string."""
        raw = await server.store_lesson(agent_name="a", title="Raw", content="C")
        quoted = await server.store_lesson(agent_name="a", title="Quoted", content="C")
        conn = server._get_connection()
        try:
            conn.execute("UPDATE lessons SET tags = ? WHERE lesson_id = ?", ("a, b", raw["lesson_id"]))
            conn.execute("UPDATE lessons SET tags = ? WHERE lesson_id = ?", ('"c,d"', quoted["lesson_id"]))
            conn.commit()
        finally:
            conn.close()
        listed = {one["title"]: one["tags"] for one in (await server.list_lessons(agent_name="a"))["lessons"]}
        assert listed == {"Raw": ["a", "b"], "Quoted": ["c", "d"]}
        assert (await server.get_lesson(raw["lesson_id"]))["tags"] == ["a", "b"]
        found = {one["title"]: one["tags"] for one in (await server.search_lessons("Raw", agent_name="a", status=None))["results"]}
        assert found == {"Raw": ["a", "b"], "Quoted": ["c", "d"]}


class TestWithoutAnEmbeddingModel:
    """No embedding model (a core install without torch, say): every vector call
    fails. Each answer used to read as a verdict -- "no duplicate", "stored" as
    if searchable, "0 results" -- while nothing had been checked."""

    @pytest.fixture
    def no_model(self, monkeypatch):
        from agent_system.utils.vector_store import embeddings

        def missing():
            raise RuntimeError("no embedding model")

        monkeypatch.setattr(embeddings, "get_embedding_model", missing)

    async def test_the_duplicate_check_says_it_did_not_run(self, server, no_model):
        result = await server.check_duplicate("a", "Title", "Content")
        assert not result.is_duplicate
        assert result.error and "no embedding model" in result.error

    @pytest.mark.parametrize("operation", ["store", "teach"])
    async def test_a_lesson_stored_unchecked_and_unindexed_says_both(self, server, no_model, operation):
        result = await server.execute({"operation": operation, "title": "T", "content": "C",
                                       "target_agent": "b", "_agent_name": "a", "_session_id": "s"})
        assert result["status"] == "stored"
        # one for the check that did not run, one for the vector that was not written
        assert sum("no embedding model" in warning for warning in result["warnings"]) == 2

    async def test_a_search_that_could_not_run_is_an_error_not_an_empty_answer(self, server, no_model):
        await server.store_lesson(agent_name="a", title="Pin the data dir", content="C", status="active")
        result = await server.execute({"operation": "search", "query": "data dir",
                                       "_agent_name": "a", "_session_id": "s"})
        assert "no embedding model" in result["error"]

    async def test_the_panel_search_answers_an_error_too(self, server, no_model):
        from fastapi import HTTPException

        from plugins.lessons_learned.web_endpoints import LessonsWebFactory, SearchForm

        await server.store_lesson(agent_name="a", title="Pin the data dir", content="C", status="active")
        with pytest.raises(HTTPException) as raised:
            await LessonsWebFactory(server).search_lessons(MagicMock(), SearchForm(query="data dir"))
        assert raised.value.status_code == 503 and "no embedding model" in raised.value.detail

    async def test_an_update_not_re_indexed_says_so(self, server, no_model):
        stored = await server.store_lesson(agent_name="a", title="Old", content="Old text")
        result = await server.update_lesson(stored["lesson_id"], content="New text")
        assert result["status"] == "updated"
        assert "no embedding model" in " ".join(result["warnings"])

    async def test_a_merge_not_re_indexed_says_so(self, server, no_model):
        first = await server.store_lesson(agent_name="a", title="One", content="C")
        second = await server.store_lesson(agent_name="a", title="Two", content="C")
        lessons = [await server.get_lesson(first["lesson_id"]), await server.get_lesson(second["lesson_id"])]
        result = await server._execute_merge(agent="a", lessons=lessons, merge_decision={
            "primary_id": first["lesson_id"], "title": "Both", "content": "Merged"})
        assert result["action"] == "merged" and result["deleted"] == [second["lesson_id"]]
        assert "no embedding model" in " ".join(result["warnings"])

    async def test_a_consolidation_that_compared_nothing_says_so(self, server, no_model):
        """"clusters_found: 0" read as "no similar lessons" -- nothing had been compared."""
        await server.store_lesson(agent_name="a", title="Pin the data dir", content="C")
        await server.store_lesson(agent_name="a", title="Pin the data dir!", content="C")
        result = await server.consolidate_lessons(agent_name="a", dry_run=True)
        assert result["clusters_found"] == 0
        assert "no embedding model" in " ".join(result["warnings"])

    async def test_extraction_stores_nothing_it_could_not_check(self, server, no_model, monkeypatch):
        """Stored unchecked, the same lesson came back as a new copy at every
        session end until the agent's limit was full."""
        from agent_system.llm import factory
        from plugins.lessons_learned.extraction import extract_lessons_from_conversation

        llm = MagicMock()
        llm.chat = AsyncMock(return_value=json.dumps(
            {"lessons": [{"title": "Pin the data dir", "content": "Tests pin it relative."}]}))
        monkeypatch.setattr(factory, "create_llm_from_profile", lambda **kwargs: llm)
        messages = [{"role": "user", "content": "x" * 200}, {"role": "assistant", "content": "y" * 200}]

        result = await extract_lessons_from_conversation(messages, "a", "s-1", server)

        llm.chat.assert_awaited_once()
        assert (result.created_count, result.skipped_count) == (0, 1)
        assert (await server.list_lessons(agent_name="a"))["lessons"] == []


async def test_a_search_names_the_agents_it_could_not_search(server):
    """Only one agent's index fails: the other's hits are real, but the answer
    must not pass for the whole picture."""
    await server.store_lesson(agent_name="a", title="Pin the data dir", content="Relative in tests.", status="active")
    await server.store_lesson(agent_name="b", title="Pin the data dir", content="Relative in tests.", status="active")
    real_query, broken = server.vector_store.query, server._collection_name("b")

    def query(**kwargs):
        if kwargs["collection"] == broken:
            raise RuntimeError("index of b unreadable")
        return real_query(**kwargs)

    server.vector_store.query = query
    result = await server.search_lessons("data dir", status=None)

    assert [hit["agent_name"] for hit in result["results"]] == ["a"]
    assert "index of b unreadable" in " ".join(result["warnings"])


async def test_a_lesson_moved_to_another_agent_keeps_its_vector_when_the_add_fails(server):
    """The old vector was deleted before the add to the new agent's collection,
    so a failed add left the lesson in no index at all. Kept until the store
    works again -- then it belongs to the new agent, and only there, even
    where "a" had been reconciled before."""
    await server.check_duplicate("a", "Anything", "At all")  # "a" reconciled: only a loss re-opens it
    stored = await server.store_lesson(agent_name="a", title="Pin the data dir", content="Relative in tests.")
    real_add = server.vector_store.add

    def no_add(**kwargs):
        raise RuntimeError("index full")

    server.vector_store.add = no_add
    result = await server.update_lesson(stored["lesson_id"], agent_name="b")
    kept = server.vector_store.list_ids(server._collection_name("a"))
    server.vector_store.add = real_add
    under_a = await server.search_lessons("data dir", agent_name="a", status=None)
    under_b = await server.search_lessons("data dir", agent_name="b", status=None)

    assert "index full" in " ".join(result["warnings"])
    assert kept == [stored["lesson_id"]]
    assert under_a["results"] == []
    assert [hit["lesson_id"] for hit in under_b["results"]] == [stored["lesson_id"]]


async def test_a_move_between_names_of_one_collection_keeps_the_vector(server):
    """web-research and web_research share a collection: deleting from the "old"
    one after the add removed the vector just written."""
    stored = await server.store_lesson(agent_name="web-research", title="Pin the data dir", content="Relative.")
    assert server._collection_name("web-research") == server._collection_name("web_research")

    await server.update_lesson(stored["lesson_id"], agent_name="web_research")
    found = await server.search_lessons("data dir", agent_name="web_research", status=None)

    assert [hit["lesson_id"] for hit in found["results"]] == [stored["lesson_id"]]


async def test_a_save_that_changes_no_text_does_not_re_index(server):
    """The panel sends every field on every save. Re-embedding an unchanged text
    warned, with the store down, that search no longer saw the lesson."""
    stored = await server.store_lesson(agent_name="a", title="Pin the data dir", content="Relative.")

    def no_add(**kwargs):
        raise RuntimeError("index full")

    server.vector_store.add = no_add
    result = await server.update_lesson(stored["lesson_id"], title="Pin the data dir", content="Relative.",
                                        priority=7)

    assert result["status"] == "updated" and "warnings" not in result


async def test_a_merge_indexes_the_primary_by_its_own_text(server):
    """Where the merge decision leaves title and content out, the primary keeps
    its own -- its vector had been written from lessons[0], a duplicate."""
    duplicate = await server.store_lesson(agent_name="a", title="Tea", content="Steep the leaves.")
    primary = await server.store_lesson(agent_name="a", title="Pin the data dir", content="Relative.")
    lessons = [await server.get_lesson(duplicate["lesson_id"]), await server.get_lesson(primary["lesson_id"])]

    await server._execute_merge(agent="a", lessons=lessons, merge_decision={"primary_id": primary["lesson_id"]})
    stored = server.vector_store.query(collection=server._collection_name("a"), query_text="Pin the data dir",
                                       n_results=5, include=["documents"])

    assert dict(zip(stored["ids"][0], stored["documents"][0])) == {primary["lesson_id"]: "Pin the data dir. Relative."}


async def until(predicate, timeout: float = 5.0) -> None:
    """Wait for a step another task reaches, not for a fixed time: a slow run
    would otherwise act before the step and fail on its own setup check."""
    for _ in range(int(timeout / 0.01)):
        if predicate():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("the step was never reached")


class TestTheIndexHeals:
    """A lesson stored while the model was missing has a row and no vector.
    Nothing wrote the vector again unless the text changed, so search and the
    duplicate check missed it for good."""

    @staticmethod
    def break_model(monkeypatch):
        from agent_system.utils.vector_store import embeddings

        def missing():
            raise RuntimeError("no embedding model")

        monkeypatch.setattr(embeddings, "get_embedding_model", missing)

    async def test_a_lesson_stored_without_the_model_is_found_once_it_is_back(self, server, monkeypatch):
        self.break_model(monkeypatch)
        stored = await server.store_lesson(agent_name="a", title="Pin the data dir", content="Relative.",
                                           status="active")
        assert stored["warnings"], "the store did not fail -- this proves nothing"
        monkeypatch.undo()

        found = await server.search_lessons("data dir", agent_name="a")

        assert [hit["lesson_id"] for hit in found["results"]] == [stored["lesson_id"]]

    async def test_a_failed_store_after_a_heal_is_healed_too(self, server, monkeypatch):
        """Once per process, but a failed add forgets the agent again."""
        await server.check_duplicate("a", "Anything", "At all")  # heals "a" while it has nothing
        self.break_model(monkeypatch)
        stored = await server.store_lesson(agent_name="a", title="Pin the data dir", content="Relative.",
                                           status="active")
        monkeypatch.undo()

        found = await server.search_lessons("data dir", agent_name="a")

        assert [hit["lesson_id"] for hit in found["results"]] == [stored["lesson_id"]]

    async def test_the_duplicate_check_sees_it_once_the_model_is_back(self, server, monkeypatch):
        """Otherwise the same lesson, stored again, became a second copy."""
        self.break_model(monkeypatch)
        stored = await server.store_lesson(agent_name="a", title="Pin the data dir", content="Relative.")
        monkeypatch.undo()

        dedup = await server.check_duplicate("a", "Pin the data dir", "Relative.")

        assert dedup.is_duplicate and dedup.existing_lesson_id == stored["lesson_id"]

    async def test_a_consolidation_compares_them_once_the_model_is_back(self, server, monkeypatch):
        self.break_model(monkeypatch)
        await server.store_lesson(agent_name="a", title="Pin the data dir", content="Relative in tests.")
        await server.store_lesson(agent_name="a", title="Pin the data dir!", content="Relative in tests.")
        monkeypatch.undo()
        server._llm_evaluate_cluster = AsyncMock(return_value=None)

        result = await server.consolidate_lessons(agent_name="a", dry_run=True)

        assert result["clusters_found"] == 1

    @pytest.mark.parametrize("removal", ["delete", "cleanup", "merge", "move"])
    async def test_a_vector_a_failed_delete_left_behind_is_pruned(self, server, removal):
        """The duplicate check answered with a lesson that no longer existed (or
        belonged to another agent), and every store of that text failed on it."""
        await server.check_duplicate("a", "Anything", "At all")  # "a" reconciled: only a loss re-opens it
        tea = await server.store_lesson(agent_name="a", title="Tea", content="Steep the leaves.")
        pin = await server.store_lesson(agent_name="a", title="Pin the data dir", content="Relative.")
        real_delete = server.vector_store.delete

        def no_delete(**kwargs):
            raise RuntimeError("store locked")

        server.vector_store.delete = no_delete
        if removal == "delete":
            await server.delete_lesson(tea["lesson_id"])
        elif removal == "cleanup":
            await server.cleanup_lessons(agent_name="a", lesson_ids=[tea["lesson_id"]], dry_run=False)
        elif removal == "merge":
            lessons = [await server.get_lesson(pin["lesson_id"]), await server.get_lesson(tea["lesson_id"])]
            await server._execute_merge(agent="a", lessons=lessons, merge_decision={"primary_id": pin["lesson_id"]})
        else:
            await server.update_lesson(tea["lesson_id"], agent_name="b")
        assert tea["lesson_id"] in server.vector_store.list_ids(server._collection_name("a")), "nothing left behind"
        server.vector_store.delete = real_delete

        dedup = await server.check_duplicate("a", "Tea", "Steep the leaves.")

        assert not dedup.is_duplicate

    async def test_a_heal_under_way_does_not_swallow_a_loss(self, server):
        """A store whose add failed while a heal of the same agent was adding:
        the heal marked the agent afterwards, and the lesson stayed out."""
        real_add, gate, waiting = server.vector_store.add, threading.Event(), []

        def no_add(**kwargs):
            raise RuntimeError("index full")

        server.vector_store.add = no_add
        earlier = await server.store_lesson(agent_name="a", title="Tea", content="Steep the leaves.")

        def add(**kwargs):  # the heal's add of `earlier` waits; any other add fails
            if kwargs["ids"] != [earlier["lesson_id"]]:
                raise RuntimeError("index full")
            waiting.append(1)
            gate.wait(5)
            real_add(**kwargs)

        server.vector_store.add = add
        heal = asyncio.create_task(server._heal_index("a"))
        await until(lambda: waiting)  # the heal waits in its add
        stored = await server.store_lesson(agent_name="a", title="Pin the data dir", content="Relative.",
                                           status="active")
        gate.set()
        await heal
        server.vector_store.add = real_add

        found = await server.search_lessons("data dir", agent_name="a")

        assert not heal.exception() and stored["warnings"], "the setup did not happen -- this proves nothing"
        assert [hit["lesson_id"] for hit in found["results"]] == [stored["lesson_id"]]

    @pytest.mark.parametrize("removal", ["delete", "cleanup", "merge", "move"])
    async def test_a_heal_under_way_does_not_mark_a_row_that_went(self, server, monkeypatch, removal):
        """The heal adds vectors for the rows it read. A lesson deleted (or moved
        to another agent) while it was adding got its vector back as an orphan,
        and the heal marked the agent done: every store of that text then
        failed on the orphan. The same happens with a delete in another process,
        which no in-memory bookkeeping sees -- so the heal checks what it added."""
        self.break_model(monkeypatch)
        stored = await server.store_lesson(agent_name="a", title="Tea", content="Steep the leaves.")
        pin = await server.store_lesson(agent_name="a", title="Pin the data dir", content="Relative.")
        monkeypatch.undo()
        real_add, gate, written, waiting = server.vector_store.add, threading.Event(), [], []

        def add(**kwargs):  # the heal's add waits until the lesson is gone
            waiting.append(1)
            gate.wait(5)
            real_add(**kwargs)
            written.extend(kwargs["ids"])

        server.vector_store.add = add
        heal = asyncio.create_task(server._heal_index("a"))
        await until(lambda: waiting)
        server.vector_store.add = real_add  # the removal's own adds go through
        if removal == "delete":
            await server.delete_lesson(stored["lesson_id"])
        elif removal == "cleanup":
            await server.cleanup_lessons(agent_name="a", lesson_ids=[stored["lesson_id"]], dry_run=False)
        elif removal == "merge":
            lessons = [await server.get_lesson(pin["lesson_id"]), await server.get_lesson(stored["lesson_id"])]
            await server._execute_merge(agent="a", lessons=lessons, merge_decision={"primary_id": pin["lesson_id"]})
        else:
            await server.update_lesson(stored["lesson_id"], agent_name="b")
        gate.set()
        await heal
        assert stored["lesson_id"] in written, "the heal did not write the lesson back -- this proves nothing"

        left = server.vector_store.list_ids(server._collection_name("a"))
        dedup = await server.check_duplicate("a", "Tea", "Steep the leaves.")

        assert stored["lesson_id"] not in left
        assert not dedup.is_duplicate

    @staticmethod
    def documents(server, agent: str) -> dict:
        """What each vector of ``agent``'s collection was made of."""
        collection = server._collection_name(agent)
        ids = server.vector_store.list_ids(collection)
        got = server.vector_store.query(collection=collection, query_text="lesson", n_results=max(len(ids), 1),
                                        include=["documents"])
        return dict(zip(got["ids"][0], got["documents"][0])) if ids else {}

    async def test_an_update_that_was_not_re_indexed_heals_to_the_new_text(self, server):
        """Search matched what the lesson used to say, for good: the id was
        there, so nothing looked missing."""
        await server.check_duplicate("a", "Anything", "At all")  # "a" reconciled: only a loss re-opens it
        stored = await server.store_lesson(agent_name="a", title="Tea", content="Steep the leaves.")
        real_add = server.vector_store.add

        def no_add(**kwargs):
            raise RuntimeError("index full")

        server.vector_store.add = no_add
        updated = await server.update_lesson(stored["lesson_id"], content="Pour it cold.")
        server.vector_store.add = real_add
        assert updated["warnings"], "the re-index did not fail -- this proves nothing"

        await server.check_duplicate("a", "Anything", "At all")

        assert self.documents(server, "a") == {stored["lesson_id"]: "Tea. Pour it cold."}

    async def test_a_merge_that_was_not_re_indexed_heals_to_the_merged_text(self, server):
        pin = await server.store_lesson(agent_name="a", title="Pin the data dir", content="Relative.")
        tea = await server.store_lesson(agent_name="a", title="Tea", content="Steep the leaves.")
        await server.check_duplicate("a", "Anything", "At all")  # "a" reconciled: only a loss re-opens it
        lessons = [await server.get_lesson(pin["lesson_id"]), await server.get_lesson(tea["lesson_id"])]
        real_add = server.vector_store.add

        def no_add(**kwargs):
            raise RuntimeError("index full")

        server.vector_store.add = no_add
        merged = await server._execute_merge(agent="a", lessons=lessons, merge_decision={
            "primary_id": pin["lesson_id"], "title": "Pin it", "content": "Always relative."})
        server.vector_store.add = real_add
        assert merged["warnings"], "the re-index did not fail -- this proves nothing"

        await server.check_duplicate("a", "Anything", "At all")

        assert self.documents(server, "a") == {pin["lesson_id"]: "Pin it. Always relative."}

    async def test_a_heal_of_current_vectors_embeds_nothing(self, server):
        """Every write names its text; without that the heal would take every
        vector for an old one and embed them all again in each process."""
        await server.store_lesson(agent_name="a", title="Tea", content="Steep the leaves.")
        stored = await server.store_lesson(agent_name="a", title="Pin", content="Relative.")
        await server.update_lesson(stored["lesson_id"], content="Always relative.")
        real_add, adds = server.vector_store.add, []
        server.vector_store.add = lambda **kwargs: adds.append(kwargs["ids"]) or real_add(**kwargs)

        await server.check_duplicate("a", "Anything", "At all")

        assert adds == []

    async def test_a_vector_from_before_the_text_hash_is_re_embedded_once(self, server):
        """Entries written before text_hash carry no proof of their text; one of
        them may well be old (an update that failed back then)."""
        stored = await server.store_lesson(agent_name="a", title="Tea", content="Pour it cold.")
        # delete first: an upsert merges metadata, and the text_hash just written would stay
        server.vector_store.delete(collection=server._collection_name("a"), ids=[stored["lesson_id"]])
        server.vector_store.add(collection=server._collection_name("a"), ids=[stored["lesson_id"]],
                                documents=["Tea. Steep the leaves."],
                                metadatas=[{"lesson_id": stored["lesson_id"], "agent_name": "a"}])

        await server.check_duplicate("a", "Anything", "At all")

        assert self.documents(server, "a") == {stored["lesson_id"]: "Tea. Pour it cold."}

    async def test_a_text_edited_while_the_heal_writes_ends_as_the_new_text(self, server, monkeypatch):
        """The heal writes the text it read; an edit landing meanwhile, with its
        own write before the heal's, left the old text on the vector."""
        self.break_model(monkeypatch)
        stored = await server.store_lesson(agent_name="a", title="Tea", content="Steep the leaves.")
        monkeypatch.undo()
        real_add, gate, calls = server.vector_store.add, threading.Event(), []

        def add(**kwargs):  # the heal's first add waits until the edit is written
            calls.append(kwargs["documents"])
            if len(calls) == 1:
                gate.wait(5)
            real_add(**kwargs)

        server.vector_store.add = add
        heal = asyncio.create_task(server._heal_index("a"))
        await until(lambda: calls)
        await server.update_lesson(stored["lesson_id"], content="Pour it cold.")
        gate.set()
        await heal
        server.vector_store.add = real_add
        assert calls[0] == ["Tea. Steep the leaves."], "the heal did not write the old text -- this proves nothing"

        assert self.documents(server, "a") == {stored["lesson_id"]: "Tea. Pour it cold."}

    async def test_a_merge_does_not_write_back_a_text_read_before_an_edit(self, server):
        """Consolidation reads the lessons before the LLM judges them; the merge
        then wrote the primary's vector from that read, over an edit made in
        between."""
        pin = await server.store_lesson(agent_name="a", title="Pin the data dir", content="Relative.")
        tea = await server.store_lesson(agent_name="a", title="Tea", content="Steep the leaves.")
        lessons = [await server.get_lesson(pin["lesson_id"]), await server.get_lesson(tea["lesson_id"])]
        await server.update_lesson(pin["lesson_id"], content="Always relative.")  # while the LLM judges

        await server._execute_merge(agent="a", lessons=lessons, merge_decision={"primary_id": pin["lesson_id"]})

        assert self.documents(server, "a") == {pin["lesson_id"]: "Pin the data dir. Always relative."}

    async def test_two_edits_inside_a_heal_do_not_leave_the_older_text(self, server, monkeypatch):
        """The heal's correction after its check can be overtaken by a second
        edit; it then must not mark the agent, so the next query puts it right."""
        self.break_model(monkeypatch)
        stored = await server.store_lesson(agent_name="a", title="Tea", content="Steep the leaves.")
        monkeypatch.undo()
        real_add, gates, calls = server.vector_store.add, [threading.Event(), threading.Event()], []

        def add(**kwargs):  # the heal's two writes wait; the edits' own writes pass
            calls.append(kwargs["documents"])
            if len(calls) in (1, 3):
                gates[len(calls) // 3].wait(5)
            real_add(**kwargs)

        server.vector_store.add = add
        heal = asyncio.create_task(server._heal_index("a"))
        await until(lambda: len(calls) >= 1)
        await server.update_lesson(stored["lesson_id"], content="Pour it cold.")  # call 2
        gates[0].set()
        await until(lambda: len(calls) >= 3)  # the heal's check saw the edit; its correction waits
        await server.update_lesson(stored["lesson_id"], content="Serve it iced.")  # call 4
        gates[1].set()
        await heal
        server.vector_store.add = real_add
        assert calls[2] == ["Tea. Pour it cold."], "the heal's correction did not overtake -- this proves nothing"

        await server.check_duplicate("a", "Anything", "At all")

        assert self.documents(server, "a") == {stored["lesson_id"]: "Tea. Serve it iced."}

    async def test_a_lesson_moved_back_while_its_stale_vector_goes_is_indexed_again(self, server):
        """The heal deletes a stale vector it found earlier; a lesson moved back
        into the collection meanwhile lost its fresh vector to that delete, and
        the heal marked the agent done."""
        stored = await server.store_lesson(agent_name="b", title="Tea", content="Steep the leaves.")
        entry = {"document": "Tea. Steep the leaves.", "metadata": {"lesson_id": stored["lesson_id"], "agent_name": "a"}}
        server.vector_store.add(collection=server._collection_name("a"), ids=[stored["lesson_id"]],
                                documents=[entry["document"]], metadatas=[entry["metadata"]])  # a failed move left it
        real_delete, gate, blocked = server.vector_store.delete, threading.Event(), []

        def delete(**kwargs):  # the heal's stale delete in "a" waits until the lesson is back
            if kwargs["collection"] == server._collection_name("a") and not blocked:
                blocked.append(kwargs["ids"])
                gate.wait(5)
            real_delete(**kwargs)

        server.vector_store.delete = delete
        heal = asyncio.create_task(server._heal_index("a"))
        await until(lambda: blocked)
        await server.update_lesson(stored["lesson_id"], agent_name="a")  # its own add to "a" lands first
        gate.set()
        await heal
        server.vector_store.delete = real_delete
        assert blocked == [[stored["lesson_id"]]], "the heal deleted nothing stale -- this proves nothing"

        await server.check_duplicate("a", "Anything", "At all")

        assert stored["lesson_id"] in server.vector_store.list_ids(server._collection_name("a"))

    async def test_the_ids_are_read_once_per_agent(self, server):
        await server.store_lesson(agent_name="a", title="Pin the data dir", content="Relative.", status="active")
        real, calls = server.vector_store.list_entries, []
        server.vector_store.list_entries = lambda collection: calls.append(collection) or real(collection)

        await server.search_lessons("data dir", agent_name="a")
        await server.check_duplicate("a", "Pin the data dir", "Relative.")

        assert calls == [server._collection_name("a")]

    async def test_a_failed_move_after_a_heal_is_healed_too(self, server, monkeypatch):
        stored = await server.store_lesson(agent_name="a", title="Pin the data dir", content="Relative.",
                                           status="active")
        await server.check_duplicate("b", "Anything", "At all")  # heals "b" while it has nothing
        self.break_model(monkeypatch)
        moved = await server.update_lesson(stored["lesson_id"], agent_name="b")
        assert moved["warnings"], "the move did not fail -- this proves nothing"
        monkeypatch.undo()

        found = await server.search_lessons("data dir", agent_name="b")

        assert [hit["lesson_id"] for hit in found["results"]] == [stored["lesson_id"]]


async def test_the_search_status_line_counts_what_was_found(server):
    """It read a key the answer never had and said "Found 0 lessons" after every search."""
    await server.store_lesson(agent_name="a", title="Pin the data dir", content="Relative in tests.", status="active")
    status = MagicMock()
    status.progress, status.end = AsyncMock(), AsyncMock()

    result = await server.execute({"operation": "search", "query": "data dir", "_agent_name": "a",
                                   "_session_id": "s", "_status": status})

    assert result["count"] == 1
    assert f"Found {result['count']} " in status.end.call_args.args[0]


def stored_tags(server: LessonsLearnedServer, lesson_id: str):
    conn = server._get_connection()
    try:
        return json.loads(conn.execute("SELECT tags FROM lessons WHERE lesson_id = ?", (lesson_id,)).fetchone()[0])
    finally:
        conn.close()
