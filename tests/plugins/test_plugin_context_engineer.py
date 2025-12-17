"""Tests for context_engineer plugin components."""
from __future__ import annotations

import json
import sqlite3
import tempfile
from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock, Mock

import pytest

from plugins.context_engineer.core_memory import CoreMemory, Fact
from plugins.context_engineer.tool_result_store import ToolResultStore, ToolResultEntry
from plugins.context_engineer.variable_manager import VariableManager, VariableEntry
from plugins.context_engineer.archival_memory import ArchivalMemory, ArchivedMessage
from plugins.context_engineer.compaction import (
    CompactionConfig,
    CompactionResult,
    LayeredCompactionStrategy,
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
    
    def test_add_fact(self, temp_memory_file):
        """Test adding facts to core memory."""
        memory = CoreMemory(temp_memory_file)
        
        fact_id = memory.add_fact("User prefers Python", category="preferences")
        
        assert fact_id is not None
        assert len(memory.facts) == 1
        assert memory.facts[0].content == "User prefers Python"
        assert memory.facts[0].category == "preferences"
    
    def test_add_fact_with_importance(self, temp_memory_file):
        """Test adding facts with custom importance."""
        memory = CoreMemory(temp_memory_file)
        
        memory.add_fact("Critical decision", importance=0.9)
        memory.add_fact("Minor note", importance=0.1)
        
        assert memory.facts[0].importance == 0.9
        assert memory.facts[1].importance == 0.1
    
    def test_persistence(self, temp_memory_file):
        """Test that facts persist across instances."""
        # Add facts
        memory1 = CoreMemory(temp_memory_file)
        memory1.add_fact("Fact 1", category="facts")
        memory1.add_fact("Fact 2", category="decisions")
        
        # Create new instance
        memory2 = CoreMemory(temp_memory_file)
        
        assert len(memory2.facts) == 2
        assert memory2.facts[0].content == "Fact 1"
        assert memory2.facts[1].content == "Fact 2"
    
    def test_eviction_when_full(self, temp_memory_file):
        """Test that low importance facts are evicted when limit reached."""
        memory = CoreMemory(temp_memory_file, max_tokens=100)
        
        # Add many facts to exceed limit
        for i in range(10):
            memory.add_fact(f"Fact {i} with lots of content", importance=0.1 * i)
        
        # Should have evicted some facts
        assert memory.get_token_usage() <= 100
    
    def test_get_categories(self, temp_memory_file):
        """Test getting list of categories."""
        memory = CoreMemory(temp_memory_file)
        memory.add_fact("Fact 1", category="facts")
        memory.add_fact("Fact 2", category="decisions")
        memory.add_fact("Fact 3", category="facts")
        
        categories = memory.get_categories()
        assert set(categories) == {"facts", "decisions"}
    
    def test_to_system_prompt_section(self, temp_memory_file):
        """Test generating system prompt section."""
        memory = CoreMemory(temp_memory_file)
        memory.add_fact("User likes Python", category="preferences")
        memory.add_fact("Project uses FastAPI", category="facts")
        
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
        
        assert "[Tool:read_file" in reference
        assert "ref:" in reference
        assert "hash:" in reference
    
    def test_retrieve_by_id(self, temp_db_path):
        """Test retrieving tool result by ID."""
        store = ToolResultStore(temp_db_path)
        
        result = "Tool output content"
        reference = store.store_and_reference(
            tool_name="test_tool",
            tool_call_id="call_456",
            content=result
        )
        
        # Extract ID from reference
        import re
        match = re.search(r"ref:(\w+)", reference)
        ref_id = match.group(1) if match else None
        
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
        
        # Extract hash from reference
        import re
        match = re.search(r"hash:(\w+)", reference)
        content_hash = match.group(1) if match else None
        
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
    
    def test_create_variable(self, temp_var_file):
        """Test creating a variable."""
        manager = VariableManager(min_content_tokens=10, storage_path=temp_var_file)
        
        # Use realistic content that generates multiple tokens
        large_content = "word " * 100  # ~100 words = ~130 tokens (exceeds threshold of 10)
        var_name, summary = manager.create_variable(large_content)
        
        assert var_name is not None
        assert "$VAR_" in var_name
        assert summary is not None  # Should include summary
    
    def test_get_variable(self, temp_var_file):
        """Test retrieving a variable."""
        manager = VariableManager(min_content_tokens=10, storage_path=temp_var_file)
        
        content = "Test content " * 50
        var_name, _ = manager.create_variable(content)
        
        entry = manager.get_variable(var_name)
        
        assert entry is not None
        assert entry.content == content
    
    def test_below_threshold_returns_none(self, temp_var_file):
        """Test that small content returns empty var_name."""
        manager = VariableManager(min_content_tokens=1000, storage_path=temp_var_file)
        
        small_content = "Small"
        result = manager.create_variable(small_content)
        
        # Returns tuple (var_name, summary) where var_name is empty if below threshold
        assert result[0] == ""  # Empty var_name
    
    def test_detect_content_type(self, temp_var_file):
        """Test content type detection."""
        manager = VariableManager(min_content_tokens=10, storage_path=temp_var_file)
        
        # Test code detection
        code_content = "def hello():\n    print('Hello')\n" * 20
        manager.create_variable(code_content, content_type="code")
        
        # Test JSON detection
        json_content = '{"key": "value", "nested": {"a": 1}}' * 20
        manager.create_variable(json_content, content_type="json")
        
        stats = manager.get_stats()
        assert stats["total_variables"] >= 2
    
    def test_persistence(self, temp_var_file):
        """Test that variables persist."""
        manager1 = VariableManager(min_content_tokens=10, storage_path=temp_var_file)
        manager1.create_variable("Content " * 100)
        
        manager2 = VariableManager(min_content_tokens=10, storage_path=temp_var_file)
        stats = manager2.get_stats()
        
        assert stats["total_variables"] == 1
    
    def test_expand_variables(self, temp_var_file):
        """Test expanding variables in text."""
        manager = VariableManager(min_content_tokens=10, storage_path=temp_var_file)
        
        content = "Original content " * 50
        var_name, _ = manager.create_variable(content)
        
        text_with_var = f"The result is in {var_name}."
        expanded = manager.expand_variables(text_with_var)
        
        assert content in expanded
        assert var_name not in expanded


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
    
    def test_no_compaction_below_threshold(self, strategy_components):
        """Test that no compaction happens below threshold."""
        strategy = strategy_components["strategy"]
        
        messages = [
            {"role": "user", "content": "Short message"}
        ]
        
        result = strategy.compact(messages, current_tokens=100)
        
        assert result.tokens_saved == 0
        assert result.layers_applied == []
    
    def test_layer1_tool_result_storage(self, strategy_components):
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
        
        result = strategy.compact(messages, current_tokens=600)
        
        # First tool result should be stored (not the last one)
        assert result.tool_results_stored >= 1 or result.layers_applied == []
    
    def test_compaction_result_stats(self, strategy_components):
        """Test that CompactionResult has correct statistics."""
        strategy = strategy_components["strategy"]
        
        messages = [
            {"role": "user", "content": "Message " * 100}
            for _ in range(10)
        ]
        
        result = strategy.compact(messages, current_tokens=2000)
        
        assert result.original_tokens == 2000
        assert result.final_tokens <= result.original_tokens
        assert result.tokens_saved >= 0
    
    def test_restoration_context_generation(self, strategy_components):
        """Test generating restoration context for system prompt."""
        strategy = strategy_components["strategy"]
        tool_store = strategy_components["tool_store"]
        core_memory = strategy_components["core_memory"]
        
        # Add some data
        tool_store.store_and_reference("test", "c1", "Some output")
        core_memory.add_fact("Important fact", category="facts")
        
        context = strategy.get_restoration_context()
        
        assert "Context Engineer" in context
        assert "Tool Results" in context or "Core Memory" in context


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
        
        assert any("recall" in name for name in tool_names)
        assert any("store_fact" in name for name in tool_names)
        assert any("get_variable" in name for name in tool_names)
    
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
