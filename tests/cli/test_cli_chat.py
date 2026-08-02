"""Chat mode: live per-operation renderer + turn event routing.

The renderer contract mirrors the WebUI (see chat_module.js): one line per
operation key, progress rewritten in place, the end line REPLACES the progress
line and must stand on its own. Thinking is a counter line, not a token flood.
"""
import io

from agent_system.cli_utils.chat import (
    ChatRenderer,
    parse_chat_command,
    run_chat_turn,
)
from agent_system.mcp.status import StatusEvent, StatusPhase, status_bus


def _ev(server="tool.x", message="working", phase=StatusPhase.PROGRESS,
        request_id="r1", depth=0, level="info"):
    return StatusEvent(server=server, request_id=request_id, message=message,
                       phase=phase, depth_level=depth, level=level)


def _renderer(ansi=True, width=60, height=30):
    out = io.StringIO()
    # StringIO has no encoding attribute -> symbol picker must fall back to
    # utf-8 and keep the unicode set.
    return ChatRenderer(ansi=ansi, out=out, width_override=width,
                        height_override=height), out


class TestRendererLiveRegion:
    """Every operation key owns ONE region line, exactly like a front-panel
    row: updates travel up to it via cursor movement instead of appending."""

    def test_progress_rewrites_the_same_line_instead_of_appending(self):
        r, out = _renderer()
        r.handle_status(_ev(message="step 1"))
        r.handle_status(_ev(message="step 2"))
        text = out.getvalue()
        assert "\x1b[1A\r\x1b[K" in text    # up one line, rewrite in place
        assert text.count("\n") == 1        # only the initial append
        assert "step 2" in text

    def test_end_lands_on_the_operations_own_line(self):
        r, out = _renderer()
        r.handle_status(_ev(message="running"))
        r.handle_status(_ev(message="done -- exit 0", phase=StatusPhase.END))
        text = out.getvalue()
        assert "\x1b[1A" in text            # rewrites, does not append
        assert "✓" in text and "done -- exit 0" in text
        assert text.count("\n") == 1

    def test_interleaved_operations_keep_their_own_lines(self):
        """The exact complaint: coordinator step-updates and worker events
        alternate -- each must keep updating its OWN row, not spawn new ones."""
        r, out = _renderer()
        r.handle_status(_ev(request_id="coord", message="step 1/500"))
        r.handle_status(_ev(request_id="worker", message="Executing Tools"))
        r.handle_status(_ev(request_id="coord", message="step 2/500"))
        r.handle_status(_ev(request_id="worker", message="Calling LLM"))
        text = out.getvalue()
        assert text.count("\n") == 2        # exactly one line per operation
        assert "\x1b[2A" in text            # coord update climbed over worker
        assert r._total == 2

    def test_end_for_an_upper_line_rewrites_it_in_place(self):
        r, out = _renderer()
        r.handle_status(_ev(request_id="a", message="op a"))
        r.handle_status(_ev(request_id="b", message="op b"))
        r.handle_status(_ev(request_id="a", message="a done", phase=StatusPhase.END))
        text = out.getvalue()
        assert "a done" in text
        assert text.count("\n") == 2        # no third line for the end
        assert "\x1b[2A" in text

    def test_error_phase_is_red_with_error_symbol(self):
        r, out = _renderer()
        r.handle_status(_ev(message="boom", phase=StatusPhase.ERROR))
        text = out.getvalue()
        assert "✗" in text and "boom" in text
        assert "\x1b[31m" in text

    def test_distinct_errors_do_not_overwrite_each_other(self):
        r, out = _renderer()
        r.error_line("ERROR: erster")
        r.error_line("ERROR: zweiter")
        text = out.getvalue()
        assert "erster" in text and "zweiter" in text
        assert text.count("\n") == 2        # two separate committed lines

    def test_depth_indents(self):
        r, out = _renderer()
        r.handle_status(_ev(message="nested", depth=2, phase=StatusPhase.END))
        # colour prefix may precede the indent -- check the plain content
        assert "    ✓" in out.getvalue().replace("\x1b[32m", "").replace("\x1b[0m", "")

    def test_updates_climb_over_narration_to_their_own_line(self):
        """The reported break: intermediate narration between steps made every
        coordinator update spawn a new line. Narration lines are anonymous
        region lines now -- counted in the offsets, climbed over."""
        r, out = _renderer()
        r.handle_status(_ev(request_id="coord", message="step 6/500"))
        r.println("Jetzt implementiere ich beide Caches.", color="90")
        r.handle_status(_ev(request_id="coord", message="step 7/500"))
        text = out.getvalue()
        assert "\x1b[2A" in text            # climbed over the narration line
        assert text.count("\n") == 2        # no third line for step 7
        assert r._total == 2

    def test_println_hard_wraps_and_counts_physical_lines(self):
        """A soft-wrapped narration line would silently shift every offset
        above it -- so long text is wrapped by the renderer itself."""
        r, out = _renderer(width=21)        # 20 usable columns
        r.println("x" * 45)
        assert r._total == 3                # 20 + 20 + 5
        assert out.getvalue().count("\n") == 3

    def test_println_colours_per_chunk_after_slicing(self):
        r, out = _renderer(width=21)
        r.println("y" * 30, color="90")
        text = out.getvalue()
        # every physical line carries its own complete escape pair
        assert text.count("\x1b[90m") == 2 and text.count("\x1b[0m") == 2
        for chunk in text.split("\x1b")[1:]:
            assert chunk.startswith("[") and "m" in chunk[:6]

    def test_commit_ends_the_region_for_foreign_output(self):
        r, out = _renderer()
        r.handle_status(_ev(message="running"))
        r.commit()
        assert r._total == 0 and r._lines == {}
        # a later event for the same key appends fresh below
        r.handle_status(_ev(message="again"))
        assert r._total == 1

    def test_close_commits_the_region(self):
        r, out = _renderer()
        r.handle_status(_ev(message="running"))
        r.close()
        assert r._total == 0 and r._lines == {}

    def test_line_beyond_cursor_reach_continues_below(self):
        """A row scrolled out of the viewport cannot be reached -- ESC[nA
        clips at the top edge and would corrupt a foreign line. The key gets
        rebound to a fresh bottom line instead."""
        r, out = _renderer(height=3)
        r.handle_status(_ev(request_id="old", message="early"))
        for i in range(5):
            r.handle_status(_ev(request_id=f"fill{i}", message=f"line {i}"))
        r.handle_status(_ev(request_id="old", message="early done",
                            phase=StatusPhase.END))
        text = out.getvalue()
        assert text.count("\n") == 7        # the end APPENDED, no cursor climb
        assert "\x1b[6A" not in text
        assert r._lines["old"] == 6         # rebound to the newest line


class TestRendererWidth:
    def test_long_messages_are_capped_to_terminal_width(self):
        r, out = _renderer(width=40)
        r.handle_status(_ev(message="x" * 300, phase=StatusPhase.END))
        # strip colour, measure the visible line
        plain = (out.getvalue().replace("\x1b[32m", "").replace("\x1b[0m", "")
                 .strip("\n"))
        assert len(plain) < 40
        assert plain.endswith("…")

    def test_cap_never_cuts_an_escape_sequence(self):
        """Colour is applied AFTER capping -- a cut escape sequence is exactly
        the raw-garbage bug this feature replaces."""
        r, out = _renderer(width=30)
        r.handle_status(_ev(message="y" * 300))
        text = out.getvalue()
        chunks = text.split("\x1b")
        assert len(chunks) > 1  # colour IS emitted in ansi mode
        for chunk in chunks[1:]:
            # every escape introducer is followed by a complete [..m sequence
            assert chunk.startswith("[")
            assert "m" in chunk[:6]

    def test_multiline_message_is_folded_to_one_line(self):
        r, out = _renderer()
        r.handle_status(_ev(message="a\nb\nc"))
        # one appended region line; the message's own newlines are folded away
        assert out.getvalue().count("\n") == 1


class TestThinkingCounter:
    def test_deltas_become_a_counter_not_a_token_stream(self):
        r, out = _renderer()
        for _ in range(3):
            r.thinking_delta()
        r.thinking_done()
        text = out.getvalue()
        assert "~3 tokens" in text
        assert "Thought" in text

    def test_counter_line_is_dim_and_updates_in_place(self):
        r, out = _renderer()
        r.thinking_delta()
        r.thinking_done()
        text = out.getvalue()
        assert "\x1b[90m" in text
        assert text.count("✻") >= 1
        assert text.count("\n") == 1           # one region line for the block

    def test_status_event_does_not_interrupt_the_counter(self):
        """Front-panel behaviour: a tool reporting mid-thought updates its own
        row while the thinking counter keeps counting on its row."""
        r, out = _renderer()
        r.thinking_delta()
        r.handle_status(_ev(message="tool starts"))
        r.thinking_delta()
        r.thinking_done()
        text = out.getvalue()
        assert "~2 tokens" in text             # the block kept counting
        assert "tool starts" in text
        assert text.count("\n") == 2           # counter line + tool line

    def test_second_thinking_block_gets_its_own_line(self):
        r, out = _renderer()
        r.thinking_delta()
        r.thinking_done()
        r.thinking_delta()
        r.thinking_done()
        text = out.getvalue()
        assert text.count("Thought") == 2      # two finalized blocks
        assert "~2 tokens" not in text         # the counter reset in between
        assert text.count("\n") == 2           # block 2 did NOT overwrite block 1

    def test_close_finalizes_a_live_counter(self):
        r, out = _renderer()
        r.thinking_delta()
        r.close()
        assert "Thought" in out.getvalue()


class TestNonAnsiFallback:
    def test_no_escapes_and_chronological_lines(self):
        r, out = _renderer(ansi=False)
        r.handle_status(_ev(message="step 1"))
        r.handle_status(_ev(message="step 2"))
        r.handle_status(_ev(message="done", phase=StatusPhase.END))
        text = out.getvalue()
        assert "\x1b" not in text and "\r" not in text
        assert text.count("\n") == 3

    def test_thinking_prints_only_the_summary(self):
        r, out = _renderer(ansi=False)
        for _ in range(5):
            r.thinking_delta()
        assert out.getvalue() == ""       # nothing while counting
        r.thinking_done()
        assert "~5 tokens" in out.getvalue()


class TestNarration:
    def test_narration_is_dimmed_per_line(self):
        r, out = _renderer()
        r.narration("erste Zeile\nzweite Zeile")
        text = out.getvalue()
        assert text.count("\x1b[90m") == 2
        assert "erste Zeile" in text and "zweite Zeile" in text

    def test_empty_narration_prints_nothing(self):
        r, out = _renderer()
        r.narration("   \n  ")
        assert out.getvalue() == ""


class TestParseChatCommand:
    def test_aliases(self):
        for line in ("/exit", "/quit", "/q", "/bye", " /EXIT "):
            assert parse_chat_command(line) == "exit"
        assert parse_chat_command("/new") == "new"
        assert parse_chat_command("/session") == "session"
        assert parse_chat_command("/help") == "help"
        assert parse_chat_command("/?") == "help"

    def test_unknown_command_vs_normal_input(self):
        assert parse_chat_command("/nope") == "unknown"
        assert parse_chat_command("hello world") is None
        assert parse_chat_command("was ist 1/2?") is None


class _FakeAgent:
    """Yields a canned event stream; publishes one status event mid-turn."""

    def __init__(self, events):
        self._events = events
        self.cancelled_requests = []

    async def run_events(self, task, session_id=None, llm_override=None,
                         llm_profile_info_override=None):
        for ev in self._events:
            if ev.get("_publish_status"):
                await status_bus.publish(_ev(message="tool ran",
                                             phase=StatusPhase.END))
                continue
            yield ev

    async def cancel_request(self, request_id):
        self.cancelled_requests.append(request_id)
        return True


class TestRunChatTurn:
    async def test_full_turn_routes_everything(self):
        agent = _FakeAgent([
            {"type": "start", "request_id": "req-9", "session_id": "s1"},
            {"type": "thinking_delta", "delta": "a"},
            {"type": "thinking_delta", "delta": "b"},
            {"_publish_status": True},
            {"type": "thinking_complete",
             "assistant": {"content": "Zwischenstand", "tool_calls": [{"id": "t1"}]}},
            {"type": "thinking_complete",
             "assistant": {"content": "final text", "tool_calls": None}},
            {"type": "final", "summary": "final text"},
            {"type": "end"},
        ])
        r, out = _renderer()
        state = {}
        result = await run_chat_turn(agent, "frage", "s1", r, state=state)

        assert state["request_id"] == "req-9"
        assert result["summary"] == "final text"
        assert result["cancelled"] is False and result["errors"] == []
        text = out.getvalue()
        assert "~2 tokens" in text            # counter, not token stream
        assert "tool ran" in text             # status event was drained
        assert "Zwischenstand" in text        # intermediate narration shown
        assert "final text" not in text       # final renders elsewhere, once
        assert r._total == 0                  # renderer closed the region

    async def test_error_event_is_collected_and_shown(self):
        agent = _FakeAgent([
            {"type": "start", "request_id": "r", "session_id": "s"},
            {"type": "error", "message": "LLM exploded"},
            {"type": "end"},
        ])
        r, out = _renderer()
        result = await run_chat_turn(agent, "x", "s", r)
        assert result["errors"] == ["LLM exploded"]
        assert "LLM exploded" in out.getvalue()

    async def test_cancelled_event_marks_the_result(self):
        agent = _FakeAgent([
            {"type": "start", "request_id": "r", "session_id": "s"},
            {"type": "cancelled", "request_id": "r"},
            {"type": "end"},
        ])
        r, _t = _renderer()
        result = await run_chat_turn(agent, "x", "s", r)
        assert result["cancelled"] is True

    async def test_unsubscribes_from_the_status_bus(self):
        before = len(getattr(status_bus, "_subscribers", []))
        agent = _FakeAgent([
            {"type": "start", "request_id": "r", "session_id": "s"},
            {"type": "end"},
        ])
        r, _t = _renderer()
        await run_chat_turn(agent, "x", "s", r)
        assert len(getattr(status_bus, "_subscribers", [])) == before

    async def test_show_status_false_subscribes_nothing(self):
        before = len(getattr(status_bus, "_subscribers", []))
        agent = _FakeAgent([{"type": "end"}])
        r, _t = _renderer()
        await run_chat_turn(agent, "x", "s", r, show_status=False)
        assert len(getattr(status_bus, "_subscribers", [])) == before


class _ExplodingAgent:
    async def run_events(self, task, session_id=None, llm_override=None,
                         llm_profile_info_override=None):
        raise RuntimeError("auth kaputt")
        yield  # pragma: no cover -- makes this an async generator

    async def cancel_request(self, request_id):
        return True


class TestTurnRobustness:
    async def test_exception_still_unsubscribes_the_status_queue(self):
        before = len(getattr(status_bus, "_subscribers", []))
        r, _t = _renderer()
        try:
            await run_chat_turn(_ExplodingAgent(), "x", "s", r)
        except RuntimeError:
            pass
        assert len(getattr(status_bus, "_subscribers", [])) == before

    def test_exploding_turn_returns_an_error_instead_of_killing_the_repl(self):
        import asyncio
        from agent_system.cli_utils.chat import _ChatContext, _execute_turn

        ctx = _ChatContext(
            agent=_ExplodingAgent(), entry_name="t", session_service=None,
            session_user="u", session_id="s", was_new_session=True,
            llm_profile="p", llm_override=None, llm_profile_info=None,
            show_status=False,
        )
        r, _t = _renderer()
        loop = asyncio.new_event_loop()
        try:
            result = _execute_turn(loop, ctx, "hi", r)
        finally:
            loop.close()
        assert result["errors"] and "auth kaputt" in result["errors"][0]
        assert result["cancelled"] is False


class TestVtReassertion:
    """Console modes are per-console: a spawned MSYS bash resets the VT flag,
    and every escape printed afterwards renders literally. The renderer must
    re-assert before each ANSI paint."""

    def test_ansi_paints_reassert_vt(self, monkeypatch):
        import agent_system.cli_utils.chat as chat_mod
        calls = []
        monkeypatch.setattr(chat_mod, "reassert_vt", lambda: calls.append(1))
        r, _t = _renderer()
        r.handle_status(_ev(message="run"))                       # _open
        r.handle_status(_ev(message="done", phase=StatusPhase.END))  # _final
        r.println("x")                                            # println
        assert len(calls) >= 3

    def test_non_ansi_never_touches_the_console(self, monkeypatch):
        import agent_system.cli_utils.chat as chat_mod

        def _boom():
            raise AssertionError("non-ANSI output must not call reassert_vt")

        monkeypatch.setattr(chat_mod, "reassert_vt", _boom)
        r, _t = _renderer(ansi=False)
        r.handle_status(_ev(message="run"))
        r.println("x")
