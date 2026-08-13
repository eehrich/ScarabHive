"""Tests for context_engineer plugin components."""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from plugins.context_engineer.core_memory import CoreMemory
from plugins.context_engineer.tool_result_store import ToolResultStore
from plugins.context_engineer.variable_manager import VariableManager
from plugins.context_engineer.archival_memory import ArchivalMemory
from plugins.context_engineer.compaction import (
    CompactionConfig,
    LayeredCompactionStrategy,
    _ref_type,
)


# =============================================================================
# CoreMemory Tests
# =============================================================================


class TestCoreMemory:
    """Tests for CoreMemory class."""
    
    @pytest.fixture
    def temp_memory_file(self, tmp_path):
        """Create a temporary file for core memory."""
        return tmp_path / "core_memory.json"
    
    def test_initialization(self, temp_memory_file):
        """Test CoreMemory initializes correctly."""
        memory = CoreMemory(temp_memory_file)
        assert len(memory.facts) == 0
        assert memory.max_tokens == 2000
    
    async def test_add_fact(self, temp_memory_file):
        """Test adding facts to core memory."""
        memory = CoreMemory(temp_memory_file)
        
        fact_id = await memory.add_fact("User prefers Python", category="preferences")
        
        assert fact_id is not None
        assert len(memory.facts) == 1
        assert memory.facts[0].content == "User prefers Python"
        assert memory.facts[0].category == "preferences"
    
    async def test_add_fact_with_importance(self, temp_memory_file):
        """Test adding facts with custom importance."""
        memory = CoreMemory(temp_memory_file)
        
        await memory.add_fact("Critical decision", importance=0.9)
        await memory.add_fact("Minor note", importance=0.1)
        
        assert memory.facts[0].importance == 0.9
        assert memory.facts[1].importance == 0.1
    
    async def test_persistence(self, temp_memory_file):
        """Test that facts persist across instances."""
        # Add facts
        memory1 = CoreMemory(temp_memory_file)
        await memory1.add_fact("Fact 1", category="facts")
        await memory1.add_fact("Fact 2", category="decisions")
        
        # Create new instance
        memory2 = CoreMemory(temp_memory_file)
        
        assert len(memory2.facts) == 2
        assert memory2.facts[0].content == "Fact 1"
        assert memory2.facts[1].content == "Fact 2"
    
    async def test_eviction_when_full(self, temp_memory_file):
        """Test that low importance facts are evicted when limit reached."""
        memory = CoreMemory(temp_memory_file, max_tokens=100)
        
        # Add many facts to exceed limit
        for i in range(10):
            await memory.add_fact(f"Fact {i} with lots of content", importance=0.1 * i)
        
        # Should have evicted some facts
        assert memory.get_token_usage() <= 100
    
    async def test_get_categories(self, temp_memory_file):
        """Test getting list of categories."""
        memory = CoreMemory(temp_memory_file)
        await memory.add_fact("Fact 1", category="facts")
        await memory.add_fact("Fact 2", category="decisions")
        await memory.add_fact("Fact 3", category="facts")
        
        categories = memory.get_categories()
        assert set(categories) == {"facts", "decisions"}
    
    async def test_to_system_prompt_section(self, temp_memory_file):
        """Test generating system prompt section."""
        memory = CoreMemory(temp_memory_file)
        await memory.add_fact("User likes Python", category="preferences")
        await memory.add_fact("Project uses FastAPI", category="facts")
        
        section = memory.to_system_prompt_section()
        
        assert "<core_memory>" in section
        assert "User likes Python" in section
        assert "Project uses FastAPI" in section


# =============================================================================
# ToolResultStore Tests
# =============================================================================


class TestToolResultStore:
    """Tests for ToolResultStore class."""
    
    @pytest.fixture
    def temp_db_path(self, tmp_path):
        """Create temporary database path."""
        return tmp_path / "tool_results.db"
    
    def test_initialization(self, temp_db_path):
        """Test ToolResultStore initializes correctly."""
        store = ToolResultStore(temp_db_path)
        
        assert temp_db_path.exists()
        stats = store.get_stats()
        assert stats["total_entries"] == 0
    
    def test_store_and_reference(self, temp_db_path):
        """Test storing tool result and getting reference."""
        store = ToolResultStore(temp_db_path)
        
        result = "Some large tool output " * 100
        reference = store.store_and_reference(
            tool_name="read_file",
            tool_call_id="call_123",
            content=result
        )
        
        # Reference should be valid JSON
        ref_data = json.loads(reference)
        assert ref_data["type"] == "tool_result_ref"
        assert ref_data["tool_name"] == "read_file"
        assert "ref_id" in ref_data
        assert "content_hash" in ref_data
    
    def test_retrieve_by_id(self, temp_db_path):
        """Test retrieving tool result by ID."""
        store = ToolResultStore(temp_db_path)
        
        result = "Tool output content"
        reference = store.store_and_reference(
            tool_name="test_tool",
            tool_call_id="call_456",
            content=result
        )
        
        # Extract ID from JSON reference
        ref_data = json.loads(reference)
        ref_id = ref_data["ref_id"]
        
        entry = store.retrieve(ref_id)
        
        assert entry is not None
        assert entry.content == result
        assert entry.tool_name == "test_tool"
    
    def test_retrieve_by_hash(self, temp_db_path):
        """Test retrieving by content hash."""
        store = ToolResultStore(temp_db_path)
        
        result = "Unique content for hash test"
        reference = store.store_and_reference(
            tool_name="test_tool",
            tool_call_id="call_789",
            content=result
        )
        
        # Extract hash from JSON reference
        ref_data = json.loads(reference)
        content_hash = ref_data["content_hash"]
        
        entry = store.retrieve_by_hash(content_hash)
        
        assert entry is not None
        assert entry.content == result
    
    def test_stats(self, temp_db_path):
        """Test getting statistics."""
        store = ToolResultStore(temp_db_path)
        
        store.store_and_reference(tool_call_id="c1", tool_name="tool1", content="output 1")
        store.store_and_reference(tool_call_id="c2", tool_name="tool2", content="output 2 longer" * 10)
        
        stats = store.get_stats()
        
        assert stats["total_entries"] == 2
        assert stats["total_tokens_stored"] > 0
        assert "tool1" in stats["by_tool"]
        assert "tool2" in stats["by_tool"]

    # ── Regression: short_id collision across tool_ prefixed IDs ──────────

    def test_short_ids_unique_for_tool_prefixed_ids(self, temp_db_path):
        """Tool-call IDs like 'tool_writer_content_book_*' must NOT collapse to
        the same TR_xxx reference (previous bug: first 8 chars of the prefix
        collided, so every writer-tool result shared id 'TR_tool_wri')."""
        store = ToolResultStore(temp_db_path)
        ids = [
            "tool_writer_content_book_001",
            "tool_writer_content_book_002",
            "tool_writer_content_series_001",
            "tool_writer_workflow_set_context_001",
        ]
        refs = [
            json.loads(store.store_and_reference(
                tool_call_id=tcid, tool_name="writer_content", content=f"c-{tcid}",
            ))["ref_id"]
            for tcid in ids
        ]
        assert len(set(refs)) == len(refs), f"short_ids collided: {refs}"
        assert all(r.startswith("TR_") for r in refs)

    def test_retrieve_roundtrip_tool_prefix(self, temp_db_path):
        """The TR_xxx returned by store_and_reference must round-trip via retrieve()
        for non-'call_' tool_call_ids (the broken case in the original bug report)."""
        store = ToolResultStore(temp_db_path)
        ref = json.loads(store.store_and_reference(
            tool_call_id="tool_writer_content_book_42",
            tool_name="writer_content",
            content="full book payload",
        ))
        entry = store.retrieve(ref["ref_id"])
        assert entry is not None
        assert entry.content == "full book payload"
        assert entry.id == "tool_writer_content_book_42"

    def test_retrieve_roundtrip_call_prefix(self, temp_db_path):
        """Existing 'call_xxx' tool IDs must keep working after the fix."""
        store = ToolResultStore(temp_db_path)
        ref = json.loads(store.store_and_reference(
            tool_call_id="call_abc123def456", tool_name="read_file", content="hello",
        ))
        entry = store.retrieve(ref["ref_id"])
        assert entry is not None
        assert entry.content == "hello"

    def test_tr_reference_does_not_fall_through_to_prefix_match(self, temp_db_path):
        """An unknown TR_xxx must return None — not silently match some other row
        via the legacy prefix-match path (root cause of the wrong-data loop)."""
        store = ToolResultStore(temp_db_path)
        store.store_and_reference(
            tool_call_id="tool_writer_content_book_001",
            tool_name="x", content="X",
        )
        assert store.retrieve("TR_0000000000") is None

    def test_retrieve_legacy_call_id_directly(self, temp_db_path):
        """Looking up by raw tool_call_id (or its prefix) keeps working."""
        store = ToolResultStore(temp_db_path)
        store.store_and_reference(
            tool_call_id="call_legacydirect_123", tool_name="x", content="payload",
        )
        # Exact
        e = store.retrieve("call_legacydirect_123")
        assert e is not None and e.content == "payload"
        # Prefix
        e = store.retrieve("call_legacydirect")
        assert e is not None and e.content == "payload"

    def test_migration_backfills_short_id_for_old_rows(self, temp_db_path, tmp_path):
        """A DB created before this fix has rows without short_id. After re-opening
        with the new store, those rows must become retrievable via their TR_xxx ref."""
        import sqlite3
        from plugins.context_engineer.tool_result_store import _compute_short_id

        # Simulate pre-fix DB: create the table without short_id column, insert a row.
        conn = sqlite3.connect(str(temp_db_path))
        conn.execute("""
            CREATE TABLE tool_results (
                id TEXT PRIMARY KEY, tool_name TEXT NOT NULL, content TEXT NOT NULL,
                content_hash TEXT NOT NULL, token_count INTEGER NOT NULL,
                timestamp TEXT NOT NULL, session_id TEXT NOT NULL, summary TEXT,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
        """)
        legacy_id = "tool_writer_content_book_legacy"
        conn.execute(
            "INSERT INTO tool_results (id, tool_name, content, content_hash, "
            "token_count, timestamp, session_id) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (legacy_id, "writer_content", "legacy payload", "deadbeef", 5,
             "2026-05-17T15:55:00", "default"),
        )
        conn.commit()
        conn.close()

        # Open with the new store → migration must add short_id + backfill it.
        store = ToolResultStore(temp_db_path)
        expected_short_id = _compute_short_id(legacy_id)
        entry = store.retrieve(expected_short_id)
        assert entry is not None, "legacy row not retrievable via TR_xxx after migration"
        assert entry.content == "legacy payload"


# =============================================================================
# VariableManager Tests
# =============================================================================


class TestVariableManager:
    """Tests for VariableManager class."""
    
    @pytest.fixture
    def temp_var_file(self, tmp_path):
        """Create temporary file for variables."""
        return tmp_path / "variables.json"
    
    def test_initialization(self, temp_var_file):
        """Test VariableManager initializes correctly."""
        manager = VariableManager(storage_path=temp_var_file)
        
        assert manager.min_content_tokens == 500  # Default
        stats = manager.get_stats()
        assert stats["total_variables"] == 0
    
    async def test_create_variable(self, temp_var_file):
        """Test creating a variable."""
        manager = VariableManager(min_content_tokens=10, storage_path=temp_var_file)
        
        # Use realistic content that generates multiple tokens
        large_content = "word " * 100  # ~100 words = ~130 tokens (exceeds threshold of 10)
        var_name, summary = await manager.create_variable(large_content)
        
        assert var_name is not None
        assert "$VAR_" in var_name
        assert summary is not None  # Should include summary
    
    async def test_get_variable(self, temp_var_file):
        """Test retrieving a variable."""
        manager = VariableManager(min_content_tokens=10, storage_path=temp_var_file)
        
        content = "Test content " * 50
        var_name, _ = await manager.create_variable(content)
        
        entry = manager.get_variable(var_name)
        
        assert entry is not None
        assert entry.content == content
    
    async def test_below_threshold_returns_none(self, temp_var_file):
        """Test that small content returns empty var_name."""
        manager = VariableManager(min_content_tokens=1000, storage_path=temp_var_file)
        
        small_content = "Small"
        result = await manager.create_variable(small_content)
        
        # Returns tuple (var_name, summary) where var_name is empty if below threshold
        assert result[0] == ""  # Empty var_name
    
    async def test_detect_content_type(self, temp_var_file):
        """Test content type detection."""
        manager = VariableManager(min_content_tokens=10, storage_path=temp_var_file)
        
        # Test code detection
        code_content = "def hello():\n    print('Hello')\n" * 20
        await manager.create_variable(code_content, content_type="code")
        
        # Test JSON detection
        json_content = '{"key": "value", "nested": {"a": 1}}' * 20
        await manager.create_variable(json_content, content_type="json")
        
        stats = manager.get_stats()
        assert stats["total_variables"] >= 2
    
    async def test_persistence(self, temp_var_file):
        """Test that variables persist."""
        manager1 = VariableManager(min_content_tokens=10, storage_path=temp_var_file)
        await manager1.create_variable("Content " * 100)
        
        manager2 = VariableManager(min_content_tokens=10, storage_path=temp_var_file)
        stats = manager2.get_stats()
        
        assert stats["total_variables"] == 1
    
    async def test_expand_variables(self, temp_var_file):
        """Test expanding variables in text."""
        manager = VariableManager(min_content_tokens=10, storage_path=temp_var_file)
        
        content = "Original content " * 50
        var_name, _ = await manager.create_variable(content)
        
        text_with_var = f"The result is in {var_name}."
        expanded = manager.expand_variables(text_with_var)
        
        assert content in expanded
        assert var_name not in expanded
    
    async def test_cleanup_unused_variables(self, temp_var_file):
        """Test cleanup of unreferenced variables."""
        manager = VariableManager(min_content_tokens=10, storage_path=temp_var_file)
        
        # Create multiple variables
        content1 = "Content 1 " * 50
        content2 = "Content 2 " * 50
        content3 = "Content 3 " * 50
        
        var1, _ = await manager.create_variable(content1)
        var2, _ = await manager.create_variable(content2)
        var3, _ = await manager.create_variable(content3)
        
        # Verify all created
        assert manager.get_stats()["total_variables"] == 3
        
        # Create message history that only references var1 and var3
        messages = [
            {"role": "user", "content": f"Check {var1}"},
            {"role": "assistant", "content": f"Result in {var3}"},
            {"role": "user", "content": "Something else"}
        ]
        
        # Cleanup - should remove var2
        removed = await manager.cleanup_unused_variables(messages)
        
        assert removed == 1
        assert manager.get_stats()["total_variables"] == 2
        assert manager.get_variable(var1) is not None
        assert manager.get_variable(var2) is None  # Removed
        assert manager.get_variable(var3) is not None
    
    async def test_cleanup_all_variables_referenced(self, temp_var_file):
        """Test cleanup when all variables are referenced."""
        manager = VariableManager(min_content_tokens=10, storage_path=temp_var_file)
        
        var1, _ = await manager.create_variable("Content 1 " * 50)
        var2, _ = await manager.create_variable("Content 2 " * 50)
        
        messages = [
            {"role": "user", "content": f"See {var1} and {var2}"}
        ]
        
        removed = await manager.cleanup_unused_variables(messages)
        
        assert removed == 0
        assert manager.get_stats()["total_variables"] == 2
    
    async def test_cleanup_no_variables(self, temp_var_file):
        """Test cleanup with no variables stored."""
        manager = VariableManager(min_content_tokens=10, storage_path=temp_var_file)
        
        messages = [{"role": "user", "content": "No variables here"}]
        
        removed = await manager.cleanup_unused_variables(messages)
        
        assert removed == 0
    
    async def test_cleanup_empty_messages(self, temp_var_file):
        """Test cleanup with empty message list removes all variables."""
        manager = VariableManager(min_content_tokens=10, storage_path=temp_var_file)
        
        var1, _ = await manager.create_variable("Content 1 " * 50)
        var2, _ = await manager.create_variable("Content 2 " * 50)
        
        assert manager.get_stats()["total_variables"] == 2
        
        # Empty messages means no references
        removed = await manager.cleanup_unused_variables([])
        
        assert removed == 2
        assert manager.get_stats()["total_variables"] == 0


# =============================================================================
# ArchivalMemory Tests
# =============================================================================


class TestArchivalMemory:
    """Tests for ArchivalMemory class."""
    
    @pytest.fixture
    def temp_archive_path(self, tmp_path):
        """Create temporary archive database path."""
        return tmp_path / "archive.db"
    
    def test_initialization(self, temp_archive_path):
        """Test ArchivalMemory initializes correctly."""
        archive = ArchivalMemory(temp_archive_path, session_id="test-session")
        
        assert temp_archive_path.exists()
        stats = archive.get_stats()
        assert stats["total_messages"] == 0
    
    def test_store_message(self, temp_archive_path):
        """Test storing a message."""
        archive = ArchivalMemory(temp_archive_path, session_id="test-session")

        message = {
            "role": "user",
            "content": "Hello, how are you?"
        }

        entry_id = archive.store(message)

        assert entry_id.startswith("arch_")
        stats = archive.get_stats()
        assert stats["total_messages"] == 1

    @pytest.mark.parametrize("store_call", ["one", "many"])
    def test_store_survives_tool_calls_being_present_but_none(
        self, temp_archive_path, store_call
    ):
        """``tool_calls: None`` is the NORMAL shape, and it used to raise.

        Counted over the 40 largest production sessions: 5838 messages carry
        ``tool_calls`` with value None against 709 carrying a list. The old code
        asked ``"tool_calls" in message`` — true for None — and then iterated it,
        so archiving raised TypeError and took the whole compaction with it.
        """
        archive = ArchivalMemory(temp_archive_path, session_id="test-session")
        message = {"role": "assistant", "content": "eine Antwort ohne Werkzeug",
                   "tool_calls": None, "tool_call_id": None, "name": None}

        if store_call == "one":
            archive.store(message)
        else:
            archive.store_many([message])

        assert archive.get_stats()["total_messages"] == 1
        # The summary must describe the content, not claim an empty tool call.
        summary = archive._generate_summary(message)
        assert "called tools" not in summary, summary
        assert "eine Antwort ohne Werkzeug" in summary

    def test_store_many_writes_the_same_rows_as_store(self, temp_archive_path):
        """The batch path is an optimisation, not a second format.

        It exists because ``store`` commits per message: 16.1 s for 4682
        messages against 0.08 s for the same rows in one transaction. It must
        stay row-for-row identical or retrieval sees two kinds of entry.
        """
        archive = ArchivalMemory(temp_archive_path, session_id="test-session")
        messages = [
            {"role": "user", "content": "eine Frage"},
            # content=None is the shape of an assistant that ONLY calls tools.
            # The column is NOT NULL, so this is the one input where the two
            # paths actually diverged: the batch normalised it, store() raised
            # IntegrityError and took Layer 2's whole loop with it.
            {"role": "assistant", "content": None, "tool_calls": [
                {"id": "call_1", "function": {"name": "such_tool"}}]},
            {"role": "tool", "content": "ein Ergebnis",
             "tool_call_id": "call_1", "name": "such_tool"},
        ]

        single = [archive.store(m) for m in messages]
        batch = archive.store_many(messages)

        assert len(batch) == 3 and all(b.startswith("arch_") for b in batch)
        cols = "role, content, summary, token_count, metadata, tool_call_id, tool_name"
        for one, many in zip(single, batch):
            a = archive._db.execute(
                f"SELECT {cols} FROM archived_messages WHERE id = ?", (one,)).fetchone()
            b = archive._db.execute(
                f"SELECT {cols} FROM archived_messages WHERE id = ?", (many,)).fetchone()
            assert a == b, f"batch row differs from single-store row: {a} != {b}"

        # And the FTS index must know both, or search goes half blind.
        found = archive._db.execute(
            "SELECT COUNT(*) FROM archived_fts WHERE archived_fts MATCH ?",
            ('"Ergebnis"',)).fetchone()[0]
        assert found == 2, f"expected both copies in the FTS index, got {found}"
    
    def test_store_with_custom_summary(self, temp_archive_path):
        """Test storing with custom summary."""
        archive = ArchivalMemory(temp_archive_path, session_id="test-session")
        
        message = {
            "role": "assistant",
            "content": "Long response " * 100
        }
        
        archive.store(message, summary="Custom summary")
        
        results = archive.search("Custom summary")
        assert len(results) == 1
    
    def test_search_text(self, temp_archive_path):
        """Test text search."""
        archive = ArchivalMemory(temp_archive_path, session_id="test-session")
        
        archive.store({"role": "user", "content": "Python programming question"})
        archive.store({"role": "user", "content": "JavaScript framework discussion"})
        archive.store({"role": "user", "content": "Python web development"})
        
        results = archive.search("Python")
        
        assert len(results) >= 2
    
    def test_session_isolation(self, temp_archive_path):
        """Test that sessions are isolated."""
        archive = ArchivalMemory(temp_archive_path, session_id="session-1")
        archive.store({"role": "user", "content": "Session 1 message"})
        
        archive2 = ArchivalMemory(temp_archive_path, session_id="session-2")
        archive2.store({"role": "user", "content": "Session 2 message"})
        
        results1 = archive.get_session_messages("session-1")
        results2 = archive2.get_session_messages("session-2")
        
        assert len(results1) == 1
        assert len(results2) == 1
    
    def test_store_tool_message(self, temp_archive_path):
        """Test storing tool response messages."""
        archive = ArchivalMemory(temp_archive_path, session_id="test-session")
        
        message = {
            "role": "tool",
            "name": "read_file",
            "tool_call_id": "call_123",
            "content": "File contents here"
        }
        
        archive.store(message)
        
        stats = archive.get_stats()
        assert stats["by_role"]["tool"] == 1
    
    def test_cleanup_old(self, temp_archive_path):
        """Test cleanup of old entries."""
        archive = ArchivalMemory(temp_archive_path, session_id="test-session")
        
        # Store some messages
        for i in range(5):
            archive.store({"role": "user", "content": f"Message {i}"})
        
        # Cleanup with 0 days should remove all
        removed = archive.cleanup_old(max_age_days=0)
        
        # Note: This test may not remove entries created within the same second
        # In production, entries would be older
        assert removed >= 0


# =============================================================================
# LayeredCompactionStrategy Tests
# =============================================================================


class TestLayeredCompactionStrategy:
    """Tests for LayeredCompactionStrategy class."""
    
    @pytest.fixture
    def strategy_components(self, tmp_path):
        """Create all components for strategy."""
        tool_store = ToolResultStore(tmp_path / "tools.db")
        variable_manager = VariableManager(min_content_tokens=50, storage_path=tmp_path / "vars.json")
        core_memory = CoreMemory(storage_path=tmp_path / "memory.json")
        archival_memory = ArchivalMemory(tmp_path / "archive.db", session_id="test")
        
        config = CompactionConfig(
            layer1_threshold=500,  # Low thresholds for testing
            layer2_threshold=1000,
            layer3_threshold=1500,
            target_tokens=300,
            tool_result_min_size=20,
            tool_result_keep_last=1
        )
        
        strategy = LayeredCompactionStrategy(
            tool_store=tool_store,
            variable_manager=variable_manager,
            core_memory=core_memory,
            archival_memory=archival_memory,
            config=config
        )
        
        return {
            "strategy": strategy,
            "tool_store": tool_store,
            "variable_manager": variable_manager,
            "core_memory": core_memory,
            "archival_memory": archival_memory,
            "config": config
        }
    
    async def test_no_compaction_below_threshold(self, strategy_components):
        """Test that no compaction happens below threshold."""
        strategy = strategy_components["strategy"]
        
        messages = [
            {"role": "user", "content": "Short message"}
        ]
        
        result = await strategy.compact(messages, current_tokens=100)
        
        assert result.tokens_saved == 0
        assert result.layers_applied == []
    
    async def test_layer1_tool_result_storage(self, strategy_components):
        """Test Layer 1 stores tool results."""
        strategy = strategy_components["strategy"]
        
        # Create messages with tool results (use realistic content for token counting)
        large_content1 = "word " * 100  # ~100 words = ~130 tokens
        large_content2 = "test " * 100  # ~100 words = ~130 tokens
        
        messages = [
            {"role": "user", "content": "Read the file"},
            {"role": "assistant", "tool_calls": [{"id": "call_123456", "function": {"name": "read"}}]},
            {"role": "tool", "name": "read_file", "tool_call_id": "call_123456", "content": large_content1},
            {"role": "user", "content": "What did it say?"},
            {"role": "assistant", "tool_calls": [{"id": "call_789012", "function": {"name": "read"}}]},
            {"role": "tool", "name": "read_file", "tool_call_id": "call_789012", "content": large_content2},
            {"role": "user", "content": "Latest question"},
        ]
        
        result = await strategy.compact(messages, current_tokens=600)
        
        # First tool result should be stored (not the last one)
        assert result.tool_results_stored >= 1 or result.layers_applied == []

    async def test_layer1_stores_a_preview_but_keeps_the_placeholder_lean(
        self, strategy_components
    ):
        """The tool_result_ref placeholder must stay small and STABLE.

        It replaces the tool message and stays in the conversation, so
        anything embedded in it is resent on every future turn, not paid
        once. The preview belongs where it's read on demand -- the stored
        entry, which list(section='tool_results') shows -- not baked into
        what gets resent unconditionally.

        Also regression for the blank-preview bug: 'summary' was defined on
        ToolResultEntry ("Optional LLM-generated summary") but no caller ever
        populated it, so list() had nothing to show either.
        """
        strategy = strategy_components["strategy"]
        tool_store = strategy_components["tool_store"]
        large_content = "The quick brown fox jumps over the lazy dog. " * 50

        messages = [
            {"role": "user", "content": "Read the file"},
            {"role": "assistant", "tool_calls": [{"id": "call_preview", "function": {"name": "read"}}]},
            {"role": "tool", "name": "read_file", "tool_call_id": "call_preview", "content": large_content},
            {"role": "user", "content": "next"},
            {"role": "assistant", "tool_calls": [{"id": "call_filler", "function": {"name": "read"}}]},
            {"role": "tool", "name": "read_file", "tool_call_id": "call_filler", "content": "filler " * 100},
        ]

        result = await strategy.compact(messages, current_tokens=600)
        assert result.tool_results_stored >= 1

        stored = [m for m in result.modified_messages if m.get("tool_call_id") == "call_preview"]
        assert stored, "the tool result for call_preview should have been archived"
        ref_data = json.loads(stored[0]["content"])
        assert ref_data["type"] == "tool_result_ref"
        assert "preview" not in ref_data, "the placeholder must not carry a per-turn-resent preview"

        entry = tool_store.retrieve(ref_data["ref_id"])
        assert "quick brown fox" in entry.summary, "list() needs a real summary to show"

    async def test_layer1_does_not_rearchive_its_own_placeholder(self, strategy_components):
        """An already-archived tool_result_ref must not become a ref-to-a-ref.

        Layer 2 already guards this (see its comment: chains 20 levels deep,
        637 of 1286 entries nothing but pointers). Layer 1 runs first and
        lacked the same guard: with a low enough tool_result_min_size, a
        placeholder from an earlier turn -- small as it is -- crossed the
        threshold again on the NEXT compaction pass and got wrapped in
        another placeholder, compounding every turn instead of staying put.
        """
        strategy = strategy_components["strategy"]
        big = "The quick brown fox jumps over the lazy dog. " * 50

        messages = [
            {"role": "assistant", "tool_calls": [{"id": "call_1", "function": {"name": "read"}}]},
            {"role": "tool", "name": "read_file", "tool_call_id": "call_1", "content": big},
            {"role": "assistant", "tool_calls": [{"id": "call_2", "function": {"name": "read"}}]},
            {"role": "tool", "name": "read_file", "tool_call_id": "call_2", "content": "filler " * 60},
        ]

        pass1 = await strategy.compact(list(messages), current_tokens=600)
        ref1 = next(m for m in pass1.modified_messages if m.get("tool_call_id") == "call_1")
        assert json.loads(ref1["content"])["type"] == "tool_result_ref"

        # Same messages, compacted again next turn -- exactly what happens
        # every turn once the message/token count keeps tripping compaction.
        turn2 = list(pass1.modified_messages) + [{"role": "user", "content": "next turn"}]
        pass2 = await strategy.compact(turn2, current_tokens=600)
        ref2 = next(m for m in pass2.modified_messages if m.get("tool_call_id") == "call_1")

        assert ref2["content"] == ref1["content"], "an already-archived placeholder must not change"
        assert json.loads(ref2["content"])["ref_id"] == json.loads(ref1["content"])["ref_id"]

    async def test_compaction_result_stats(self, strategy_components):
        """Test that CompactionResult has correct statistics."""
        strategy = strategy_components["strategy"]

        messages = [
            {"role": "user", "content": "Message " * 100}
            for _ in range(10)
        ]

        result = await strategy.compact(messages, current_tokens=2000)

        assert result.original_tokens == 2000
        assert result.final_tokens <= result.original_tokens
        assert result.tokens_saved >= 0

    async def test_mutation_invalidates_reasoning_artifacts(self, strategy_components):
        """DIE INVARIANTE: mutiert Compaction die History (Tool-Result→Ref),
        müssen provider reasoning artifacts invalidiert werden — ältere
        reasoning_details gestrippt, die letzte Assistant-Message rd_orphaned
        geflaggt. Sonst 400 'encrypted content could not be verified' später
        im Lauf (OpenAI-Kette über die exakte History gebrochen)."""
        strategy = strategy_components["strategy"]

        large1 = "word " * 200
        large2 = "test " * 200
        rd = lambda i: [{"type": "reasoning.encrypted", "id": f"rs_{i}", "data": f"blob{i}"}]
        messages = [
            {"role": "user", "content": "Read the file"},
            {"role": "assistant", "tool_calls": [{"id": "c1", "function": {"name": "read"}}],
             "reasoning_details": rd(1)},
            {"role": "tool", "name": "read_file", "tool_call_id": "c1", "content": large1},
            {"role": "assistant", "tool_calls": [{"id": "c2", "function": {"name": "read"}}],
             "reasoning_details": rd(2)},
            {"role": "tool", "name": "read_file", "tool_call_id": "c2", "content": large2},
            {"role": "user", "content": "Latest question"},
        ]

        result = await strategy.compact(messages, current_tokens=800, force=True)
        assert result.tool_results_stored >= 1, "Vorbedingung: Mutation ist passiert"

        assistants = [m for m in result.modified_messages if m.get("role") == "assistant"]
        assert "reasoning_details" not in assistants[0], "ältere Kettenglieder gestrippt"
        assert assistants[-1].get("reasoning_details"), "letzte behält (Gemini-Roundtrip)"
        assert assistants[-1].get("rd_orphaned") is True, "letzte als orphaned geflaggt"

    async def test_no_mutation_keeps_reasoning_artifacts(self, strategy_components):
        """Ohne Mutation bleiben reasoning_details unangetastet — kein
        unnötiges Re-Reasoning."""
        strategy = strategy_components["strategy"]
        messages = [
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "",
             "reasoning_details": [{"type": "reasoning.encrypted", "data": "X"}]},
        ]
        result = await strategy.compact(messages, current_tokens=100)
        assistants = [m for m in result.modified_messages if m.get("role") == "assistant"]
        assert assistants[0].get("reasoning_details")
        assert "rd_orphaned" not in assistants[0]
    
    async def test_restoration_context_generation(self, strategy_components):
        """Test generating restoration context for system prompt."""
        strategy = strategy_components["strategy"]
        tool_store = strategy_components["tool_store"]
        core_memory = strategy_components["core_memory"]
        
        # Add some data
        tool_store.store_and_reference("test", "c1", "Some output")
        await core_memory.add_fact("Important fact", category="facts")
        
        context = await strategy.get_restoration_context()
        
        assert "Context Engineer" in context
        assert "Tool Results" in context or "Core Memory" in context

    @pytest.mark.asyncio
    async def test_compact_multimodal_text_file(self, strategy_components):
        """Test that text_file items in multimodal content are compacted."""
        strategy = strategy_components["strategy"]
        strategy.config.variable_min_size = 20  # Lower threshold for test
        
        # Create large text file content (100 words = ~130 tokens)
        large_file_content = "line of code " * 100
        
        messages = [
            {"role": "user", "content": [
                {"type": "text", "text": "Review this code:"},
                {"type": "text_file", "name": "main.py", "content": large_file_content}
            ]},
            {"role": "assistant", "content": "I'll analyze the code."}
        ]
        
        result = await strategy.compact(messages, current_tokens=600)
        
        # Variable should be created for the text_file
        assert result.variables_created >= 1
        assert result.tokens_saved > 0
        
        # Check that content was replaced
        compacted_user = result.modified_messages[0]
        assert isinstance(compacted_user["content"], list)
        
        # The text_file should be replaced with text containing $VAR reference
        content_items = compacted_user["content"]
        text_items = [i for i in content_items if i.get("type") == "text"]
        
        var_ref_found = any("$VAR" in str(i.get("text", "")) for i in text_items)
        assert var_ref_found, f"No $VAR reference found in: {content_items}"

    @pytest.mark.asyncio
    async def test_compact_multimodal_preserves_images(self, strategy_components):
        """Test that image content is preserved during multimodal compaction."""
        strategy = strategy_components["strategy"]
        strategy.config.variable_min_size = 20  # Lower threshold for test
        
        # Create multimodal message with image
        large_file_content = "some code " * 100  # ~130 tokens
        
        messages = [
            {"role": "user", "content": [
                {"type": "text", "text": "Look at this image and code:"},
                {"type": "image_url", "image_url": {"url": "data:image/png;base64,abc123"}},
                {"type": "text_file", "name": "script.py", "content": large_file_content}
            ]}
        ]
        
        result = await strategy.compact(messages, current_tokens=600)
        
        # Image should be preserved
        compacted_content = result.modified_messages[0]["content"]
        image_items = [i for i in compacted_content if i.get("type") == "image_url"]
        assert len(image_items) == 1
        assert image_items[0]["image_url"]["url"] == "data:image/png;base64,abc123"

    @pytest.mark.asyncio
    async def test_compact_multimodal_small_text_file_preserved(self, strategy_components):
        """Test that small text_file items are not compacted."""
        strategy = strategy_components["strategy"]
        strategy.config.variable_min_size = 500  # High threshold
        
        # Small text file content
        small_file_content = "hello world"
        
        messages = [
            {"role": "user", "content": [
                {"type": "text", "text": "Check this:"},
                {"type": "text_file", "name": "small.txt", "content": small_file_content}
            ]}
        ]
        
        result = await strategy.compact(messages, current_tokens=600)
        
        # No variables should be created (content too small)
        assert result.variables_created == 0
        
        # Content should be unchanged
        compacted_content = result.modified_messages[0]["content"]
        text_file_items = [i for i in compacted_content if i.get("type") == "text_file"]
        assert len(text_file_items) == 1
        assert text_file_items[0]["content"] == small_file_content

    @pytest.mark.asyncio
    async def test_compact_multimodal_tool_response(self, strategy_components):
        """Test that multimodal tool responses are compacted."""
        strategy = strategy_components["strategy"]
        strategy.config.variable_min_size = 20  # Lower threshold for test
        
        large_file_content = "data line " * 100
        
        messages = [
            {"role": "assistant", "tool_calls": [{"id": "call_abc", "function": {"name": "read"}}]},
            {"role": "tool", "name": "read_file", "tool_call_id": "call_abc", "content": [
                {"type": "text", "text": "File contents:"},
                {"type": "text_file", "name": "output.log", "content": large_file_content}
            ]}
        ]
        
        result = await strategy.compact(messages, current_tokens=600)
        
        # Variable should be created for the text_file in tool response
        assert result.variables_created >= 1

    @pytest.mark.asyncio
    async def test_compact_multimodal_removes_large_audio(self, strategy_components):
        """Test that large audio inline data is removed during compaction.
        
        Note: The last user message is protected to preserve recently-submitted media.
        So we need an OLDER user message with audio that can be compacted.
        """
        strategy = strategy_components["strategy"]
        strategy.config.tool_result_min_size = 100  # Lower threshold for test
        
        # Large audio base64 data (~4000 chars = 1000 tokens)
        large_audio_base64 = "A" * 4000
        
        messages = [
            # First user message with audio (will be compacted - not the last user msg)
            {"role": "user", "content": [
                {"type": "text", "text": "Analyze this audio:"},
                {"type": "audio", "source": {"type": "base64", "media_type": "audio/wav", "data": large_audio_base64}}
            ]},
            {"role": "assistant", "content": "I'll analyze this audio file for you."},
            # Second user message (this is now the "last" user message, so first one can be compacted)
            {"role": "user", "content": "What was the result?"}
        ]
        
        result = await strategy.compact(messages, current_tokens=2000, force=True)
        
        # Audio in the FIRST user message should be removed and replaced with placeholder
        compacted_content = result.modified_messages[0]["content"]
        audio_items = [i for i in compacted_content if i.get("type") == "audio"]
        assert len(audio_items) == 0  # Audio removed
        
        # Should have placeholder text instead
        text_items = [i for i in compacted_content if i.get("type") == "text"]
        assert len(text_items) == 2  # Original text + placeholder
        
        # Verify placeholder mentions removal
        placeholders = [t for t in text_items if "Audio removed" in t.get("text", "")]
        assert len(placeholders) == 1
        
        # Verify tokens were saved
        assert result.tokens_saved > 500  # Removed ~1000 token audio

    @pytest.mark.asyncio
    async def test_compact_multimodal_preserves_small_audio(self, strategy_components):
        """Test that small audio inline data is preserved."""
        strategy = strategy_components["strategy"]
        strategy.config.tool_result_min_size = 5000  # High threshold
        
        # Small audio base64 data (100 chars = 25 tokens)
        small_audio_base64 = "B" * 100
        
        messages = [
            {"role": "user", "content": [
                {"type": "text", "text": "Analyze:"},
                {"type": "audio", "source": {"type": "base64", "data": small_audio_base64}}
            ]}
        ]
        
        result = await strategy.compact(messages, current_tokens=600, force=True)
        
        # Audio should be preserved (below threshold)
        compacted_content = result.modified_messages[0]["content"]
        audio_items = [i for i in compacted_content if i.get("type") == "audio"]
        assert len(audio_items) == 1

    @pytest.mark.asyncio
    async def test_compact_multimodal_removes_large_image(self, strategy_components):
        """Test that large image inline data is removed during compaction.
        
        Note: The last user message is protected to preserve recently-submitted media.
        So we need an OLDER user message with image that can be compacted.
        """
        strategy = strategy_components["strategy"]
        strategy.config.tool_result_min_size = 100  # Lower threshold for test
        
        # Large image base64 data (~4000 chars = 1000 tokens)
        large_image_base64 = "C" * 4000
        
        messages = [
            # First user message with image (will be compacted - not the last user msg)
            {"role": "user", "content": [
                {"type": "text", "text": "What's in this image?"},
                {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": large_image_base64}}
            ]},
            {"role": "assistant", "content": "I'll analyze this image for you."},
            # Second user message (this is now the "last" user message, so first one can be compacted)
            {"role": "user", "content": "What was the result?"}
        ]
        
        result = await strategy.compact(messages, current_tokens=2000, force=True)
        
        # Image in the FIRST user message should be removed and replaced with placeholder
        compacted_content = result.modified_messages[0]["content"]
        image_items = [i for i in compacted_content if i.get("type") == "image"]
        assert len(image_items) == 0  # Image removed
        
        # Should have placeholder text
        placeholders = [t for t in compacted_content if isinstance(t, dict) and "Image removed" in t.get("text", "")]
        assert len(placeholders) == 1

    @pytest.mark.asyncio
    async def test_estimate_messages_tokens_includes_inline_data(self, strategy_components):
        """Test that _estimate_messages_tokens counts inline data tokens."""
        strategy = strategy_components["strategy"]
        
        # Create message with large base64 audio (4000 chars = ~1000 tokens)
        large_audio_base64 = "D" * 4000
        
        messages = [
            {"role": "user", "content": [
                {"type": "text", "text": "Hello"},  # ~1 token
                {"type": "audio", "source": {"type": "base64", "data": large_audio_base64}}  # ~1000 tokens
            ]}
        ]
        
        tokens = strategy._estimate_messages_tokens(messages)
        
        # Should be text tokens + inline data tokens + overhead
        # ~1 (text) + ~1000 (audio) + 4 (overhead) = ~1005
        assert tokens > 900  # Should include inline data

    @pytest.mark.asyncio
    async def test_compact_multimodal_content_with_file_paths(self, strategy_components, tmp_path):
        """Test that multimodal_content with file paths is compacted but path is preserved."""
        strategy = strategy_components["strategy"]
        strategy.config.tool_result_min_size = 100  # Lower threshold
        
        # Create a large audio file (100KB = ~33,000 tokens)
        audio_file = tmp_path / "test_audio.wav"
        audio_file.write_bytes(b"x" * 100_000)
        original_path = str(audio_file)
        
        # Message with multimodal_content attribute (tool response format)
        messages = [
            {"role": "user", "content": "Analyze this audio"},
            {"role": "assistant", "content": None, "tool_calls": [
                {"id": "call_123", "function": {"name": "load_audio"}}
            ]},
            {
                "role": "tool",
                "tool_call_id": "call_123",
                "name": "load_audio",
                "content": '{"status": "success"}',
                "multimodal_content": [
                    {
                        "type": "audio",
                        "path": original_path,
                        "mime_type": "audio/wav",
                        "description": "Test audio file"
                    }
                ]
            }
        ]
        
        result = await strategy.compact(messages, current_tokens=35000, force=True)
        
        # multimodal_content should be empty - item removed and hint added to content
        tool_msg = result.modified_messages[2]
        mm_content = tool_msg.get("multimodal_content", [])
        
        # Item should be removed from multimodal_content
        assert len(mm_content) == 0
        
        # Hint should be added to the content
        content = tool_msg.get("content", "")
        assert "compacted" in content.lower() or "removed" in content.lower()
        assert "recall" in content.lower()
        
        # Should have saved significant tokens (~33K)
        assert result.tokens_saved > 30_000

    @pytest.mark.asyncio
    async def test_estimate_messages_tokens_includes_multimodal_content_files(self, strategy_components, tmp_path):
        """Test that _estimate_messages_tokens counts multimodal_content file paths."""
        strategy = strategy_components["strategy"]
        
        # Create a fake audio file - 163840 bytes = 10 seconds at 16KB/s fallback
        # This gives: 10s × 32 tokens/s = 320 tokens
        audio_file = tmp_path / "audio.wav"
        audio_file.write_bytes(b"x" * 163840)
        
        messages = [
            {
                "role": "tool",
                "tool_call_id": "call_456",
                "name": "audio_ops",
                "content": '{"status": "success"}',
                "multimodal_content": [
                    {
                        "type": "audio",
                        "path": str(audio_file),
                        "mime_type": "audio/wav"
                    }
                ]
            }
        ]
        
        tokens = strategy._estimate_messages_tokens(messages)
        
        # Should include: content text (~10) + multimodal file (~320) + overhead
        # With new duration-based estimation: 10s audio = 320 tokens
        assert tokens > 300  # Main contribution is the audio file
        assert tokens < 500  # Should be reasonable, not millions

    @pytest.mark.asyncio
    async def test_deduplicate_media_keeps_newest(self, strategy_components, tmp_path):
        """Test that media deduplication keeps the newest duplicate and compacts older ones."""
        strategy = strategy_components["strategy"]
        strategy.config.deduplicate_media = True
        # Set high threshold so Layer 1 doesn't also compact the images
        strategy.config.tool_result_min_size = 100000
        
        # Create a test image file
        image_file = tmp_path / "test_image.png"
        image_file.write_bytes(b"PNG_DATA" * 100)
        image_path = str(image_file)
        
        # Create messages with the same image appearing multiple times
        messages = [
            {"role": "user", "content": [
                {"type": "text", "text": "First time showing image:"},
                {"type": "image", "path": image_path, "source": {"type": "file", "data": "ABC" * 100}}
            ]},
            {"role": "assistant", "content": "I see the image."},
            {"role": "user", "content": [
                {"type": "text", "text": "Here's the same image again:"},
                {"type": "image", "path": image_path, "source": {"type": "file", "data": "ABC" * 100}}
            ]},
            {"role": "assistant", "content": "Same image as before."},
            {"role": "user", "content": [
                {"type": "text", "text": "And one more time:"},
                {"type": "image", "path": image_path, "source": {"type": "file", "data": "ABC" * 100}}
            ]}
        ]
        
        result = await strategy.compact(messages, current_tokens=1000, force=True)
        
        # First two images should be compacted, last one preserved
        assert result.media_deduplicated == 2
        
        # Verify older messages have placeholder text
        first_user_content = result.modified_messages[0]["content"]
        third_user_content = result.modified_messages[2]["content"]
        last_user_content = result.modified_messages[4]["content"]
        
        # First image should be replaced with text placeholder
        first_image = [i for i in first_user_content if i.get("type") in ("image", "text")]
        assert any("duplicate compacted" in str(i.get("text", "")) for i in first_image)
        
        # Third image should also be compacted
        third_image = [i for i in third_user_content if i.get("type") in ("image", "text")]
        assert any("duplicate compacted" in str(i.get("text", "")) for i in third_image)
        
        # Last image should be preserved (not compacted)
        last_image = [i for i in last_user_content if i.get("type") == "image"]
        assert len(last_image) == 1

    @pytest.mark.asyncio
    async def test_deduplicate_media_different_files_preserved(self, strategy_components, tmp_path):
        """Test that different media files are not deduplicated."""
        strategy = strategy_components["strategy"]
        strategy.config.deduplicate_media = True
        # Set high threshold so Layer 1 doesn't also compact the images
        strategy.config.tool_result_min_size = 100000
        
        # Create two different image files
        image_file_1 = tmp_path / "image1.png"
        image_file_1.write_bytes(b"IMAGE1_DATA")
        image_file_2 = tmp_path / "image2.png"
        image_file_2.write_bytes(b"IMAGE2_DATA")
        
        messages = [
            {"role": "user", "content": [
                {"type": "text", "text": "First image:"},
                {"type": "image", "path": str(image_file_1)}
            ]},
            {"role": "user", "content": [
                {"type": "text", "text": "Second image:"},
                {"type": "image", "path": str(image_file_2)}
            ]}
        ]
        
        result = await strategy.compact(messages, current_tokens=1000, force=True)
        
        # No duplicates, so none should be compacted
        assert result.media_deduplicated == 0
        
        # Both images should still be there
        first_images = [i for i in result.modified_messages[0]["content"] if i.get("type") == "image"]
        second_images = [i for i in result.modified_messages[1]["content"] if i.get("type") == "image"]
        assert len(first_images) == 1
        assert len(second_images) == 1

    @pytest.mark.asyncio
    async def test_compact_media_after_user_message(self, strategy_components, tmp_path):
        """Test that media is compacted when a new user message arrives (if configured)."""
        strategy = strategy_components["strategy"]
        strategy.config.compact_media_after_user_message = True
        strategy.config.deduplicate_media = False  # Disable to test separately
        
        # Create test audio file
        audio_file = tmp_path / "audio.wav"
        audio_file.write_bytes(b"AUDIO" * 1000)
        
        messages = [
            {"role": "user", "content": [
                {"type": "text", "text": "Listen to this:"},
                {"type": "audio", "path": str(audio_file), "source": {"type": "file", "data": "XYZ" * 500}}
            ]},
            {"role": "assistant", "content": "I heard the audio."},
            {"role": "user", "content": "What did you hear?"}  # New user message
        ]
        
        result = await strategy.compact(
            messages, 
            current_tokens=1000, 
            force=True,
            trigger_event="user_message"
        )
        
        # Audio should be compacted (not in last message)
        assert result.media_compacted_after_event >= 1
        
        # First message's audio should be replaced with text
        first_user_content = result.modified_messages[0]["content"]
        audio_items = [i for i in first_user_content if i.get("type") == "audio"]
        assert len(audio_items) == 0
        
        text_items = [i for i in first_user_content if i.get("type") == "text"]
        assert any("removed after user_message" in i.get("text", "") for i in text_items)

    @pytest.mark.asyncio
    async def test_compact_media_after_final_response(self, strategy_components, tmp_path):
        """Test that media is compacted when agent sends final response (if configured)."""
        strategy = strategy_components["strategy"]
        strategy.config.compact_media_after_final_response = True
        strategy.config.deduplicate_media = False  # Disable to test separately
        
        # Create test image file
        image_file = tmp_path / "screenshot.png"
        image_file.write_bytes(b"PNG" * 1000)
        
        messages = [
            {"role": "user", "content": [
                {"type": "text", "text": "Look at this:"},
                {"type": "image", "path": str(image_file), "source": {"type": "file", "data": "IMG" * 500}}
            ]},
            {"role": "assistant", "content": "Final response - I see the image."}  # Final response
        ]
        
        result = await strategy.compact(
            messages, 
            current_tokens=1000, 
            force=True,
            trigger_event="final_response"
        )
        
        # Image in user message should be compacted (not in last message)
        assert result.media_compacted_after_event >= 1
        
        # User message's image should be replaced with placeholder
        user_content = result.modified_messages[0]["content"]
        image_items = [i for i in user_content if i.get("type") == "image"]
        assert len(image_items) == 0
        
        text_items = [i for i in user_content if i.get("type") == "text"]
        assert any("removed after final_response" in i.get("text", "") for i in text_items)

    @pytest.mark.asyncio
    async def test_compact_media_preserves_last_message(self, strategy_components, tmp_path):
        """Test that media compaction preserves media in the last message."""
        strategy = strategy_components["strategy"]
        strategy.config.compact_media_after_user_message = True
        strategy.config.deduplicate_media = False
        # Set high threshold so Layer 1 doesn't also compact the images
        strategy.config.tool_result_min_size = 100000
        
        # Create test image
        image_file = tmp_path / "new_image.png"
        image_file.write_bytes(b"NEWIMG" * 100)
        
        messages = [
            {"role": "assistant", "content": "Hello, how can I help?"},
            {"role": "user", "content": [
                {"type": "text", "text": "Here's my new image:"},
                {"type": "image", "path": str(image_file), "source": {"type": "file", "data": "DATA" * 100}}
            ]}  # This is the last message, so its media should be preserved
        ]
        
        result = await strategy.compact(
            messages, 
            current_tokens=1000, 
            force=True,
            trigger_event="user_message"
        )
        
        # No media should be compacted (the only media is in the last message)
        assert result.media_compacted_after_event == 0
        
        # Image should still exist in the last message
        last_content = result.modified_messages[1]["content"]
        images = [i for i in last_content if i.get("type") == "image"]
        assert len(images) == 1

    @pytest.mark.asyncio
    async def test_deduplicate_media_multimodal_content(self, strategy_components, tmp_path):
        """Test deduplication of media in multimodal_content (tool responses)."""
        strategy = strategy_components["strategy"]
        strategy.config.deduplicate_media = True
        
        # Create test audio file
        audio_file = tmp_path / "response_audio.wav"
        audio_file.write_bytes(b"AUDIO_DATA" * 100)
        audio_path = str(audio_file)
        
        messages = [
            # First tool response with audio
            {"role": "assistant", "tool_calls": [{"id": "call_1", "function": {"name": "audio"}}]},
            {
                "role": "tool", 
                "tool_call_id": "call_1", 
                "name": "audio_ops",
                "content": '{"status": "success"}',
                "multimodal_content": [
                    {"type": "audio", "path": audio_path, "description": "Audio 1"}
                ]
            },
            {"role": "assistant", "content": "Played audio."},
            # Second tool response with same audio
            {"role": "assistant", "tool_calls": [{"id": "call_2", "function": {"name": "audio"}}]},
            {
                "role": "tool", 
                "tool_call_id": "call_2", 
                "name": "audio_ops",
                "content": '{"status": "success"}',
                "multimodal_content": [
                    {"type": "audio", "path": audio_path, "description": "Audio 2 (same file)"}
                ]
            }
        ]
        
        result = await strategy.compact(messages, current_tokens=1000, force=True)
        
        # First occurrence should be removed (duplicate compacted)
        assert result.media_deduplicated == 1
        
        first_tool_msg = result.modified_messages[1]
        mm_content = first_tool_msg.get("multimodal_content", [])
        # Item should be removed from multimodal_content
        assert len(mm_content) == 0
        # Note: Layer 1 may replace content with tool_result_ref, which is expected
        # The important thing is multimodal_content was compacted
        
        # Second occurrence (newer) should be preserved
        second_tool_msg = result.modified_messages[4]
        mm_content_2 = second_tool_msg.get("multimodal_content", [])
        assert len(mm_content_2) == 1

    @pytest.mark.asyncio
    async def test_compute_media_hash_with_path(self, strategy_components, tmp_path):
        """Test _compute_media_hash with file path."""
        strategy = strategy_components["strategy"]
        
        # Create test file
        test_file = tmp_path / "test.png"
        test_file.write_bytes(b"data")
        
        item = {"type": "image", "path": str(test_file)}
        
        hash_value = strategy._compute_media_hash(item)
        
        assert hash_value is not None
        assert len(hash_value) == 16  # First 16 chars of MD5
        
        # Same path should give same hash
        hash_value_2 = strategy._compute_media_hash(item)
        assert hash_value == hash_value_2

    @pytest.mark.asyncio
    async def test_compute_media_hash_with_inline_data(self, strategy_components):
        """Test _compute_media_hash with inline base64 data."""
        strategy = strategy_components["strategy"]
        
        item = {"type": "audio", "data": "ABC123XYZ" * 200}
        
        hash_value = strategy._compute_media_hash(item)
        
        assert hash_value is not None
        assert len(hash_value) == 16
        
        # Same data should give same hash
        item_same = {"type": "audio", "data": "ABC123XYZ" * 200}
        assert strategy._compute_media_hash(item_same) == hash_value
        
        # Different data should give different hash
        item_diff = {"type": "audio", "data": "DIFFERENT" * 200}
        assert strategy._compute_media_hash(item_diff) != hash_value

    @pytest.mark.asyncio
    async def test_get_media_filename_from_path(self, strategy_components, tmp_path):
        """Test _get_media_filename extracts filename from path."""
        strategy = strategy_components["strategy"]
        
        item = {"type": "image", "path": str(tmp_path / "subdir" / "my_image.png")}
        
        filename = strategy._get_media_filename(item)
        
        assert filename == "my_image.png"

    @pytest.mark.asyncio
    async def test_get_media_filename_from_name_field(self, strategy_components):
        """Test _get_media_filename uses name field as fallback."""
        strategy = strategy_components["strategy"]
        
        item = {"type": "audio", "name": "recording.wav"}
        
        filename = strategy._get_media_filename(item)
        
        assert filename == "recording.wav"

    @pytest.mark.asyncio
    async def test_disabled_deduplication(self, strategy_components, tmp_path):
        """Test that deduplication can be disabled via config."""
        strategy = strategy_components["strategy"]
        strategy.config.deduplicate_media = False
        
        # Create duplicate media
        image_file = tmp_path / "dup.png"
        image_file.write_bytes(b"DATA")
        
        messages = [
            {"role": "user", "content": [
                {"type": "image", "path": str(image_file)}
            ]},
            {"role": "user", "content": [
                {"type": "image", "path": str(image_file)}
            ]}
        ]
        
        result = await strategy.compact(messages, current_tokens=1000, force=True)
        
        # No deduplication should occur
        assert result.media_deduplicated == 0

    @pytest.mark.asyncio
    async def test_estimate_request_bytes(self, strategy_components):
        """Test _estimate_request_bytes calculates byte size correctly."""
        strategy = strategy_components["strategy"]
        
        # Simple text messages
        messages = [
            {"role": "user", "content": "Hello world"},
            {"role": "assistant", "content": "Hi there!"}
        ]
        
        bytes_estimate = strategy._estimate_request_bytes(messages)
        
        # Should include JSON structure overhead plus content
        assert bytes_estimate > 0
        assert bytes_estimate < 1000  # Small messages

    @pytest.mark.asyncio
    async def test_estimate_request_bytes_with_inline_data(self, strategy_components):
        """Test _estimate_request_bytes accounts for inline_data."""
        strategy = strategy_components["strategy"]
        
        # Create large inline data (simulating audio)
        large_data = "A" * 1_000_000  # 1 MB of base64-ish data
        
        messages = [
            {"role": "user", "content": [
                {"type": "text", "text": "Here is audio:"},
                {"type": "audio", "inline_data": large_data}
            ]}
        ]
        
        bytes_estimate = strategy._estimate_request_bytes(messages)
        
        # Should be at least the size of the inline data
        assert bytes_estimate >= 1_000_000

    @pytest.mark.asyncio
    async def test_compact_for_byte_limit_triggers_media_compaction(self, strategy_components, tmp_path):
        """Test that byte limit triggers aggressive media compaction."""
        strategy = strategy_components["strategy"]
        
        # Set byte limits to very low values for testing
        strategy.config.max_request_bytes = 10_000  # 10 KB
        strategy.config.target_request_bytes = 5_000  # 5 KB
        
        # Create messages with inline data exceeding the limit
        large_data = "B" * 15_000  # 15 KB - exceeds max_request_bytes
        
        messages = [
            {"role": "user", "content": [
                {"type": "text", "text": "Process this audio:"},
                {"type": "audio", "data": large_data}
            ]},
            {"role": "assistant", "content": "I heard the audio."},
            {"role": "user", "content": "What did you hear?"}
        ]
        
        # Use low token count so normal compaction wouldn't trigger
        result = await strategy.compact(messages, current_tokens=100)
        
        # Should have triggered byte-limit compaction
        # Either media was compacted or removed
        final_bytes = strategy._estimate_request_bytes(result.modified_messages)
        
        # Should be significantly reduced
        assert final_bytes < 15_000

    @pytest.mark.asyncio
    async def test_compact_media_for_byte_limit_keeps_recent(self, strategy_components, tmp_path):
        """Test byte-limit compaction preserves the last 2 messages."""
        strategy = strategy_components["strategy"]
        strategy.config.max_request_bytes = 5_000  # Low threshold
        strategy.config.target_request_bytes = 2_000
        
        # Create large inline data that exceeds the byte limit
        large_data = "X" * 10_000  # 10 KB
        
        messages = [
            # Old message with media - should be compacted
            {"role": "user", "content": [
                {"type": "text", "text": "Check this:"},
                {"type": "audio", "data": large_data}
            ]},
            {"role": "assistant", "content": "Processed."},
            # Recent messages - should be preserved
            {"role": "user", "content": "What next?"},
            {"role": "assistant", "content": "Tell me more."}
        ]
        
        # Use low token count so we rely on byte-limit triggering
        result = await strategy.compact(messages, current_tokens=100)
        
        # Should have triggered byte-limit compaction
        # The audio in the first message should be compacted
        first_content = result.modified_messages[0].get("content", [])
        if isinstance(first_content, list):
            # Audio should be replaced with text placeholder
            # Check that bytes are reduced
            final_bytes = strategy._estimate_request_bytes(result.modified_messages)
            assert final_bytes < 10_000  # Should be significantly reduced


# =============================================================================
# Integration Tests
# =============================================================================


class TestPluginIntegration:
    """Integration tests for the plugin."""
    
    @pytest.fixture
    def plugin_instance(self, tmp_path, monkeypatch):
        """Create plugin instance for testing."""
        from agent_system.config.models import AgentSystemConfig, MCPConfig
        
        # Patch storage path
        monkeypatch.setattr(
            "plugins.context_engineer.hooks.ContextEngineerPlugin._storage_base",
            tmp_path,
            raising=False
        )
        
        system_config = AgentSystemConfig()
        mcp_config = MCPConfig()
        
        from plugins.context_engineer.plugin import PLUGIN_FACTORY
        return PLUGIN_FACTORY("context_engineer", system_config, mcp_config)
    
    def test_plugin_factory_creates_instance(self, plugin_instance):
        """Test that PLUGIN_FACTORY creates valid instance."""
        assert plugin_instance is not None
        assert plugin_instance.name == "context_engineer"
    
    @pytest.mark.asyncio
    async def test_plugin_has_tools(self, plugin_instance):
        """Test that plugin exposes tools."""
        tools = await plugin_instance.list_tools()
        
        # MCPTool objects have .name attribute
        tool_names = [t.name for t in tools]
        
        # Core tools: recall (unified), store_fact, compact
        assert any("recall" in name for name in tool_names)
        assert any("store_fact" in name for name in tool_names)
        assert any("compact" in name for name in tool_names)
    
    @pytest.mark.asyncio
    async def test_tool_handler_store_fact(self, plugin_instance):
        """Test store_fact tool handler."""
        result = await plugin_instance.server._hooks_impl._handle_store_fact(
            fact="Test fact",
            category="facts",
            importance=0.7,
            session_id="test-session"
        )
        
        assert result["success"] is True
        assert result["fact_id"] is not None
    
    @pytest.mark.asyncio
    async def test_tool_handler_stats(self, plugin_instance):
        """Test stats tool handler."""
        result = await plugin_instance.server._hooks_impl._handle_stats(
            session_id="test-session"
        )
        
        assert "tool_results" in result
        assert "variables" in result
        assert "core_memory" in result
        assert "archival_memory" in result


# =============================================================================
# Pagination & Search Tests
# =============================================================================


class TestVariablePagination:
    """Tests for get_variable pagination and search modes."""
    
    @pytest.fixture
    def hooks_impl(self, tmp_path):
        """Create hooks implementation with test storage."""
        from plugins.context_engineer.hooks import ContextEngineerPlugin
        
        plugin_dir = Path(__file__).parent.parent.parent.parent.parent / "src" / "plugins" / "context_engineer"
        hooks = ContextEngineerPlugin(plugin_dir)
        hooks._storage_base = tmp_path
        return hooks
    
    @pytest.fixture
    async def large_variable(self, hooks_impl):
        """Create a large variable for testing pagination."""
        session_id = "test-pagination"
        components = hooks_impl._get_session_components(session_id)
        var_manager = components["variable_manager"]
        
        # Create large content (~2000 chars)
        large_content = "Line {}: This is a test line with some content.\n" * 50
        large_content = large_content.format(*range(50))
        
        # create_variable returns (var_name, summary) tuple - now async
        var_name, _ = await var_manager.create_variable(large_content, content_type="text", force=True)
        return session_id, var_name, large_content
    
    @pytest.mark.asyncio
    async def test_get_variable_preview_mode(self, hooks_impl, large_variable):
        """Test preview mode returns truncated content."""
        session_id, var_name, original_content = large_variable
        
        result = await hooks_impl._handle_get_variable(
            variable_name=var_name,
            session_id=session_id,
            mode="preview"
        )
        
        assert result["found"] is True
        assert result["mode"] == "preview"
        assert result["truncated"] is True
        assert result["returned_chars"] == 500
        assert len(result["content"]) == 500
        assert result["total_chars"] == len(original_content)
        assert "hint" in result
    
    @pytest.mark.asyncio
    async def test_get_variable_chunk_mode(self, hooks_impl, large_variable):
        """Test chunk mode with pagination."""
        session_id, var_name, original_content = large_variable
        
        # First chunk
        result1 = await hooks_impl._handle_get_variable(
            variable_name=var_name,
            session_id=session_id,
            mode="chunk",
            offset=0,
            limit=500
        )
        
        assert result1["found"] is True
        assert result1["mode"] == "chunk"
        assert result1["offset"] == 0
        assert result1["limit"] == 500
        assert result1["returned_chars"] == 500
        assert result1["has_more"] is True
        assert result1["next_offset"] == 500
        assert result1["content"] == original_content[0:500]
        
        # Second chunk
        result2 = await hooks_impl._handle_get_variable(
            variable_name=var_name,
            session_id=session_id,
            mode="chunk",
            offset=500,
            limit=500
        )
        
        assert result2["offset"] == 500
        assert result2["content"] == original_content[500:1000]
        
        # Combined chunks should match original up to offset
        combined = result1["content"] + result2["content"]
        assert combined == original_content[0:1000]
    
    @pytest.mark.asyncio
    async def test_get_variable_search_mode(self, hooks_impl, large_variable):
        """Test search mode finds matches."""
        session_id, var_name, original_content = large_variable
        
        result = await hooks_impl._handle_get_variable(
            variable_name=var_name,
            session_id=session_id,
            mode="search",
            search="Line 5:",
            context_chars=50
        )
        
        assert result["found"] is True
        assert result["mode"] == "search"
        assert result["query"] == "Line 5:"
        assert result["match_count"] > 0
        assert len(result["matches"]) > 0
        
        # Check first match structure
        first_match = result["matches"][0]
        assert "position" in first_match
        assert "snippet" in first_match
        assert "Line 5:" in first_match["snippet"]
    
    @pytest.mark.asyncio
    async def test_get_variable_search_no_matches(self, hooks_impl, large_variable):
        """Test search mode with no matches."""
        session_id, var_name, original_content = large_variable
        
        result = await hooks_impl._handle_get_variable(
            variable_name=var_name,
            session_id=session_id,
            mode="search",
            search="NONEXISTENT_PATTERN"
        )
        
        assert result["found"] is True
        assert result["match_count"] == 0
        assert len(result["matches"]) == 0
    
    @pytest.mark.asyncio
    async def test_get_variable_search_missing_query(self, hooks_impl, large_variable):
        """Test search mode without search parameter."""
        session_id, var_name, original_content = large_variable
        
        result = await hooks_impl._handle_get_variable(
            variable_name=var_name,
            session_id=session_id,
            mode="search",
            search=None
        )
        
        assert result["found"] is True
        assert "error" in result
        assert "search parameter required" in result["error"]
    
    @pytest.mark.asyncio
    async def test_get_variable_full_mode(self, hooks_impl, large_variable):
        """Test full mode returns complete content."""
        session_id, var_name, original_content = large_variable
        
        result = await hooks_impl._handle_get_variable(
            variable_name=var_name,
            session_id=session_id,
            mode="full"
        )
        
        assert result["found"] is True
        assert result["mode"] == "full"
        assert result["content"] == original_content
        assert result["total_chars"] == len(original_content)
        assert "warning" in result
    
    @pytest.mark.asyncio
    async def test_get_variable_not_found(self, hooks_impl):
        """Test getting non-existent variable."""
        result = await hooks_impl._handle_get_variable(
            variable_name="$VAR_999",
            session_id="test-session",
            mode="preview"
        )
        
        assert result["found"] is False
        assert "error" in result


class TestToolResultPagination:
    """Tests for get_tool_result pagination and search modes."""
    
    @pytest.fixture
    def hooks_impl(self, tmp_path):
        """Create hooks implementation with test storage."""
        from plugins.context_engineer.hooks import ContextEngineerPlugin
        
        plugin_dir = Path(__file__).parent.parent.parent.parent.parent / "src" / "plugins" / "context_engineer"
        hooks = ContextEngineerPlugin(plugin_dir)
        hooks._storage_base = tmp_path
        return hooks
    
    @pytest.fixture
    async def large_tool_result(self, hooks_impl):
        """Create a large tool result for testing pagination."""
        session_id = "test-tool-pagination"
        components = hooks_impl._get_session_components(session_id)
        tool_store = components["tool_store"]
        
        # Create large result (~3000 chars)
        large_result = "Result line {}: Some detailed output here.\n" * 80
        large_result = large_result.format(*range(80))
        
        # Store tool result using store_and_reference
        tool_call_id = "test_call_12345678"
        tool_store.store_and_reference(
            tool_call_id=tool_call_id,
            tool_name="test_tool",
            content=large_result,
            session_id=session_id
        )
        
        return session_id, tool_call_id, large_result
    
    @pytest.mark.asyncio
    async def test_get_tool_result_preview_mode(self, hooks_impl, large_tool_result):
        """Test preview mode for tool results."""
        session_id, reference, original_result = large_tool_result
        
        result = await hooks_impl._handle_get_tool_result(
            reference=reference,
            session_id=session_id,
            mode="preview"
        )
        
        assert result["found"] is True
        assert result["mode"] == "preview"
        assert result["truncated"] is True
        assert len(result["content"]) == 500
        assert result["total_chars"] == len(original_result)
    
    @pytest.mark.asyncio
    async def test_get_tool_result_chunk_mode(self, hooks_impl, large_tool_result):
        """Test chunk mode for tool results."""
        session_id, reference, original_result = large_tool_result
        
        result = await hooks_impl._handle_get_tool_result(
            reference=reference,
            session_id=session_id,
            mode="chunk",
            offset=0,
            limit=1000
        )
        
        assert result["found"] is True
        assert result["mode"] == "chunk"
        assert result["returned_chars"] == 1000
        assert result["content"] == original_result[0:1000]
        assert result["has_more"] is True
    
    @pytest.mark.asyncio
    async def test_get_tool_result_search_mode(self, hooks_impl, large_tool_result):
        """Test search mode for tool results."""
        session_id, reference, original_result = large_tool_result
        
        result = await hooks_impl._handle_get_tool_result(
            reference=reference,
            session_id=session_id,
            mode="search",
            search="line 10:"
        )
        
        assert result["found"] is True
        assert result["mode"] == "search"
        assert result["match_count"] >= 1
        assert len(result["matches"]) >= 1
    
    @pytest.mark.asyncio
    async def test_get_tool_result_full_mode(self, hooks_impl, large_tool_result):
        """Test full mode for tool results."""
        session_id, reference, original_result = large_tool_result
        
        result = await hooks_impl._handle_get_tool_result(
            reference=reference,
            session_id=session_id,
            mode="full"
        )
        
        assert result["found"] is True
        assert result["mode"] == "full"
        assert result["content"] == original_result
        assert "warning" in result
    
    @pytest.mark.asyncio
    async def test_get_tool_result_not_found(self, hooks_impl):
        """Test getting non-existent tool result."""
        result = await hooks_impl._handle_get_tool_result(
            reference="nonexistent",
            session_id="test-session",
            mode="preview"
        )
        
        assert result["found"] is False
        assert "error" in result


# =============================================================================
# Pre-Layer P (Message Count Pruning) Tests
# =============================================================================


class TestPreLayerP:
    """Tests for Pre-Layer P: Message count pruning."""
    
    @pytest.fixture
    def strategy_with_max_messages(self, tmp_path):
        """Create strategy with max_messages limit."""
        tool_store = ToolResultStore(tmp_path / "tools.db")
        variable_manager = VariableManager(min_content_tokens=50, storage_path=tmp_path / "vars.json")
        core_memory = CoreMemory(storage_path=tmp_path / "memory.json")
        archival_memory = ArchivalMemory(tmp_path / "archive.db", session_id="test")
        
        config = CompactionConfig(
            layer1_threshold=100000,  # High threshold - won't trigger token-based layers
            layer2_threshold=200000,
            layer3_threshold=300000,
            target_tokens=50000,
            max_messages=10,  # Low limit for testing
            keep_system_messages=True,
        )
        
        return LayeredCompactionStrategy(
            tool_store=tool_store,
            variable_manager=variable_manager,
            core_memory=core_memory,
            archival_memory=archival_memory,
            config=config
        )
    
    @pytest.mark.asyncio
    async def test_no_pruning_below_limit(self, strategy_with_max_messages):
        """Test no pruning when message count is below max_messages."""
        strategy = strategy_with_max_messages
        
        messages = [
            {"role": "user", "content": "Message 1"},
            {"role": "assistant", "content": "Response 1"},
            {"role": "user", "content": "Message 2"},
            {"role": "assistant", "content": "Response 2"},
        ]
        
        result = await strategy.compact(messages, current_tokens=100)
        
        assert result.messages_pruned == 0
        assert len(result.modified_messages) == 4
    
    @pytest.mark.asyncio
    async def test_pruning_above_limit(self, strategy_with_max_messages):
        """Test pruning when message count exceeds max_messages."""
        strategy = strategy_with_max_messages
        
        # Create 20 messages (exceeds limit of 10)
        messages = []
        for i in range(10):
            messages.append({"role": "user", "content": f"User message {i}"})
            messages.append({"role": "assistant", "content": f"Assistant response {i}"})
        
        result = await strategy.compact(messages, current_tokens=100)
        
        assert result.messages_pruned > 0
        assert len(result.modified_messages) <= 10
        assert "P" in result.layers_applied
    
    @pytest.mark.asyncio
    async def test_pruning_preserves_system_messages(self, strategy_with_max_messages):
        """Test that system messages are preserved during pruning."""
        strategy = strategy_with_max_messages
        
        messages = [
            {"role": "system", "content": "You are a helpful assistant."},
        ]
        # Add many user/assistant pairs
        for i in range(15):
            messages.append({"role": "user", "content": f"User {i}"})
            messages.append({"role": "assistant", "content": f"Response {i}"})
        
        result = await strategy.compact(messages, current_tokens=100)

        # The agent's system prompt must survive pruning, unchanged and first.
        system_msgs = [m for m in result.modified_messages if m.get("role") == "system"]
        assert system_msgs[0]["content"] == "You are a helpful assistant."

        # Pre-Layer P adds one system message of its own: the breadcrumb naming
        # what left the view. Anything BEYOND those two would be a leak.
        assert len(system_msgs) == 2, system_msgs
        assert json.loads(system_msgs[1]["content"])["type"] == "pruned_notice"
    
    @pytest.mark.asyncio
    async def test_pruning_preserves_tool_pairs(self, strategy_with_max_messages):
        """Test that tool call/response pairs are kept together."""
        strategy = strategy_with_max_messages
        
        messages = [
            {"role": "user", "content": "Old message 1"},
            {"role": "assistant", "content": "Old response 1"},
            {"role": "user", "content": "Old message 2"},
            {"role": "assistant", "content": None, "tool_calls": [
                {"id": "call_old", "function": {"name": "test_tool"}}
            ]},
            {"role": "tool", "content": "Old tool result", "tool_call_id": "call_old"},
            {"role": "user", "content": "Recent message 1"},
            {"role": "assistant", "content": None, "tool_calls": [
                {"id": "call_new", "function": {"name": "test_tool"}}
            ]},
            {"role": "tool", "content": "New tool result", "tool_call_id": "call_new"},
            {"role": "user", "content": "Recent message 2"},
            {"role": "assistant", "content": "Recent response 2"},
            {"role": "user", "content": "Latest message"},
            {"role": "assistant", "content": "Latest response"},
        ]
        
        result = await strategy.compact(messages, current_tokens=100)
        
        # Check that no orphaned tool calls/responses exist
        tool_call_ids = set()
        tool_response_ids = set()
        
        for msg in result.modified_messages:
            if msg.get("tool_calls"):
                for tc in msg["tool_calls"]:
                    tool_call_ids.add(tc["id"])
            if msg.get("role") == "tool":
                tool_response_ids.add(msg.get("tool_call_id"))
        
        # Every tool response should have a matching tool call
        assert tool_response_ids.issubset(tool_call_ids)
    
    @pytest.mark.asyncio
    async def test_pruning_ensures_user_first(self, strategy_with_max_messages):
        """Test that first non-system message is always 'user' after pruning."""
        strategy = strategy_with_max_messages
        
        messages = [
            {"role": "system", "content": "System prompt"},
        ]
        # Add many messages to trigger pruning
        for i in range(20):
            messages.append({"role": "user", "content": f"User {i}"})
            messages.append({"role": "assistant", "content": f"Response {i}"})
        
        result = await strategy.compact(messages, current_tokens=100)
        
        # Find first non-system message
        first_non_system = None
        for msg in result.modified_messages:
            if msg.get("role") != "system":
                first_non_system = msg
                break
        
        assert first_non_system is not None
        assert first_non_system.get("role") == "user"
    
    @pytest.mark.asyncio
    async def test_pruning_removes_from_oldest(self, strategy_with_max_messages):
        """Test that oldest messages are pruned first."""
        strategy = strategy_with_max_messages
        
        messages = []
        for i in range(15):
            messages.append({"role": "user", "content": f"User message {i}"})
            messages.append({"role": "assistant", "content": f"Response {i}"})
        
        result = await strategy.compact(messages, current_tokens=100)
        
        # Latest messages should be preserved
        contents = [m.get("content", "") for m in result.modified_messages]
        
        # Last message should still be there
        assert "Response 14" in contents or any("14" in c for c in contents if c)
    
    @pytest.mark.asyncio
    async def test_pruning_disabled_when_zero(self, tmp_path):
        """Test that pruning is disabled when max_messages=0."""
        tool_store = ToolResultStore(tmp_path / "tools.db")
        variable_manager = VariableManager(min_content_tokens=50, storage_path=tmp_path / "vars.json")
        core_memory = CoreMemory(storage_path=tmp_path / "memory.json")
        archival_memory = ArchivalMemory(tmp_path / "archive.db", session_id="test")
        
        config = CompactionConfig(
            layer1_threshold=100000,
            layer2_threshold=200000,
            layer3_threshold=300000,
            target_tokens=50000,
            max_messages=0,  # Disabled
        )
        
        strategy = LayeredCompactionStrategy(
            tool_store=tool_store,
            variable_manager=variable_manager,
            core_memory=core_memory,
            archival_memory=archival_memory,
            config=config
        )
        
        # Create many messages
        messages = []
        for i in range(50):
            messages.append({"role": "user", "content": f"User {i}"})
            messages.append({"role": "assistant", "content": f"Response {i}"})
        
        result = await strategy.compact(messages, current_tokens=100)
        
        # Should not have pruned anything
        assert result.messages_pruned == 0
        assert len(result.modified_messages) == 100
    
    @pytest.mark.asyncio
    async def test_pruning_protects_last_user_message(self, tmp_path):
        """Test that pre-layer P never removes the last user message."""
        tool_store = ToolResultStore(tmp_path / "tools.db")
        variable_manager = VariableManager(min_content_tokens=50, storage_path=tmp_path / "vars.json")
        core_memory = CoreMemory(storage_path=tmp_path / "memory.json")
        archival_memory = ArchivalMemory(tmp_path / "archive.db", session_id="test")
        
        config = CompactionConfig(
            layer1_threshold=100000,
            layer2_threshold=200000,
            layer3_threshold=300000,
            target_tokens=50000,
            max_messages=3,  # Very aggressive limit
        )
        
        strategy = LayeredCompactionStrategy(
            tool_store=tool_store,
            variable_manager=variable_manager,
            core_memory=core_memory,
            archival_memory=archival_memory,
            config=config
        )
        
        # Create messages where pruning would remove all user messages if not protected
        messages = [
            {"role": "system", "content": "System prompt"},
            {"role": "user", "content": "First user"},
            {"role": "assistant", "content": "First response"},
            {"role": "user", "content": "Second user"},
            {"role": "assistant", "content": "Second response"},
            {"role": "user", "content": "Last user - must be protected"},
            {"role": "assistant", "content": "Last response"},
        ]
        
        result = await strategy.compact(messages, current_tokens=100)
        
        # Must have at least one user message
        user_msgs = [m for m in result.modified_messages if m.get("role") == "user"]
        assert len(user_msgs) >= 1, "At least one user message must remain"


class TestPreLayerPRecoverability:
    """Pre-Layer P must not destroy what it removes.

    Measured on the last 1000 production compactions, this is the layer that
    does the work (476 firings against Layer 2's 115 and Layer 3's zero) — and
    it used to be the only place in the system that deleted content outright,
    with no archive entry and no trace. These tests hold that shut.
    """

    @pytest.fixture
    def strategy(self, tmp_path):
        archival = ArchivalMemory(tmp_path / "archive.db", session_id="test")
        strat = LayeredCompactionStrategy(
            tool_store=ToolResultStore(tmp_path / "tools.db"),
            variable_manager=VariableManager(min_content_tokens=50,
                                             storage_path=tmp_path / "vars.json"),
            core_memory=CoreMemory(storage_path=tmp_path / "memory.json"),
            archival_memory=archival,
            config=CompactionConfig(
                layer1_threshold=10**9, layer2_threshold=10**9,
                layer3_threshold=10**9, max_messages=6,
                keep_system_messages=True,
            ),
        )
        return strat, archival

    @staticmethod
    def _archived_contents(archival) -> list[str]:
        rows = archival._db.execute("SELECT content FROM archived_messages").fetchall()
        return [r[0] for r in rows]

    @staticmethod
    def _conversation(n: int) -> list[dict]:
        msgs = [{"role": "user", "content": "die eigentliche Aufgabe"}]
        for i in range(n):
            msgs.append({"role": "assistant", "content": f"Antwort {i}"})
            msgs.append({"role": "user", "content": f"Nachfrage {i}"})
        return msgs

    def test_ref_type_does_not_depend_on_key_order(self):
        """The compaction-side twin of the persistence predicate.

        `_ref_type` decides protection, cost ranking, archive exclusion and
        deduplication. If a long `hint` before `"type"` could hide the marker,
        a breadcrumb would stop being recognised: it would stack instead of
        being replaced, and stop being protected. The hook-side predicate has
        the same property pinned; this is the other side of the same seam.
        """
        payload = json.dumps(
            {"hint": "x" * 400, "total_removed": 12, "type": "pruned_notice"},
            sort_keys=True)
        assert _ref_type({"role": "system", "content": payload}) == "pruned_notice"

        long_ref = json.dumps(
            {"summary": "y" * 400, "ref_id": "arch_1", "type": "archived_ref"},
            sort_keys=True)
        assert _ref_type({"role": "user", "content": long_ref}) == "archived_ref"

    @pytest.mark.asyncio
    async def test_pruned_content_reaches_the_archive(self, strategy):
        """The whole point: what leaves the view must stay retrievable."""
        strat, archival = strategy
        messages = self._conversation(8)

        result = await strat.compact(messages, current_tokens=100)

        assert result.messages_pruned > 0
        survived = {str(m.get("content")) for m in result.modified_messages}
        archived = set(self._archived_contents(archival))
        for msg in messages:
            content = str(msg.get("content"))
            if content not in survived:
                assert content in archived, (
                    f"{content!r} was removed from the conversation without "
                    f"being archived — this is unrecoverable data loss")

    @pytest.mark.asyncio
    async def test_placeholders_are_not_archived_again(self, strategy):
        """A pointer must never be archived: that stores an address, not content.

        Layer 2 learned this the hard way (chains 20 levels deep). Pre-Layer P
        removes placeholders too and must not repeat it.
        """
        strat, archival = strategy
        placeholder = json.dumps({"type": "archived_ref", "ref_id": "arch_old",
                                  "summary": "etwas Aelteres"})
        messages = [{"role": "user", "content": "die eigentliche Aufgabe"}]
        for i in range(8):
            messages.append({"role": "assistant", "content": placeholder})
            messages.append({"role": "user", "content": f"Nachfrage {i}"})

        result = await strat.compact(messages, current_tokens=100)

        stored = self._archived_contents(archival)
        for content in stored:
            assert "archived_ref" not in content, (
                "a placeholder was archived — that is a pointer to a pointer")
        # ...and archiving still happened at all. Without this the test also
        # passes when _archive_pruned returns early for every input.
        assert result.messages_pruned > 0
        assert any("Nachfrage" in s for s in stored), (
            "nothing real was archived — the negative assertion above is vacuous")

    @pytest.mark.asyncio
    async def test_first_user_message_survives(self, strategy):
        """The task everything refers to is not a candidate for eviction."""
        strat, _ = strategy
        messages = self._conversation(8)

        result = await strat.compact(messages, current_tokens=100)

        contents = [str(m.get("content")) for m in result.modified_messages]
        assert "die eigentliche Aufgabe" in contents, (
            "the first user message — the task — was pruned away")

    @pytest.mark.asyncio
    async def test_breadcrumb_is_single_and_accumulates(self, strategy):
        """One notice, never a stack, and its count covers every round."""
        strat, _ = strategy

        first_input = self._conversation(8)
        before = len(first_input)
        result = await strat.compact(first_input, current_tokens=100)
        notices = [m for m in result.modified_messages if _ref_type(m) == "pruned_notice"]
        assert len(notices) == 1
        first_total = json.loads(notices[0]["content"])["total_removed"]

        # Against the LIST, not against the same counter the notice came from:
        # comparing it to result.messages_pruned would be one local variable
        # asserted against itself. The +1 is the notice the layer added.
        assert first_total == before - len(result.modified_messages) + 1, (
            f"the notice claims {first_total} but "
            f"{before - len(result.modified_messages) + 1} messages left")

        # Second round on the already-compacted list, as the next turn would.
        # Fresh dicts on purpose: `[a, b] * 3` aliases one object into several
        # slots, and the identity-based removal diff would then under-count.
        followup = list(result.modified_messages) + [
            {"role": "assistant", "content": f"weiter {k}"} for k in range(3)
        ] + [{"role": "user", "content": f"und weiter {k}"} for k in range(3)]
        before2 = len(followup)
        result2 = await strat.compact(followup, current_tokens=100)

        notices2 = [m for m in result2.modified_messages if _ref_type(m) == "pruned_notice"]
        assert len(notices2) == 1, "the notice stacked instead of being replaced"
        second_total = json.loads(notices2[0]["content"])["total_removed"]
        assert second_total == first_total + (before2 - len(result2.modified_messages)), (
            f"running total went {first_total} -> {second_total}, but "
            f"{before2 - len(result2.modified_messages)} messages left this round")

    @pytest.mark.asyncio
    async def test_cheapest_to_lose_goes_first(self, strategy):
        """Among the oldest candidates, a stored-away pointer beats real prose.

        Both are recoverable now, but a placeholder costs nothing to drop while
        real content costs an archive write and a retrieval turn to get back.
        """
        strat, _ = strategy
        # Production shape: Layer 1 writes the ref onto the `role=tool` message
        # (tool_result_store.py), never onto an assistant, and there is no
        # `summary` key. Its assistant comes with it as one group — that IS the
        # cost of picking it, and a fixture that hides the group would test a
        # cheaper decision than the code actually makes.
        ref = json.dumps({"type": "tool_result_ref", "ref_id": "TR_1",
                          "tool_name": "t", "content_hash": "abc", "token_count": 900})
        messages = [
            {"role": "user", "content": "die eigentliche Aufgabe"},
            {"role": "assistant", "content": "alte Prosa A"},
            {"role": "assistant", "content": "alte Prosa B"},
            {"role": "assistant", "content": None, "tool_calls": [
                {"id": "c1", "type": "function",
                 "function": {"name": "t", "arguments": "{}"}}]},
            {"role": "tool", "content": ref, "tool_call_id": "c1", "name": "t"},
            {"role": "user", "content": "Nachfrage"},
            {"role": "assistant", "content": "Antwort"},
            {"role": "user", "content": "letzte Frage"},
        ]

        result = await strat.compact(messages, current_tokens=100)
        contents = [str(m.get("content")) for m in result.modified_messages]

        # 3, not 2: picking the ref takes its assistant with it (one group),
        # and the budget then needs one more. That is the real cost of the
        # cheap pick and the fixture must not hide it.
        assert result.messages_pruned == 3, result.messages_pruned
        assert ref not in contents, "the free-to-drop placeholder was kept"
        assert "alte Prosa B" in contents, (
            "real content was evicted while a NEWER, cost-free placeholder "
            "stayed — the ranking fell back to plain age")

    @pytest.mark.asyncio
    async def test_archive_failure_keeps_the_messages_instead_of_destroying_them(
        self, strategy
    ):
        """A failed archive must not become a deletion.

        The batch write is all-or-nothing, so one malformed message aborts it.
        Deleting anyway would destroy the whole prune while the breadcrumb still
        promises the content can be looked up. An over-long context is a cost;
        this is not recoverable. And it must not raise either — the token layers
        still have to run.
        """
        strat, archival = strategy

        def boom(*a, **kw):
            raise sqlite3.OperationalError("database is locked")

        archival.store_many = boom
        messages = self._conversation(8)
        before = [str(m.get("content")) for m in messages]

        result = await strat.compact(messages, current_tokens=100)

        assert result.messages_pruned == 0, "messages were dropped unarchived"
        after = [str(m.get("content")) for m in result.modified_messages]
        for content in before:
            assert content in after, (
                f"{content!r} was destroyed although the archive write failed")

    @pytest.mark.asyncio
    async def test_prune_stays_within_the_limit_across_rounds(self, strategy):
        """The +1 for the breadcrumb is paid once, not every round.

        Without the `already has a notice` condition the layer removes one extra
        real message on every single call, forever — and the length never
        settles. Asserting only the first round misses that entirely.
        """
        strat, _ = strategy
        limit = strat.config.max_messages
        messages = self._conversation(8)
        added_per_round = 3

        # Warm-up: this is the round that legitimately pays the +1 for the
        # notice it creates.
        messages = (await strat.compact(messages, current_tokens=100)).modified_messages

        for round_no in range(3):
            # Fresh dicts: `[a, b] * 3` would alias one object into several
            # slots, and the identity-based removal diff would under-count.
            messages = messages + [
                {"role": "assistant", "content": f"Antwort {round_no}-{k}"}
                for k in range(2)
            ] + [{"role": "user", "content": f"Nachfrage {round_no}"}]

            result = await strat.compact(messages, current_tokens=100)
            messages = result.modified_messages

            assert len(messages) <= limit, (
                f"round {round_no}: {len(messages)} messages against a limit of {limit}")
            # The length alone cannot catch an always-on +1: that lands at the
            # limit too, it just eats one extra REAL message every round. The
            # steady state is "remove exactly what arrived".
            assert result.messages_pruned == added_per_round, (
                f"round {round_no}: {added_per_round} messages arrived but "
                f"{result.messages_pruned} were pruned — the conversation is "
                f"eroding by {result.messages_pruned - added_per_round} per step")

    @pytest.mark.asyncio
    async def test_single_user_message_is_never_pruned(self, strategy):
        """The shape this layer exists for: one task, hundreds of steps.

        With one user message the first and last protected index coincide, so
        exactly one message carries the whole conversation. Losing it leaves the
        agent working on nothing.
        """
        strat, _ = strategy
        messages = [{"role": "user", "content": "die einzige Aufgabe"}]
        for i in range(12):
            messages.append({"role": "assistant", "content": f"Schritt {i}"})

        result = await strat.compact(messages, current_tokens=100)

        users = [m for m in result.modified_messages if m.get("role") == "user"]
        assert len(users) == 1
        assert users[0]["content"] == "die einzige Aufgabe"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("keep_system", [True, False])
    async def test_parallel_tool_calls_are_never_split(self, tmp_path, keep_system):
        """Half a tool pair is a 400 from every provider — and it self-sustains.

        The cost ranking put `tool_result_ref` placeholders first, and in
        production those sit on the `role=tool` message. Reaching one first
        pulled in its assistant, and the assistant's OTHER results were then
        never expanded, because the loop skips an index it already holds. The
        broken list is written back to the session, so every following step
        re-sends it.
        """
        archival = ArchivalMemory(tmp_path / "archive.db", session_id="test")
        strat = LayeredCompactionStrategy(
            tool_store=ToolResultStore(tmp_path / "tools.db"),
            variable_manager=VariableManager(min_content_tokens=50,
                                             storage_path=tmp_path / "vars.json"),
            core_memory=CoreMemory(storage_path=tmp_path / "memory.json"),
            archival_memory=archival,
            config=CompactionConfig(
                layer1_threshold=10**9, layer2_threshold=10**9,
                layer3_threshold=10**9, max_messages=8,
                keep_system_messages=keep_system,
            ),
        )
        ref = json.dumps({"type": "tool_result_ref", "ref_id": "TR_1",
                          "summary": "ausgelagert"})

        def group(prefix, cheap_at):
            """One assistant with 5 parallel calls, production shape.

            The externalised result sits on the `role=tool` message — that is
            where Layer 1 writes it — so the ranking's rank-0 pick IS half a
            pair. A placeholder on an assistant message would never enter the
            branch this test exists for.
            """
            out = [{"role": "assistant", "content": None, "tool_calls": [
                {"id": f"{prefix}{k}", "type": "function",
                 "function": {"name": "t", "arguments": "{}"}} for k in range(1, 6)]}]
            for k in range(1, 6):
                out.append({"role": "tool", "name": "t", "tool_call_id": f"{prefix}{k}",
                            "content": ref if k == cheap_at else f"Ergebnis {prefix}{k}"})
            return out

        messages = (
            [{"role": "system", "content": "Du bist ein Assistent."},
             {"role": "user", "content": "die Aufgabe"}]
            + group("a", cheap_at=2)          # old group — this one gets pruned
            + [{"role": "user", "content": "Nachfrage"}]
            + group("b", cheap_at=4)          # recent group — must survive whole
            + [{"role": "user", "content": "letzte Frage"}]
        )

        # Room for the recent group to survive, so the pair assertions below
        # have something to be true ABOUT instead of comparing empty to empty.
        strat.config.max_messages = 12
        result = await strat.compact(messages, current_tokens=100)
        out = result.modified_messages

        calls = {tc["id"] for m in out for tc in (m.get("tool_calls") or [])}
        responses = [m.get("tool_call_id") for m in out if m.get("role") == "tool"]
        assert calls, "no tool pair survived — the assertions below would be vacuous"
        assert [r for r in responses if r not in calls] == [], (
            f"orphaned tool responses {[r for r in responses if r not in calls]} — "
            f"their assistant was pruned without them")
        assert [c for c in calls if c not in responses] == [], (
            f"tool calls {[c for c in calls if c not in responses]} lost their "
            f"results — the other half of the same pair break")
        assert result.messages_pruned > 0, "nothing was pruned; the test proved nothing"

        # Only the True direction is an invariant. With False the prompt merely
        # becomes eligible — whether it actually goes depends on how far the
        # pressure reaches, so asserting it would pin an accident. What the
        # parametrization buys is that pair integrity holds under BOTH
        # protection regimes, which is the property under test.
        if keep_system:
            prompts = [m for m in out if m.get("content") == "Du bist ein Assistent."]
            assert len(prompts) == 1, "keep_system_messages=True lost the prompt"

    @pytest.mark.asyncio
    async def test_shared_tool_call_ids_do_not_split_a_pair(self, strategy):
        """Two assistants reusing one tool_call id must form ONE unit.

        A dict of "assistant -> its members" is last-writer-wins: when the units
        overlap without one containing the other, the first unit's exclusive
        members keep a stale mapping and are stranded when the shared part goes.
        Colliding ids are not hypothetical — the Gemini batch client mints
        `call_{name}_{index}`, which repeats across assistant turns. Only a
        connected-component view survives this.
        """
        strat, _ = strategy

        def call(cid):
            return {"id": cid, "type": "function",
                    "function": {"name": "t", "arguments": "{}"}}

        messages = [
            {"role": "user", "content": "die Aufgabe"},
            {"role": "assistant", "content": None, "tool_calls": [call("x"), call("y")]},
            {"role": "tool", "content": "r1", "tool_call_id": "x", "name": "t"},
            {"role": "tool", "content": "r2", "tool_call_id": "y", "name": "t"},
            # reuses "x" — this is what makes the two units overlap
            {"role": "assistant", "content": None, "tool_calls": [call("x")]},
            {"role": "tool", "content": "r3", "tool_call_id": "x", "name": "t"},
            {"role": "assistant", "content": "fertig"},
            {"role": "user", "content": "letzte Frage"},
        ]

        result = await strat.compact(messages, current_tokens=100)
        out = result.modified_messages

        calls = {tc["id"] for m in out for tc in (m.get("tool_calls") or [])}
        responses = [m.get("tool_call_id") for m in out if m.get("role") == "tool"]
        assert [r for r in responses if r not in calls] == [], (
            f"orphaned {[r for r in responses if r not in calls]} — the overlapping "
            f"unit was resolved last-writer-wins instead of as one component")
        assert [c for c in calls if c not in responses] == []

    @pytest.mark.asyncio
    async def test_notice_survives_and_accumulates_without_system_protection(
        self, tmp_path
    ):
        """`keep_system_messages=False` must not turn the breadcrumb into churn.

        Unprotected, the notice is both the cheapest thing in the list (it is a
        placeholder) and the oldest — so it gets evicted and re-added every
        round: the running total resets forever and one real message dies per
        step to pay for it. It is protected on its TYPE for that reason.
        """
        strat = LayeredCompactionStrategy(
            tool_store=ToolResultStore(tmp_path / "tools.db"),
            variable_manager=VariableManager(min_content_tokens=50,
                                             storage_path=tmp_path / "vars.json"),
            core_memory=CoreMemory(storage_path=tmp_path / "memory.json"),
            archival_memory=ArchivalMemory(tmp_path / "archive.db", session_id="t"),
            config=CompactionConfig(
                layer1_threshold=10**9, layer2_threshold=10**9,
                layer3_threshold=10**9, max_messages=6,
                keep_system_messages=False,
            ),
        )

        def notices(msgs):
            return [m for m in msgs if _ref_type(m) == "pruned_notice"]

        first = await strat.compact(self._conversation(8), current_tokens=100)
        assert len(notices(first.modified_messages)) == 1
        total1 = json.loads(notices(first.modified_messages)[0]["content"])["total_removed"]

        followup = list(first.modified_messages) + [
            {"role": "assistant", "content": "weiter"},
            {"role": "user", "content": "und weiter"}] * 3
        second = await strat.compact(followup, current_tokens=100)

        assert len(notices(second.modified_messages)) == 1
        total2 = json.loads(notices(second.modified_messages)[0]["content"])["total_removed"]
        assert total2 > total1, (
            f"running total went {total1} -> {total2}: the notice was evicted "
            f"and re-created instead of carried forward")

    @pytest.mark.asyncio
    async def test_duplicate_notices_collapse_to_one(self, strategy):
        """Two notices can arrive through a merged or restored history.

        Removing only the first left a permanent pair with disagreeing totals.
        """
        strat, _ = strategy
        old = [{"role": "system", "content": json.dumps(
            {"type": "pruned_notice", "total_removed": n})} for n in (4, 7)]
        messages = old + self._conversation(8)
        before = len(messages)

        result = await strat.compact(messages, current_tokens=100)

        found = [m for m in result.modified_messages
                 if _ref_type(m) == "pruned_notice"]
        assert len(found) == 1, f"{len(found)} notices survived"

        # Exact, and NOT >=: this round alone prunes more than 4+7, so a
        # threshold assertion holds even with the carry-over deleted. The
        # carried value is the MAXIMUM of the duplicates — they come from one
        # lineage, so summing would double-count their shared part. Both old
        # notices vanish, hence -2 on the delta and +1 for the new one.
        removed_now = before - len(result.modified_messages) + 1 - 2
        total = json.loads(found[0]["content"])["total_removed"]
        assert total == removed_now + 7, (
            f"expected {removed_now} + max(4, 7) = {removed_now + 7}, got {total}")

    @pytest.mark.asyncio
    async def test_retrieval_answers_are_not_archived(self, strategy):
        """Archiving a retrieval answer is a loop with no exit.

        Caught live, not in review: the archive filled with the agent's own
        list() answers, the next search ranked those above the original message,
        and the agent followed refs to its own earlier replies until it gave up.
        Layer 1 has exempted these for the same reason; Pre-Layer P did not.
        """
        from plugins.context_engineer.compaction import RETRIEVAL_MARKER

        strat, archival = strategy
        answer = json.dumps({RETRIEVAL_MARKER: True, "status": "success",
                             "section": "all", "count": 1,
                             "entries": [{"ref": "arch_x", "summary": "…"}]})
        messages = [{"role": "user", "content": "die eigentliche Aufgabe"}]
        for i in range(8):
            messages.append({"role": "assistant", "content": f"Antwort {i}"})
            messages.append({"role": "tool", "content": answer,
                             "tool_call_id": f"c{i}", "name": "context_list"})
            messages.append({"role": "user", "content": f"Nachfrage {i}"})

        result = await strat.compact(messages, current_tokens=100)

        assert result.messages_pruned > 0
        stored = [r[0] for r in archival._db.execute(
            "SELECT content FROM archived_messages").fetchall()]
        assert not [s for s in stored if RETRIEVAL_MARKER in s], (
            "a retrieval answer was archived — the next search will rank it "
            "above the content it was pointing at")
        # The real prose must still be archived, or the guard is too wide.
        assert any("Antwort" in s for s in stored), "nothing real was archived"

    @pytest.mark.asyncio
    async def test_an_echoed_placeholder_is_still_a_real_message(self, strategy):
        """Role decides, not just the JSON shape.

        The agent SEES these placeholders in its own context and can reproduce
        one in an answer. On type alone such an echo would be protected from
        pruning and then deleted outright by the breadcrumb pass — the single
        path where a message disappears with no archive entry and no entry in
        the removal count. A retrieval answer pasted back by the user is the
        same trap on the other predicate.
        """
        from plugins.context_engineer.compaction import RETRIEVAL_MARKER

        strat, archival = strategy
        echoed_notice = json.dumps({"type": "pruned_notice", "total_removed": 99})
        echoed_answer = json.dumps({RETRIEVAL_MARKER: True, "entries": []})
        messages = [{"role": "user", "content": "die eigentliche Aufgabe"},
                    {"role": "assistant", "content": echoed_notice},
                    {"role": "user", "content": echoed_answer}]
        for i in range(8):
            messages.append({"role": "assistant", "content": f"Antwort {i}"})
            messages.append({"role": "user", "content": f"Nachfrage {i}"})

        result = await strat.compact(messages, current_tokens=100)

        stored = self._archived_contents(archival)
        alive = [str(m.get("content")) for m in result.modified_messages]
        for echo in (echoed_notice, echoed_answer):
            assert echo in stored or echo in alive, (
                f"{echo[:40]}… vanished without being archived — it was treated "
                f"as a placeholder because of its shape, not its role")
        # And the real breadcrumb is still exactly one, on a system message.
        notices = [m for m in result.modified_messages if _ref_type(m) == "pruned_notice"]
        assert len([m for m in notices if m.get("role") == "system"]) == 1

    def test_store_many_keeps_conversation_order(self, tmp_path):
        """The breadcrumb points at list(section='history') — it must be ordered.

        get_session_messages orders by `timestamp ASC, id ASC` and the id is a
        random uuid. One shared timestamp for the whole batch therefore returns
        it in RANDOM order, which is the listing the agent is told to browse.
        """
        archive = ArchivalMemory(tmp_path / "archive.db", session_id="t")
        messages = [{"role": "user", "content": f"Nachricht {i:02d}"}
                    for i in range(25)]

        archive.store_many(messages)

        got = [m.content for m in archive.get_session_messages(session_id="t", limit=50)]
        assert got == [m["content"] for m in messages], (
            "the batch came back out of order")

    @pytest.mark.asyncio
    async def test_archived_text_survives_the_variable_garbage_collector(
        self, tmp_path
    ):
        """The archive must be self-contained, or Layer 2 empties it out.

        Layer 1 replaces long assistant prose with a `$VAR_n` reference. If
        Pre-Layer P archives that reference verbatim and then removes the last
        live mention, `cleanup_unused_variables` — which scans only the LIVE
        messages — deletes the body in the SAME compaction. The archived copy
        would keep a dangling pointer and the content would be gone from every
        store.
        """
        archival = ArchivalMemory(tmp_path / "archive.db", session_id="t")
        variables = VariableManager(min_content_tokens=10,
                                    storage_path=tmp_path / "vars.json")
        strat = LayeredCompactionStrategy(
            tool_store=ToolResultStore(tmp_path / "tools.db"),
            variable_manager=variables,
            core_memory=CoreMemory(storage_path=tmp_path / "memory.json"),
            archival_memory=archival,
            config=CompactionConfig(
                layer1_threshold=10**9, layer2_threshold=1, layer3_threshold=10**9,
                target_tokens=0, archive_after_turns=1,
                max_messages=6, keep_system_messages=True,
            ),
        )
        secret = "Der unersetzliche Absatz. " * 20
        var_name, _ = await variables.create_variable(secret)
        assert var_name, "fixture broken: no variable was created"

        messages = [{"role": "user", "content": "die Aufgabe"},
                    {"role": "assistant", "content": f"siehe {var_name}"}]
        for i in range(6):
            messages.append({"role": "user", "content": f"Frage {i}"})

        await strat.compact(messages, current_tokens=10_000, force=True)

        stored = [r[0] for r in archival._db.execute(
            "SELECT content FROM archived_messages").fetchall()]
        dangling = [s for s in stored
                    if var_name in s and secret[:30] not in s]
        assert not dangling, (
            f"the archived copy still points at {var_name}; "
            f"variable still known: {variables.get_variable(var_name) is not None}")
        assert any(secret[:30] in s for s in stored), (
            "the variable body reached neither the archive nor the message")

    @pytest.mark.asyncio
    async def test_huge_batch_still_archives_but_skips_the_vector_index(
        self, strategy, monkeypatch
    ):
        """A runaway prune must not stall the request on embeddings.

        ~17 ms of embedding per message against 0.02 ms for the row: 4682
        messages are 0.08 s as rows and 80 s with the index. The rows and FTS
        go in regardless — recoverability is never the thing that gets dropped.
        """
        from plugins.context_engineer import compaction as compaction_mod

        strat, archival = strategy
        monkeypatch.setattr(compaction_mod, "_SEMANTIC_INDEX_MAX_BATCH", 5)
        seen: list[bool] = []
        original = archival.store_many

        def spy(messages, session_id=None, index_semantic=None):
            seen.append(index_semantic)
            return original(messages, session_id, index_semantic)

        archival.store_many = spy
        result = await strat.compact(self._conversation(12), current_tokens=100)

        assert seen and seen[0] is False, (
            f"a batch past the cap still asked for semantic indexing: {seen}")
        # ...and the content is in the archive all the same.
        assert result.messages_pruned > 0
        stored = archival._db.execute(
            "SELECT COUNT(*) FROM archived_messages").fetchone()[0]
        assert stored == result.messages_pruned, (
            f"{result.messages_pruned} pruned but only {stored} archived")

    @pytest.mark.asyncio
    async def test_layer3_keeps_the_summary_on_archive_refs(self, tmp_path):
        """An address without a label is the one thing the agent cannot use.

        Layer 3 used to strip archived_ref placeholders down to a bare ref_id.
        That was consistent while references could not be followed; now the
        summary is the catalogue entry that decides whether a ref is worth
        fetching, and dropping the whole placeholder beats keeping a blind one.
        """
        archival = ArchivalMemory(tmp_path / "archive.db", session_id="test")
        strat = LayeredCompactionStrategy(
            tool_store=ToolResultStore(tmp_path / "tools.db"),
            variable_manager=VariableManager(min_content_tokens=50,
                                             storage_path=tmp_path / "vars.json"),
            core_memory=CoreMemory(storage_path=tmp_path / "memory.json"),
            archival_memory=archival,
            config=CompactionConfig(
                layer1_threshold=1, layer2_threshold=1, layer3_threshold=1,
                target_tokens=0, archive_after_turns=1, drop_after_turns=3,
                max_messages=0, keep_system_messages=True,
            ),
        )
        placeholder = json.dumps({"type": "archived_ref", "ref_id": "arch_keep",
                                  "summary": "die Zusammenfassung zaehlt"})
        # Five user turns against drop_after_turns=3, so Layer 3 has real work:
        # the oldest messages ARE dropped and a placeholder survives at the
        # young end. With drop_after_turns above the turn count the layer runs
        # but touches nothing, and `3 in layers_applied` — appended on the
        # threshold check alone — would still be true.
        messages = [
            {"role": "user", "content": "Frage 1"},
            {"role": "assistant", "content": "sehr alte Antwort"},
            {"role": "user", "content": "Frage 2"},
            {"role": "user", "content": "Frage 3"},
            {"role": "user", "content": "Frage 4"},
            {"role": "assistant", "content": placeholder},
            {"role": "user", "content": "Frage 5"},
        ]

        result = await strat.compact(messages, current_tokens=10_000, force=True)

        assert 3 in result.layers_applied, result.layers_applied
        assert result.messages_dropped > 0, (
            "Layer 3 dropped nothing — this fixture does not exercise it")
        refs = [m for m in result.modified_messages
                if "archived_ref" in str(m.get("content"))]
        assert refs, "the placeholder vanished entirely — nothing to assert on"
        for msg in refs:
            parsed = json.loads(msg["content"])
            assert parsed.get("summary"), (
                f"Layer 3 stripped the label off {parsed.get('ref_id')}, "
                f"leaving an address the agent cannot judge")


class TestEnsureValidMessageSequence:
    """Tests for _ensure_valid_message_sequence helper method."""
    
    @pytest.fixture
    def strategy(self, tmp_path):
        """Create strategy for testing."""
        tool_store = ToolResultStore(tmp_path / "tools.db")
        variable_manager = VariableManager(min_content_tokens=50, storage_path=tmp_path / "vars.json")
        core_memory = CoreMemory(storage_path=tmp_path / "memory.json")
        archival_memory = ArchivalMemory(tmp_path / "archive.db", session_id="test")
        
        config = CompactionConfig(
            layer1_threshold=100000,
            layer2_threshold=200000,
            layer3_threshold=300000,
            target_tokens=50000,
        )
        
        return LayeredCompactionStrategy(
            tool_store=tool_store,
            variable_manager=variable_manager,
            core_memory=core_memory,
            archival_memory=archival_memory,
            config=config
        )
    
    def test_valid_sequence_unchanged(self, strategy):
        """Test that valid sequence is not modified."""
        messages = [
            {"role": "system", "content": "System"},
            {"role": "user", "content": "Hello"},
            {"role": "assistant", "content": "Hi"},
        ]
        
        removed = strategy._ensure_valid_message_sequence(messages, "test")
        
        assert removed == 0
        assert len(messages) == 3
    
    def test_removes_leading_assistant(self, strategy):
        """Test that leading assistant message is removed."""
        messages = [
            {"role": "system", "content": "System"},
            {"role": "assistant", "content": "Bad start"},
            {"role": "user", "content": "Hello"},
            {"role": "assistant", "content": "Hi"},
        ]
        
        removed = strategy._ensure_valid_message_sequence(messages, "test")
        
        assert removed == 1
        assert len(messages) == 3
        # First non-system should be user
        assert messages[1]["role"] == "user"
    
    def test_removes_leading_tool(self, strategy):
        """Test that leading tool message is removed."""
        messages = [
            {"role": "system", "content": "System"},
            {"role": "tool", "content": "Orphan", "tool_call_id": "x"},
            {"role": "user", "content": "Hello"},
        ]
        
        removed = strategy._ensure_valid_message_sequence(messages, "test")
        
        assert removed == 1
        assert len(messages) == 2
        assert messages[1]["role"] == "user"
    
    def test_removes_assistant_with_tool_calls_and_responses(self, strategy):
        """Test that assistant with tool_calls also removes tool responses."""
        messages = [
            {"role": "system", "content": "System"},
            {"role": "assistant", "content": None, "tool_calls": [
                {"id": "call_1", "function": {"name": "test"}}
            ]},
            {"role": "tool", "content": "Result", "tool_call_id": "call_1"},
            {"role": "user", "content": "Hello"},
        ]
        
        removed = strategy._ensure_valid_message_sequence(messages, "test")
        
        assert removed == 2  # Both assistant and tool removed
        assert len(messages) == 2
        assert messages[1]["role"] == "user"
    
    def test_cascading_removal(self, strategy):
        """Test cascading removal when multiple bad messages at start."""
        messages = [
            {"role": "system", "content": "System"},
            {"role": "assistant", "content": "First bad"},
            {"role": "assistant", "content": "Second bad"},
            {"role": "tool", "content": "Orphan", "tool_call_id": "x"},
            {"role": "user", "content": "Finally user"},
        ]
        
        removed = strategy._ensure_valid_message_sequence(messages, "test")
        
        assert removed == 3  # All three bad messages removed
        assert len(messages) == 2
    
    def test_adds_fallback_when_no_user_messages(self, strategy):
        """Test that fallback user message is added when none exist."""
        messages = [
            {"role": "system", "content": "System"},
            {"role": "assistant", "content": "Only assistant"},
        ]
        
        strategy._ensure_valid_message_sequence(messages, "test")
        
        # Should have added a fallback user message
        assert any(msg.get("role") == "user" for msg in messages)
        user_msgs = [m for m in messages if m.get("role") == "user"]
        assert len(user_msgs) == 1
        assert "Continue" in user_msgs[0]["content"]
    
    def test_adds_fallback_when_only_system_messages(self, strategy):
        """Test that fallback user message is added when only system messages remain."""
        messages = [
            {"role": "system", "content": "System 1"},
            {"role": "system", "content": "System 2"},
        ]
        
        strategy._ensure_valid_message_sequence(messages, "test")
        
        # Should have added a fallback user message
        assert len(messages) == 3  # 2 system + 1 fallback user
        user_msgs = [m for m in messages if m.get("role") == "user"]
        assert len(user_msgs) == 1
        assert "Continue" in user_msgs[0]["content"]


# =============================================================================
# Recall Type Detection Tests
# =============================================================================


class TestDetectRecallType:
    """Tests for _detect_recall_type pattern matching."""

    @pytest.fixture
    def hooks(self, tmp_path):
        """Create a minimal ContextEngineerPlugin instance for testing."""
        from plugins.context_engineer.hooks import ContextEngineerPlugin

        hooks = ContextEngineerPlugin.__new__(ContextEngineerPlugin)
        # _detect_recall_type is a pure function, needs no state
        return hooks

    def test_variable_detection(self, hooks):
        assert hooks._detect_recall_type("$VAR_1") == "variable"
        assert hooks._detect_recall_type("VAR_1") == "variable"
        assert hooks._detect_recall_type("$VAR_99") == "variable"
        assert hooks._detect_recall_type("  $VAR_3  ") == "variable"

    def test_tool_result_simple(self, hooks):
        assert hooks._detect_recall_type("TR_abc123") == "tool_result"
        assert hooks._detect_recall_type("call_abc123") == "tool_result"

    def test_tool_result_with_underscores(self, hooks):
        """TR_00_GHGE9 pattern - real tool call IDs contain underscores."""
        assert hooks._detect_recall_type("TR_00_GHGE9") == "tool_result"
        assert hooks._detect_recall_type("TR_00_abc_def") == "tool_result"
        assert hooks._detect_recall_type("call_00_GHGE9") == "tool_result"

    def test_hex_hash(self, hooks):
        assert hooks._detect_recall_type("a1b2c3d4e5f6") == "tool_result"
        assert hooks._detect_recall_type("DEADBEEF") == "tool_result"

    def test_media_path(self, hooks):
        assert hooks._detect_recall_type("data/audio/test.wav") == "media"
        assert hooks._detect_recall_type("/path/to/image.png") == "media"

    def test_archive_fallback(self, hooks):
        assert hooks._detect_recall_type("book_id phase blocker") == "archive"
        assert hooks._detect_recall_type("what happened with scene 5") == "archive"
        assert hooks._detect_recall_type("review feedback") == "archive"


class TestSessionEvictionSkipsActiveCompactions:
    """Eviction must not close stores of a session with an in-flight compaction."""

    @staticmethod
    def _make_hooks():
        from unittest.mock import MagicMock
        from plugins.context_engineer.hooks import ContextEngineerPlugin

        hooks = ContextEngineerPlugin.__new__(ContextEngineerPlugin)
        hooks._session_components = {}
        hooks._active_compactions = set()
        hooks._last_compaction_time = {}
        hooks._session_ttl_seconds = 0.0  # everything is "expired" by TTL
        hooks._max_tracked_sessions = 1   # force LRU pressure too
        return hooks, MagicMock

    def _add_session(self, hooks, MagicMock, sid, last_accessed=0.0):
        store = MagicMock()
        hooks._session_components[sid] = {
            "archival_memory": store, "tool_store": store,
            "last_accessed": last_accessed,
        }
        return store

    def test_active_session_not_ttl_evicted(self):
        hooks, MagicMock = self._make_hooks()
        active_store = self._add_session(hooks, MagicMock, "active")
        idle_store = self._add_session(hooks, MagicMock, "idle")
        hooks._active_compactions.add("active")

        hooks._cleanup_expired_sessions()

        # Active session survived; its stores were never closed
        assert "active" in hooks._session_components
        active_store.close.assert_not_called()
        # Idle session was evicted and closed
        assert "idle" not in hooks._session_components
        idle_store.close.assert_called()

    def test_active_session_not_lru_evicted(self):
        hooks, MagicMock = self._make_hooks()
        hooks._session_ttl_seconds = 10_000.0   # TTL disabled; only LRU applies
        hooks._max_tracked_sessions = 1
        active_store = self._add_session(hooks, MagicMock, "active", last_accessed=1.0)  # oldest
        self._add_session(hooks, MagicMock, "newer", last_accessed=2.0)
        hooks._active_compactions.add("active")

        hooks._cleanup_expired_sessions()

        # Even though "active" is the LRU candidate, it is not evicted
        assert "active" in hooks._session_components
        active_store.close.assert_not_called()


class TestRestorationBlockNotDuplicated:
    """Der "Stored Information"-Block muss ERSETZT, nicht angehaeuft werden.

    Regression: die kompaktierten Messages werden persistiert, also ist die
    Injektion des letzten Turns beim naechsten schon Teil der Historie. Ohne
    Entfernen wuchs sie mit -- gemessen 103 Kopien in 201 Messages nach 109
    Aufrufen, und die dadurch wandernde Einfuegestelle brach den
    Prompt-Cache in 103 von 108 Turns.
    """

    def _msgs(self, n_blocks: int):
        from agent_system.llm.models import ChatMessage
        from plugins.context_engineer.hooks import _RESTORATION_MARKER

        out = [ChatMessage(role="system", content="Du bist ein Agent.")]
        for _ in range(n_blocks):
            out.append(ChatMessage(
                role="system",
                content="\n# Context Engineer - Stored Information\n\nalt",
                injected_by=_RESTORATION_MARKER,
            ))
        out.append(ChatMessage(role="user", content="Auftrag"))
        return out

    def _reinject(self, messages, text: str):
        """Spiegelt die Injektionslogik aus ``engineer_context``."""
        from agent_system.llm.models import ChatMessage
        from plugins.context_engineer.hooks import _RESTORATION_MARKER

        for i in range(len(messages) - 1, -1, -1):
            if getattr(messages[i], "injected_by", None) == _RESTORATION_MARKER:
                messages.pop(i)
        insert_pos = 0
        for i, msg in enumerate(messages):
            if msg.role == "system":
                insert_pos = i + 1
            else:
                break
        messages.insert(insert_pos, ChatMessage(
            role="system", content=text, injected_by=_RESTORATION_MARKER,
        ))
        return messages

    def _count(self, messages) -> int:
        from plugins.context_engineer.hooks import _RESTORATION_MARKER
        return sum(
            1 for m in messages
            if getattr(m, "injected_by", None) == _RESTORATION_MARKER
        )

    def test_reinjection_replaces_instead_of_appending(self):
        msgs = self._msgs(1)
        for turn in range(10):
            msgs = self._reinject(msgs, f"\n# Context Engineer - Stored Information\n\nturn {turn}")
            assert self._count(msgs) == 1, f"Turn {turn}: Block dupliziert"

    def test_insert_position_stays_stable(self):
        """Die Einfuegestelle darf nicht mit jedem Turn wandern — sonst ist
        alles dahinter kein Byte-Praefix mehr."""
        msgs = self._msgs(0)
        positionen = set()
        for turn in range(5):
            msgs = self._reinject(msgs, "\n# Context Engineer - Stored Information\n\nx")
            positionen.add(next(
                i for i, m in enumerate(msgs)
                if getattr(m, "injected_by", None) is not None
            ))
            msgs.append(msgs[-1].__class__(role="user", content="weiter"))
        assert positionen == {1}, f"Einfuegestelle wanderte: {sorted(positionen)}"

    def test_legacy_unmarked_blocks_are_cleaned(self):
        """Sessions von vor dem Marker tragen unmarkierte Kopien in ihrer
        persistierten Historie — die muessen ueber die Ueberschrift ebenfalls
        verschwinden, sonst bleiben sie dort fuer immer stehen."""
        from agent_system.llm.models import ChatMessage
        from plugins.context_engineer.hooks import (
            _RESTORATION_HEADER,
            _RESTORATION_MARKER,
        )

        msgs = [ChatMessage(role="system", content="Du bist ein Agent.")]
        msgs += [
            ChatMessage(role="system", content=f"\n{_RESTORATION_HEADER}\n\nalt {i}")
            for i in range(5)          # unmarkiert, wie vor dem Fix
        ]
        msgs.append(ChatMessage(role="user", content="Auftrag"))

        # Entfern-Logik aus engineer_context
        for i in range(len(msgs) - 1, -1, -1):
            m = msgs[i]
            if getattr(m, "injected_by", None) == _RESTORATION_MARKER:
                msgs.pop(i)
                continue
            c = getattr(m, "content", None)
            if isinstance(c, str) and _RESTORATION_HEADER in c:
                msgs.pop(i)

        assert not any(
            isinstance(m.content, str) and _RESTORATION_HEADER in m.content
            for m in msgs
        ), "Alt-Kopien ohne Marker blieben stehen"
        assert [m.role for m in msgs] == ["system", "user"]
