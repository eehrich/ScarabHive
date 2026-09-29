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
        assert srv.max_memories_per_session == 0  # not the default: 0 is the setting that turns the cap off
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


class TestAgentOverrides:
    @pytest.mark.asyncio
    async def test_an_agents_override_wins_over_the_server_entry(self, tmp_path):
        from agent_system.hooks.plugin_hook import HookContext, HookType
        from agent_system.llm.models import ChatMessage

        srv = _server(tmp_path, max_memories=10, use_semantic_injection=True)
        srv._store_memory_in_chroma = AsyncMock()
        srv._operation_search = AsyncMock(side_effect=AssertionError("the agent switched semantic injection off"))
        for i in range(3):
            await srv._operation_store(session_id="s1", title=f"memory {i}", content="x", importance=5)
        context = HookContext(hook_type=HookType.PRE_LLM_CALL, request_id="r-1", session_id="s1",
                              messages=[ChatMessage(role="user", content="hello")],
                              hook_config={"max_memories": 1, "use_semantic_injection": False})

        result = await srv.on_pre_llm_call(context)

        injected = context.messages[-1].content
        assert result.metadata == {"injected_memories": 1} and injected.count("- `mem_") == 1


class TestWrongValues:
    """What the model gets wrong is refused with a reason -- never kept, never an exception."""

    @pytest.mark.asyncio
    async def test_an_update_out_of_range_is_refused_and_the_session_still_loads(self, tmp_path):
        srv = _server(tmp_path)
        srv._store_memory_in_chroma = AsyncMock()
        await srv.execute({"operation": "store", "title": "Keep me", "content": "x", "session_id": "s1"})

        answers = [await srv.execute({"operation": "update", "memory_id": "mem_001", "session_id": "s1", **change})
                   for change in ({"importance": 0}, {"title": "t" * 201}, {"content": ""})]
        # a number sent as text is kept as the number it is: sorted and summed with the others
        await srv.execute({"operation": "update", "memory_id": "mem_001", "session_id": "s1", "importance": "7"})
        fresh = _server(tmp_path)  # a restart: the file is read again
        listed = await fresh.execute({"operation": "list", "sort_by": "importance", "session_id": "s1"})

        assert [answer["error"].startswith("Cannot update mem_001") for answer in answers] == [True] * 3
        assert [(m["title"], m["importance"]) for m in listed["memories"]] == [("Keep me", 7)]

    @pytest.mark.asyncio
    async def test_a_store_out_of_range_is_refused_and_takes_no_id(self, tmp_path):
        srv = _server(tmp_path)
        srv._store_memory_in_chroma = AsyncMock()

        refused = await srv.execute({"operation": "store", "title": "Too important", "content": "x", "importance": 11,
                                     "session_id": "s1"})
        stored = await srv.execute({"operation": "store", "title": "Fine", "content": "x", "session_id": "s1"})

        assert refused["error"].startswith("Cannot store") and stored["memory_id"] == "mem_001"
        srv._store_memory_in_chroma.assert_awaited_once()


class TestIds:
    @pytest.mark.asyncio
    async def test_a_deleted_newest_id_is_not_handed_out_again(self, tmp_path):
        srv = _server(tmp_path)
        srv._store_memory_in_chroma = AsyncMock()
        srv._delete_memory_from_chroma = AsyncMock()
        for title in ("First", "Second"):
            await srv.execute({"operation": "store", "title": title, "content": "x", "session_id": "s1"})
        await srv.execute({"operation": "delete", "memory_id": "mem_002", "session_id": "s1"})

        fresh = _server(tmp_path)  # a restart: the counter comes from the file
        fresh._store_memory_in_chroma = AsyncMock()
        stored = await fresh.execute({"operation": "store", "title": "Third", "content": "x", "session_id": "s1"})

        assert stored["memory_id"] == "mem_003"


class TestRecall:
    @pytest.mark.asyncio
    async def test_recall_counts_an_access_and_changes_nothing(self, tmp_path):
        srv = _server(tmp_path)
        srv._store_memory_in_chroma = AsyncMock()
        await srv.execute({"operation": "store", "title": "Read me", "content": "x", "session_id": "s1"})
        before = (await srv._load_collection("s1")).memories["mem_001"].updated_at

        await srv.execute({"operation": "recall", "memory_id": "mem_001", "session_id": "s1"})

        memory = (await srv._load_collection("s1")).memories["mem_001"]
        assert (memory.access_count, memory.updated_at) == (1, before)


class TestHookFallback:
    @pytest.mark.asyncio
    async def test_a_failing_search_falls_back_to_importance_and_names_the_instance(self, tmp_path):
        from agent_system.hooks.plugin_hook import HookContext, HookType
        from agent_system.llm.models import ChatMessage
        from plugins.memory.server import ChromaDBError

        srv = MemoryServer(name="notes", system_config=MagicMock(), server_config=_config(tmp_path))
        srv._store_memory_in_chroma = AsyncMock()
        srv._operation_search = AsyncMock(side_effect=ChromaDBError("the vector store is gone"))
        for title, importance in (("Minor", 2), ("Major", 9)):
            await srv._operation_store(session_id="s1", title=title, content="x", importance=importance)
        context = HookContext(hook_type=HookType.PRE_LLM_CALL, request_id="r-1", session_id="s1",
                              messages=[ChatMessage(role="user", content="what matters?")])

        await srv.on_pre_llm_call(context)

        appended = context.messages[-1]
        assert appended.injected_by == "notes" and "`notes(operation='recall'" in appended.content
        assert appended.content.index("Major") < appended.content.index("Minor")



class TestIdsUnderPressure:
    @pytest.mark.asyncio
    async def test_a_delete_first_keeps_the_number_of_a_file_from_before(self, tmp_path):
        seeding = _server(tmp_path)
        seeding._store_memory_in_chroma = AsyncMock()
        for title in ("One", "Two", "Three"):
            await seeding.execute({"operation": "store", "title": title, "content": "x", "session_id": "s1"})
        file = tmp_path / "mem" / "s1.json"
        file.write_text(file.read_text(encoding="utf-8").replace('"last_number": 3', '"last_number": 0'),
                        encoding="utf-8")  # as written before the number was kept
        deleting = _server(tmp_path)
        deleting._delete_memory_from_chroma = AsyncMock()
        await deleting.execute({"operation": "delete", "memory_id": "mem_003", "session_id": "s1"})

        storing = _server(tmp_path)
        storing._store_memory_in_chroma = AsyncMock()
        stored = await storing.execute({"operation": "store", "title": "Four", "content": "x", "session_id": "s1"})

        assert stored["memory_id"] == "mem_004"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("indexing_takes", [0, 0.05], ids=["one-store-ends-first", "both-wait-on-the-index"])
    async def test_two_first_stores_at_once_get_two_ids(self, tmp_path, indexing_takes):
        import asyncio

        seeding = _server(tmp_path)
        seeding._store_memory_in_chroma = AsyncMock()
        await seeding.execute({"operation": "store", "title": "Old", "content": "x", "session_id": "s1"})
        srv = _server(tmp_path)  # after a restart: no counter yet, the file is read by both

        async def indexing(*_args):  # a real vector store takes a moment: the other store runs meanwhile
            if indexing_takes:
                await asyncio.sleep(indexing_takes)

        srv._store_memory_in_chroma = AsyncMock(side_effect=indexing)

        stored = await asyncio.gather(*(srv.execute({"operation": "store", "title": title, "content": "x",
                                                     "session_id": "s1"}) for title in ("Left", "Right")))

        kept = (await _server(tmp_path)._load_collection("s1")).memories
        assert sorted(answer["memory_id"] for answer in stored) == ["mem_002", "mem_003"] and len(kept) == 3


class TestUpdateWithAFailingIndex:
    @pytest.mark.asyncio
    async def test_a_failed_reindex_keeps_the_memory_as_it_was(self, tmp_path):
        from plugins.memory.server import ChromaDBError

        srv = _server(tmp_path)
        srv._store_memory_in_chroma = AsyncMock()
        await srv.execute({"operation": "store", "title": "Old title", "content": "old text", "session_id": "s1"})
        srv._store_memory_in_chroma = AsyncMock(side_effect=ChromaDBError("the vector store is down"))

        answer = await srv.execute({"operation": "update", "memory_id": "mem_001", "title": "New title",
                                    "content": "new text", "session_id": "s1"})
        await srv.execute({"operation": "recall", "memory_id": "mem_001", "session_id": "s1"})  # saves the file

        kept = (await _server(tmp_path)._load_collection("s1")).memories["mem_001"]
        assert "error" in answer and (kept.title, kept.content) == ("Old title", "old text")


class TestEvictionDuringAStore:
    @pytest.mark.asyncio
    async def test_a_session_evicted_while_a_store_waits_hands_out_no_id_twice(self, tmp_path):
        import asyncio

        srv = _server(tmp_path)
        srv._store_memory_in_chroma = AsyncMock()
        await srv.execute({"operation": "store", "title": "First", "content": "x", "session_id": "s1"})
        gate = asyncio.Event()

        async def waiting(*_args):
            await gate.wait()

        srv._store_memory_in_chroma = AsyncMock(side_effect=waiting)
        held = asyncio.create_task(srv.execute({"operation": "store", "title": "Held", "content": "x",
                                                "session_id": "s1"}))
        await asyncio.sleep(0.05)  # it has its id and waits on the index
        srv._cache_access_times["s1"] -= 4000  # idle past the cache's time to live
        await srv._load_collection("other")  # another session's load clears the cache
        srv._store_memory_in_chroma = AsyncMock()
        second = await srv.execute({"operation": "store", "title": "Second", "content": "x", "session_id": "s1"})
        gate.set()
        first = await held

        assert (first["memory_id"], second["memory_id"]) == ("mem_002", "mem_003")
