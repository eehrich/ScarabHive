"""'info' reads the sub-agent's transcript, tail by default, paged on request.

The previous 'info' returned a fixed six-message, 200-character-per-message
peephole no matter what the caller asked for -- the coordinator could not read
more even when it genuinely needed to. These tests build a REAL sub-session
through SubAgentManager (not a mock), write a crafted transcript into it, and
call the server's _handle_info directly -- the same call path the tool uses.
"""
from __future__ import annotations

import tempfile
from pathlib import Path
from unittest.mock import Mock

import pytest

from agent_system.config.models import AgentConfig, AgentSystemConfig, MCPConfig
from agent_system.services.session_manager import SessionManager
from agent_system.services.session_service import SessionService
from plugins.sub_agent_manager.manager import SubAgentManager
from plugins.sub_agent_manager.server import SubAgentManagerServer

USER = "testuser"
PARENT_ID = "coord_info_test"


@pytest.fixture
async def temp_storage():
    with tempfile.TemporaryDirectory() as tmpdir:
        yield Path(tmpdir)


@pytest.fixture
async def session_manager(temp_storage):
    return SessionManager(storage_path=str(temp_storage))


@pytest.fixture
async def session_service(session_manager):
    return SessionService(session_manager=session_manager)


@pytest.fixture
def sub_agent_manager(session_service):
    from agent_system.mcp.base import MCPRegistry

    registry = MCPRegistry()
    mock_agent = Mock()
    mock_agent.name = "web_research_agent"
    mock_agent.agent_config = AgentConfig(llm_profile="normal")
    registry.register("web_research_agent", mock_agent)
    return SubAgentManager(session_service, registry)


def _make_server(**pagination_overrides) -> SubAgentManagerServer:
    system_config = Mock(spec=AgentSystemConfig)
    mcp_config = Mock(spec=MCPConfig)
    mcp_config.max_sub_agents_per_session = 10
    mcp_config.max_nesting_depth = 5
    mcp_config.max_sub_agents_per_type = 3
    mcp_config.allowed_agents = ["*"]
    mcp_config.blocked_agents = []
    for key, value in pagination_overrides.items():
        setattr(mcp_config, key, value)
    return SubAgentManagerServer(name="sub_agent_manager", system_config=system_config, mcp_config=mcp_config)


@pytest.fixture
def server():
    """A real SubAgentManagerServer with default pagination config."""
    return _make_server()


@pytest.fixture
def capped_server():
    """A server whose info_max_limit is smaller than the test transcript.

    With the DEFAULT cap (200) and this fixture's 32-message transcript,
    "capped" and "there just wasn't more" are indistinguishable -- a removed
    cap would pass unnoticed. This fixture makes the cap the binding
    constraint instead.
    """
    return _make_server(info_max_limit=10)


def _message(role: str, content, **extra) -> dict:
    msg = {"role": role, "content": content}
    msg.update(extra)
    return msg


@pytest.fixture
async def sub_session(session_manager, sub_agent_manager):
    """A real sub-session with a 30-message transcript of varied shapes."""
    await session_manager.create_session(
        user_id=USER, session_id=PARENT_ID, title="Coordinator", agent_name="meta_agent", llm_profile="normal",
    )
    sub_id = await sub_agent_manager.create_sub_session(
        parent_session_id=PARENT_ID, agent_type="web_research_agent", initial_message="start",
    )

    messages = []
    for i in range(14):  # 14 user/assistant pairs = 28 messages
        messages.append(_message("user", f"user turn {i}"))
        messages.append(_message("assistant", f"assistant reply {i}"))
    # A tool call with no content, and its result -- the shape the old code
    # rendered as an empty string.
    messages.append(_message(
        "assistant", "",
        tool_calls=[{"id": "call_1", "type": "function",
                     "function": {"name": "datetime_operations", "arguments": '{"operation":"current"}'}}],
    ))
    messages.append(_message("tool", "2026-08-11T12:00:00Z", name="datetime_operations", tool_call_id="call_1"))
    # Multimodal content (text + text_file blocks), the shape session files
    # actually use for attachments.
    messages.append(_message("user", [
        {"type": "text", "text": "see attached"},
        {"type": "text_file", "content": "file body here"},
    ]))
    # A very long message, to exercise truncation.
    messages.append(_message("assistant", "x" * 10_000))

    sub_data = await session_manager.load_session(USER, sub_id)
    sub_data["messages"] = messages
    await session_manager.save_session(sub_data)

    return sub_id, len(messages)


def _params(sub_id: str, session_service, **extra) -> dict:
    p = {"_session_id": PARENT_ID, "_session_service": session_service, "instance_id": sub_id}
    p.update(extra)
    return p


class TestTailIsTheDefault:
    @pytest.mark.asyncio
    async def test_no_offset_returns_the_most_recent_messages(self, server, session_service, sub_session):
        sub_id, total = sub_session
        result = await server._handle_info(_params(sub_id, session_service))

        assert result["window"]["mode"] == "tail"
        assert result["window"]["total"] == total
        assert result["window"]["returned"] == server.info_default_limit
        assert result["window"]["start_index"] == total - server.info_default_limit
        # It really is the tail, not an arbitrary slice.
        assert result["messages"][-1]["content"].startswith("x" * 50)

    @pytest.mark.asyncio
    async def test_tail_has_more_before_but_not_after(self, server, session_service, sub_session):
        sub_id, total = sub_session
        result = await server._handle_info(_params(sub_id, session_service))
        assert result["window"]["has_more_before"] is True
        assert result["window"]["has_more_after"] is False

    @pytest.mark.asyncio
    async def test_custom_limit_changes_the_tail_size(self, server, session_service, sub_session):
        sub_id, total = sub_session
        result = await server._handle_info(_params(sub_id, session_service, limit=5))
        assert result["window"]["returned"] == 5
        assert result["window"]["start_index"] == total - 5


class TestPagingThroughTheFullTranscript:
    @pytest.mark.asyncio
    async def test_offset_zero_starts_at_the_beginning(self, server, session_service, sub_session):
        sub_id, total = sub_session
        result = await server._handle_info(_params(sub_id, session_service, offset=0, limit=10))
        assert result["window"]["mode"] == "offset"
        assert result["window"]["start_index"] == 0
        assert result["messages"][0]["content"] == "user turn 0"  # the very first crafted message
        assert result["window"]["has_more_before"] is False
        assert result["window"]["has_more_after"] is True

    @pytest.mark.asyncio
    async def test_walking_offset_by_limit_covers_everything_exactly_once(
        self, server, session_service, sub_session
    ):
        """The paging contract the tool description promises: 0, limit, 2*limit, ..."""
        sub_id, total = sub_session
        limit = 9
        seen_indices = []
        offset = 0
        for _ in range(100):  # hard stop so a bug here can't hang the test
            result = await server._handle_info(_params(sub_id, session_service, offset=offset, limit=limit))
            seen_indices.extend(m["index"] for m in result["messages"])
            if not result["window"]["has_more_after"]:
                break
            offset += limit
        else:
            pytest.fail("has_more_after never became false -- would page forever")

        assert seen_indices == list(range(total)), "paging must cover every message exactly once, in order"

    @pytest.mark.asyncio
    async def test_offset_past_the_end_returns_empty_not_an_error(self, server, session_service, sub_session):
        sub_id, total = sub_session
        result = await server._handle_info(_params(sub_id, session_service, offset=total + 50, limit=10))
        assert result["window"]["returned"] == 0
        assert result["messages"] == []


class TestLimitIsCapped:
    @pytest.mark.asyncio
    async def test_a_huge_limit_is_capped_server_side(self, capped_server, session_service, sub_session):
        """One call must not be able to dump an unbounded transcript back
        into the coordinator's own context."""
        sub_id, total = sub_session
        assert total > capped_server.info_max_limit, "fixture too small to prove capping vs. exhaustion"
        result = await capped_server._handle_info(_params(sub_id, session_service, offset=0, limit=100_000))
        assert result["window"]["returned"] == capped_server.info_max_limit

    @pytest.mark.asyncio
    async def test_capped_result_is_smaller_than_the_full_transcript(
        self, capped_server, session_service, sub_session
    ):
        """Same guard, phrased the other way: capped must mean INCOMPLETE."""
        sub_id, total = sub_session
        result = await capped_server._handle_info(_params(sub_id, session_service, offset=0, limit=100_000))
        assert result["window"]["returned"] < total
        assert result["window"]["has_more_after"] is True


class TestMessageFormatting:
    @pytest.mark.asyncio
    async def test_multimodal_content_is_flattened_to_text(self, server, session_service, sub_session):
        sub_id, total = sub_session
        # The multimodal message is the third-from-last of the 30.
        result = await server._handle_info(_params(sub_id, session_service, offset=0, limit=total))
        multimodal = next(m for m in result["messages"] if "see attached" in m["content"])
        assert "file body here" in multimodal["content"]
        # No raw Python/JSON block-list repr leaked through.
        assert "'type'" not in multimodal["content"]

    @pytest.mark.asyncio
    async def test_tool_call_is_surfaced_even_with_empty_content(self, server, session_service, sub_session):
        sub_id, total = sub_session
        result = await server._handle_info(_params(sub_id, session_service, offset=0, limit=total))
        caller = next(m for m in result["messages"] if m.get("tool_calls"))
        assert caller["tool_calls"][0]["name"] == "datetime_operations"
        assert "current" in caller["tool_calls"][0]["arguments"]

    @pytest.mark.asyncio
    async def test_tool_result_carries_the_tool_name(self, server, session_service, sub_session):
        sub_id, total = sub_session
        result = await server._handle_info(_params(sub_id, session_service, offset=0, limit=total))
        tool_msg = next(m for m in result["messages"] if m["role"] == "tool")
        assert tool_msg["tool_name"] == "datetime_operations"

    @pytest.mark.asyncio
    async def test_long_content_is_truncated_with_a_visible_marker(self, server, session_service, sub_session):
        sub_id, total = sub_session
        result = await server._handle_info(_params(sub_id, session_service, offset=0, limit=total, max_chars=100))
        long_msg = next(m for m in result["messages"] if m["content"].startswith("x" * 50))
        assert len(long_msg["content"]) < 10_000
        assert "more chars omitted" in long_msg["content"]

    @pytest.mark.asyncio
    async def test_max_chars_zero_means_unlimited(self, server, session_service, sub_session):
        sub_id, total = sub_session
        result = await server._handle_info(_params(sub_id, session_service, offset=0, limit=total, max_chars=0))
        long_msg = next(m for m in result["messages"] if m["content"].startswith("x" * 50))
        assert long_msg["content"] == "x" * 10_000
        assert "omitted" not in long_msg["content"]


class TestOwnershipStillEnforced:
    @pytest.mark.asyncio
    async def test_info_for_a_foreign_session_is_rejected(self, server, session_service, sub_session):
        """Pagination must not have loosened the ownership check.

        Whether it is caught by the parent_link comparison or, earlier, by
        SessionManager's own cross-user check depends on how the caller's
        _session_id resolves to a user -- either way the read must fail.
        """
        sub_id, total = sub_session
        params = _params(sub_id, session_service)
        params["_session_id"] = "someone_elses_coordinator"
        result = await server._handle_info(params)
        assert result.get("status") == "error"
        assert "messages" not in result
