"""'info' reads the sub-agent's transcript, tail by default, paged on request.

The previous 'info' returned a fixed six-message, 200-character-per-message
peephole no matter what the caller asked for -- the coordinator could not read
more even when it genuinely needed to. These tests build a REAL sub-session
through SubAgentManager (not a mock), write a crafted transcript into it, and
call the server's _handle_info directly -- the same call path the tool uses.
"""
from __future__ import annotations

import asyncio
import logging
import tempfile
from pathlib import Path
from unittest.mock import Mock

import pytest

from agent_system.config.models import AgentConfig, AgentSystemConfig, ToolServerConfig
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
    from agent_system.tools.base import ToolServerRegistry

    registry = ToolServerRegistry()
    mock_agent = Mock()
    mock_agent.name = "web_research_agent"
    mock_agent.agent_config = AgentConfig(llm_profile="normal")
    registry.register("web_research_agent", mock_agent)
    return SubAgentManager(session_service, registry)


def _make_server(**pagination_overrides) -> SubAgentManagerServer:
    system_config = Mock(spec=AgentSystemConfig)
    server_config = Mock(spec=ToolServerConfig)
    server_config.max_sub_agents_per_session = 10
    server_config.max_nesting_depth = 5
    server_config.max_sub_agents_per_type = 3
    server_config.allowed_agents = ["*"]
    server_config.blocked_agents = []
    for key, value in pagination_overrides.items():
        setattr(server_config, key, value)
    return SubAgentManagerServer(name="sub_agent_manager", system_config=system_config, server_config=server_config)


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


class TestInfoOnAnInstanceThatDoesNotExist:
    """Production hit this on 2026-09-16: a coordinator asked for 'info' on an
    instance_id no create() had ever produced (a guessed or stale id). The
    lookup goes through SessionManager.load_session, which raises
    SessionNotFoundError -- not FileNotFoundError, which is what the except
    clause here used to catch. The mismatch let the raw exception fall through
    to the outer generic handler as an ERROR-level traceback instead of the
    intended one-line "Sub-agent '<id>' not found".
    """

    @pytest.mark.asyncio
    async def test_a_never_created_instance_id_gets_a_clean_not_found(self, server, session_service, session_manager):
        await session_manager.create_session(
            user_id=USER, session_id=PARENT_ID, title="Coordinator", agent_name="meta_agent", llm_profile="normal",
        )
        result = await server._handle_info(_params("sub_never_existed_999", session_service))
        assert result["status"] == "error"
        assert result["error"].startswith("Sub-agent 'sub_never_existed_999' not found.")
        assert "no sub-agents at all" in result["error"]

    @pytest.mark.asyncio
    async def test_the_hint_names_the_real_siblings_instead_of_the_guessed_id(
        self, server, session_service, sub_agent_manager, sub_session
    ):
        """The production case: 'panel-beats-P04' guessed from one sibling's
        label and another's suffix. Neither existed, both do."""
        real_sub_id, _ = sub_session
        result = await server._handle_info(_params("sub_assembled_from_two_others_12345", session_service))
        assert result["status"] == "error"
        assert real_sub_id in result["error"]
        assert "exactly as returned" in result["error"]


class TestAMistakenCallIsNoErrorInTheLog:
    """A call naming a sub-agent that is not there, not the caller's, or busy is answered, and the
    model can correct it: INFO in the log, no traceback. Production 22.09.2026: a `continue` on
    'sub_Auditor-B_496875' logged "Error in continue_sub_agent" with two chained tracebacks."""

    @staticmethod
    def logged(caplog):
        """The refusals at INFO, and every record that reads as a fault."""
        records = [r for r in caplog.records if r.name == "plugins.sub_agent_manager.server"]
        return ([r.getMessage() for r in records if r.levelno == logging.INFO and " refused: " in r.getMessage()],
                [r.getMessage() for r in records if r.levelno >= logging.WARNING or r.exc_info])

    @staticmethod
    def continue_params(sub_id, session_service, sub_agent_manager):
        return _params(sub_id, session_service, message="go on", _agent=Mock(registry=sub_agent_manager._registry))

    @pytest.mark.asyncio
    async def test_a_continue_on_an_instance_that_was_never_created(
            self, server, session_service, sub_agent_manager, sub_session, caplog):
        real_sub_id, _ = sub_session
        with caplog.at_level(logging.INFO, logger="plugins.sub_agent_manager.server"):
            result = await server._handle_continue(
                self.continue_params("sub_Auditor-B_496875", session_service, sub_agent_manager))

        assert result["status"] == "error"
        assert result["error"].startswith("Sub-agent 'sub_Auditor-B_496875' not found."), result
        assert real_sub_id in result["error"], result  # the ids that would have worked
        refusals, faults = self.logged(caplog)
        assert faults == [] and len(refusals) == 1, caplog.text
        assert refusals[0].startswith("continue_sub_agent refused: "), refusals

    @pytest.mark.asyncio
    async def test_an_id_no_session_can_have_is_not_found_either(
            self, server, session_service, sub_agent_manager, sub_session, caplog):
        """A label, or an id with a quote or a space: `load_session` refuses its format with a bare
        ValueError -- as it does a corrupt file, which stays a fault."""
        real_sub_id, _ = sub_session
        with caplog.at_level(logging.INFO, logger="plugins.sub_agent_manager.server"):
            continued = await server._handle_continue(
                self.continue_params("Auditor B", session_service, sub_agent_manager))
            info = await server._handle_info(_params(f"'{real_sub_id}'", session_service))

        assert continued["error"].startswith("Sub-agent 'Auditor B' not found."), continued
        assert info["error"].startswith(f"Sub-agent ''{real_sub_id}'' not found."), info
        assert real_sub_id in continued["error"], continued
        refusals, faults = self.logged(caplog)
        assert faults == [] and len(refusals) == 2, caplog.text

    @pytest.mark.asyncio
    async def test_a_continue_on_another_sessions_instance(
            self, server, session_service, session_manager, sub_agent_manager, sub_session, caplog):
        await session_manager.create_session(
            user_id=USER, session_id="another_coordinator", title="Other", agent_name="meta_agent",
            llm_profile="normal")
        theirs = await sub_agent_manager.create_sub_session(
            parent_session_id="another_coordinator", agent_type="web_research_agent", initial_message="start")
        with caplog.at_level(logging.INFO, logger="plugins.sub_agent_manager.server"):
            result = await server._handle_continue(self.continue_params(theirs, session_service, sub_agent_manager))

        assert "does not belong to current session" in result["error"], result
        refusals, faults = self.logged(caplog)
        assert faults == [] and len(refusals) == 1, caplog.text

    @pytest.mark.asyncio
    async def test_a_continue_on_an_instance_that_is_running(
            self, server, session_service, sub_agent_manager, sub_session, caplog):
        sub_id, _ = sub_session
        server._take_slot(sub_id)  # held by this test's task, which is still going
        with caplog.at_level(logging.INFO, logger="plugins.sub_agent_manager.server"):
            result = await server._handle_continue(self.continue_params(sub_id, session_service, sub_agent_manager))

        assert "is already running" in result["error"], result
        refusals, faults = self.logged(caplog)
        assert faults == [] and len(refusals) == 1, caplog.text

    @pytest.mark.parametrize("holder", ["ended", "none"])
    @pytest.mark.asyncio
    async def test_a_running_slot_no_run_holds_is_a_fault(
            self, server, session_service, sub_agent_manager, sub_session, caplog, holder):
        """A leaked slot refuses every continue on the instance for the life of the process: the
        manager's fault, so it keeps its traceback -- whether the task that took it has ended or no
        task took it."""
        sub_id, _ = sub_session
        if holder == "ended":
            async def take():
                server._take_slot(sub_id)
            await asyncio.create_task(take())
        else:
            server._running_agents.add(sub_id)
        with caplog.at_level(logging.INFO, logger="plugins.sub_agent_manager.server"):
            result = await server._handle_continue(self.continue_params(sub_id, session_service, sub_agent_manager))

        assert "is already running" in result["error"], result
        refusals, faults = self.logged(caplog)
        assert refusals == [] and len(faults) == 1 and "Error in continue_sub_agent" in faults[0], caplog.text

    @pytest.mark.asyncio
    async def test_a_slot_no_run_still_going_holds_is_let_go_by_whoever_releases_it(self, server):
        async def take():
            server._take_slot("sub_ended")
        await asyncio.create_task(take())
        server._running_agents.add("sub_untaken")
        async with server._running_lock:
            server._release_slot("sub_ended")
            server._release_slot("sub_untaken")
        assert not {"sub_ended", "sub_untaken"} & server._running_agents and server._slot_holders == {}

    @pytest.mark.asyncio
    async def test_a_leaked_slot_does_not_keep_its_run_alive(self, server):
        """A leak keeps the slot, not the ended task with every frame of its run."""
        import gc
        import weakref

        async def take():
            server._take_slot("sub_x")
        task = asyncio.create_task(take())
        await task
        ended = weakref.ref(task)
        del task
        await asyncio.sleep(0)  # the loop still holds a finished task until its callbacks have run
        gc.collect()

        assert ended() is None
        assert "sub_x" in server._running_agents and not server._slot_has_a_run("sub_x")

    @pytest.mark.asyncio
    async def test_a_corrupt_sub_agent_file_is_a_fault_not_a_missing_sub_agent(
            self, server, session_service, session_manager, sub_agent_manager, sub_session, temp_storage, caplog):
        """load_session refuses a corrupt file with the same bare ValueError as an id of the wrong
        format: the model must not be told "not found" about a sub-agent that exists."""
        sub_id, _ = sub_session
        (temp_storage / USER / f"{sub_id}.json").write_text("{not json", encoding="utf-8")
        session_manager._cache.clear()
        with caplog.at_level(logging.INFO, logger="plugins.sub_agent_manager.server"):
            continued = await server._handle_continue(
                self.continue_params(sub_id, session_service, sub_agent_manager))
            info = await server._handle_info(_params(sub_id, session_service))

        assert "not found" not in continued["error"] and "not found" not in info["error"], (continued, info)
        refusals, faults = self.logged(caplog)
        assert refusals == [] and len(faults) == 2, caplog.text

    @pytest.mark.asyncio
    async def test_info_on_an_instance_that_was_never_created_or_is_another_sessions(
            self, server, session_service, session_manager, sub_agent_manager, sub_session, caplog):
        await session_manager.create_session(
            user_id=USER, session_id="another_coordinator", title="Other", agent_name="meta_agent",
            llm_profile="normal")
        theirs = await sub_agent_manager.create_sub_session(
            parent_session_id="another_coordinator", agent_type="web_research_agent", initial_message="start")
        with caplog.at_level(logging.INFO, logger="plugins.sub_agent_manager.server"):
            never = await server._handle_info(_params("sub_never_existed_999", session_service))
            foreign = await server._handle_info(_params(theirs, session_service))

        assert "not found" in never["error"] and "does not belong" in foreign["error"], (never, foreign)
        refusals, faults = self.logged(caplog)
        assert faults == [] and len(refusals) == 2, caplog.text
        assert all(r.startswith("get_sub_agent_info refused: ") for r in refusals), refusals

    @pytest.mark.asyncio
    async def test_a_fault_of_the_manager_still_logs_its_traceback(self, server, session_service, caplog):
        """The counter-proof: no session behind the call is a wiring fault, not the model's."""
        params = _params("sub_whatever", session_service)
        del params["_session_id"]
        with caplog.at_level(logging.INFO, logger="plugins.sub_agent_manager.server"):
            result = await server._handle_info(params)

        assert result["status"] == "error"
        refusals, faults = self.logged(caplog)
        assert refusals == [] and len(faults) == 1 and "Error in get_sub_agent_info" in faults[0], caplog.text

    @pytest.mark.asyncio
    async def test_a_continue_whose_agent_is_gone_still_logs_its_traceback(
            self, server, session_service, sub_session, caplog):
        """The counter-proof on continue: the stored sub-agent's type is no longer registered."""
        sub_id, _ = sub_session
        # as the real registry answers a name it does not know
        registry = Mock(get=Mock(side_effect=KeyError("web_research_agent")))
        params = _params(sub_id, session_service, message="go on", _agent=Mock(registry=registry))
        with caplog.at_level(logging.INFO, logger="plugins.sub_agent_manager.server"):
            result = await server._handle_continue(params)

        assert "Agent type 'web_research_agent' not found" in result["error"], result
        refusals, faults = self.logged(caplog)
        assert refusals == [] and len(faults) == 1 and "Error in continue_sub_agent" in faults[0], caplog.text


class TestInfoSaysWhatTheSubAgentIsDoing:
    """Stored, a running and an idle sub-agent both say "active"; `info` hands the model the
    words `list` and the injected list use."""

    @pytest.mark.asyncio
    async def test_idle_once_its_run_is_over_and_running_while_one_is(self, server, session_service, sub_session):
        sub_id, _ = sub_session
        idle = await server._handle_info(_params(sub_id, session_service))
        server._running_agents.add(sub_id)
        running = await server._handle_info(_params(sub_id, session_service))

        assert (idle["status"], running["status"]) == ("idle", "running")
