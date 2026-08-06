"""Tests for the list / search / read browsing surface.

The tool it replaces took a free-text query and guessed which of five stores was
meant. Two consequences drove this rewrite and are pinned here:

* retrieval was all-or-nothing — a stored tool result came back whole, measured
  in production at 134k characters in a single call, undoing the compaction that
  had put it away;
* there was no way to ask what is stored at all, so an agent that could not
  guess the right keywords could not reach its own history.
"""
import json
from pathlib import Path

import pytest

from plugins.context_engineer.hooks import ContextEngineerPlugin, _one_line, _ref_kind
from plugins.context_engineer.paging import (
    MAX_READ_CHARS,
    find_in_text,
    slice_text,
)

PLUGIN_DIR = Path(__file__).parent.parent


@pytest.fixture
def hooks(tmp_path):
    impl = ContextEngineerPlugin(PLUGIN_DIR)
    impl._storage_base = tmp_path
    return impl


@pytest.fixture
def session(hooks):
    """A session with archived messages, a stored tool result and a variable."""
    sid = "browse-test"
    parts = hooks._get_session_components(sid)
    archival = parts["archival_memory"]
    for i in range(1, 26):
        archival.store(
            {"role": "assistant" if i % 2 else "user",
             "content": f"Message {i}: chapter {i} notes about the blitter"},
            session_id=sid)
    parts["tool_store"].store_and_reference(
        tool_call_id="call_big",
        tool_name="writer_content_batch_scene",
        content="HEAD " + ("scene text " * 4000) + " Kapitel 3 marker " + ("tail " * 500),
        session_id=sid,
    )
    return sid


class TestReadIsAlwaysBounded:
    """A read that hands back everything is how the retrieval tool became the
    biggest consumer of the context it was protecting."""

    @pytest.mark.asyncio
    async def test_large_tool_result_is_not_dumped(self, hooks, session):
        listed = await hooks._handle_context_list(section="tool_results", session_id=session)
        ref = listed["entries"][0]["ref"]

        result = await hooks._handle_context_read(ref=ref, session_id=session)

        assert result["status"] == "success"
        assert result["total_chars"] > 40_000, "fixture is meant to be large"
        assert len(result["content"]) <= MAX_READ_CHARS
        assert result["truncated"] is True
        assert result["next_offset"] == len(result["content"])

    @pytest.mark.asyncio
    async def test_next_offset_walks_to_the_end(self, hooks, session):
        listed = await hooks._handle_context_list(section="tool_results", session_id=session)
        ref = listed["entries"][0]["ref"]

        seen, offset, pages = 0, 0, 0
        while offset is not None and pages < 40:
            page = await hooks._handle_context_read(
                ref=ref, session_id=session, offset=offset, limit=MAX_READ_CHARS)
            seen += page["returned_chars"]
            offset = page["next_offset"]
            pages += 1

        assert offset is None, "paging never terminated"
        assert seen == page["total_chars"]

    @pytest.mark.asyncio
    async def test_limit_cannot_be_raised_past_the_ceiling(self, hooks, session):
        listed = await hooks._handle_context_list(section="tool_results", session_id=session)
        result = await hooks._handle_context_read(
            ref=listed["entries"][0]["ref"], session_id=session, limit=999_999)
        assert result["returned_chars"] <= MAX_READ_CHARS

    @pytest.mark.asyncio
    async def test_find_returns_only_the_matching_parts(self, hooks, session):
        """The reason a 134k result was pulled back whole: there was no way to
        ask for the one section that mattered."""
        listed = await hooks._handle_context_list(section="tool_results", session_id=session)
        result = await hooks._handle_context_read(
            ref=listed["entries"][0]["ref"], session_id=session, find="Kapitel 3")

        assert result["match_count"] >= 1
        assert all("Kapitel 3" in m["snippet"] for m in result["matches"])
        budget = sum(len(m["snippet"]) for m in result["matches"])
        assert budget < result["total_chars"] / 10


class TestListMakesTheHistoryVisible:
    @pytest.mark.asyncio
    async def test_lists_archived_messages_with_refs(self, hooks, session):
        page = await hooks._handle_context_list(section="history", session_id=session, limit=10)
        assert page["total"] == 25
        assert page["count"] == 10
        assert page["next_offset"] == 10
        assert all(e["ref"].startswith("arch_") for e in page["entries"])

    @pytest.mark.asyncio
    async def test_paging_visits_every_entry_exactly_once(self, hooks, session):
        """Without a stable order, paging silently skips or repeats — same-second
        timestamps are the norm here (a tool call and its result)."""
        seen, offset = [], 0
        while offset is not None:
            page = await hooks._handle_context_list(
                section="history", session_id=session, offset=offset, limit=7)
            seen.extend(e["ref"] for e in page["entries"])
            offset = page["next_offset"]

        assert len(seen) == 25
        assert len(set(seen)) == 25

    @pytest.mark.asyncio
    async def test_rows_carry_no_bodies(self, hooks, session):
        page = await hooks._handle_context_list(section="tool_results", session_id=session)
        row = page["entries"][0]
        assert "content" not in row
        assert row["chars"] > 40_000
        assert len(row["summary"]) <= 201

    @pytest.mark.asyncio
    async def test_role_filter(self, hooks, session):
        page = await hooks._handle_context_list(
            section="history", session_id=session, role="user", limit=50)
        assert page["total"] == 12
        assert {e["role"] for e in page["entries"]} == {"user"}

    @pytest.mark.asyncio
    async def test_role_narrows_a_filtered_search_too(self, hooks, session):
        """Accepting the parameter and ignoring it is worse than not offering
        it: the caller believes the result was narrowed."""
        all_hits = await hooks._handle_context_list(
            section="history", filter="blitter", limit=50, session_id=session)
        user_only = await hooks._handle_context_list(
            section="history", filter="blitter", role="user", limit=50,
            session_id=session)

        assert user_only["count"] < all_hits["count"]
        assert {e["role"] for e in user_only["entries"]} == {"user"}

    @pytest.mark.asyncio
    async def test_unknown_section_names_the_valid_ones(self, hooks, session):
        result = await hooks._handle_context_list(section="nope", session_id=session)
        assert result["status"] == "error"
        assert "history" in result["sections"]


class TestFilteringReturnsExcerpts:
    @pytest.mark.asyncio
    async def test_hits_carry_refs_and_snippets_not_bodies(self, hooks, session):
        result = await hooks._handle_context_list(filter="blitter", session_id=session)
        assert result["count"] > 0
        for e in result["entries"]:
            assert "content" not in e
            assert len(e.get("match", "")) <= 461

    @pytest.mark.asyncio
    async def test_finds_inside_stored_tool_output(self, hooks, session):
        """Most of an agent's archived context IS tool output — a search that
        only covers messages misses what it is usually asked about."""
        result = await hooks._handle_context_list(filter="Kapitel 3", session_id=session)
        hits = [e for e in result["entries"] if e["kind"] == "tool_result"]
        assert hits, "stored tool results were not searched"
        assert "Kapitel 3" in hits[0]["match"]

    @pytest.mark.asyncio
    async def test_a_hit_can_be_read(self, hooks, session):
        """The two verbs must compose: search gives a ref, read takes it."""
        found = await hooks._handle_context_list(filter="blitter", session_id=session)
        ref = next(e["ref"] for e in found["entries"] if e["kind"] == "message")
        result = await hooks._handle_context_read(ref=ref, session_id=session)
        assert result["status"] == "success"
        assert "blitter" in result["content"]

    @pytest.mark.asyncio
    async def test_all_without_a_filter_is_refused(self, hooks, session):
        """Browsing pages through ONE store in a defined order; 'all' has no
        order to page through, so it only means something with a filter."""
        result = await hooks._handle_context_list(section="all", session_id=session)
        assert result["status"] == "error"
        assert "filter" in result["hint"]

    @pytest.mark.asyncio
    async def test_the_section_default_follows_the_intent(self, hooks, session):
        """Browsing defaults to the conversation, filtering to everything —
        and the reply says which it used, so it is visible, not hidden."""
        browsing = await hooks._handle_context_list(session_id=session)
        filtering = await hooks._handle_context_list(filter="blitter", session_id=session)
        assert browsing["section"] == "history"
        assert filtering["section"] == "all"

    @pytest.mark.asyncio
    async def test_browsing_pages_but_filtering_does_not_pretend_to(self, hooks, session):
        """Filtered hits are ranked, not ordered — offering a next_offset would
        promise a stable page that does not exist."""
        browsing = await hooks._handle_context_list(session_id=session, limit=5)
        filtering = await hooks._handle_context_list(filter="blitter", session_id=session)
        assert browsing["next_offset"] == 5
        assert "next_offset" not in filtering


class TestReferencesAreDeclaredNotGuessed:
    """`recall` inferred the store from the shape of a free-text query and was
    documented as misrouting agents who wrote '$TR_…'. A ref is an address."""

    @pytest.mark.parametrize("ref,kind", [
        ("arch_98e3ae42f357", "message"),
        ("TR_BFCDDCA04A", "tool_result"),
        ("$TR_BFCDDCA04A", "tool_result"),
        ("call_abc123", "tool_result"),
        ("$VAR_1", "variable"),
        ("VAR_12", "variable"),
        ("/tmp/clip.wav", "media"),
        ("C:/x/img.PNG", "media"),
    ])
    def test_every_store_owns_a_shape(self, ref, kind):
        assert _ref_kind(ref) == kind

    @pytest.mark.parametrize("not_a_ref", [
        "chapter 3 assertions", "", "   ", "the blitter", "notes.md",
    ])
    def test_prose_is_not_a_reference(self, not_a_ref):
        assert _ref_kind(not_a_ref) is None

    @pytest.mark.asyncio
    async def test_prose_is_refused_with_the_valid_shapes(self, hooks, session):
        """No silent fallback into a keyword search — that ambiguity is exactly
        what made the old tool unpredictable."""
        result = await hooks._handle_context_read(ref="chapter 3 assertions", session_id=session)
        assert result["status"] == "error"
        assert "arch_" in result["hint"] and "TR_" in result["hint"]

    @pytest.mark.asyncio
    async def test_wellformed_but_unknown_ref_says_so(self, hooks, session):
        result = await hooks._handle_context_read(ref="arch_deadbeef00", session_id=session)
        assert result["status"] == "error"
        assert "no archived message" in result["error"]


class TestPagingPrimitives:
    def test_slice_reports_how_to_continue(self):
        out = slice_text("x" * 5000, offset=0, limit=2000)
        assert out["returned_chars"] == 2000
        assert out["next_offset"] == 2000
        assert out["truncated"] is True

    def test_last_slice_has_no_next(self):
        out = slice_text("x" * 100, offset=0, limit=2000)
        assert out["next_offset"] is None
        assert out["truncated"] is False

    def test_offset_past_the_end_is_empty_not_an_error(self):
        out = slice_text("abc", offset=99, limit=10)
        assert out["content"] == ""
        assert out["next_offset"] is None

    def test_find_does_not_return_overlapping_duplicates(self):
        """Advancing by one character would fill the whole budget with
        near-identical snippets of one repeated word."""
        out = find_in_text("aaaa " * 50, "aaaa")
        positions = [m["at"] for m in out["matches"]]
        assert positions == sorted(set(positions))
        assert all(b - a >= 4 for a, b in zip(positions, positions[1:]))

    def test_find_reports_that_more_exist(self):
        out = find_in_text("needle " * 100, "needle", max_matches=3)
        assert out["match_count"] == 3
        assert out["more_matches"] is True

    def test_one_line_collapses_and_bounds(self):
        assert _one_line("a\n\n  b\tc", 100) == "a b c"
        assert len(_one_line("x" * 500, 50)) == 50


class TestArchivePointerChains:
    """Found by a live agent run, not by a test.

    Compaction replaces an archived message with a placeholder that carries its
    ref. The next compaction round treated that placeholder as an ordinary
    message and archived it too — storing a pointer to a pointer. Measured in
    one session: chains 20 levels deep, 637 of 1286 entries being nothing but
    pointers, and summaries degraded to `User: {"type": "archived_ref"...}`.

    The agent behaved perfectly and still failed: it searched, it read, and
    every read handed back another placeholder.
    """

    def test_a_placeholder_is_not_archived_again(self):
        from plugins.context_engineer.compaction import _is_archive_pointer

        placeholder = {"role": "user", "content": json.dumps(
            {"type": "archived_ref", "ref_id": "arch_abc", "summary": "User: hi"})}
        assert _is_archive_pointer(placeholder) is True

    @pytest.mark.parametrize("message", [
        {"role": "user", "content": "plain text about archived_ref as a topic"},
        {"role": "user", "content": json.dumps({"type": "tool_result_ref", "ref_id": "TR_1"})},
        {"role": "user", "content": "{not json at all"},
        {"role": "user", "content": [{"type": "text", "text": "list content"}]},
        {"role": "user", "content": None},
    ])
    def test_ordinary_messages_are_still_archived(self, message):
        from plugins.context_engineer.compaction import _is_archive_pointer
        assert _is_archive_pointer(message) is False

    @pytest.mark.asyncio
    async def test_read_follows_a_chain_to_the_content(self, hooks):
        """Archives written before the guard still hold chains — the content is
        alive at the bottom, so a read must reach it instead of returning the
        next placeholder."""
        sid = "chain"
        archival = hooks._get_session_components(sid)["archival_memory"]
        real = archival.store(
            {"role": "user", "content": "Freigabe QS-4711, Pruefer Ingrid Salzmann"},
            session_id=sid)
        mid = archival.store(
            {"role": "user", "content": json.dumps(
                {"type": "archived_ref", "ref_id": real, "summary": "User: …"})},
            session_id=sid)
        top = archival.store(
            {"role": "user", "content": json.dumps(
                {"type": "archived_ref", "ref_id": mid, "summary": "User: …"})},
            session_id=sid)

        result = await hooks._handle_context_read(ref=top, session_id=sid)

        assert result["status"] == "success"
        assert "QS-4711" in result["content"]
        assert result["resolved_ref"] == real
        assert result["resolved_through"] == 2

    @pytest.mark.asyncio
    async def test_a_direct_hit_reports_no_hops(self, hooks):
        sid = "chain2"
        archival = hooks._get_session_components(sid)["archival_memory"]
        ref = archival.store({"role": "user", "content": "direkt"}, session_id=sid)
        result = await hooks._handle_context_read(ref=ref, session_id=sid)
        assert "resolved_through" not in result
        assert result["content"] == "direkt"

    @pytest.mark.asyncio
    async def test_a_pointer_cycle_does_not_hang(self, hooks):
        """Damaged data can point in a circle; the walk must end."""
        sid = "cycle"
        archival = hooks._get_session_components(sid)["archival_memory"]
        a = archival.store({"role": "user", "content": "placeholder"}, session_id=sid)
        b = archival.store({"role": "user", "content": json.dumps(
            {"type": "archived_ref", "ref_id": a})}, session_id=sid)
        archival._db.execute(
            "UPDATE archived_messages SET content = ? WHERE id = ?",
            (json.dumps({"type": "archived_ref", "ref_id": b}), a))
        archival._db.commit()

        result = await hooks._handle_context_read(ref=a, session_id=sid)
        assert result["status"] == "error"


class TestRetrievalResultsSurviveCompaction:
    """The second thing the live run exposed, and the more damaging one.

    The agent read the right ref on its FIRST try and the answer was in the
    returned chunk. It then reported the answer missing — because compaction
    externalised that read result before the model ever saw it, replacing the
    content with a reference to itself. The agent read the reference, that
    result was stored too, and so on: 25 read results externalised in one turn.

    Retrieval that can be undone by the compactor is not retrieval.
    """

    @staticmethod
    def _tool_msg(payload):
        return {"role": "tool", "name": "whatever_the_server_is_called",
                "content": json.dumps(payload)}

    def test_an_answer_that_marks_itself_is_exempt(self):
        from plugins.context_engineer.compaction import (
            RETRIEVAL_MARKER,
            _is_retrieval_result,
        )
        msg = self._tool_msg({"status": "success", RETRIEVAL_MARKER: True,
                              "content": "..."})
        assert _is_retrieval_result(msg) is True

    def test_the_check_does_not_depend_on_the_tool_name(self):
        """Tool names are built from the server name in plugins.yaml. Matching
        on them would switch the exemption off silently on a rename."""
        from plugins.context_engineer.compaction import (
            RETRIEVAL_MARKER,
            _is_retrieval_result,
        )
        msg = self._tool_msg({RETRIEVAL_MARKER: True})
        msg["name"] = "renamed_ctx_read"
        assert _is_retrieval_result(msg) is True

    @pytest.mark.parametrize("payload", [
        {"status": "success", "content": "an ordinary tool answer"},
        {"status": "error", "error": "nope"},
        {"retrieval_result": "yes"},      # not the boolean true
        {"retrieval_result": False},
    ])
    def test_other_tool_output_is_still_externalised(self, payload):
        from plugins.context_engineer.compaction import _is_retrieval_result
        assert _is_retrieval_result(self._tool_msg(payload)) is False

    @pytest.mark.parametrize("content", [
        "plain text mentioning retrieval_result", "{broken json", "", None,
        [{"type": "text", "text": "list content"}],
    ])
    def test_non_json_content_is_not_mistaken(self, content):
        from plugins.context_engineer.compaction import _is_retrieval_result
        assert _is_retrieval_result({"role": "tool", "content": content}) is False

    @pytest.mark.asyncio
    async def test_the_tools_actually_mark_their_answers(self, hooks, session, tmp_path):
        """The marker is worthless if the tools forget to set it — this is the
        seam where the two halves meet."""
        from unittest.mock import MagicMock

        from agent_system.config.models import MCPConfig
        from plugins.context_engineer.compaction import RETRIEVAL_MARKER
        from plugins.context_engineer.server import ContextEngineerServer

        srv = ContextEngineerServer("context_engineer", MagicMock(),
                                    MCPConfig(type="context_engineer", enabled=True))
        srv._hooks_impl = hooks

        listed = await srv.list({"_session_id": session})
        found = await srv.list({"filter": "blitter", "_session_id": session})
        got = await srv.read({"ref": listed["entries"][0]["ref"],
                              "_session_id": session})

        for name, result in (("list", listed), ("list+filter", found), ("read", got)):
            assert result.get(RETRIEVAL_MARKER) is True, f"{name} did not mark its answer"


class TestLayerTwoNeverReArchivesAPlaceholder:
    """The guard has to hold on BOTH paths into the archive set.

    The age loop skips placeholders, but a tool call and its result are added
    together through the tool_call map — bypassing that skip entirely. Guarding
    only the loop looked fixed and was not: 13 of 57 entries in a live session
    were still pointers, every one of them half of a tool pair.
    """

    @pytest.fixture
    def strategy(self, tmp_path):
        from plugins.context_engineer.archival_memory import ArchivalMemory
        from plugins.context_engineer.compaction import (
            CompactionConfig,
            LayeredCompactionStrategy,
        )
        from plugins.context_engineer.core_memory import CoreMemory
        from plugins.context_engineer.tool_result_store import ToolResultStore
        from plugins.context_engineer.variable_manager import VariableManager

        archival = ArchivalMemory(tmp_path / "archive.db", session_id="t")
        strat = LayeredCompactionStrategy(
            tool_store=ToolResultStore(tmp_path / "tools.db"),
            variable_manager=VariableManager(min_content_tokens=50,
                                             storage_path=tmp_path / "vars.json"),
            core_memory=CoreMemory(storage_path=tmp_path / "memory.json"),
            archival_memory=archival,
            config=CompactionConfig(archive_after_turns=1, keep_system_messages=True),
        )
        return strat, archival

    @staticmethod
    def _placeholder(role, ref, **extra):
        return {"role": role, "content": json.dumps(
            {"type": "archived_ref", "ref_id": ref, "summary": "…"}), **extra}

    @pytest.mark.asyncio
    async def test_a_placeholder_pulled_in_by_its_pair_is_not_re_archived(self, strategy):
        """The mixed pair is the case that slipped through.

        A placeholder alone never reaches the archive set — the age loop skips
        it. It gets in as the PARTNER of a real message being archived: the
        tool_call map adds the whole pair at once, past every per-message check.
        """
        from plugins.context_engineer.compaction import CompactionResult

        strat, archival = strategy
        messages = [
            {"role": "user", "content": "erste Frage"},
            # real assistant message -> archived on age, and it drags its pair in
            {"role": "assistant", "content": "rufe Werkzeug auf",
             "tool_calls": [{"id": "call_1", "type": "function",
                             "function": {"name": "t", "arguments": "{}"}}]},
            # ...whose result was already archived in an earlier round
            self._placeholder("tool", "arch_bbb", tool_call_id="call_1", name="t"),
            {"role": "user", "content": "zweite Frage"},
            {"role": "assistant", "content": "Antwort"},
        ]
        result = CompactionResult(original_tokens=0, final_tokens=0, tokens_saved=0)
        result.modified_messages = [dict(m) for m in messages]
        strat._current_session_id = "t"

        await strat._apply_layer2(result)

        stored = archival.get_session_messages(session_id="t", limit=50)
        pointers = [s for s in stored if "archived_ref" in (s.content or "")[:60]]
        assert not pointers, f"placeholder re-archived via its pair: {[p.id for p in pointers]}"
        assert any("rufe Werkzeug auf" in (s.content or "") for s in stored), \
            "the real half of the pair should still be archived"

    @pytest.mark.asyncio
    async def test_real_messages_are_still_archived(self, strategy):
        """The guard must not stop ordinary archiving — otherwise compaction
        silently does nothing."""
        from plugins.context_engineer.compaction import CompactionResult

        strat, archival = strategy
        result = CompactionResult(original_tokens=0, final_tokens=0, tokens_saved=0)
        result.modified_messages = [
            {"role": "user", "content": "alte Frage mit echtem Inhalt"},
            {"role": "assistant", "content": "alte Antwort mit echtem Inhalt"},
            {"role": "user", "content": "neue Frage"},
        ]
        strat._current_session_id = "t"

        await strat._apply_layer2(result)

        stored = archival.get_session_messages(session_id="t", limit=50)
        assert stored, "nothing was archived at all"
        assert all("archived_ref" not in (s.content or "")[:60] for s in stored)


class TestLayerOneHonoursTheExemption:
    """Predicate and marker are only two thirds of it.

    Removing the exemption from layer 1 broke nothing in the suite: the tests
    checked that the answer marks itself and that the check recognises it, but
    not that compaction actually acts on it. That is the seam where the live
    failure happened.
    """

    @pytest.fixture
    def strategy(self, tmp_path):
        from plugins.context_engineer.archival_memory import ArchivalMemory
        from plugins.context_engineer.compaction import (
            CompactionConfig,
            LayeredCompactionStrategy,
        )
        from plugins.context_engineer.core_memory import CoreMemory
        from plugins.context_engineer.tool_result_store import ToolResultStore
        from plugins.context_engineer.variable_manager import VariableManager

        store = ToolResultStore(tmp_path / "tools.db", session_id="t")
        strat = LayeredCompactionStrategy(
            tool_store=store,
            variable_manager=VariableManager(min_content_tokens=50,
                                             storage_path=tmp_path / "vars.json"),
            core_memory=CoreMemory(storage_path=tmp_path / "memory.json"),
            archival_memory=ArchivalMemory(tmp_path / "archive.db", session_id="t"),
            # every tool result is old enough and big enough to be externalised
            config=CompactionConfig(tool_result_min_size=1, tool_result_keep_last=0),
        )
        return strat, store

    @staticmethod
    def _tool_msg(call_id, payload):
        return {"role": "tool", "name": "context_engineer_read",
                "tool_call_id": call_id, "content": json.dumps(payload)}

    async def _run_layer1(self, strat, messages):
        from plugins.context_engineer.compaction import CompactionResult
        result = CompactionResult(original_tokens=0, final_tokens=0, tokens_saved=0)
        result.modified_messages = [dict(m) for m in messages]
        strat._current_session_id = "t"
        await strat._apply_layer1(result)
        return result.modified_messages

    @pytest.mark.asyncio
    async def test_a_marked_answer_stays_in_the_conversation(self, strategy):
        from plugins.context_engineer.compaction import RETRIEVAL_MARKER

        strat, store = strategy
        body = "die gesuchte Pruefnummer ist ZX-9931 " * 60
        out = await self._run_layer1(strat, [
            {"role": "user", "content": "was stand da?"},
            self._tool_msg("call_1", {"status": "success", RETRIEVAL_MARKER: True,
                                      "content": body}),
        ])

        assert "ZX-9931" in str(out[1]["content"]), \
            "the retrieval answer was externalised — the agent never sees what it fetched"
        assert store.count_entries("t") == 0

    @pytest.mark.asyncio
    async def test_an_unmarked_result_of_the_same_size_is_externalised(self, strategy):
        """The exemption must be narrow: ordinary bulky output still goes away,
        or layer 1 would stop doing its job."""
        strat, store = strategy
        body = "beliebige grosse Werkzeugausgabe " * 60
        out = await self._run_layer1(strat, [
            {"role": "user", "content": "mach was"},
            {"role": "tool", "name": "writer_content_batch_scene",
             "tool_call_id": "call_2", "content": body},
        ])

        assert body not in str(out[1]["content"])
        assert "tool_result_ref" in str(out[1]["content"]),             "layer 1 stopped externalising ordinary output — the exemption is too wide"
