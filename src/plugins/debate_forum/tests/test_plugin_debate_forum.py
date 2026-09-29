"""Debate Forum Plugin - Test Suite.

Tests cover:
- Database layer (channels + messages CRUD, stats, format_thread)
- tool methods via DebateForumServer
- Error handling and edge cases
"""
import pytest
from pathlib import Path
from unittest.mock import MagicMock, AsyncMock

from plugins.debate_forum.database import DebateForumDB
from plugins.debate_forum.server import DebateForumServer


# =============================================================================
# Fixtures
# =============================================================================

@pytest.fixture
def db(tmp_path: Path) -> DebateForumDB:
    """Fresh SQLite database for each test."""
    db_path = tmp_path / "test_forum.db"
    return DebateForumDB(db_path, wal_mode=False)


@pytest.fixture
def mock_system_config() -> MagicMock:
    return MagicMock()


@pytest.fixture
def mock_server_config() -> MagicMock:
    config = MagicMock()
    config.config = {}
    return config


@pytest.fixture
def server(db: DebateForumDB, mock_system_config: MagicMock, mock_server_config: MagicMock) -> DebateForumServer:
    """DebateForumServer with test DB (bypasses SchemaBasedToolServer schema loading)."""
    srv = DebateForumServer.__new__(DebateForumServer)
    srv.name = "debate_forum"
    srv.system_config = mock_system_config
    srv.server_config = mock_server_config
    srv.db = db
    srv.min_message_length = 50
    return srv


@pytest.fixture
def status_mock() -> AsyncMock:
    """Mock _status parameter for tool calls."""
    s = AsyncMock()
    s.progress = AsyncMock()
    s.end = AsyncMock()
    return s


# =============================================================================
# Database Tests
# =============================================================================

class TestDebateForumDB:
    """Tests for the SQLite database layer."""

    def test_create_channel(self, db: DebateForumDB):
        result = db.create_channel(name="test-debate", topic="Which synopsis is best?")
        assert result["channel_id"] == 1
        assert result["name"] == "test-debate"
        assert result["status"] == "active"

    def test_create_channel_with_metadata(self, db: DebateForumDB):
        result = db.create_channel(
            name="book-3",
            topic="Test topic",
            context="Some context",
            metadata={"book_id": 3, "stage": "synopsis"},
        )
        channel = db.get_channel(result["channel_id"])
        assert channel is not None
        assert channel["name"] == "book-3"
        assert channel["context"] == "Some context"
        assert channel["metadata_json"]["book_id"] == 3

    def test_get_channel_not_found(self, db: DebateForumDB):
        assert db.get_channel(999) is None

    def test_list_channels_empty(self, db: DebateForumDB):
        channels = db.list_channels()
        assert channels == []

    def test_list_channels_with_status_filter(self, db: DebateForumDB):
        db.create_channel(name="active-1", topic="t1")
        db.create_channel(name="active-2", topic="t2")
        r = db.create_channel(name="to-conclude", topic="t3")
        db.conclude_channel(r["channel_id"], verdict={"winner": "A"})

        active = db.list_channels(status="active")
        assert len(active) == 2

        concluded = db.list_channels(status="concluded")
        assert len(concluded) == 1
        assert concluded[0]["name"] == "to-conclude"

    def test_list_channels_search(self, db: DebateForumDB):
        db.create_channel(name="synopsis-debate", topic="Which is best?")
        db.create_channel(name="character-debate", topic="Character depth review")

        found = db.list_channels(search="synopsis")
        assert len(found) == 1
        assert found[0]["name"] == "synopsis-debate"

    def test_count_channels(self, db: DebateForumDB):
        assert db.count_channels() == 0
        db.create_channel(name="c1", topic="t")
        db.create_channel(name="c2", topic="t")
        assert db.count_channels() == 2
        assert db.count_channels(status="active") == 2

    def test_conclude_channel(self, db: DebateForumDB):
        r = db.create_channel(name="to-conclude", topic="t")
        verdict = {"winner": "Synopsis A", "score": 8.5}
        ok = db.conclude_channel(r["channel_id"], verdict=verdict, summary="Synopsis A wins")
        assert ok is True

        channel = db.get_channel(r["channel_id"])
        assert channel["status"] == "concluded"
        assert channel["verdict_json"]["winner"] == "Synopsis A"
        assert channel["verdict_summary"] == "Synopsis A wins"

    def test_conclude_nonexistent_channel(self, db: DebateForumDB):
        ok = db.conclude_channel(999, verdict={"x": 1})
        assert ok is False

    def test_archive_channel(self, db: DebateForumDB):
        r = db.create_channel(name="to-archive", topic="t")
        ok = db.archive_channel(r["channel_id"])
        assert ok is True
        channel = db.get_channel(r["channel_id"])
        assert channel["status"] == "archived"

    def test_rename_channel(self, db: DebateForumDB):
        r = db.create_channel(name="revise-B05-story-152", topic="t")
        ok = db.rename_channel(r["channel_id"], "revise-B04-story-152")
        assert ok is True
        channel = db.get_channel(r["channel_id"])
        assert channel["name"] == "revise-B04-story-152"
        # Channel-ID, topic, status unverändert
        assert channel["id"] == r["channel_id"]
        assert channel["topic"] == "t"
        assert channel["status"] == "active"

    def test_rename_channel_nonexistent(self, db: DebateForumDB):
        ok = db.rename_channel(99999, "new-name")
        assert ok is False

    def test_rename_channel_empty_name_rejected(self, db: DebateForumDB):
        r = db.create_channel(name="original", topic="t")
        assert db.rename_channel(r["channel_id"], "") is False
        assert db.rename_channel(r["channel_id"], "   ") is False
        # Name unverändert
        channel = db.get_channel(r["channel_id"])
        assert channel["name"] == "original"

    def test_post_message(self, db: DebateForumDB):
        ch = db.create_channel(name="ch", topic="t")
        result = db.post_message(
            channel_id=ch["channel_id"],
            agent_name="Mira",
            agent_role="advocate",
            round_num=1,
            content="Synopsis A is clearly the strongest choice.",
        )
        assert result["message_id"] == 1
        assert result["channel_id"] == ch["channel_id"]

    def test_get_messages(self, db: DebateForumDB):
        ch = db.create_channel(name="ch", topic="t")
        cid = ch["channel_id"]

        db.post_message(cid, "Mira", "advocate", 1, "Pro A")
        db.post_message(cid, "Sven", "critic", 1, "Counter-argument")
        db.post_message(cid, "Mira", "advocate", 2, "Rebuttal")

        messages = db.get_messages(cid)
        assert len(messages) == 3
        # Ordered by round then id
        assert messages[0]["agent_name"] == "Mira"
        assert messages[1]["agent_name"] == "Sven"
        assert messages[2]["round"] == 2

    def test_get_messages_with_limit(self, db: DebateForumDB):
        ch = db.create_channel(name="ch", topic="t")
        cid = ch["channel_id"]
        for i in range(5):
            db.post_message(cid, "Agent", "role", 1, f"msg {i}")
        messages = db.get_messages(cid, limit=3)
        assert len(messages) == 3

    def test_get_message_count(self, db: DebateForumDB):
        ch = db.create_channel(name="ch", topic="t")
        cid = ch["channel_id"]
        assert db.get_message_count(cid) == 0
        db.post_message(cid, "A", "r", 1, "hello")
        assert db.get_message_count(cid) == 1

    def test_format_thread(self, db: DebateForumDB):
        ch = db.create_channel(name="synopsis-debate", topic="Best synopsis?", context="Fantasy book")
        cid = ch["channel_id"]
        db.post_message(cid, "Mira", "advocate", 1, "I support Synopsis A.")
        db.post_message(cid, "Sven", "critic", 1, "Synopsis A has weak pacing.")

        text = db.format_thread(cid)
        assert "FORUM-DEBATTE: synopsis-debate" in text
        assert "TOPIC: Best synopsis?" in text
        assert "KONTEXT:\nFantasy book" in text
        assert '[ADVOCATE "Mira" | Runde 1]' in text
        assert '[CRITIC "Sven" | Runde 1]' in text

    def test_format_thread_with_verdict(self, db: DebateForumDB):
        ch = db.create_channel(name="ch", topic="t")
        cid = ch["channel_id"]
        db.post_message(cid, "A", "mod", 1, "msg")
        db.conclude_channel(cid, verdict={}, summary="A wins unanimously")

        text = db.format_thread(cid)
        assert "VERDICT: A wins unanimously" in text

    def test_format_thread_nonexistent(self, db: DebateForumDB):
        assert db.format_thread(999) == ""

    def test_get_stats(self, db: DebateForumDB):
        stats = db.get_stats()
        assert stats["total_channels"] == 0
        assert stats["total_messages"] == 0

        ch = db.create_channel(name="ch", topic="t")
        db.post_message(ch["channel_id"], "A", "r", 1, "msg")

        stats = db.get_stats()
        assert stats["total_channels"] == 1
        assert stats["active"] == 1
        assert stats["total_messages"] == 1

    def test_post_message_updates_channel_timestamp(self, db: DebateForumDB):
        ch = db.create_channel(name="ch", topic="t")
        cid = ch["channel_id"]
        before = db.get_channel(cid)["updated_at"]

        # SQLite datetime has second-level precision, so we just check it doesn't error
        db.post_message(cid, "A", "r", 1, "msg")
        after = db.get_channel(cid)["updated_at"]
        assert after >= before

    def test_append_message(self, db: DebateForumDB):
        ch = db.create_channel(name="ch", topic="t")
        cid = ch["channel_id"]
        posted = db.post_message(cid, "Mira", "advocate", 1, "Part one. ")
        res = db.append_message(posted["message_id"], "Part two.")
        assert res is not None
        assert res["message_id"] == posted["message_id"]
        assert res["channel_id"] == cid
        # Content is concatenated into ONE message, not a new row.
        msgs = db.get_messages(cid)
        assert len(msgs) == 1
        assert msgs[0]["content"] == "Part one. Part two."
        assert res["length"] == len("Part one. Part two.")

    def test_append_message_nonexistent(self, db: DebateForumDB):
        assert db.append_message(999, "x") is None

    def test_get_message(self, db: DebateForumDB):
        ch = db.create_channel(name="ch", topic="t")
        posted = db.post_message(ch["channel_id"], "A", "r", 1, "hello there")
        msg = db.get_message(posted["message_id"])
        assert msg is not None and msg["content"] == "hello there"
        assert db.get_message(999) is None


# =============================================================================
# Server Tool Tests
# =============================================================================

class TestDebateForumServer:
    """Tests for tool methods."""

    @pytest.mark.asyncio
    async def test_create_channel_tool(self, server: DebateForumServer, status_mock: AsyncMock):
        result = await server.create_channel({
            "name": "test-channel",
            "topic": "Testing debates",
            "_status": status_mock,
        })
        assert result["status"] == "created"
        assert result["channel_id"] == 1
        status_mock.progress.assert_called_once()
        status_mock.end.assert_called_once()

    @pytest.mark.asyncio
    async def test_create_channel_missing_fields(self, server: DebateForumServer):
        result = await server.create_channel({"name": "", "topic": ""})
        assert "error" in result

    @pytest.mark.asyncio
    async def test_post_message_tool(self, server: DebateForumServer, status_mock: AsyncMock):
        ch = await server.create_channel({"name": "ch", "topic": "t"})
        result = await server.post_message({
            "channel_id": ch["channel_id"],
            "agent_name": "Mira",
            "agent_role": "advocate",
            "round": 1,
            "content": "First argument in favor of Synopsis A with strong character arcs.",
            "_status": status_mock,
        })
        assert result["status"] == "posted"
        assert result["message_id"] == 1

    @pytest.mark.asyncio
    async def test_post_message_nonexistent_channel(self, server: DebateForumServer):
        result = await server.post_message({
            "channel_id": 999,
            "agent_name": "A",
            "agent_role": "r",
            "round": 1,
            "content": "This is a test message long enough to pass the minimum length filter.",
        })
        assert "error" in result
        assert "not found" in result["error"]

    @pytest.mark.asyncio
    async def test_post_message_concluded_channel(self, server: DebateForumServer):
        ch = await server.create_channel({"name": "ch", "topic": "t"})
        await server.conclude({"channel_id": ch["channel_id"], "verdict": {"x": 1}})
        result = await server.post_message({
            "channel_id": ch["channel_id"],
            "agent_name": "A",
            "agent_role": "r",
            "round": 1,
            "content": "This message arrives too late after the channel has been concluded.",
        })
        assert "error" in result
        assert "concluded" in result["error"]

    @pytest.mark.asyncio
    async def test_post_message_too_short(self, server: DebateForumServer):
        ch = await server.create_channel({"name": "ch", "topic": "t"})
        result = await server.post_message({
            "channel_id": ch["channel_id"],
            "agent_name": "A",
            "agent_role": "r",
            "round": 1,
            "content": "Hallo Sven",
        })
        assert "error" in result
        assert "too short" in result["error"]

    @pytest.mark.asyncio
    async def test_post_message_append_chunks(self, server: DebateForumServer, status_mock: AsyncMock):
        ch = await server.create_channel({"name": "ch", "topic": "t"})
        first = await server.post_message({
            "channel_id": ch["channel_id"],
            "agent_name": "Mira",
            "agent_role": "advocate",
            "round": 1,
            "content": '{"synopsis": "a very long first chunk of the JSON output ',
            "_status": status_mock,
        })
        assert first["status"] == "posted"
        mid = first["message_id"]
        # Continuation chunk — short on its own, must NOT be rejected by min-length.
        second = await server.post_message({
            "append": True,
            "message_id": mid,
            "content": 'that continues here", "world": "..."}',
            "_status": status_mock,
        })
        assert second["status"] == "appended"
        assert second["message_id"] == mid
        # Stored as ONE complete message.
        msg = server.db.get_message(mid)
        assert msg["content"].startswith('{"synopsis"')
        assert msg["content"].endswith('"world": "..."}')
        assert len(server.db.get_messages(ch["channel_id"])) == 1

    @pytest.mark.asyncio
    async def test_append_requires_message_id(self, server: DebateForumServer):
        result = await server.post_message({"append": True, "content": "more text here"})
        assert "error" in result and "message_id" in result["error"]

    @pytest.mark.asyncio
    async def test_append_nonexistent_message(self, server: DebateForumServer):
        result = await server.post_message({
            "append": True, "message_id": 999, "content": "orphan chunk",
        })
        assert "error" in result and "not found" in result["error"]

    @pytest.mark.asyncio
    async def test_append_to_concluded_channel_rejected(self, server: DebateForumServer):
        ch = await server.create_channel({"name": "ch", "topic": "t"})
        posted = await server.post_message({
            "channel_id": ch["channel_id"], "agent_name": "A", "agent_role": "r",
            "round": 1, "content": "initial chunk long enough to pass min length filter",
        })
        await server.conclude({"channel_id": ch["channel_id"], "verdict": {"x": 1}})
        result = await server.post_message({
            "append": True, "message_id": posted["message_id"], "content": "late chunk",
        })
        assert "error" in result and "not active" in result["error"]

    @pytest.mark.asyncio
    async def test_get_thread_text(self, server: DebateForumServer):
        ch = await server.create_channel({"name": "ch", "topic": "Best approach?"})
        await server.post_message({
            "channel_id": ch["channel_id"],
            "agent_name": "Mira",
            "agent_role": "advocate",
            "round": 1,
            "content": "Approach A is by far the best option given the current constraints and goals.",
        })
        result = await server.get_thread({"channel_id": ch["channel_id"]})
        assert "thread" in result
        assert "FORUM-DEBATTE" in result["thread"]
        assert result["message_count"] == 1

    @pytest.mark.asyncio
    async def test_get_thread_json(self, server: DebateForumServer):
        ch = await server.create_channel({"name": "ch", "topic": "t"})
        await server.post_message({
            "channel_id": ch["channel_id"],
            "agent_name": "A",
            "agent_role": "r",
            "round": 1,
            "content": "This is a test message with enough content for the JSON thread format.",
        })
        result = await server.get_thread({
            "channel_id": ch["channel_id"],
            "format": "json",
        })
        assert "channel" in result
        assert "messages" in result
        assert len(result["messages"]) == 1

    @pytest.mark.asyncio
    async def test_get_thread_not_found(self, server: DebateForumServer):
        result = await server.get_thread({"channel_id": 999})
        assert "error" in result

    @pytest.mark.asyncio
    async def test_conclude_tool(self, server: DebateForumServer, status_mock: AsyncMock):
        ch = await server.create_channel({"name": "ch", "topic": "t"})
        result = await server.conclude({
            "channel_id": ch["channel_id"],
            "verdict": {"winner": "A", "score": 9},
            "summary": "A wins clearly",
            "_status": status_mock,
        })
        assert result["status"] == "concluded"
        status_mock.end.assert_called_once()

    @pytest.mark.asyncio
    async def test_conclude_not_found(self, server: DebateForumServer):
        result = await server.conclude({
            "channel_id": 999,
            "verdict": {"x": 1},
        })
        assert "error" in result

    @pytest.mark.asyncio
    async def test_list_channels_tool(self, server: DebateForumServer):
        await server.create_channel({"name": "ch1", "topic": "t1"})
        await server.create_channel({"name": "ch2", "topic": "t2"})
        result = await server.list_channels({})
        assert result["count"] == 2
        assert len(result["channels"]) == 2

    @pytest.mark.asyncio
    async def test_list_channels_with_filter(self, server: DebateForumServer):
        ch = await server.create_channel({"name": "ch1", "topic": "t1"})
        await server.create_channel({"name": "ch2", "topic": "t2"})
        await server.conclude({"channel_id": ch["channel_id"], "verdict": {"x": 1}})

        result = await server.list_channels({"status": "active"})
        assert result["count"] == 1
        assert result["channels"][0]["name"] == "ch2"

    @pytest.mark.asyncio
    async def test_full_debate_workflow(self, server: DebateForumServer):
        """End-to-end: create channel → post messages → conclude."""
        # Create channel
        ch = await server.create_channel({
            "name": "synopsis-battle",
            "topic": "Best synopsis for Book 3",
            "context": "Fantasy novel, dark tone",
        })
        cid = ch["channel_id"]

        # Round 1: advocate + critic
        await server.post_message({
            "channel_id": cid,
            "agent_name": "Mira",
            "agent_role": "advocate",
            "round": 1,
            "content": "Synopsis A has the strongest character arc with deep emotional development.",
        })
        await server.post_message({
            "channel_id": cid,
            "agent_name": "Sven",
            "agent_role": "critic",
            "round": 1,
            "content": "Synopsis A's pacing is inconsistent in act 2, breaks dramatic tension.",
        })

        # Round 2: rebuttal
        await server.post_message({
            "channel_id": cid,
            "agent_name": "Mira",
            "agent_role": "advocate",
            "round": 2,
            "content": "The pacing serves the tension build-up intentionally.",
        })

        # Moderator concludes
        await server.post_message({
            "channel_id": cid,
            "agent_name": "Kai",
            "agent_role": "moderator",
            "round": 2,
            "content": "Both arguments have merit. Synopsis A wins on character depth.",
        })

        result = await server.conclude({
            "channel_id": cid,
            "verdict": {"winner": "Synopsis A", "score": 8.2, "reason": "Stronger character arc"},
            "summary": "Synopsis A selected — stronger character arc outweighs pacing concerns.",
        })
        assert result["status"] == "concluded"

        # Verify thread
        thread = await server.get_thread({"channel_id": cid})
        assert thread["message_count"] == 4
        assert "Mira" in thread["thread"]
        assert "Sven" in thread["thread"]
        assert "Kai" in thread["thread"]
