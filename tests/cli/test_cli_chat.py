"""Chat mode: live per-operation renderer + turn event routing.

The renderer contract mirrors the WebUI (see chat_module.js): one line per
operation key, progress rewritten in place, the end line REPLACES the progress
line and must stand on its own. Thinking is a counter line, not a token flood.
"""
import asyncio
import builtins
import io
import json
import logging
import sys

import pytest

from agent_system.cli_utils.chat import (
    _ASCII_SYMBOLS,
    _KeyReader,
    _poll_typed_input,
    ChatRenderer,
    _accumulate_usage,
    _format_usage,
    _read_input,
    _show_history,
    _show_costs,
    _show_skills,
    _show_tools,
    _show_last,
    _restore_logging,
    _silence_stdout_logging,
    display_width,
    parse_chat_command,
    run_chat_turn,
    suggest_command,
)
from agent_system.mcp.status import StatusEvent, StatusPhase, status_bus


def _bus_queue_count() -> int:
    """Queue subscribers currently registered on the bus.

    The earlier version of these tests read a `_subscribers` attribute that
    does not exist and defaulted it to [] -- every leak assertion was 0 == 0.
    No getattr default here: if the attribute is renamed the test must break.
    """
    return len(status_bus._queue_handlers)


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
            assert parse_chat_command(line)[0] == "exit"
        assert parse_chat_command("/new")[0] == "new"
        assert parse_chat_command("/session")[0] == "session"
        assert parse_chat_command("/sessions")[0] == "sessions"
        assert parse_chat_command("/help")[0] == "help"
        assert parse_chat_command("/?")[0] == "help"

    def test_payload_is_split_off(self):
        assert parse_chat_command("/resume ab12cd") == ("resume", "ab12cd")
        assert parse_chat_command("/resume") == ("resume", "")

    def test_paths_are_messages_not_commands(self):
        """A path is ordinary input for a sysadmin/coder agent. Treating it as
        a typo'd command silently swallowed the message."""
        assert parse_chat_command("/etc/nginx/nginx.conf pruefen") == (
            None, "/etc/nginx/nginx.conf pruefen")
        assert parse_chat_command("/usr/local/bin")[0] is None
        assert parse_chat_command("/tmp/x.log lesen")[0] is None

    def test_mistyped_command_is_flagged_not_sent_to_the_llm(self):
        """The other half: "/h" is a typo, not a message. Passing it through
        spent a whole LLM turn on it."""
        assert parse_chat_command("/sesion") == ("unknown", "/sesion")
        assert parse_chat_command("/xyz") == ("unknown", "/xyz")
        # ...while the short forms people actually type ARE aliases
        assert parse_chat_command("/h")[0] == "help"

    def test_suggests_the_closest_command(self):
        assert suggest_command("/hel") == "/help"      # prefix
        assert suggest_command("/sesion") == "/session"  # difflib
        assert suggest_command("/zzzzz") is None

    def test_double_slash_escapes_a_command_word(self):
        assert parse_chat_command("//new heisst bei uns anders") == (
            None, "/new heisst bei uns anders")

    def test_normal_input_passes_through(self):
        assert parse_chat_command("hello world") == (None, "hello world")
        assert parse_chat_command("was ist 1/2?") == (None, "was ist 1/2?")


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


class TestTheReportedContextFill:
    """`context_tokens` must describe the WINDOW, not the turn's throughput.

    A multi-step turn resends the whole history on every step, so summing
    the calls (which is right for cost) would report several times the
    window size and show "ctx 300k/272k".
    """

    class _Client:
        def __init__(self, window):
            self.model = "m"
            self.context_window = window

    def _agent(self, events, window=272000):
        agent = _FakeAgent(events)
        agent.llm = self._Client(window)
        return agent

    async def test_the_last_call_decides_not_the_sum(self):
        agent = self._agent([
            {"type": "start", "request_id": "r", "session_id": "s"},
            {"type": "thinking_complete", "assistant": {"tool_calls": [{"id": "t"}]},
             "usage": {"prompt_tokens": 5000, "completion_tokens": 100}},
            {"type": "thinking_complete", "assistant": {"tool_calls": None},
             "usage": {"prompt_tokens": 9000, "completion_tokens": 300}},
            {"type": "final", "summary": "done",
             "usage": {"prompt_tokens": 9000, "completion_tokens": 300}},
            {"type": "end"},
        ])
        r, _ = _renderer()
        result = await run_chat_turn(agent, "q", "s", r)

        assert result["context_tokens"] == 9300      # last call, not 14k+
        assert result["context_window"] == 272000
        assert result["usage"]["prompt_tokens"] == 14000  # cost still sums

    async def test_an_extra_final_call_becomes_the_last_one(self):
        """After max_steps a separate final-answer call runs with no
        thinking_complete of its own — that one holds the real fill."""
        agent = self._agent([
            {"type": "start", "request_id": "r", "session_id": "s"},
            {"type": "thinking_complete", "assistant": {"tool_calls": [{"id": "t"}]},
             "usage": {"prompt_tokens": 5000, "completion_tokens": 100}},
            {"type": "final", "summary": "done",
             "usage": {"prompt_tokens": 7000, "completion_tokens": 50}},
            {"type": "end"},
        ])
        r, _ = _renderer()
        result = await run_chat_turn(agent, "q", "s", r)
        assert result["context_tokens"] == 7050

    async def test_a_client_without_a_window_reports_only_the_fill(self):
        agent = self._agent([
            {"type": "start", "request_id": "r", "session_id": "s"},
            {"type": "thinking_complete", "assistant": {"tool_calls": None},
             "usage": {"prompt_tokens": 800, "completion_tokens": 20}},
            {"type": "final", "summary": "d"},
            {"type": "end"},
        ], window=None)
        r, _ = _renderer()
        result = await run_chat_turn(agent, "q", "s", r)
        assert result["context_tokens"] == 820
        assert "context_window" not in result

    async def test_a_usage_less_step_does_not_erase_the_reference(self):
        """The server emits thinking_complete without usage (empty-assistant
        branch, and both `if usage` guards). That must not reset what the
        last known call was: the fill would vanish, and — worse — the final
        event would stop recognizing itself as a repeat and bill the call
        a second time."""
        agent = self._agent([
            {"type": "start", "request_id": "r", "session_id": "s"},
            {"type": "thinking_complete", "assistant": {"tool_calls": [{"id": "t"}]},
             "usage": {"prompt_tokens": 8000, "completion_tokens": 200}},
            {"type": "thinking_complete", "assistant": {"tool_calls": None}},
            {"type": "final", "summary": "done",
             "usage": {"prompt_tokens": 8000, "completion_tokens": 200}},
            {"type": "end"},
        ])
        r, _ = _renderer()
        result = await run_chat_turn(agent, "q", "s", r)

        assert result["context_tokens"] == 8200, "the fill survives the gap"
        assert result["usage"]["prompt_tokens"] == 8000, (
            "the final event repeats the last call and must not be billed twice")

    async def test_a_mock_window_is_not_taken_for_a_number(self):
        """Same guard as _call_pricing_key next door: a bare MagicMock answers
        every attribute, and `mock // 1000` in the formatter would take the
        chat loop down after the answer was already on screen."""
        from unittest.mock import MagicMock

        agent = _FakeAgent([
            {"type": "start", "request_id": "r", "session_id": "s"},
            {"type": "thinking_complete", "assistant": {"tool_calls": None},
             "usage": {"prompt_tokens": 800, "completion_tokens": 20}},
            {"type": "final", "summary": "d"},
            {"type": "end"},
        ])
        agent.llm = MagicMock()
        r, _ = _renderer()
        result = await run_chat_turn(agent, "q", "s", r)

        assert "context_window" not in result
        assert _format_usage({}, 1.0, _ASCII_SYMBOLS,
                             context=(result["context_tokens"], None))

    async def test_a_turn_without_usage_reports_no_fill(self):
        """No LLM call ran (a pure command turn) — the footer must not claim
        a context size of 0."""
        agent = self._agent([
            {"type": "start", "request_id": "r", "session_id": "s"},
            {"type": "final", "summary": "d"},
            {"type": "end"},
        ])
        r, _ = _renderer()
        result = await run_chat_turn(agent, "q", "s", r)
        assert "context_tokens" not in result


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
        before = _bus_queue_count()
        agent = _FakeAgent([
            {"type": "start", "request_id": "r", "session_id": "s"},
            {"type": "end"},
        ])
        r, _t = _renderer()
        await run_chat_turn(agent, "x", "s", r)
        assert _bus_queue_count() == before

    async def test_show_status_false_subscribes_nothing(self):
        before = _bus_queue_count()
        agent = _FakeAgent([{"type": "end"}])
        r, _t = _renderer()
        await run_chat_turn(agent, "x", "s", r, show_status=False)
        assert _bus_queue_count() == before


class _ExplodingAgent:
    async def run_events(self, task, session_id=None, llm_override=None,
                         llm_profile_info_override=None):
        raise RuntimeError("auth kaputt")
        yield  # pragma: no cover -- makes this an async generator

    async def cancel_request(self, request_id):
        return True


class TestTurnRobustness:
    async def test_exception_still_unsubscribes_the_status_queue(self):
        before = _bus_queue_count()
        r, _t = _renderer()
        try:
            await run_chat_turn(_ExplodingAgent(), "x", "s", r)
        except RuntimeError:
            pass
        assert _bus_queue_count() == before

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


class TestDisplayWidth:
    """Region offsets assume one printed line is one physical line. len()
    counts code points, so a CJK/emoji chunk could wrap and silently shift
    every offset above it -- the corruption the hard wrap exists to prevent."""

    def test_wide_characters_count_two_columns(self):
        assert display_width("abc") == 3
        assert display_width("日本語") == 6
        assert display_width("a日b") == 4

    def test_combining_marks_count_zero(self):
        assert display_width("é") == 1        # e + combining acute

    def test_wrapped_lines_never_exceed_the_column_budget(self):
        r, out = _renderer(width=21)                # 20 usable columns
        r.println("日本語" * 12)
        for line in out.getvalue().split("\n")[:-1]:
            plain = line.replace("\x1b[90m", "").replace("\x1b[0m", "")
            assert display_width(plain) <= 20
        assert r._total == out.getvalue().count("\n")

    def test_fit_caps_by_columns_not_code_points(self):
        r, out = _renderer(width=15)
        r.handle_status(_ev(message="日" * 40, phase=StatusPhase.END))
        plain = (out.getvalue().replace("\x1b[32m", "").replace("\x1b[0m", "")
                 .strip("\n"))
        assert display_width(plain) <= 14


class TestNarrationWrapping:
    def test_wraps_on_word_boundaries(self):
        r, out = _renderer(width=21)
        r.println("alpha beta gamma delta epsilon")
        lines = [ln for ln in out.getvalue().split("\n") if ln]
        assert "alpha" in lines[0]
        assert any("epsilon" in ln for ln in lines)
        for ln in lines:                            # no word cut in half
            assert "alph" not in ln or "alpha" in ln

    def test_word_longer_than_the_line_is_still_split(self):
        r, out = _renderer(width=11)                # 10 usable
        r.println("x" * 25)
        assert r._total == out.getvalue().count("\n")
        assert r._total >= 3

    def test_blank_line_stays_blank(self):
        r, out = _renderer()
        r.println("")
        assert out.getvalue() == "\n"
        assert r._total == 1


class TestErrorOutput:
    def test_errors_are_not_truncated_to_one_line(self):
        """The actionable part (provider body, env var, URL) sits at the END;
        capping to terminal width dropped exactly that."""
        r, out = _renderer(width=40)
        r.error_line("ERROR: LLM call failed: 401 Unauthorized -- set "
                     "ANTHROPIC_API_KEY in config/llm.yaml to fix this")
        plain = out.getvalue().replace("\x1b[31m", "").replace("\x1b[0m", "")
        assert "ANTHROPIC_API_KEY" in plain
        assert "…" not in plain

    def test_multiline_errors_keep_their_lines(self):
        r, out = _renderer()
        r.error_line("Zeile eins\nZeile zwei")
        plain = out.getvalue().replace("\x1b[31m", "").replace("\x1b[0m", "")
        assert "Zeile eins" in plain and "Zeile zwei" in plain


class TestResize:
    def test_width_change_ends_the_region(self):
        """A resize reflows already-printed lines, so recorded offsets stop
        matching physical lines -- climbing after that corrupts the viewport."""
        r, out = _renderer(width=60)
        r.handle_status(_ev(request_id="a", message="erste"))
        r._width_override = 30                      # user drags the window
        r.handle_status(_ev(request_id="a", message="zweite"))
        text = out.getvalue()
        assert "\x1b[1A" not in text                # no climb across the resize
        assert text.count("\n") == 2                # appended instead

class TestUsage:
    """Cost is resolved PER CALL in _accumulate_usage (a session can span
    several models); _format_usage only renders what was already decided."""

    def test_accumulates_tokens_across_calls(self):
        total = {}
        _accumulate_usage(total, {"prompt_tokens": 100, "completion_tokens": 20})
        _accumulate_usage(total, {"prompt_tokens": 50, "completion_tokens": 5})
        assert total["prompt_tokens"] == 150
        assert total["completion_tokens"] == 25

    def test_ignores_malformed_usage(self):
        total = {}
        _accumulate_usage(total, None)
        _accumulate_usage(total, "nonsense")
        assert total == {}

    def test_format_is_compact(self):
        line = _format_usage({"prompt_tokens": 1200, "completion_tokens": 830,
                              "cost": 0.0213}, 221.0, _ASCII_SYMBOLS)
        assert "1.2k" in line and "830" in line and "$0.0213" in line
        assert "3m41s" in line

    def test_cached_tokens_are_extracted_like_the_usage_tracker(self):
        total = {}
        _accumulate_usage(total, {"prompt_tokens": 1000,
                                  "prompt_tokens_details": {"cached_tokens": 900}})
        _accumulate_usage(total, {"prompt_tokens": 1000,
                                  "prompt_tokens_details": {"cached_tokens": 500}})
        assert total["cached_tokens"] == 1400
        assert total["prompt_tokens"] == 2000

    def test_cache_rate_is_shown(self):
        line = _format_usage({"prompt_tokens": 1000, "completion_tokens": 10,
                              "cached_tokens": 940, "cost": 0.01},
                             1.0, _ASCII_SYMBOLS)
        assert "94% cached" in line

    def test_a_zero_cache_rate_is_shown_rather_than_hidden(self):
        """It used to be omitted when nothing was cached — but "0% cached" is
        the case worth seeing: the prefix cache is not being hit at all."""
        line = _format_usage({"prompt_tokens": 1000, "completion_tokens": 10},
                             1.0, _ASCII_SYMBOLS)
        assert "0% cached" in line

    def test_a_cache_write_is_named_next_to_a_zero_read_rate(self):
        """"0% cached" alone cannot tell a broken cache from the first turn of
        a working one — that ambiguity is what made Claude's dropped
        cache_control look like a config error for an hour."""
        line = _format_usage({"prompt_tokens": 19485, "completion_tokens": 54,
                              "cached_tokens": 0, "cache_write_tokens": 19396},
                             1.0, _ASCII_SYMBOLS)
        assert "0% cached, +19.4k written" in line

    def test_no_write_no_noise(self):
        """Counter-check: a provider that reports no writes must not get a
        "+0 written" tacked on."""
        line = _format_usage({"prompt_tokens": 5000, "completion_tokens": 10,
                              "cached_tokens": 4000}, 1.0, _ASCII_SYMBOLS)
        assert "written" not in line and "80% cached" in line

    def test_nothing_sent_means_no_cache_rate(self):
        """Counter-check: 0/0 must not render as "0% cached" (or divide)."""
        assert "cached" not in _format_usage({}, 1.0, _ASCII_SYMBOLS)


class TestContextFill:
    """The window fill after a turn, next to the throughput of that turn.

    Both come from the LAST call: its prompt is the whole history as the
    model saw it. Summing every call would report how much was pushed
    through the turn, which for a multi-step turn counts the same history
    once per step.
    """

    def test_fill_percentage_of_the_window(self):
        line = _format_usage({"prompt_tokens": 100, "completion_tokens": 10},
                             1.0, _ASCII_SYMBOLS, context=(118909, 272000))
        assert "ctx 118.9k/272k (43%)" in line

    def test_without_a_known_window_only_the_size(self):
        """A client that does not declare context_window must still show the
        fill instead of dropping the segment or dividing by zero."""
        line = _format_usage({"prompt_tokens": 100, "completion_tokens": 10},
                             1.0, _ASCII_SYMBOLS, context=(5000, None))
        assert "ctx 5.0k" in line and "%)" not in line

    def test_the_session_total_has_no_fill_and_no_rate(self):
        """Elapsed there is wall time including the user typing — a tok/s
        computed over it would describe the human, not the model."""
        line = _format_usage({"prompt_tokens": 100, "completion_tokens": 900},
                             600.0, _ASCII_SYMBOLS)
        assert "ctx" not in line and "tok/s" not in line

    def test_throughput_counts_generated_tokens_only(self):
        """Prompt tokens are not generated; counting them would scale the
        rate with the history on every turn."""
        line = _format_usage({"prompt_tokens": 100000, "completion_tokens": 840},
                             20.0, _ASCII_SYMBOLS, context=(1000, 200000))
        # Compared as a whole segment: "42 tok/s" is a substring of the
        # "5042 tok/s" that counting the prompt would produce, so `in` would
        # pass on exactly the bug this guards.
        assert line.split(_ASCII_SYMBOLS["sep"])[-1] == "42 tok/s"

    def test_provider_cost_is_billing_and_carries_no_tilde(self):
        total = {}
        _accumulate_usage(total, {"prompt_tokens": 10, "cost": 0.02}, "any-model")
        assert total["cost"] == 0.02
        assert total["cost_is_estimate"] is False
        assert "~" not in _format_usage(total, 1.0, _ASCII_SYMBOLS)

    def test_one_estimated_call_makes_the_whole_sum_an_estimate(self, monkeypatch):
        """Otherwise a partly guessed total would be presented as billing."""
        import agent_system.llm.pricing as pricing
        monkeypatch.setattr(pricing, "estimate_cost", lambda *a, **k: 0.5)
        total = {}
        _accumulate_usage(total, {"prompt_tokens": 10, "cost": 0.02}, "m")
        _accumulate_usage(total, {"prompt_tokens": 10}, "m")
        assert total["cost"] == pytest.approx(0.52)
        assert total["cost_is_estimate"] is True
        assert "~$0.5200" in _format_usage(total, 1.0, _ASCII_SYMBOLS)

    def test_unpriceable_calls_are_named_not_dropped(self):
        """Mixed providers used to yield a total that silently omitted every
        call without a price -- too low AND labelled exact."""
        total = {}
        _accumulate_usage(total, {"prompt_tokens": 10, "cost": 0.02}, "m")
        _accumulate_usage(total, {"prompt_tokens": 999}, "gibt-es-nicht-xyz")
        assert total["cost_unpriced_calls"] == 1
        assert "+1 unpriced" in _format_usage(total, 1.0, _ASCII_SYMBOLS)

    def test_tokens_still_count_when_the_price_is_unknown(self):
        total = {}
        _accumulate_usage(total, {"prompt_tokens": 999}, "gibt-es-nicht-xyz")
        assert total["prompt_tokens"] == 999
        assert "cost" not in total

    def test_format_without_usage_still_shows_time(self):
        assert _format_usage({}, 5.0, _ASCII_SYMBOLS) == "5s"

def _feed(lines):
    """Replacement for input() that yields the given lines, then EOF."""
    it = iter(lines)

    def fake_input(prompt=""):
        try:
            return next(it)
        except StopIteration:
            raise EOFError
    return fake_input


class TestMultilineInput:
    """Pasting a code block used to fire ONE TURN PER LINE: line 1 started a
    task and the rest sat in the console buffer, launching back to back."""

    def test_fenced_block_is_one_message(self, monkeypatch):
        monkeypatch.setattr(builtins, "input",
                            _feed(['"""', "move.w d0,d1", "rts", '"""']))
        assert _read_input("> ") == "move.w d0,d1\nrts"

    def test_fence_closing_on_the_content_line(self, monkeypatch):
        monkeypatch.setattr(builtins, "input",
                            _feed(['"""', "eine zeile", 'zwei"""']))
        assert _read_input("> ") == "eine zeile\nzwei"

    def test_backslash_continuation(self, monkeypatch):
        monkeypatch.setattr(builtins, "input",
                            _feed(["erste \\", "zweite \\", "dritte"]))
        assert _read_input("> ") == "erste \nzweite \ndritte"

    def test_plain_line_is_untouched(self, monkeypatch):
        monkeypatch.setattr(builtins, "input", _feed(["normale frage"]))
        assert _read_input("> ") == "normale frage"

    def test_unterminated_fence_ends_at_eof(self, monkeypatch):
        monkeypatch.setattr(builtins, "input", _feed(['"""', "abc"]))
        assert _read_input("> ") == "abc"


class TestLoggingSilence:
    def test_stdout_handler_is_muted_and_restored(self):
        """Console log lines land in the same stream as the live region but are
        invisible to its accounting -- every later climb would land too high."""
        root = logging.getLogger()
        handler = logging.StreamHandler(sys.stdout)
        handler.setLevel(logging.WARNING)
        root.addHandler(handler)
        try:
            silenced = _silence_stdout_logging()
            assert handler.level > logging.CRITICAL
            _restore_logging(silenced)
            assert handler.level == logging.WARNING
        finally:
            root.removeHandler(handler)

    def test_file_handlers_keep_logging(self, tmp_path):
        root = logging.getLogger()
        handler = logging.FileHandler(tmp_path / "x.log", encoding="utf-8")
        handler.setLevel(logging.INFO)
        root.addHandler(handler)
        try:
            _silence_stdout_logging()
            assert handler.level == logging.INFO   # untouched: nothing is lost
        finally:
            root.removeHandler(handler)
            handler.close()


class TestNoStatusGating:
    async def test_narration_is_suppressed_with_no_status(self):
        """--no-status means stdout carries answers only; narration ignored it."""
        agent = _FakeAgent([
            {"type": "start", "request_id": "r", "session_id": "s"},
            {"type": "thinking_complete",
             "assistant": {"content": "Zwischenstand", "tool_calls": [{"id": "t"}]}},
            {"type": "final", "summary": "fertig"},
            {"type": "end"},
        ])
        r, out = _renderer()
        await run_chat_turn(agent, "x", "s", r, show_status=False)
        assert "Zwischenstand" not in out.getvalue()


class TestTurnUsage:
    async def test_usage_is_summed_over_the_turn(self):
        agent = _FakeAgent([
            {"type": "start", "request_id": "r", "session_id": "s"},
            {"type": "thinking_complete",
             "assistant": {"content": "a", "tool_calls": [{"id": "t"}]},
             "usage": {"prompt_tokens": 10, "completion_tokens": 2}},
            {"type": "thinking_complete",
             "assistant": {"content": "b", "tool_calls": None},
             "usage": {"prompt_tokens": 30, "completion_tokens": 4}},
            {"type": "final", "summary": "b"},
            {"type": "end"},
        ])
        r, _t = _renderer()
        result = await run_chat_turn(agent, "x", "s", r)
        assert result["usage"]["prompt_tokens"] == 40
        assert result["usage"]["completion_tokens"] == 6


class _Msg:
    """Stand-in for a ChatMessage."""

    def __init__(self, role, content=None, tool_calls=None):
        self.role = role
        self.content = content
        self.tool_calls = tool_calls


class _Tracker:
    def __init__(self, messages):
        self._messages = messages

    def get_session_messages(self, session_id):
        return self._messages


class _AgentWithHistory:
    def __init__(self, messages):
        self._session_tracker = _Tracker(messages)


def _ctx_with(messages):
    from agent_system.cli_utils.chat import _ChatContext
    return _ChatContext(
        agent=_AgentWithHistory(messages), entry_name="a", session_service=None,
        session_user="u", session_id="s1", was_new_session=False,
        llm_profile="p", llm_override=None, llm_profile_info=None,
        show_status=True,
    )


_TURN = [
    _Msg("user", "erste frage"),
    _Msg("assistant", "erste antwort"),
    _Msg("user", "zweite frage"),
    _Msg("assistant", "ich rufe ein tool", tool_calls=[
        {"function": {"name": "terminal_execute", "arguments": '{"command":"ls"}'}}]),
    _Msg("tool", "a.txt\nb.txt"),
    _Msg("assistant", "zweite antwort"),
]


class TestHistoryCommand:
    def test_shows_the_requested_number_of_exchanges(self):
        r, out = _renderer()
        _show_history(_ctx_with(_TURN), r, "1")
        text = out.getvalue()
        assert "zweite frage" in text
        assert "erste frage" not in text        # only the last exchange

    def test_counts_exchanges_not_raw_messages(self):
        """One turn holds several tool messages -- "2" must mean two USER
        turns, not two list entries."""
        r, out = _renderer()
        _show_history(_ctx_with(_TURN), r, "2")
        text = out.getvalue()
        assert "erste frage" in text and "zweite frage" in text

    def test_shows_tool_calls_and_results(self):
        r, out = _renderer()
        _show_history(_ctx_with(_TURN), r, "2")
        text = out.getvalue()
        assert "terminal_execute" in text       # the request
        assert "a.txt" in text                  # the result

    def test_empty_session_says_so(self, capsys):
        r, _t = _renderer()
        _show_history(_ctx_with([]), r, "")
        assert "No messages" in capsys.readouterr().out

    def test_bad_count_is_reported_not_crashed(self, capsys):
        r, _t = _renderer()
        _show_history(_ctx_with(_TURN), r, "viele")
        assert "Usage: /history" in capsys.readouterr().out

    def test_multimodal_content_degrades_readably(self):
        messages = [_Msg("user", [{"type": "text", "text": "was ist das"},
                                  {"type": "image", "source": {}}])]
        r, out = _renderer()
        _show_history(_ctx_with(messages), r, "1")
        text = out.getvalue()
        assert "was ist das" in text and "[image]" in text


class TestLastCommand:
    def test_shows_the_last_turns_tool_traffic_in_full(self):
        """The live region collapses a tool call to one line, so what it
        RETURNED is invisible -- this is chat's --show-mcp."""
        r, out = _renderer()
        _show_last(_ctx_with(_TURN), r)
        text = out.getvalue()
        assert "terminal_execute" in text and "a.txt" in text
        assert "erste frage" not in text        # only the last turn

    def test_turn_without_tools_says_so(self, capsys):
        r, _t = _renderer()
        _show_last(_ctx_with([_Msg("user", "hi"), _Msg("assistant", "hallo")]), r)
        assert "no tools" in capsys.readouterr().out

    def test_no_turn_yet(self, capsys):
        r, _t = _renderer()
        _show_last(_ctx_with([]), r)
        assert "No turn" in capsys.readouterr().out


_BIG_ARGS = json.dumps({
    "filePath": "E:\\ws\\cube.asm",
    "newString": "DrawLineBlit:\n    movem.l d2-d7,-(sp)\n    rts",
})
_BIG_RESULT = json.dumps({
    "status": "success",
    "content": "zeile eins\nzeile zwei\nzeile drei",
    "total_lines": 718,
})

_TOOL_TURN = [
    _Msg("user", "baue den blitter um"),
    _Msg("assistant", "ich schreibe die routine", tool_calls=[
        {"function": {"name": "coder_file_ops_replace_string_in_file",
                      "arguments": _BIG_ARGS}}]),
    _Msg("tool", _BIG_RESULT),
    _Msg("assistant", "fertig"),
]


class TestHistoryExcludesCommands:
    """Commands never reach the agent -- but before "/h" became an alias,
    unknown ones were passed through as messages and sit in old sessions."""

    def test_command_leftovers_are_not_shown(self):
        messages = [
            _Msg("user", "echte frage"),
            _Msg("assistant", "echte antwort"),
            _Msg("user", "/h"),
            _Msg("user", "/session"),
        ]
        r, out = _renderer()
        _show_history(_ctx_with(messages), r, "5")
        text = out.getvalue()
        assert "echte frage" in text
        assert "/h" not in text and "/session" not in text

    def test_command_leftovers_do_not_consume_the_count(self):
        """Counting them would push the real exchanges out of view."""
        messages = [
            _Msg("user", "frage eins"), _Msg("assistant", "antwort eins"),
            _Msg("user", "/h"),
            _Msg("user", "frage zwei"), _Msg("assistant", "antwort zwei"),
        ]
        r, out = _renderer()
        _show_history(_ctx_with(messages), r, "2")
        text = out.getvalue()
        assert "frage eins" in text and "frage zwei" in text

    def test_paths_are_not_mistaken_for_commands(self):
        messages = [_Msg("user", "/etc/nginx/nginx.conf pruefen"),
                    _Msg("assistant", "ok")]
        r, out = _renderer()
        _show_history(_ctx_with(messages), r, "1")
        assert "/etc/nginx/nginx.conf" in out.getvalue()

    def test_session_with_only_commands_says_so(self, capsys):
        r, _t = _renderer()
        _show_history(_ctx_with([_Msg("user", "/h")]), r, "5")
        assert "No agent exchanges" in capsys.readouterr().out


class TestToolTrafficRendering:
    def test_history_keeps_tool_traffic_to_one_line_each(self):
        """The raw JSON dump of a 5000-char newString made /history unreadable."""
        r, out = _renderer(width=200)
        _show_history(_ctx_with(_TOOL_TURN), r, "1")
        lines = [ln for ln in out.getvalue().split("\n") if "→" in ln or "←" in ln]
        assert len(lines) == 2                      # one request, one result
        for ln in lines:
            assert len(ln) < 200
        assert "coder_file_ops_replace_string_in_file" in out.getvalue()

    def test_history_does_not_leak_escaped_newlines(self):
        r, out = _renderer(width=200)
        _show_history(_ctx_with(_TOOL_TURN), r, "1")
        assert "\\n" not in out.getvalue()

    def test_last_shows_the_full_argument_values(self):
        r, out = _renderer(width=200)
        _show_last(_ctx_with(_TOOL_TURN), r)
        text = out.getvalue()
        assert "movem.l d2-d7,-(sp)" in text        # nothing truncated away
        assert "E:\\ws\\cube.asm" in text

    def test_last_renders_newlines_as_lines_not_escapes(self):
        """The wall of text came from \\n arriving as two characters."""
        r, out = _renderer(width=200)
        _show_last(_ctx_with(_TOOL_TURN), r)
        text = out.getvalue()
        assert "\\n" not in text
        assert "zeile eins" in text and "zeile drei" in text
        # each source line became its own physical line
        assert sum(1 for ln in text.split("\n") if "zeile" in ln) == 3

    def test_last_counts_every_physical_line(self):
        """The region's invariant: _total must match printed lines."""
        r, out = _renderer(width=200)
        _show_last(_ctx_with(_TOOL_TURN), r)
        assert out.getvalue().count("\n") > 5       # it really did wrap out
        assert r._total == 0                        # and committed at the end

    def test_non_json_tool_payloads_still_render(self):
        messages = [_Msg("user", "x"),
                    _Msg("assistant", "y", tool_calls=[
                        {"function": {"name": "t", "arguments": "nicht json"}}]),
                    _Msg("tool", "auch nicht json")]
        r, out = _renderer(width=200)
        _show_last(_ctx_with(messages), r)
        text = out.getvalue()
        assert "nicht json" in text and "auch nicht json" in text


class _Registry:
    def __init__(self, names):
        self._names = names

    def list(self):
        return self._names


class _Skills:
    def __init__(self, always=None, on_demand=None):
        self.always = always or []
        self.on_demand = on_demand or []


class _AgentConfig:
    def __init__(self, skills=None):
        self.skills = skills


class _ToolAgent:
    def __init__(self, tools, servers=(), skills=None, fail=False):
        self._tools = tools
        self.registry = _Registry(list(servers))
        self.agent_config = _AgentConfig(skills)
        self._fail = fail

    async def _list_usable_tools_with_details(self, params):
        if self._fail:
            raise RuntimeError("registry kaputt")
        return self._tools


def _tool_ctx(agent):
    from agent_system.cli_utils.chat import _ChatContext
    return _ChatContext(
        agent=agent, entry_name="amiga_coder", session_service=None,
        session_user="u", session_id="s", was_new_session=False,
        llm_profile="p", llm_override=None, llm_profile_info=None,
        show_status=True,
    )


_TOOLS = [
    {"name": "tavily_search_web_search", "description": "AI-powered web search"},
    {"name": "tavily_search_extract", "description": "Extract content from URLs"},
    {"name": "terminal_execute", "description": "Execute a shell command"},
]
_SERVERS = ["tavily_search", "terminal"]


class TestToolsCommand:
    """The point of this command: the MODEL is an unreliable source. Asked for
    "tavily_search" it answered "I don't have that" because the tool is named
    tavily_search_web_search. This reads the schema instead."""

    async def test_lists_the_real_tool_names(self):
        r, out = _renderer(width=200)
        await _show_tools(_tool_ctx(_ToolAgent(_TOOLS, _SERVERS)), r, "")
        text = out.getvalue()
        assert "tavily_search_web_search" in text
        assert "terminal_execute" in text

    async def test_groups_by_server_prefix(self):
        r, out = _renderer(width=200)
        await _show_tools(_tool_ctx(_ToolAgent(_TOOLS, _SERVERS)), r, "")
        lines = [ln for ln in out.getvalue().split("\n") if ln.strip()]
        plain = [ln.replace("\x1b[34m", "").replace("\x1b[90m", "")
                 .replace("\x1b[0m", "") for ln in lines]
        # server headers are unindented, their tools are indented below
        assert "tavily_search" in plain
        idx = plain.index("tavily_search")
        assert plain[idx + 1].startswith("  tavily_search_")

    async def test_longest_server_prefix_wins(self):
        """"coder_file_ops_read_file" must group under coder_file_ops, not
        under a shorter server that happens to share a prefix."""
        tools = [{"name": "coder_file_ops_read_file", "description": "d"}]
        r, out = _renderer(width=200)
        await _show_tools(_tool_ctx(_ToolAgent(tools, ["coder", "coder_file_ops"])), r, "")
        plain = out.getvalue().replace("\x1b[34m", "").replace("\x1b[0m", "")
        assert "\ncoder_file_ops\n" in "\n" + plain

    async def test_filter_narrows_by_name_and_description(self, capsys):
        r, out = _renderer(width=200)
        await _show_tools(_tool_ctx(_ToolAgent(_TOOLS, _SERVERS)), r, "tavily")
        text = out.getvalue()
        assert "tavily_search_web_search" in text
        assert "terminal_execute" not in text
        assert "matching 'tavily'" in capsys.readouterr().out

    async def test_filter_without_match_says_so(self, capsys):
        r, _t = _renderer()
        await _show_tools(_tool_ctx(_ToolAgent(_TOOLS, _SERVERS)), r, "nichtsda")
        assert "No tool matches" in capsys.readouterr().out

    async def test_empty_allowlist_is_reported_as_deny_all(self, capsys):
        """tools.allowed empty means deny-all -- a silent empty list would look
        like a bug in the command instead of the config."""
        r, _t = _renderer()
        await _show_tools(_tool_ctx(_ToolAgent([], [])), r, "")
        assert "deny-all" in capsys.readouterr().out

    async def test_listing_failure_is_reported_not_swallowed(self, capsys):
        r, _t = _renderer()
        await _show_tools(_tool_ctx(_ToolAgent([], [], fail=True)), r, "")
        assert "Could not list tools" in capsys.readouterr().out

    async def test_agent_without_the_api_says_so(self, capsys):
        class _Plain:
            registry = None
            agent_config = None
        r, _t = _renderer()
        await _show_tools(_tool_ctx(_Plain()), r, "")
        assert "cannot report its tools" in capsys.readouterr().out


class TestSkillsCommand:
    def test_separates_always_from_on_demand(self):
        agent = _ToolAgent([], [], skills=_Skills(always=["amiga-coding"],
                                                  on_demand=["m68k-assembly"]))
        r, out = _renderer(width=200)
        _show_skills(_tool_ctx(agent), r)
        text = out.getvalue()
        assert "amiga-coding" in text and "m68k-assembly" in text
        assert "always" in text and "on demand" in text

    def test_dict_shaped_skills_config_also_works(self):
        agent = _ToolAgent([], [], skills={"always": ["a"], "on_demand": []})
        r, out = _renderer(width=200)
        _show_skills(_tool_ctx(agent), r)
        assert "a" in out.getvalue()

    def test_agent_without_skills(self, capsys):
        r, _t = _renderer()
        _show_skills(_tool_ctx(_ToolAgent([], [])), r)
        assert "no skills" in capsys.readouterr().out


class TestIndentPreservation:
    """Nested output (/tools groups, /last key blocks) relies on indentation.
    Splitting on " " dropped it -- and only in ANSI mode, so a piped test run
    looked fine while the real terminal lost every indent."""

    def test_ansi_keeps_leading_spaces(self):
        r, out = _renderer(width=200)
        r.println("  eingerueckt")
        plain = out.getvalue().replace("\x1b[0m", "")
        assert plain.startswith("  eingerueckt")

    def test_both_modes_agree_on_the_indent(self):
        ansi_r, ansi_out = _renderer(ansi=True, width=200)
        plain_r, plain_out = _renderer(ansi=False, width=200)
        ansi_r.println("    vier spaces")
        plain_r.println("    vier spaces")
        assert ansi_out.getvalue().replace("\x1b[0m", "") == plain_out.getvalue()

    def test_wrapped_continuation_keeps_the_indent(self):
        r, out = _renderer(width=30)
        r.println("  " + "wort " * 12)
        lines = [ln for ln in out.getvalue().split("\n") if ln]
        assert len(lines) > 1
        assert all(ln.startswith("  ") for ln in lines)

    def test_indented_lines_still_respect_the_width(self):
        r, out = _renderer(width=30)
        r.println("    " + "x " * 30)
        for ln in out.getvalue().split("\n")[:-1]:
            assert display_width(ln) <= 29
        assert r._total == out.getvalue().count("\n")

    def test_whitespace_only_line_is_kept(self):
        r, out = _renderer(width=200)
        r.println("   ")
        assert r._total == 1


class TestKeyReaderBuffer:
    """Key handling is pure string work -- the platform source is mocked."""

    def _reader(self, keys):
        r = _KeyReader(active=False)
        r.enabled = True
        r._read_chars = lambda: keys        # type: ignore[method-assign]
        return r

    def test_enter_submits_and_clears(self):
        r = self._reader("hallo\r")
        assert r.poll() == ["hallo"]
        assert r.buffer == ""

    def test_every_line_of_a_paste_survives(self):
        """_read_chars drains the whole batch at once; a single result slot
        silently dropped all but the last line of a multi-line paste."""
        r = self._reader("erste\rzweite\rdritte\r")
        assert r.poll() == ["erste", "zweite", "dritte"]

    def test_arrow_keys_do_not_leak_their_escape_body(self):
        """Only the ESC byte was skipped before, so "[A" landed in the text."""
        r = self._reader("ab\x1b[Acd\x1b[3~ef")
        r.poll()
        assert r.buffer == "abcdef"

    def test_ctrl_c_as_data_discards_instead_of_typing(self):
        """A child shell can leave the console without ENABLE_PROCESSED_INPUT,
        and then Ctrl-C arrives as 0x03 rather than as a signal."""
        r = self._reader("halber satz\x03")
        assert r.poll() == []
        assert r.buffer == ""

    def test_partial_line_stays_in_the_buffer(self):
        r = self._reader("halb")
        assert r.poll() == []
        assert r.buffer == "halb"

    def test_backspace_deletes(self):
        r = self._reader("abc\bd")
        assert r.poll() == []
        assert r.buffer == "abd"

    def test_ctrl_u_clears_the_line(self):
        r = self._reader("weg damit\x15neu")
        r.poll()
        assert r.buffer == "neu"

    def test_control_characters_never_enter_the_buffer(self):
        r = self._reader("a\x01b\x1bc")
        r.poll()
        assert r.buffer == "abc"

    def test_empty_line_submits_nothing(self):
        r = self._reader("   \r")
        assert r.poll() == []         # whitespace-only is not a message

    def test_inactive_reader_reads_nothing(self):
        r = _KeyReader(active=False)
        assert r.enabled is False
        assert r.poll() == []

    def test_platform_failure_disables_instead_of_raising(self, monkeypatch):
        """Type-ahead is a convenience; a broken console must not take the turn
        down. This exercises the REAL _read_chars, not a stubbed one."""
        import os as _os

        def _boom(*_args, **_kwargs):
            raise OSError("console weg")

        r = _KeyReader(active=False)
        r.enabled = True
        if _os.name == "nt":
            import msvcrt
            monkeypatch.setattr(msvcrt, "kbhit", _boom)
        else:
            import select as _select
            monkeypatch.setattr(_select, "select", _boom)

        assert r.poll() == []
        assert r.enabled is False       # and it stops trying


class TestInputRow:
    """The input line sits on the cursor's RESTING line -- it carries no
    newline, so it must not appear in the region's line accounting."""

    def test_input_row_is_not_counted_as_a_region_line(self):
        r, out = _renderer()
        r.handle_status(_ev(request_id="a", message="läuft"))
        before = r._total
        r.set_input_row("» tippt gerade")
        assert r._total == before
        assert r._lines == {"a": 0}

    def test_updates_still_reach_their_own_line_with_input_showing(self):
        """The whole point: type-ahead must not break the offset arithmetic."""
        r, out = _renderer()
        r.handle_status(_ev(request_id="a", message="step 1"))
        r.set_input_row("» hallo")
        r.handle_status(_ev(request_id="a", message="step 2"))
        text = out.getvalue()
        assert "\x1b[1A" in text        # still climbed exactly one line
        assert text.count("\n") == 1    # no extra region line appeared

    def test_writes_erase_and_redraw_the_input_row(self):
        r, out = _renderer()
        r.set_input_row("» abc")
        start = len(out.getvalue())
        r.handle_status(_ev(message="etwas"))
        written = out.getvalue()[start:]
        assert written.startswith("\r\x1b[K")   # erased first
        assert written.rstrip().endswith("\x1b[0m")
        assert "» abc" in written               # and redrawn after

    def test_input_row_never_ends_with_a_newline(self):
        r, out = _renderer()
        r.set_input_row("» abc")
        assert not out.getvalue().endswith("\n")

    def test_close_removes_the_input_row(self):
        r, out = _renderer()
        r.set_input_row("» abc")
        r.close()
        assert r._input_row is None
        assert out.getvalue().endswith("\r\x1b[K")

    def test_long_input_is_capped_to_one_physical_line(self):
        """A soft-wrapped input line would add a physical row the region does
        not know about and shift every offset above it."""
        r, out = _renderer(width=30)
        r.set_input_row("» " + "x" * 200)
        drawn = out.getvalue()
        assert "\n" not in drawn
        plain = drawn.replace("\x1b[36m", "").replace("\x1b[0m", "").replace("\r\x1b[K", "")
        assert display_width(plain) <= 29

    def test_non_ansi_mode_has_no_input_row(self):
        r, out = _renderer(ansi=False)
        r.set_input_row("» abc")
        assert out.getvalue() == ""
        assert r._input_row is None


class _ScriptedReader:
    """Feeds a fixed sequence of poll() results, then idles."""

    def __init__(self, script):
        self.buffer = ""
        self.enabled = True
        self._script = list(script)

    def poll(self):
        if self._script:
            item = self._script.pop(0)
            if isinstance(item, tuple):
                self.buffer = item[1]
                return [item[0]]
            self.buffer = item or ""
            return []
        return []

    def close(self):
        self.enabled = False


class _InjectAgent:
    def __init__(self, accept=True):
        self.accept = accept
        self.injected = []

    async def append_user_message(self, request_id, content):
        self.injected.append((request_id, content))
        return self.accept


def _inject_ctx(agent):
    from agent_system.cli_utils.chat import _ChatContext
    return _ChatContext(
        agent=agent, entry_name="a", session_service=None, session_user="u",
        session_id="s", was_new_session=False, llm_profile="p",
        llm_override=None, llm_profile_info=None, show_status=True,
    )


class TestTypeAheadPoller:
    """A line typed mid-turn goes to the agent via append_user_message, which
    the agent drains at its next step boundary -- the same contract the WebUI
    has. It does not interrupt the running step."""

    async def test_submitted_line_is_injected_into_the_running_turn(self):
        agent = _InjectAgent()
        reader = _ScriptedReader([("mach lieber X", "")])
        r, out = _renderer(width=200)
        state = {"request_id": "req-1"}
        task = asyncio.create_task(
            _poll_typed_input(reader, r, _inject_ctx(agent), state))
        await asyncio.sleep(0.15)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

        assert agent.injected == [("req-1", "mach lieber X")]
        assert "mach lieber X" in out.getvalue()
        assert "queued" in out.getvalue()

    async def test_line_without_a_running_request_is_kept_for_next_turn(self):
        """No request_id yet (or already finished): the typed text must not be
        thrown away."""
        agent = _InjectAgent()
        reader = _ScriptedReader([("spaeter dann", "")])
        r, out = _renderer(width=200)
        state = {}
        task = asyncio.create_task(
            _poll_typed_input(reader, r, _inject_ctx(agent), state))
        await asyncio.sleep(0.15)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

        assert agent.injected == []
        assert state.get("typed_queue") == ["spaeter dann"]
        assert "kept for the next turn" in out.getvalue()

    async def test_two_undelivered_lines_are_both_kept_in_order(self):
        """The old typed_ahead slot only carried the FIRST line -- the second
        submitted line was confirmed to the user and then silently lost."""
        agent = _InjectAgent()
        reader = _ScriptedReader([("erste zeile", ""), ("zweite zeile", "")])
        r, _out = _renderer(width=200)
        state = {}
        task = asyncio.create_task(
            _poll_typed_input(reader, r, _inject_ctx(agent), state))
        await asyncio.sleep(0.25)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

        assert agent.injected == []
        assert state.get("typed_queue") == ["erste zeile", "zweite zeile"]

    async def test_rejected_injection_is_also_kept(self):
        agent = _InjectAgent(accept=False)
        reader = _ScriptedReader([("abgelehnt", "")])
        r, _t = _renderer(width=200)
        state = {"request_id": "req-9"}
        task = asyncio.create_task(
            _poll_typed_input(reader, r, _inject_ctx(agent), state))
        await asyncio.sleep(0.15)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

        assert agent.injected == [("req-9", "abgelehnt")]
        assert state.get("typed_queue") == ["abgelehnt"]

    async def test_partial_buffer_is_shown_on_the_input_row(self):
        agent = _InjectAgent()
        reader = _ScriptedReader(["hal", "halb", "halbe"])
        r, out = _renderer(width=200)
        task = asyncio.create_task(
            _poll_typed_input(reader, r, _inject_ctx(agent), {"request_id": "r"}))
        await asyncio.sleep(0.25)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

        text = out.getvalue()
        assert "» halbe" in text
        assert agent.injected == []          # nothing submitted yet
        assert text.count("\n") == 0         # the input row never adds a line

    async def test_agent_failure_does_not_kill_the_poller(self):
        class _Boom:
            async def append_user_message(self, request_id, content):
                raise RuntimeError("agent weg")

        reader = _ScriptedReader([("text", ""), ("noch einer", "")])
        r, _t = _renderer(width=200)
        state = {"request_id": "r"}
        task = asyncio.create_task(
            _poll_typed_input(reader, r, _inject_ctx(_Boom()), state))
        await asyncio.sleep(0.2)
        done = task.done()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

        assert not done, "the poller must survive an injection failure"
        assert state.get("typed_queue")     # and keep the text


class TestTurnUsageNotDoubleCounted:
    """The final event repeats the LAST call's usage (server.py sets
    final_event["usage"] = llm_out["usage"]). Summing both counted that call
    twice -- with 20 steps that silently inflated the reported cost."""

    async def test_single_call_is_counted_once(self):
        usage = {"prompt_tokens": 1000, "completion_tokens": 10}
        agent = _FakeAgent([
            {"type": "start", "request_id": "r", "session_id": "s"},
            {"type": "thinking_complete",
             "assistant": {"content": "fertig", "tool_calls": None},
             "usage": usage},
            {"type": "final", "summary": "fertig", "usage": usage},
            {"type": "end"},
        ])
        r, _t = _renderer()
        result = await run_chat_turn(agent, "x", "s", r)
        assert result["usage"]["prompt_tokens"] == 1000     # not 2000

    async def test_multi_step_sums_every_call_exactly_once(self):
        step1 = {"prompt_tokens": 1000, "completion_tokens": 10}
        step2 = {"prompt_tokens": 1500, "completion_tokens": 20}
        agent = _FakeAgent([
            {"type": "start", "request_id": "r", "session_id": "s"},
            {"type": "thinking_complete",
             "assistant": {"content": "zwischenstand", "tool_calls": [{"id": "t"}]},
             "usage": step1},
            {"type": "thinking_complete",
             "assistant": {"content": "fertig", "tool_calls": None},
             "usage": step2},
            {"type": "final", "summary": "fertig", "usage": step2},
            {"type": "end"},
        ])
        r, _t = _renderer()
        result = await run_chat_turn(agent, "x", "s", r)
        assert result["usage"]["prompt_tokens"] == 2500     # not 4000

    async def test_separate_final_answer_call_still_counts(self):
        """After max_steps a SEPARATE final-answer call runs which has no
        thinking_complete of its own -- dropping it would undercount."""
        step = {"prompt_tokens": 1000, "completion_tokens": 10}
        final_call = {"prompt_tokens": 1200, "completion_tokens": 30}
        agent = _FakeAgent([
            {"type": "start", "request_id": "r", "session_id": "s"},
            {"type": "thinking_complete",
             "assistant": {"content": "a", "tool_calls": [{"id": "t"}]},
             "usage": step},
            {"type": "final", "summary": "notgedrungen", "usage": final_call},
            {"type": "end"},
        ])
        r, _t = _renderer()
        result = await run_chat_turn(agent, "x", "s", r)
        assert result["usage"]["prompt_tokens"] == 2200

    async def test_cached_tokens_survive_the_turn(self):
        agent = _FakeAgent([
            {"type": "start", "request_id": "r", "session_id": "s"},
            {"type": "thinking_complete",
             "assistant": {"content": "fertig", "tool_calls": None},
             "usage": {"prompt_tokens": 2000, "completion_tokens": 5,
                       "prompt_tokens_details": {"cached_tokens": 1900}}},
            {"type": "end"},
        ])
        r, _t = _renderer()
        result = await run_chat_turn(agent, "x", "s", r)
        assert result["usage"]["cached_tokens"] == 1900


class _StatsTracker:
    def __init__(self, stats):
        self._stats = stats
        self.asked_for = None

    def get_statistics(self, session_id=None):
        self.asked_for = session_id
        return self._stats


class _CostAgent:
    def __init__(self, tracker=None, registry_raises=False):
        class _Reg:
            def get(_self, name):
                if registry_raises or tracker is None:
                    raise KeyError(name)
                return type("S", (), {"tracker": tracker})()
        self.registry = _Reg()


class TestCostsCommand:
    """The turn footer only sums the coordinator's own event stream. Sub-agents
    bill against the same wallet through their own sub-sessions, so /costs has
    to read the tracker -- the only place that sees every call in the process."""

    def _ctx(self, agent):
        from agent_system.cli_utils.chat import _ChatContext
        return _ChatContext(
            agent=agent, entry_name="a", session_service=None, session_user="u",
            session_id="sess-1", was_new_session=False, llm_profile="p",
            llm_override=None, llm_profile_info=None, show_status=True)

    def test_asks_the_tracker_for_this_session(self):
        tracker = _StatsTracker({"totals": {"cost": 1.0, "prompt_tokens": 10,
                                            "completion_tokens": 2,
                                            "cost_known_calls": 1,
                                            "cost_estimated_calls": 0,
                                            "cache_hit_rate": 0.0},
                                 "timespan": {"sample_count": 1}})
        r, out = _renderer(width=200)
        _show_costs(self._ctx(_CostAgent(tracker)), r)
        assert tracker.asked_for == "sess-1"   # the tree filter needs the id
        assert "$1.0000" in out.getvalue()

    def test_estimated_totals_are_marked(self, capsys):
        tracker = _StatsTracker({"totals": {"cost": 0.5, "prompt_tokens": 100,
                                            "completion_tokens": 10,
                                            "cost_known_calls": 2,
                                            "cost_estimated_calls": 2,
                                            "cache_hit_rate": 50.0},
                                 "timespan": {"sample_count": 2}})
        r, out = _renderer(width=200)
        _show_costs(self._ctx(_CostAgent(tracker)), r)
        assert "~$0.5000" in out.getvalue()
        assert "not provider billing" in capsys.readouterr().out

    def test_unpriced_calls_are_named(self):
        """3 calls seen, only 1 priced -- the total is incomplete and says so."""
        tracker = _StatsTracker({"totals": {"cost": 0.1, "prompt_tokens": 10,
                                            "completion_tokens": 1,
                                            "cost_known_calls": 1,
                                            "cost_estimated_calls": 0,
                                            "cache_hit_rate": 0.0},
                                 "timespan": {"sample_count": 3}})
        r, out = _renderer(width=200)
        _show_costs(self._ctx(_CostAgent(tracker)), r)
        assert "(2 unpriced)" in out.getvalue()

    def test_missing_plugin_says_so_and_falls_back(self, capsys):
        r, _t = _renderer(width=200)
        ctx = self._ctx(_CostAgent(None))
        ctx.total_usage = {"prompt_tokens": 5, "completion_tokens": 1}
        _show_costs(ctx, r)
        printed = capsys.readouterr().out
        assert "not active" in printed
        assert "This chat's own turns" in printed

    def test_tracker_failure_is_reported(self, capsys):
        class _Boom:
            def get_statistics(self, session_id=None):
                raise RuntimeError("tracker kaputt")
        r, _t = _renderer(width=200)
        _show_costs(self._ctx(_CostAgent(_Boom())), r)
        assert "Could not read usage statistics" in capsys.readouterr().out

    def test_empty_session_says_so(self, capsys):
        r, _t = _renderer(width=200)
        _show_costs(self._ctx(_CostAgent(_StatsTracker({}))), r)
        assert "No LLM calls recorded" in capsys.readouterr().out


class _AsyncOnlyServer:
    """A hybrid plugin: async list_tools() and deliberately NO get_tools().

    This is what every sub_agent_manager instance looks like -- asking only for
    get_tools() skipped them entirely.
    """

    def __init__(self, tools):
        self._tools = tools

    async def list_tools(self):
        return self._tools


class _MCPToolLike:
    def __init__(self, name, description="", input_schema=None):
        self.name = name
        self.description = description
        # The real MCPTool always carries one; the schema builder reads it
        # unconditionally, so a fake without it vanishes from the result.
        self.input_schema = input_schema or {"type": "object", "properties": {}}


class TestToolGrouping:
    def _ctx_with_servers(self, tools, servers):
        class _Reg:
            def list(_s):
                return list(servers)
        agent = _ToolAgent(tools, servers)
        agent.registry = _Reg()
        return _tool_ctx(agent)

    async def test_single_tool_server_groups_under_itself(self):
        """A server whose one tool carries its bare name (sequential_thinking,
        todo) used to be split at "_" and invented a bogus group."""
        tools = [{"name": "sequential_thinking", "description": "d"},
                 {"name": "todo", "description": "d"}]
        r, out = _renderer(width=200)
        await _show_tools(self._ctx_with_servers(tools, ["sequential_thinking", "todo"]), r, "")
        plain = out.getvalue().replace("\x1b[34m", "").replace("\x1b[90m", "").replace("\x1b[0m", "")
        headers = [ln for ln in plain.split("\n") if ln and not ln.startswith(" ")]
        assert "sequential_thinking" in headers
        assert "todo" in headers
        assert "sequential" not in headers          # the invented group
        assert "(unknown server)" not in headers

    async def test_unmatched_tool_is_named_not_guessed(self):
        tools = [{"name": "voellig_fremd_tool", "description": "d"}]
        r, out = _renderer(width=200)
        await _show_tools(self._ctx_with_servers(tools, ["andere"]), r, "")
        assert "(unknown server)" in out.getvalue()
