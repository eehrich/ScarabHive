"""
Config-wiring + per-session cap tests for MemoryServer (PB20).

Before the fix these schema.yaml fields were dead config: the inject hook
hardcoded max_memories/use_semantic and _operation_store never enforced
max_memories_per_session (unbounded JSON + vector growth).
"""

import pytest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

from plugins.memory.server import MemoryServer


def _config(tmp_path: Path, **overrides) -> MagicMock:
    cfg = MagicMock()
    cfg.storage_path = str(tmp_path / "mem")
    cfg.max_memories = 10
    cfg.max_memories_per_session = 5000
    cfg.auto_extract_keywords = True
    cfg.search_n_results = 5
    cfg.use_semantic_injection = True
    for k, v in overrides.items():
        setattr(cfg, k, v)
    return cfg


def _server(tmp_path: Path, **overrides) -> MemoryServer:
    return MemoryServer(name="memory", system_config=MagicMock(), server_config=_config(tmp_path, **overrides))


class TestConfigWiring:
    def test_reads_config_values(self, tmp_path):
        srv = _server(
            tmp_path,
            max_memories=7,
            max_memories_per_session=42,
            search_n_results=3,
            use_semantic_injection=False,
            auto_extract_keywords=False,
        )
        assert srv.max_memories == 7
        assert srv.max_memories_per_session == 42
        assert srv.search_n_results == 3
        assert srv.use_semantic_injection is False
        assert srv.auto_extract_keywords is False

    def test_defaults_when_absent(self, tmp_path):
        cfg = MagicMock()
        cfg.storage_path = str(tmp_path / "mem")
        for attr in ("max_memories", "max_memories_per_session",
                     "search_n_results", "use_semantic_injection",
                     "auto_extract_keywords"):
            delattr(cfg, attr)  # force getattr(..., None) -> schema default
        srv = MemoryServer(name="memory", system_config=MagicMock(), server_config=cfg)
        assert srv.max_memories == 10
        assert srv.max_memories_per_session == 5000
        assert srv.search_n_results == 5
        assert srv.use_semantic_injection is True
        assert srv.auto_extract_keywords is True

    def test_hook_uses_configured_max(self, tmp_path):
        # The hook reads self.max_memories (no longer hardcoded 10).
        srv = _server(tmp_path, max_memories=3)
        assert srv.max_memories == 3


class TestPerSessionCap:
    @pytest.mark.asyncio
    async def test_cap_evicts_least_important_oldest(self, tmp_path):
        srv = _server(tmp_path, max_memories_per_session=3)
        srv._delete_memory_from_chroma = AsyncMock()
        srv._store_memory_in_chroma = AsyncMock()

        # Store 5 memories with increasing importance 1..5
        for i in range(1, 6):
            await srv._operation_store(
                session_id="s1",
                title=f"m{i}",
                content=f"content {i}",
                importance=i,
            )

        coll = await srv._load_collection("s1")
        assert len(coll.memories) == 3  # cap enforced
        kept_importance = sorted(m.importance for m in coll.memories.values())
        # the two least-important (1,2) were evicted; 3,4,5 survive
        assert kept_importance == [3, 4, 5]
        # eviction also removed them from the vector store
        assert srv._delete_memory_from_chroma.await_count == 2

    @pytest.mark.asyncio
    async def test_cap_zero_disables_enforcement(self, tmp_path):
        srv = _server(tmp_path, max_memories_per_session=0)
        srv._delete_memory_from_chroma = AsyncMock()
        srv._store_memory_in_chroma = AsyncMock()
        for i in range(4):
            await srv._operation_store(session_id="s1", title=f"m{i}", content="x", importance=5)
        coll = await srv._load_collection("s1")
        assert len(coll.memories) == 4
        srv._delete_memory_from_chroma.assert_not_awaited()


class TestKeywordGate:
    @pytest.mark.asyncio
    async def test_auto_extract_disabled_keeps_keywords_empty(self, tmp_path):
        srv = _server(tmp_path, auto_extract_keywords=False)
        srv._store_memory_in_chroma = AsyncMock()
        await srv._operation_store(session_id="s1", title="title here", content="body text")
        coll = await srv._load_collection("s1")
        mem = next(iter(coll.memories.values()))
        assert mem.keywords == []

    @pytest.mark.asyncio
    async def test_auto_extract_enabled_fills_keywords(self, tmp_path):
        srv = _server(tmp_path, auto_extract_keywords=True)
        srv._store_memory_in_chroma = AsyncMock()
        await srv._operation_store(session_id="s1", title="alpha beta", content="gamma delta")
        coll = await srv._load_collection("s1")
        mem = next(iter(coll.memories.values()))
        assert len(mem.keywords) > 0
