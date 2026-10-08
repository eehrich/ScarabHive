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
import os
import subprocess
import sys
import threading
import time
from types import SimpleNamespace

import pytest

from agent_system.cli_utils.chat import (
    _ASCII_SYMBOLS,
    _KeyReader,
    _PromptEditor,
    _WAKE_POLL_S,
    WAKE_TASK,
    _WokenAtThePrompt,
    _build_prompt_editor,
    _compose_in_editor,
    _copy_last_answer,
    _copy_to_clipboard,
    _editor_command,
    _editor_needs_a_terminal,
    _take_wake_mark,
    _watch_for_wake,
    _history_seed,
    _poll_typed_input,
    ChatRenderer,
    _accumulate_usage,
    _call_pricing_key,
    _format_usage,
    _read_input,
    _handle_vars,
    _show_history,
    _show_costs,
    _show_skills,
    _show_tools,
    _switch_model,
    _show_last,
    _restore_logging,
    _silence_stdout_logging,
    display_width,
    run_chat_turn,
    suggest_command,
)
from agent_system.chat_commands import parse_chat_command
from agent_system.tools.status import StatusEvent, StatusPhase, status_bus


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

    async def run_events(self, task, request_id=None, session_id=None, llm_override=None,
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
        """A final whose usage no thinking_complete reported is a call of its
        own (the max-steps call was one before it became a regular step) —
        that one holds the real fill."""
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
    async def run_events(self, task, request_id=None, session_id=None, llm_override=None,
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


@pytest.fixture
def pt_prompt(tmp_path, monkeypatch):
    """A real prompt_toolkit prompt, driven by keystrokes over a pipe.

    The readers are the production ones (_build_line_readers), only the
    terminal underneath is swapped -- create_app_session redirects whatever
    is built inside it to the pipe and a DummyOutput.
    """
    pipe_input = pytest.importorskip("prompt_toolkit.input").create_pipe_input
    from prompt_toolkit.application import create_app_session
    from prompt_toolkit.output import DummyOutput

    monkeypatch.chdir(tmp_path)
    with pipe_input() as pipe:
        with create_app_session(input=pipe, output=DummyOutput()):
            yield pipe


def _press_when_ready(session_holder, pipe, text, timeout=5.0):
    """Send keys once the prompt is up AND its history has been loaded.

    prompt_toolkit loads history in a background task, so keys sent in the
    same breath as the prompt call arrive before there is anything to recall
    -- a race in the harness, not in the product (a person needs tenths of a
    second to reach the arrow key). Gating on the loaded working lines makes
    the test deterministic instead of sleep-dependent.
    """
    session = session_holder["session"]

    def wait_for(predicate, deadline):
        while time.monotonic() < deadline:
            if predicate():
                return True
            time.sleep(0.01)
        return False

    def run():
        deadline = time.monotonic() + timeout
        # BOTH conditions, in this order. Waiting only for the loaded history
        # was wrong and flaky (3 of 5 runs): the previous prompt leaves its
        # accepted line in the working lines, so that test is already true
        # before the next prompt starts -- the key then sat in the pipe and
        # was read before there was anything to recall.
        running = wait_for(lambda: session.app.is_running, deadline)
        # `_working_lines` is private, but it is the only place that says the
        # async history load has arrived. It gates timing; the assertion is
        # on the returned string.
        loaded = running and wait_for(
            lambda: len(session.default_buffer._working_lines) > 1, deadline)
        # A silent timeout would still send the key and then blame the
        # product for a harness problem.
        session_holder["gate"] = "open" if loaded else "timeout"
        pipe.send_text(text)

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return thread


class TestPromptHistory:
    """Arrow-up at the prompt recalls what was typed before, like any shell.

    stdlib readline cannot provide this on Windows (importing it activates
    pyreadline3 and breaks input() outright), which is why the prompt runs on
    prompt_toolkit.
    """

    def _recall(self, pt_prompt, editor, keys="\x1b[A\n"):
        """Press arrow-up once the prompt is up, and return what came back."""
        holder = {"session": editor._session}
        _press_when_ready(holder, pt_prompt, keys)
        recalled = editor.read("> ")
        assert holder.get("gate") == "open", "history never loaded -- harness"
        return recalled

    def test_arrow_up_recalls_the_previous_line(self, pt_prompt):
        editor = _build_prompt_editor([])
        assert editor is not None, "no prompt_toolkit editor -- test is vacuous"

        pt_prompt.send_text("erste frage\n")
        assert editor.read("> ") == "erste frage"

        assert self._recall(pt_prompt, editor) == "erste frage"

    def test_a_resumed_session_starts_with_its_own_messages(self, pt_prompt):
        # What _history_seed hands over on resume. Two presses on purpose:
        # reaching PAST the freshly typed line to the seeded one is what
        # proves both share the session's history. One press would also pass
        # against the private history prompt_toolkit builds for itself.
        editor = _build_prompt_editor(["frage von gestern"])
        pt_prompt.send_text("frage von heute\n")
        assert editor.read("> ") == "frage von heute"

        assert self._recall(pt_prompt, editor, "\x1b[A\x1b[A\n") == "frage von gestern"

    def test_reseeding_drops_the_previous_session_entries(self, pt_prompt):
        # What /new and /resume do: the session changes underneath the prompt,
        # and the history has to change with it. Keeping the old entries would
        # offer the abandoned conversation above the new transcript.
        editor = _build_prompt_editor(["aus session A"])
        editor.reseed(["aus session B"])

        assert self._recall(pt_prompt, editor) == "aus session B"
        assert self._recall(pt_prompt, editor, "\x1b[A\x1b[A\n") == "aus session B", \
            "session A's entry was still reachable"

    def test_reseeding_keeps_the_commands_typed_in_this_process(self, pt_prompt):
        # Commands never enter a session, so a reseed built from session
        # messages alone wiped them -- the /resume just typed included.
        editor = _build_prompt_editor(["aus session A"])
        # Shaped like a plugin command, sent as a message: it is session A's.
        pt_prompt.send_text("/todo:milch kaufen\n")
        assert editor.read("> ") == "/todo:milch kaufen"
        pt_prompt.send_text("/resume s2\n")
        assert editor.read("> ") == "/resume s2"
        # What the REPL does for a line it ran as a command.
        editor.remember_command("/resume s2")

        editor.reseed(["aus session B"])

        assert self._recall(pt_prompt, editor) == "/resume s2"
        # Two presses: one past the command lands on B only if nothing of
        # session A sits in between.
        assert self._recall(pt_prompt, editor, "\x1b[A\x1b[A\n") == "aus session B", \
            "a message of session A survived the reseed"

    def test_a_turn_that_bypassed_the_prompt_is_still_recallable(self, pt_prompt):
        # An initial_task or a line typed ahead during a turn never passes
        # session.prompt(), so nothing would add it on its own.
        editor = _build_prompt_editor([])
        editor.remember("aus der warteschlange")

        assert self._recall(pt_prompt, editor) == "aus der warteschlange"

    def test_pasted_continuation_lines_stay_out_of_the_history(self, pt_prompt):
        editor = _build_prompt_editor([])
        pt_prompt.send_text('"""\nzeile a\nzeile b"""\n')

        text = _read_input("> ", read_line=editor.read,
                           read_cont=editor.read_continuation)
        assert text == "zeile a\nzeile b", "fixture did not exercise the fence"

        # Only the line typed AT the prompt is history -- the fence opener.
        # Reading the continuation through the same session would make the
        # pasted lines the newest entries instead.
        assert self._recall(pt_prompt, editor) == '"""'


class _RecordingEditor:
    """Stand-in for _PromptEditor that records what the REPL asks of it."""

    def __init__(self, lines):
        self._lines = iter(lines)
        self.seeds = []
        self.remembered = []
        self.commands = []

    def read(self, prompt):
        try:
            return next(self._lines)
        except StopIteration:
            raise EOFError

    def read_continuation(self, prompt):
        return self.read(prompt)

    def reseed(self, seed):
        self.seeds.append(list(seed))

    def remember(self, text):
        self.remembered.append(text)

    def remember_command(self, text):
        self.commands.append(text)


def drive_chat_repl(monkeypatch, lines, initial_task=None, turn_probe=None,
                    editor=None, resume=None, skills=(), plugin_commands=(),
                    was_new_session=False, **run_kwargs):
    """Run the real run_chat_loop over *lines* against fakes, return the editor.

    Module level so that anything touching the REPL's own dispatch can use it,
    not only the history tests -- a command wired into the loop and tested
    only through its handler is a command nobody has ever seen dispatched.
    """
    import agent_system.cli_utils.chat as chat

    editor = editor or _RecordingEditor(lines)
    # Session-aware on purpose: a tracker that answers the same for every
    # id makes every seed [] , and then "reseed was called" passes even if
    # it is called BEFORE the session id changes -- which is the bug.
    messages = {"s1": [_user_message("frage aus s1")],
                "s2": [_user_message("frage aus s2")]}
    tracker = SimpleNamespace(
        get_session_messages=lambda sid: messages.get(sid, []),
        # It really WRITES. setdefault kept the old list for a known id, so a
        # cut driven through the REPL did nothing and passed anyway.
        set_session_messages=lambda sid, msgs: messages.__setitem__(sid, list(msgs)),
        get_session_template_vars=lambda sid: {},
        set_session_template_vars=lambda sid, values: None,
        set_session_metadata=lambda sid, meta: None,
    )
    agent = SimpleNamespace(_session_tracker=tracker, agent_config=None,
                            llm=SimpleNamespace(model="m"))

    def _editor(seed, suggest=None, loop=None):
        # What the REPL would have handed prompt_toolkit: the tests read
        # it off the editor instead of driving a console.
        editor.suggest = suggest
        return editor

    monkeypatch.setattr(chat, "_build_prompt_editor", _editor)
    monkeypatch.setattr(chat, "collect_plugin_commands",
                        lambda agent_: list(plugin_commands))
    monkeypatch.setattr(chat, "_available_skills", lambda ctx: list(skills))
    monkeypatch.setattr(
        chat, "_execute_turn",
        turn_probe or (lambda loop, ctx, task, renderer, editor=None: {}))
    async def _saved(ctx):
        return True

    monkeypatch.setattr(chat, "_save_session", _saved)
    monkeypatch.setattr(chat.sys.stdin, "isatty", lambda: True, raising=False)
    monkeypatch.setattr(chat.sys.stdout, "isatty", lambda: True, raising=False)

    async def _resumed(ctx, session_id):
        ctx.session_id = session_id
        return True

    monkeypatch.setattr(chat, "_resume_session", resume or _resumed)

    loop = asyncio.new_event_loop()
    try:
        chat.run_chat_loop(
            agent=agent, entry_name="a", session_service=None,
            session_user="u", session_id="s1", was_new_session=was_new_session,
            llm_profile="p", show_status=False, loop=loop,
            initial_task=initial_task, **run_kwargs)
    finally:
        loop.close()
    return editor


def _user_message(text):
    return SimpleNamespace(role="user", content=text)


class TestReplKeepsTheHistoryOnTheLiveSession:
    """The editor is built once; the session under it is not.

    Without this wiring the prompt keeps offering the session that was open at
    process start, while the transcript on screen belongs to another one --
    and docs/cli_reference.md promises the opposite.
    """

    def _drive(self, monkeypatch, lines, **kwargs):
        return drive_chat_repl(monkeypatch, lines, **kwargs)

    def test_resume_points_the_history_at_the_new_session(self, monkeypatch):
        editor = self._drive(monkeypatch, ["/resume s2"])
        # The CONTENT is the assertion: reseeding with s1's messages, or
        # reseeding before the session id moves, both leave the prompt on the
        # abandoned conversation while the transcript shows the new one.
        assert editor.seeds == [["frage aus s2"]]

    def test_new_points_the_history_at_the_fresh_session(self, monkeypatch):
        editor = self._drive(monkeypatch, ["/new"])
        assert editor.seeds == [[]], "the abandoned session's messages survived"

    def test_an_initial_task_becomes_a_history_entry(self, monkeypatch):
        # It is a turn like any other, but it never passes the prompt.
        editor = self._drive(monkeypatch, [], initial_task="aus der kommandozeile")
        assert editor.remembered == ["aus der kommandozeile"]

    def test_the_turn_still_finds_a_current_event_loop(self, monkeypatch):
        # prompt_toolkit runs its own asyncio.run() per prompt, which leaves
        # the thread WITHOUT a current event loop (measured). Nothing in the
        # REPL notices -- it always names its loop -- but a library called
        # during the turn may call asyncio.get_event_loop() and used to find
        # one. The editor here reproduces that by clearing it.
        class _ClearingEditor(_RecordingEditor):
            def read(self, prompt):
                text = super().read(prompt)
                asyncio.set_event_loop(None)
                return text

        seen = []

        def _probe(loop, ctx, task, renderer, editor=None):
            try:
                seen.append(asyncio.get_event_loop() is loop)
            except RuntimeError:
                seen.append(False)
            return {}

        self._drive(monkeypatch, ["eine frage"], turn_probe=_probe,
                    editor=_ClearingEditor(["eine frage"]))
        assert seen == [True], "the turn ran without a current event loop"

    def test_a_redirected_stdout_keeps_the_plain_reader(self, monkeypatch):
        # `agent-cli chat > log.txt`: stdin is still a terminal, so a gate on
        # stdin alone would build the editor -- which puts the tty in raw mode
        # with echo off and then draws into the file. The terminal goes silent.
        import agent_system.cli_utils.chat as chat

        built = []
        monkeypatch.setattr(chat, "_build_prompt_editor",
                            lambda seed, **kw: built.append(seed))
        monkeypatch.setattr(chat, "collect_plugin_commands", lambda agent_: [])
        monkeypatch.setattr(chat.sys.stdin, "isatty", lambda: True, raising=False)
        monkeypatch.setattr(chat.sys.stdout, "isatty", lambda: False, raising=False)
        monkeypatch.setattr(builtins, "input", _feed([]))

        tracker = SimpleNamespace(get_session_messages=lambda sid: [])
        agent = SimpleNamespace(_session_tracker=tracker, agent_config=None,
                                llm=SimpleNamespace(model="m"))
        loop = asyncio.new_event_loop()
        try:
            chat.run_chat_loop(
                agent=agent, entry_name="a", session_service=None,
                session_user="u", session_id="s1", was_new_session=False,
                llm_profile="p", show_status=False, loop=loop)
        finally:
            loop.close()
        assert built == [], "built a full-screen editor into a redirected stdout"


def _pipe(data, encoding):
    """stdin as a pipe delivers it: buffered bytes under a text layer."""
    return io.TextIOWrapper(io.BufferedReader(io.BytesIO(data)),
                            encoding=encoding, errors="replace")


class TestPipedInputFromPowerShell:
    """Windows PowerShell 5.1 prefixes piped input with a UTF-8 BOM.

    Measured: ``"/compact" | agent-cli chat`` delivers
    ``b'\\xef\\xbb\\xbf/compact\\n'``. input() returned ``'\\ufeff/compact'``,
    strip() kept the mark, and the command went to the model as a message.
    """

    def test_a_piped_bom_does_not_turn_a_command_into_a_message(self, monkeypatch):
        import agent_system.cli_utils.chat as chat

        piped = _pipe("\ufeff/exit\n".encode("utf-8"), "utf-8")
        monkeypatch.setattr(chat.sys, "stdin", piped)
        monkeypatch.setattr(chat.sys.stdout, "isatty", lambda: False, raising=False)
        monkeypatch.setattr(chat, "collect_plugin_commands", lambda agent_: [])
        turns = []
        monkeypatch.setattr(chat, "_execute_turn",
                            lambda loop, ctx, task, renderer, editor=None: turns.append(task) or {})

        tracker = SimpleNamespace(get_session_messages=lambda sid: [])
        agent = SimpleNamespace(_session_tracker=tracker, agent_config=None,
                                llm=SimpleNamespace(model="m"))
        loop = asyncio.new_event_loop()
        try:
            chat.run_chat_loop(
                agent=agent, entry_name="a", session_service=None,
                session_user="u", session_id="s1", was_new_session=False,
                llm_profile="p", show_status=False, loop=loop)
        finally:
            loop.close()
        assert turns == [], f"/exit behind a BOM was sent to the model: {turns!r}"

    def test_only_the_leading_mark_goes(self, monkeypatch):
        import agent_system.cli_utils.chat as chat

        piped = _pipe("\ufeffa\ufeffb\n".encode("utf-8"), "utf-8")
        monkeypatch.setattr(chat.sys, "stdin", piped)
        chat._skip_piped_bom()
        assert chat.sys.stdin.readline() == "a\ufeffb\n"

    def test_the_mark_goes_without_utf8_mode_too(self, monkeypatch):
        """Without PYTHONUTF8 Windows decodes a pipe as cp1252: the mark read
        as "\u00ef\u00bb\u00bf/compact" and the command went to the model as a message."""
        import agent_system.cli_utils.chat as chat

        piped = _pipe("\ufeff/compact\n".encode("utf-8"), "cp1252")
        monkeypatch.setattr(chat.sys, "stdin", piped)
        chat._skip_piped_bom()
        assert chat.sys.stdin.readline() == "/compact\n"

    def test_a_pipe_without_the_mark_keeps_its_encoding(self, monkeypatch):
        """An ANSI file piped in without UTF-8 mode stays readable."""
        import agent_system.cli_utils.chat as chat

        piped = _pipe("K\xe4se\n".encode("cp1252"), "cp1252")
        monkeypatch.setattr(chat.sys, "stdin", piped)
        chat._skip_piped_bom()
        assert chat.sys.stdin.readline() == "K\xe4se\n"

    def test_a_stray_byte_does_not_end_the_chat(self, monkeypatch):
        """agent_cli sets errors="replace" on stdin; switching the encoding
        reset that to strict, and input() raised UnicodeDecodeError."""
        import agent_system.cli_utils.chat as chat

        piped = _pipe(b"\xef\xbb\xbfK\xe4se\n", "utf-8")
        monkeypatch.setattr(chat.sys, "stdin", piped)
        chat._skip_piped_bom()
        assert chat.sys.stdin.readline() == "K\ufffdse\n"


class TestCallPricingKey:
    """Which client the turn is priced with.

    --llm and /model hand the turn a different client while agent.llm stays
    the agent's own, so reading the agent alone quoted the price of the model
    that did not run -- cheap model, expensive bill, or the reverse.
    """

    def test_the_override_is_priced_not_the_agents_own_client(self):
        agent = SimpleNamespace(llm=SimpleNamespace(model="agent-model"))
        override = SimpleNamespace(model="switched-model")
        assert _call_pricing_key(agent, override)[0] == "switched-model"

    def test_without_an_override_the_agents_client_is_priced(self):
        agent = SimpleNamespace(llm=SimpleNamespace(model="agent-model"))
        assert _call_pricing_key(agent)[0] == "agent-model"

    def test_the_batch_flag_comes_from_the_same_client(self):
        agent = SimpleNamespace(llm=SimpleNamespace(model="a", batch_provider="x"))
        override = SimpleNamespace(model="b")
        assert _call_pricing_key(agent, override) == ("b", False)

    def test_a_batch_client_is_priced_by_who_answered(self):
        """Its sync fallback answered: full price, not the batch discount."""
        batch = SimpleNamespace(model="m", batch_provider="openai", last_was_batch=True)
        assert _call_pricing_key(SimpleNamespace(llm=batch)) == ("m", True)
        batch.last_was_batch = False
        assert _call_pricing_key(SimpleNamespace(llm=batch)) == ("m", False)


class TestSessionsCommand:
    """`/sessions [count]` -- the count has to survive the REPL's dispatch.

    Driven through the real loop: a handler that reads a payload the dispatch
    never passes it is a command nobody has ever seen used.
    """

    def _dispatch(self, monkeypatch, line, **run_kwargs):
        import agent_system.cli_utils.chat as chat

        seen = {}

        async def fake_print(manager, user_id, **kwargs):
            seen.update(kwargs)
            seen["user_id"] = user_id

        monkeypatch.setattr(chat, "print_sessions", fake_print)
        drive_chat_repl(monkeypatch, [line], **run_kwargs)
        return seen

    def test_a_bare_call_uses_the_default_count(self, monkeypatch):
        from agent_system.cli_utils.session_listing import DEFAULT_LIMIT
        assert self._dispatch(monkeypatch, "/sessions")["limit"] == DEFAULT_LIMIT

    def test_a_typed_count_reaches_the_listing(self, monkeypatch):
        assert self._dispatch(monkeypatch, "/sessions 5")["limit"] == 5

    def test_zero_means_all(self, monkeypatch):
        assert self._dispatch(monkeypatch, "/sessions 0")["limit"] == 0

    def test_the_running_session_is_handed_over_as_the_marked_one(self, monkeypatch):
        seen = self._dispatch(monkeypatch, "/sessions")
        assert seen["current_session_id"] == "s1"
        assert seen["user_id"] == "u"

    def test_the_footer_and_the_hint_reach_the_listing(self, monkeypatch):
        seen = self._dispatch(monkeypatch, "/sessions")
        assert seen["footer"] == "Use /resume <id or title> to continue one."
        assert "/sessions 0 for no limit" in seen["more_hint"]

    def test_only_the_agents_meant_for_chat_are_listed(self, monkeypatch):
        """The filter is the runtime this chat's process bootstrapped -- handed
        in, not Runtime.last_started, which an in-process pipeline can swap --
        asked per session whether its agent is meant for chat (ui/both). The
        agent this chat runs on ("a" here) stays, whatever its visibility."""
        from types import SimpleNamespace as NS

        decls = {"coder": NS(visibility="ui"), "v4_scorer": NS(visibility="private"),
                 "a": NS(visibility="private")}
        seen = self._dispatch(monkeypatch, "/sessions", runtime=NS(describe=decls.get))
        assert seen["shown"]("coder") and not seen["shown"]("v4_scorer")
        assert seen["shown"]("a"), "the conversation being had was hidden from its own listing"
        assert seen["everything_hint"] == "/sessions all"

    def test_all_lists_the_pipeline_runs_too(self, monkeypatch):
        # A runtime to filter with: without one there is no filter, and "all
        # turned it off" could not be told from "there was none".
        from types import SimpleNamespace as NS

        runtime = NS(describe={"a": NS(visibility="ui")}.get)
        seen = self._dispatch(monkeypatch, "/sessions all", runtime=runtime)
        assert seen["shown"] is None and seen["limit"] == 0

    def test_a_count_that_is_not_a_count_gets_the_usage_line(self, monkeypatch,
                                                             capsys):
        # /history next door does exactly this. Listing the default instead
        # looks identical to a honoured count -- the header says "(20 of N)"
        # either way.
        seen = self._dispatch(monkeypatch, "/sessions 2o")
        assert not seen, "the listing ran with a discarded argument"
        assert "Usage: /sessions [count|all]   (got: 2o)" in capsys.readouterr().out

    def test_a_negative_count_is_not_read_as_all_of_them(self, monkeypatch):
        seen = self._dispatch(monkeypatch, "/sessions -1")
        assert not seen, "-1 listed something instead of asking what was meant"

    def test_the_index_is_walked_once_and_kept(self, monkeypatch):
        """The listing stats every session for children. /sessions asked for
        it, then asked again to fill the completion -- two walks for one
        command. What print_sessions read is what the completion gets."""
        walks = []

        async def list_root_sessions(user_id):
            walks.append(user_id)
            return [{"session_id": "ab12cd34", "title": "die davor",
                     "agent_name": "a"}]

        seen = []
        drive_chat_repl(
            monkeypatch, ["/sessions", "frage"],
            session_manager=SimpleNamespace(list_root_sessions=list_root_sessions),
            turn_probe=lambda loop, ctx, task, renderer, editor=None:
            seen.append(list(ctx.recent_sessions)) or {})

        assert walks == ["u"], f"the index was walked {len(walks)} times"
        # ...and what it read is what /resume and its Tab get to see.
        assert seen and [e["session_id"] for e in seen[0]] == ["ab12cd34"]


class TestSwitchModel:
    """/model changes the LLM of the running chat.

    The switch has to reach three places: the next turn (ctx.llm_override),
    the banner and cost lines (ctx.llm_profile / llm_profile_info), and the
    session record -- which is what agent_cli reads back when the session is
    continued later.
    """

    def _ctx(self, current="profile_a", llm_params=None):
        """The REAL context object, built the way run_chat_loop builds it.

        A hand-made double had to grow every attribute production grew --
        which is how `--llm-params` reached this test while the constructor
        that has to carry them was never run once.
        """
        from agent_system.cli_utils.chat import _ChatContext

        profiles = {
            "profile_a": SimpleNamespace(description="the cheap one"),
            "profile_b": SimpleNamespace(description="the good one"),
        }
        tracker = SimpleNamespace(metadata={})
        tracker.set_session_metadata = lambda sid, meta: tracker.metadata.update(
            {sid: meta})
        agent = SimpleNamespace(
            system_config=SimpleNamespace(
                llm_system=SimpleNamespace(profiles=profiles)),
            _session_tracker=tracker,
            llm=SimpleNamespace(model="m"))
        ctx = _ChatContext(
            agent=agent, entry_name="a", session_service=None, session_user="u",
            session_id="s1", was_new_session=False, llm_profile=current,
            llm_override=None, llm_profile_info=None, show_status=False,
            llm_params=llm_params)
        return ctx, tracker

    def test_the_agents_own_params_survive_the_switch(self, monkeypatch):
        """/model picks another model, not another agent: what the agent says
        about every model it runs on ("*") has to reach the new client too,
        and what was typed still wins over it."""
        self._patch_factory(monkeypatch)
        ctx, _ = self._ctx(llm_params={"max_tokens": 16384})
        ctx.agent.agent_config = SimpleNamespace(
            llm_params={"*": {"context_window": 200000, "max_tokens": 8000}})

        _switch_model(ctx, "profile_b")

        assert ctx.llm_override.params == {"context_window": 200000, "max_tokens": 16384},             ctx.llm_override.params

    def _patch_factory(self, monkeypatch, raises=None):
        import agent_system.llm.factory as factory

        def _create(config, llm_profile, llm_params=None):
            if raises:
                raise raises
            return SimpleNamespace(model="model-of-" + llm_profile,
                                   params=llm_params)

        monkeypatch.setattr(factory, "create_llm_from_profile", _create)
        monkeypatch.setattr(
            factory, "resolve_llm_config_for_agent",
            lambda config, agent_config: SimpleNamespace(
                spec=SimpleNamespace(provider="prov", model="model-x")))

    def test_a_bare_call_lists_the_profiles_and_marks_the_current_one(self, capsys):
        ctx, _ = self._ctx()
        # False: a listing changed nothing, so nothing is written.
        assert _switch_model(ctx, "") is False
        out = capsys.readouterr().out
        assert "profile_a" in out and "profile_b" in out
        assert "the good one" in out, "descriptions were dropped"
        current_line = [ln for ln in out.splitlines() if "profile_a" in ln and "*" in ln]
        assert current_line, out

    def test_switching_reaches_the_turn_the_banner_and_the_session(
            self, monkeypatch, capsys):
        self._patch_factory(monkeypatch)
        ctx, tracker = self._ctx()

        _switch_model(ctx, "profile_b")

        assert ctx.llm_profile == "profile_b"
        assert getattr(ctx.llm_override, "model", None) == "model-of-profile_b"
        assert ctx.llm_profile_info == "profile_b:prov/model-x"
        assert tracker.metadata["s1"]["llm_profile"] == "profile_b"
        assert "profile_b" in capsys.readouterr().out

    def test_the_llm_params_of_the_chat_go_with_the_switch(self, monkeypatch, capsys):
        """`--llm-params thinking_level=max` was dropped on /model: the new
        client was built from the bare profile."""
        self._patch_factory(monkeypatch)
        # Through the constructor, the way the CLI hands them over.
        ctx, _ = self._ctx(llm_params={"thinking_level": "max"})

        assert _switch_model(ctx, "profile_b") is True

        assert ctx.llm_override.params == {"thinking_level": "max"}
        # The params are NAMED in the banner; how they are spelled is a
        # rendering choice and not worth a red test.
        assert "thinking_level=max" in ctx.llm_profile_info

    def test_the_switch_is_written_to_the_session_at_once(self, monkeypatch):
        """Only the next turn's save wrote it: /model, then /exit, and
        `--session <id>` started on the old profile."""
        import agent_system.cli_utils.chat as chat

        saved = []
        monkeypatch.setattr(chat, "_switch_model", lambda ctx, payload: True)
        monkeypatch.setattr(chat, "_save_now",
                            lambda loop, ctx: saved.append(ctx.session_id))
        drive_chat_repl(monkeypatch, ["/model profile_b"])

        assert saved == ["s1"]

    def test_a_session_that_has_no_record_yet_waits_for_its_first_save(
            self, monkeypatch):
        """was_new_session: nothing has been written for this session. Saving
        HERE would file an empty chat as a session of its own, and the choice
        reaches disk with the first turn anyway."""
        import agent_system.cli_utils.chat as chat

        saved = []
        monkeypatch.setattr(chat, "_switch_model", lambda ctx, payload: True)
        monkeypatch.setattr(chat, "_save_now",
                            lambda loop, ctx: saved.append(ctx.session_id))
        drive_chat_repl(monkeypatch, ["/model profile_b"], was_new_session=True)

        assert saved == []

    def test_a_listing_writes_nothing(self, monkeypatch):
        import agent_system.cli_utils.chat as chat

        saved = []
        monkeypatch.setattr(chat, "_switch_model", lambda ctx, payload: False)
        monkeypatch.setattr(chat, "_save_now",
                            lambda loop, ctx: saved.append(ctx.session_id))
        drive_chat_repl(monkeypatch, ["/model"])

        assert saved == []

    def test_an_unknown_profile_changes_nothing(self, monkeypatch, capsys):
        self._patch_factory(monkeypatch)
        ctx, tracker = self._ctx()

        _switch_model(ctx, "profile_x")

        assert ctx.llm_profile == "profile_a"
        assert ctx.llm_override is None
        assert tracker.metadata == {}
        assert "Unknown LLM profile" in capsys.readouterr().out

    def test_a_typo_gets_a_suggestion(self, monkeypatch, capsys):
        self._patch_factory(monkeypatch)
        ctx, _ = self._ctx()
        # NOT a prefix of a real profile: "profile_bb" would echo back inside
        # the "Unknown LLM profile: ..." line and the assertion would hold
        # with the suggestion deleted.
        _switch_model(ctx, "profle_b")
        assert "Did you mean profile_b?" in capsys.readouterr().out

    def test_the_repl_dispatches_the_command(self, monkeypatch):
        """The handler is tested above; this is the wiring into the loop.

        Measured with coverage: the dispatch branch was executed by no test at
        all, so /model could have been unreachable and every test above would
        still have been green."""
        import agent_system.cli_utils.chat as chat

        seen = []
        monkeypatch.setattr(chat, "_switch_model",
                            lambda ctx, payload: seen.append(payload))
        drive_chat_repl(monkeypatch, ["/model profile_b", "/llm"])

        assert seen == ["profile_b", ""], "the alias or the branch is missing"

    def test_a_failing_switch_keeps_the_old_client(self, monkeypatch, capsys):
        """Building the new client is the part that can fail (a bad key, an
        unreachable endpoint). Losing the working one over it would end the
        chat for a typo."""
        self._patch_factory(monkeypatch, raises=RuntimeError("no api key"))
        ctx, tracker = self._ctx()
        ctx.llm_override = SimpleNamespace(model="the-old-one")

        _switch_model(ctx, "profile_b")

        assert ctx.llm_profile == "profile_a"
        assert ctx.llm_override.model == "the-old-one"
        assert tracker.metadata == {}
        assert "no api key" in capsys.readouterr().out


class TestThinkCommand:
    """/think sets the thinking level of the chat: into ctx.llm_params, which every /model takes along,
    onto the client of the profile the chat runs on, and into the session's metadata -- what its saves
    write and a resume reads back."""

    _ctx = TestSwitchModel._ctx
    _patch_factory = TestSwitchModel._patch_factory

    def _own(self, ctx, own="profile_a"):
        ctx.agent.agent_config = SimpleNamespace(default_llm_profile=own, llm_params=None)
        return ctx

    def test_a_level_reaches_the_client_and_the_session(self, monkeypatch, capsys):
        from agent_system.cli_utils.chat import _set_thinking

        self._patch_factory(monkeypatch)
        ctx, tracker = self._ctx()
        self._own(ctx)

        assert _set_thinking(ctx, "HIGH") is True

        assert ctx.llm_params == {"thinking_level": "high"}
        assert ctx.llm_override.params == {"thinking_level": "high"}
        assert ctx.llm_profile == "profile_a"
        assert tracker.metadata["s1"]["llm_choice"] == {"profile": None, "params": {"thinking_level": "high"}}, (
            "params on the agent's own profile were recorded as a picked profile")

    def test_default_on_the_agents_own_profile_gives_the_agent_its_own_client_back(self, monkeypatch):
        from agent_system.cli_utils.chat import _set_thinking

        self._patch_factory(monkeypatch)
        ctx, tracker = self._ctx(llm_params={"thinking_level": "max"})
        self._own(ctx)
        ctx.llm_override = SimpleNamespace(model="the override")

        assert _set_thinking(ctx, "default") is True

        assert ctx.llm_params == {}
        assert ctx.llm_override is None, "an override without a reason stayed"
        assert tracker.metadata["s1"]["llm_choice"] == {"profile": None, "params": {}}

    def test_default_on_a_picked_profile_keeps_the_profile(self, monkeypatch):
        from agent_system.cli_utils.chat import _set_thinking

        self._patch_factory(monkeypatch)
        ctx, _ = self._ctx(current="profile_b", llm_params={"thinking_level": "max"})
        self._own(ctx)

        assert _set_thinking(ctx, "default") is True

        assert ctx.llm_profile == "profile_b"
        assert ctx.llm_override.model == "model-of-profile_b"
        assert not ctx.llm_override.params

    def test_a_picked_profile_is_recorded_as_the_choice(self, monkeypatch):
        from agent_system.cli_utils.chat import _switch_model

        self._patch_factory(monkeypatch)
        ctx, tracker = self._ctx()
        self._own(ctx)

        assert _switch_model(ctx, "profile_b") is True

        assert tracker.metadata["s1"]["llm_choice"]["profile"] == "profile_b"

    def test_an_unknown_level_changes_nothing(self, monkeypatch, capsys):
        from agent_system.cli_utils.chat import _set_thinking

        self._patch_factory(monkeypatch)
        ctx, tracker = self._ctx()
        self._own(ctx)

        assert _set_thinking(ctx, "ultra") is False

        assert ctx.llm_params == {} and ctx.llm_override is None and tracker.metadata == {}
        assert "Unknown thinking level: ultra" in capsys.readouterr().out


class TestTheChatSavesItsParams:
    """The chat's own save writes its llm_params beside the profile: the record a resume reads back."""

    async def test_the_save_carries_them(self, monkeypatch):
        import agent_system.cli_utils.chat as chat

        seen = {}

        class _Service:
            async def save_session(self, **kwargs):
                seen.update(kwargs)
                return True

        ctx = _completion_ctx(monkeypatch, session_service=_Service(),
                              llm_params={"thinking_level": "high"})

        assert await chat._save_session(ctx) is True
        assert seen["llm_choice"] == {"profile": None, "params": {"thinking_level": "high"}}


class TestPromptEditorWiring:
    """What the editor hands to the prompt sessions it builds."""

    def test_the_continuation_prompt_shares_the_key_bindings(self):
        # Ctrl-Z means end-of-input on Windows, and the "... " prompt is
        # exactly where a person reaches for it -- to get out of a fence they
        # opened by accident. Bindings on the first line only would leave them
        # inserting a literal \x1a there instead.
        built = []

        class _Session:
            def __init__(self, **kwargs):
                built.append(kwargs)

        class _History:
            def append_string(self, text):
                pass

        _PromptEditor(_Session, _History, ["alt"], key_bindings="BINDINGS")

        assert len(built) == 2, "expected a main and a continuation session"
        assert [k.get("key_bindings") for k in built] == ["BINDINGS", "BINDINGS"]


class TestRecalledMultilineInput:
    """A recalled message arrives whole; it must not be re-parsed as typing."""

    def test_a_recalled_fenced_message_is_not_read_again(self):
        # The editor hands back the WHOLE message on one arrow-up. Running it
        # through the fence rules would strip the leading `"""` and wait at
        # the continuation prompt for an end that is already in the string --
        # which looks like a hang. `_feed` supplies no further lines, so a
        # second read would raise EOFError or truncate.
        recalled = '"""\nmove.w d0,d1\nrts\n"""'
        assert _read_input("> ", read_line=lambda _: recalled) == recalled

    def test_a_recalled_message_ending_in_a_backslash_is_not_continued(self):
        recalled = "erste zeile\nzweite endet auf \\"
        assert _read_input("> ", read_line=lambda _: recalled) == recalled


class TestHistorySeed:
    """The prompt history is the session's user messages, not a second store."""

    class _Message:
        def __init__(self, role, content):
            self.role = role
            self.content = content

    def _ctx(self, messages):
        tracker = type("T", (), {"get_session_messages": lambda self, sid: messages})()
        agent = type("A", (), {"_session_tracker": tracker})()
        return type("C", (), {"agent": agent, "session_id": "s1"})()

    def test_only_user_messages_in_order(self):
        ctx = self._ctx([
            self._Message("user", "erste"),
            self._Message("assistant", "antwort"),
            self._Message("tool", "ergebnis"),
            self._Message("user", "zweite"),
        ])
        assert _history_seed(ctx) == ["erste", "zweite"]

    def test_blank_and_repeated_messages_drop_out(self):
        ctx = self._ctx([
            self._Message("user", "   "),
            self._Message("user", "gleich"),
            self._Message("user", "gleich"),
            self._Message("user", "anders"),
        ])
        assert _history_seed(ctx) == ["gleich", "anders"]

    def test_multimodal_content_becomes_its_text(self):
        ctx = self._ctx([
            self._Message("user", [{"type": "text", "text": "beschreibe das"},
                                    {"type": "image_url"}]),
        ])
        assert _history_seed(ctx) == ["beschreibe das [image_url]"]

    def test_an_expanded_skill_body_is_too_long_to_recall(self):
        # A /skill invocation stores the EXPANDED skill body as the user
        # message. Recalled, it would paste a whole SKILL.md over the prompt.
        ctx = self._ctx([
            self._Message("user", "kurz genug"),
            self._Message("user", "x" * 5000),
        ])
        assert _history_seed(ctx) == ["kurz genug"]

    def test_an_escaped_command_comes_back_escaped(self):
        # "//compact" reaches the session as "/compact". Handed to the prompt
        # raw, Enter would RUN the command instead of re-sending the message.
        ctx = self._ctx([self._Message("user", "/compact")])
        assert _history_seed(ctx) == ["//compact"]

    def test_a_path_that_only_looks_like_a_command_is_untouched(self):
        ctx = self._ctx([self._Message("user", "/etc/nginx/nginx.conf lesen")])
        assert _history_seed(ctx) == ["/etc/nginx/nginx.conf lesen"]

    def test_a_qualified_plugin_command_comes_back_escaped(self):
        ctx = self._ctx([self._Message("user", "/context_engineer:compact")])
        assert _history_seed(ctx) == ["//context_engineer:compact"]

    @pytest.mark.parametrize("text", [
        "/3d drucker bauen",   # head is not command-word shaped: a message
        "/2fa aktivieren",
        "/",                   # nothing to escape at all
        "/-x",
    ])
    def test_a_head_that_is_not_a_command_word_keeps_its_slash(self, text):
        # Escaping these would be worse than not escaping them: the "//"
        # unescape does not fire for such a head, so the agent would receive
        # the message one slash longer than it was sent.
        ctx = self._ctx([self._Message("user", text)])
        assert _history_seed(ctx) == [text]


def _session_of(reader):
    """The PromptSession a reader closure holds (for the timing gate only)."""
    return reader.__closure__[0].cell_contents


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

    def set_session_messages(self, session_id, messages):
        self._messages = list(messages)


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
        RETURNED is invisible -- this is chat's --show-tools."""
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


class TestHistoryShowsWhatTheAgentWasSent:
    """Every stored user message went to the agent. One that opens with a
    command word was sent escaped -- "//help me read this" is stored as
    "/help me read this" -- and hiding it left its answer in /history with no
    question above it, and made /last start a turn early. Measured before the
    filter went: 2 command-shaped leftovers in 38,251 local sessions."""

    def test_an_escaped_message_is_shown_with_its_answer(self):
        # The count is what binds: a limit wide enough for the whole list
        # starts at 0 whatever the filter says, so the "one exchange" is the
        # escaped one only while it COUNTS as one.
        messages = [
            _Msg("user", "echte frage"),
            _Msg("assistant", "echte antwort"),
            _Msg("user", "/help me read this"),
            _Msg("assistant", "antwort auf die escapte frage"),
        ]
        r, out = _renderer()
        _show_history(_ctx_with(messages), r, "1")
        text = out.getvalue()
        assert "/help me read this" in text
        assert "antwort auf die escapte frage" in text
        assert "echte frage" not in text, "the escaped turn was not counted"

    def test_last_starts_at_it(self):
        call = {"id": "c1", "function": {"name": "tool_before", "arguments": "{}"}}
        messages = [
            _Msg("user", "frage eins"),
            _Msg("assistant", "", tool_calls=[call]),
            _Msg("user", "/help me read this"),
            _Msg("assistant", "antwort"),
        ]
        r, out = _renderer(width=200)
        _show_last(_ctx_with(messages), r)
        assert "tool_before" not in out.getvalue(), \
            "/last mixed the previous turn's tool calls into this one"

    def test_paths_are_shown_too(self):
        messages = [_Msg("user", "/etc/nginx/nginx.conf pruefen"),
                    _Msg("assistant", "ok")]
        r, out = _renderer()
        _show_history(_ctx_with(messages), r, "1")
        assert "/etc/nginx/nginx.conf" in out.getvalue()

    def test_an_empty_message_is_no_exchange(self, capsys):
        r, _t = _renderer()
        _show_history(_ctx_with([_Msg("user", "   ")]), r, "5")
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
        agent=agent, entry_name="coder", session_service=None,
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
        agent = _ToolAgent([], [], skills=_Skills(always=["adversarial-review"],
                                                  on_demand=["codebase-design"]))
        r, out = _renderer(width=200)
        _show_skills(_tool_ctx(agent), r)
        text = out.getvalue()
        assert "adversarial-review" in text and "codebase-design" in text
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

    def test_a_paste_is_one_message_with_every_line(self):
        """_read_chars drains the whole batch at once. A single result slot
        dropped all but the last line; one message per line sent a pasted
        stack trace as a burst of paid fragments."""
        r = self._reader("erste\rzweite\rdritte\r")
        assert r.poll() == ["erste\nzweite\ndritte"]

    def test_a_pasted_block_keeps_its_shape(self):
        """Indentation, tabs and blank lines inside a paste are content; a
        Windows CRLF ends one line, not two."""
        r = self._reader("def f():\r\n    return 1\r\n\r\n\tx = 2\r\n")
        assert r.poll() == ["def f():\n    return 1\n\n\tx = 2"]

    def test_a_split_multibyte_character_survives_two_reads(self, monkeypatch):
        """os.read() hands out bytes; a paste over 1 KB splits a character
        between two reads, and decoding each read alone made it U+FFFD."""
        import agent_system.cli_utils.chat as chat
        import select as _select

        text = "Grüße aus Köln\n".encode("utf-8")
        cut = text.index("ü".encode("utf-8")) + 1       # inside the ü
        chunks = [text[:cut], text[cut:]]
        monkeypatch.setattr(chat.sys, "stdin",
                            SimpleNamespace(encoding="utf-8", fileno=lambda: 99))
        monkeypatch.setattr(chat.os, "name", "posix")
        monkeypatch.setattr(_select, "select",
                            lambda r, w, x, t: (r if chunks else [], [], []))
        monkeypatch.setattr(chat.os, "read", lambda fd, n: chunks.pop(0))

        r = _KeyReader(active=False)
        r.enabled = True
        assert r.poll() == ["Grüße aus Köln"]

    def test_an_emoji_from_the_windows_console_is_one_character(self, monkeypatch):
        """getwch() returns UTF-16 code units: an emoji comes as two lone
        surrogates, which the message sanitizer drops."""
        import agent_system.cli_utils.chat as chat
        msvcrt = pytest.importorskip("msvcrt")

        # Built from the code units: a literal would be one code point.
        keys = list("ok ") + [chr(0xD83D), chr(0xDE00), "\r"]
        monkeypatch.setattr(chat.os, "name", "nt")
        monkeypatch.setattr(msvcrt, "kbhit", lambda: bool(keys))
        monkeypatch.setattr(msvcrt, "getwch", lambda: keys.pop(0))

        r = _KeyReader(active=False)
        r.enabled = True
        assert r.poll() == ["ok \U0001F600"]

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

    async def test_an_injected_line_reaches_the_input_history(self):
        """It became part of the conversation without passing the prompt, so
        nothing else would record it -- arrow-up would skip straight over the
        message the person most recently sent."""
        agent = _InjectAgent()
        reader = _ScriptedReader([("mach lieber X", "")])
        r, _ = _renderer(width=200)
        editor = _RecordingEditor([])
        state = {"request_id": "req-1", "editor": editor}
        task = asyncio.create_task(
            _poll_typed_input(reader, r, _inject_ctx(agent), state))
        await asyncio.sleep(0.15)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

        assert agent.injected, "fixture delivered nothing -- test is vacuous"
        assert editor.remembered == ["mach lieber X"]

    async def test_a_line_that_was_not_delivered_is_not_remembered(self):
        # No request to take it: it goes back to the prompt queue, and the
        # prompt path records it. Recording it here as well would double it.
        agent = _InjectAgent()
        reader = _ScriptedReader([("spaeter dann", "")])
        r, _ = _renderer(width=200)
        editor = _RecordingEditor([])
        state = {"editor": editor}
        task = asyncio.create_task(
            _poll_typed_input(reader, r, _inject_ctx(agent), state))
        await asyncio.sleep(0.15)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

        assert state.get("typed_queue") == ["spaeter dann"], "fixture did not queue"
        assert editor.remembered == []

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
        """A final whose usage no thinking_complete reported is a SEPARATE call
        (the max-steps call was one before it became a regular step) --
        dropping it would undercount."""
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


class _ContextAgent:
    """An agent with a prompt, tools, a conversation and a usage snapshot."""

    def __init__(self, messages, *, latest=None, prompt="Du bist ein Agent. " * 40,
                 tools=None, raises=False, window=0):
        self._messages = messages
        self._prompt = prompt
        self.llm = SimpleNamespace(model="m", context_window=window)
        self._tools = tools if tools is not None else [
            {"type": "function", "function": {
                "name": "file_ops_read", "description": "read a file",
                "parameters": {"type": "object",
                               "properties": {"path": {"type": "string"}}}}}]
        self._raises = raises
        self.asked_for = []
        self._session_tracker = SimpleNamespace(
            get_session_messages=lambda sid: list(messages))

        class _Reg:
            def get(_self, name):
                if latest is None:
                    raise KeyError(name)
                return type("S", (), {"tracker": type("T", (), {
                    "get_latest": staticmethod(lambda session_id=None: dict(latest))})()})()

        self.registry = _Reg()

    async def describe_context_inputs(self, session_id=None):
        """One call for both, and it gets the SESSION id: the prompt this
        session sends is rendered with its template vars."""
        self.asked_for.append(session_id)
        if self._raises:
            raise RuntimeError("the template is gone")
        return self._prompt, list(self._tools)


class TestContextCommand:
    """"42k of 200k" says the window is filling. Only the split says WHAT is
    filling it, and that is the part the person can act on."""

    def _run(self, agent, capsys=None):
        """Everything the command put on screen.

        Both halves: the header and the notes go through print(), the rows
        through the renderer -- reading only one of them would miss a block.
        """
        import agent_system.cli_utils.chat as chat

        ctx = chat._ChatContext(
            agent=agent, entry_name="a", session_service=None, session_user="u",
            session_id="sess-1", was_new_session=False, llm_profile="p",
            llm_override=None, llm_profile_info=None, show_status=True)
        renderer, out = _renderer(width=200)
        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(chat._show_context(ctx, renderer))
        finally:
            loop.close()
        printed = capsys.readouterr().out if capsys is not None else ""
        return printed + out.getvalue()

    def _conversation(self):
        return [_Msg("user", "schreib die routine"),
                _Msg("assistant", "gleich"),
                _Msg("tool", "x" * 8000),
                _Msg("assistant", "fertig")]

    def test_the_biggest_part_comes_first(self, capsys):
        text = self._run(capsys=capsys, agent=_ContextAgent(self._conversation()))

        places = [text.index(label) for label in
                  ["tool results", "system prompt", "tool schemas", "your messages"]]
        assert places == sorted(places), f"not sorted by size:\n{text}"

    def test_the_measurement_and_the_estimate_are_both_there_and_apart(self, capsys):
        text = self._run(capsys=capsys, agent=_ContextAgent(
            self._conversation(),
            latest={"context_window": 200000, "prompt_tokens": 42100,
                    "cached_tokens": 31000}))

        assert "last call" in text
        assert "42,100" in text and "200,000" in text
        assert "31,000 of them cached" in text
        # The estimate keeps its own block and its own total.
        assert "estimated" in text and "together" in text

    def test_a_measurement_from_before_a_rewrite_is_flagged(self, capsys):
        """It describes a conversation that no longer exists."""
        text = self._run(capsys=capsys, agent=_ContextAgent(
            self._conversation(),
            latest={"context_window": 100, "prompt_tokens": 40, "is_stale": True}))

        assert "stale" in text

    def test_a_part_with_nothing_in_it_gets_no_line(self, capsys):
        """A fresh session would otherwise list three zeroes, and an agent
        without tools a row saying it has none."""
        text = self._run(capsys=capsys, agent=_ContextAgent(
            [_Msg("user", "die erste frage")], tools=[]))

        assert "your messages" in text
        assert "answers" not in text, text
        assert "tool results" not in text, text
        assert "tool schemas" not in text, text

    def test_without_a_tracker_the_estimate_still_answers(self, capsys):
        text = self._run(capsys=capsys, agent=_ContextAgent(self._conversation()))

        assert "last call" not in text
        assert "tool results" in text, "the split needs no tracker"

    def test_inputs_that_cannot_be_read_are_named(self, capsys):
        """Missing, and said so -- a split that silently leaves out the system
        prompt understates the window by thousands of tokens."""
        text = self._run(capsys=capsys,
                         agent=_ContextAgent(self._conversation(), raises=True))

        assert "could not be read" in text

    def test_the_prompt_is_rendered_for_THIS_session(self):
        """Without the session id the template vars are skipped, and the one
        line the command exists to show is short by the whole var payload."""
        agent = _ContextAgent(self._conversation())
        self._run(agent=agent)

        assert agent.asked_for == ["sess-1"]

    def test_the_estimate_is_held_against_the_window_of_the_NEXT_call(self, capsys):
        """The measurement carries the window IT ran on. A /model switch makes
        those two different numbers, and a share against the old one states a
        fill that is not true."""
        text = self._run(capsys=capsys, agent=_ContextAgent(
            self._conversation(), window=128000,
            latest={"context_window": 100, "prompt_tokens": 40}))

        assert "of 100" in text, text          # the measured line, on its own
        assert "of 128,000" in text, text      # the estimate, on the live one

    def test_the_repl_dispatches_it(self, monkeypatch):
        import agent_system.cli_utils.chat as chat

        seen = []

        async def fake(ctx, renderer):
            seen.append(ctx.session_id)

        monkeypatch.setattr(chat, "_show_context", fake)
        drive_chat_repl(monkeypatch, ["/context"])

        assert seen == ["s1"]

    def test_the_short_spelling_dispatches_too(self, monkeypatch):
        import agent_system.cli_utils.chat as chat

        seen = []

        async def fake(ctx, renderer):
            seen.append("ran")

        monkeypatch.setattr(chat, "_show_context", fake)
        drive_chat_repl(monkeypatch, ["/ctx"])

        assert seen == ["ran"]


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


class _ToolDefLike:
    def __init__(self, name, description="", input_schema=None):
        self.name = name
        self.description = description
        # The real ToolDef always carries one; the schema builder reads it
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


class TestVarsCommand:
    """``/vars`` in the terminal, against the REAL session tracker.

    A fake store would prove the handler talks to itself. The tracker's own
    behaviour is the point: ``set_session_template_vars`` MERGES, so removing
    a variable needs a clear first -- with a stub that merely records calls,
    the missing clear looks like a pass.
    """

    @staticmethod
    def _ctx_with_tracker():
        from agent_system.servers.agent.components.session_tracking import SessionTracker

        agent = _ToolAgent([], [])
        agent._session_tracker = SessionTracker()
        return _tool_ctx(agent), agent._session_tracker

    def test_it_lists_what_the_tracker_holds(self):
        ctx, tracker = self._ctx_with_tracker()
        tracker.set_session_template_vars("s", {"lang": "de"})
        r, out = _renderer(width=200)
        asyncio.run(_handle_vars(ctx, r, ""))
        assert "lang" in out.getvalue() and "de" in out.getvalue()

    def test_setting_reaches_the_tracker(self):
        """Where the next turn reads it -- server.py hands exactly this dict
        to the prompt strategy."""
        ctx, tracker = self._ctx_with_tracker()
        r, _out = _renderer(width=200)
        asyncio.run(_handle_vars(ctx, r, "lang=de book_id=7"))
        assert tracker.get_session_template_vars("s") == {"lang": "de", "book_id": "7"}

    def test_unset_really_removes_it(self):
        """The tracker only ever updates, so without a clear first the old
        value survives an unset -- invisibly, because the printed listing is
        computed and looks right either way."""
        ctx, tracker = self._ctx_with_tracker()
        r, _out = _renderer(width=200)
        asyncio.run(_handle_vars(ctx, r, "lang=de keep=yes"))
        asyncio.run(_handle_vars(ctx, r, "unset lang"))
        assert tracker.get_session_template_vars("s") == {"keep": "yes"}

    def test_clear_empties_the_tracker(self):
        ctx, tracker = self._ctx_with_tracker()
        r, _out = _renderer(width=200)
        asyncio.run(_handle_vars(ctx, r, "lang=de"))
        asyncio.run(_handle_vars(ctx, r, "clear"))
        assert tracker.get_session_template_vars("s") == {}

    def test_a_refused_line_leaves_the_values_alone(self):
        ctx, tracker = self._ctx_with_tracker()
        r, out = _renderer(width=200)
        asyncio.run(_handle_vars(ctx, r, "lang=de"))
        asyncio.run(_handle_vars(ctx, r, "8ball=x"))
        assert tracker.get_session_template_vars("s") == {"lang": "de"}
        assert "Nothing changed" in out.getvalue()

    def test_an_agent_without_a_tracker_says_so(self, capsys):
        r, _out = _renderer(width=200)
        asyncio.run(_handle_vars(_tool_ctx(_ToolAgent([], [])), r, "lang=de"))
        assert "no session variables" in capsys.readouterr().out.lower()

    def test_a_removal_reaches_the_session_file(self, tmp_path):
        """The review finding this whole round exists for.

        Everything else in the system only ADDS variables, so the
        runtime->disk sync in session_service merges. A merge cannot express a
        removal: the key stayed on disk and the next load merged it straight
        back into the tracker, so `/vars unset` came undone at the next
        message on the web and at the next `/resume` in the terminal.

        Real SessionManager, real file, read back from disk -- a recording
        stub would have been green for the whole broken version.
        """
        from agent_system.services.session_manager import SessionManager

        manager = SessionManager(storage_path=str(tmp_path))
        session = asyncio.run(manager.create_session(user_id="u", session_id="s"))
        assert session["session_id"] == "s", "fixture: session id not honoured"

        ctx, tracker = self._ctx_with_tracker()
        ctx.session_manager = manager
        r, _out = _renderer(width=200)
        asyncio.run(_handle_vars(ctx, r, "lang=de keep=yes"))
        asyncio.run(_handle_vars(ctx, r, "unset lang"))

        on_disk = asyncio.run(manager.load_session("u", "s"))["context_vars"]
        assert on_disk == {"keep": "yes"}, f"removal did not reach disk: {on_disk}"

    def test_clear_reaches_the_session_file_too(self, tmp_path):
        """The emptier case: the sync skips entirely when the runtime set is
        empty (`if runtime_vars:`), so a clear used to persist nothing at all
        and every variable came back on the next load."""
        from agent_system.services.session_manager import SessionManager

        manager = SessionManager(storage_path=str(tmp_path))
        asyncio.run(manager.create_session(user_id="u", session_id="s"))

        ctx, _tracker = self._ctx_with_tracker()
        ctx.session_manager = manager
        r, _out = _renderer(width=200)
        asyncio.run(_handle_vars(ctx, r, "lang=de"))
        asyncio.run(_handle_vars(ctx, r, "clear"))

        assert asyncio.run(manager.load_session("u", "s"))["context_vars"] == {}

    def test_a_session_without_a_file_yet_is_not_an_error(self, tmp_path):
        """A conversation whose first turn has not been saved has no file. The
        variables still have to take effect in memory; the next save_session
        writes them out."""
        from agent_system.services.session_manager import SessionManager

        ctx, tracker = self._ctx_with_tracker()
        ctx.session_manager = SessionManager(storage_path=str(tmp_path))
        r, out = _renderer(width=200)
        asyncio.run(_handle_vars(ctx, r, "lang=de"))

        assert tracker.get_session_template_vars("s") == {"lang": "de"}
        assert "warning" not in out.getvalue().lower()


class TestChatHoldsTheOpenSession:
    """Session presence (core/session_presence.py): chat keeps the conversation
    in memory between turns, so it holds the session it has open -- and lets go
    of one it leaves."""

    def test_it_follows_new_resume_and_the_exit(self, monkeypatch, tmp_path):
        import agent_system.cli_utils.chat as chat
        from agent_system.core.session_presence import SessionPresence

        store = SessionPresence(tmp_path)
        monkeypatch.setattr(chat, "presence_for", lambda config: store)
        # The caller hands the session over held (agent_cli holds it before it
        # is loaded); the REPL takes that hold with it from here.
        store.hold("s1", "u", "a")
        turns = []

        def probe(loop, ctx, task, renderer, editor=None):
            running = sorted(sid for sid in {"s1", "s2", ctx.session_id}
                             if (store.get(sid, "u") or {}).get("status") == "running")
            turns.append((ctx.session_id, running))
            return {}

        drive_chat_repl(monkeypatch, ["frage", "/new", "frage", "/resume s2", "frage"],
                        turn_probe=probe)

        (first, ran_first), (fresh, ran_fresh), (resumed, ran_resumed) = turns
        assert (first, ran_first) == ("s1", ["s1"])
        assert fresh not in ("s1", "s2") and ran_fresh == [fresh]
        assert (resumed, ran_resumed) == ("s2", ["s2"])
        assert store.list_for_user("u") == [], "chat still holds a session after it exited"

    def test_it_does_not_resume_a_session_another_process_runs(self, monkeypatch, tmp_path):
        import agent_system.cli_utils.chat as chat
        from agent_system.core.session_presence import SessionBusy, SessionPresence

        store = SessionPresence(tmp_path)
        monkeypatch.setattr(chat, "presence_for", lambda config: store)
        store.hold("s1", "u", "a")
        taken = store.hold

        def hold(session_id, user_id, agent_name):
            if session_id == "s2":
                raise SessionBusy("s2", "other_agent")
            return taken(session_id, user_id, agent_name)

        monkeypatch.setattr(store, "hold", hold)
        turns = []

        def probe(loop, ctx, task, renderer, editor=None):
            turns.append((ctx.session_id, (store.get("s1", "u") or {}).get("status")))
            return {}

        drive_chat_repl(monkeypatch, ["/resume s2", "frage"], turn_probe=probe)

        # The load is what the hold comes before: a refused resume must leave
        # the chat where it is, with the session it has open still in hand.
        assert turns == [("s1", "running")]


# --------------------------------------------------------------------------
# Review round 16.09.2026: interrupts, the session a chat holds, what a turn
# costs, and what /resume brings along.
# --------------------------------------------------------------------------


def _interrupt_soon(loop):
    """A Ctrl-C the way it reaches run_until_complete: raised by the loop
    itself, while the work is still pending."""
    def _raise():
        raise KeyboardInterrupt
    loop.call_soon(_raise)


class TestCtrlCOutsideATurn:
    """One Ctrl-C during /sessions, /resume, /vars, /tools or the answer's
    formatting ended the chat with a traceback; the save left behind finished
    unseen during the next command."""

    def test_a_command_is_cancelled_and_the_chat_goes_on(self, monkeypatch, capsys):
        import agent_system.cli_utils.chat as chat

        finished = []
        cancelled = []

        async def slow_listing(ctx, payload=""):
            _interrupt_soon(asyncio.get_running_loop())
            try:
                await asyncio.sleep(0.05)
            except asyncio.CancelledError:
                # What the listing itself felt. "finished == []" alone would
                # also hold for a task merely left PENDING -- which is the bug:
                # nothing else in this run drives the loop long enough for the
                # sleep to fire, so the absence proves nothing by itself.
                cancelled.append(True)
                raise
            finished.append(True)

        turns = []
        monkeypatch.setattr(chat, "_list_sessions", slow_listing)
        drive_chat_repl(monkeypatch, ["/sessions", "danach"],
                        turn_probe=lambda loop, ctx, task, renderer, editor=None:
                        turns.append(task) or {})

        assert turns == ["danach"], "the chat ended with the Ctrl-C"
        assert cancelled == [True], "the listing was left pending, not cancelled"
        assert finished == [], "the cancelled listing still ran to its end"
        assert "(/sessions cancelled)" in capsys.readouterr().err

    def test_an_interrupted_resume_lets_go_of_the_session_it_took(self, monkeypatch):
        import agent_system.cli_utils.chat as chat

        async def slow_resume(ctx, session_id):
            _interrupt_soon(asyncio.get_running_loop())
            await asyncio.sleep(0.05)
            return True

        held, released = [], []
        monkeypatch.setattr(chat, "_hold_session",
                            lambda ctx, sid: held.append(sid) or True)
        monkeypatch.setattr(chat, "_release_session",
                            lambda ctx, sid: released.append(sid))
        drive_chat_repl(monkeypatch, ["/resume s2"], resume=slow_resume)

        assert held == ["s2"]
        assert released == ["s2", "s1"], "the hold taken for the load leaked"

    def test_a_ctrl_c_during_the_save_lets_it_finish(self, monkeypatch, capsys):
        import agent_system.cli_utils.chat as chat

        written = []

        async def slow_save(ctx):
            _interrupt_soon(asyncio.get_running_loop())
            await asyncio.sleep(0.05)
            written.append(ctx.session_id)
            return True

        monkeypatch.setattr(chat, "_save_session", slow_save)
        ctx = _inject_ctx(None)
        ctx.was_new_session = True
        loop = asyncio.new_event_loop()
        try:
            assert chat._save_now(loop, ctx) is True
        finally:
            loop.close()

        assert written == ["s"]
        assert ctx.last_saved == "s" and ctx.was_new_session is False
        assert "finishing the save" in capsys.readouterr().err

class TestWhatACtrlCStops:
    def test_a_ctrl_c_that_lost_the_race_still_drops_the_queue(self, monkeypatch):
        """The agent was already finishing (its token gone), so no
        "cancelled" came back -- and the lines typed ahead ran as new turns."""
        turns = []

        def probe(loop, ctx, task, renderer, editor=None):
            turns.append(task)
            if task == "erste":
                return {"summary": "fertig", "interrupted": True,
                        "typed_queue": ["nachgeschoben"]}
            return {}

        drive_chat_repl(monkeypatch, [], initial_task="erste", turn_probe=probe)

        assert turns == ["erste"], "a Ctrl-C was followed by another paid turn"

    def test_a_cancelled_turn_is_saved_so_the_farewell_can_name_it(
            self, monkeypatch, capsys):
        """A cancelled turn skipped the save on "the next turn saves anyway".
        Stop a turn and then LEAVE and there is no next turn: the chat had
        recorded no save, and the farewell names the session only when one
        happened. Reported from real use -- "then I don't even know what the
        session id is".
        """
        def probe(loop, ctx, task, renderer, editor=None):
            return {"cancelled": True}

        drive_chat_repl(monkeypatch, [], initial_task="abgebrochen",
                        turn_probe=probe)

        err = capsys.readouterr().err
        assert "Turn cancelled." in err, "fixture: the turn was not cancelled"
        assert "Session saved:" in err
        assert "Resume with:" in err

    def test_a_cancelled_turn_still_counts_in_the_session_total(
            self, monkeypatch, capsys):
        def probe(loop, ctx, task, renderer, editor=None):
            return {"cancelled": True,
                    "usage": {"prompt_tokens": 1200, "completion_tokens": 30}}

        drive_chat_repl(monkeypatch, [], initial_task="teuer", turn_probe=probe)

        # The numbers, not the headline: a total that lost its prompt tokens
        # still prints "Session total" (1.2k -> _format_usage's short form).
        total = capsys.readouterr().out
        assert "Session total" in total
        assert "1.2k" in total and "30" in total

    def test_an_answer_that_won_the_race_is_marked_interrupted(self):
        loop = asyncio.new_event_loop()
        try:
            async def finished_turn():
                return {"summary": "fertig", "cancelled": False, "errors": []}

            turn = loop.create_task(finished_turn())
            result = chat_module()._cancel_turn(
                loop, _inject_ctx(None), turn, {}, ChatRenderer(ansi=False))
        finally:
            loop.close()

        assert result["summary"] == "fertig"
        assert result["cancelled"] is False
        assert result["interrupted"] is True

    def test_a_hard_cancel_leaves_no_turn_on_the_loop(self):
        """The turn's unwinding drains the status queue, unsubscribes and
        closes the renderer. Left pending, all of that happened inside the
        NEXT run_until_complete -- the save, or the following turn."""
        import agent_system.cli_utils.chat as chat

        loop = asyncio.new_event_loop()
        try:
            turn = loop.create_task(asyncio.sleep(10))
            # The forcing Ctrl-C, while the grace period is still running:
            # that is the path that leaves the task behind. A timeout would
            # not -- wait_for cancels and AWAITS the task itself.
            loop.call_soon(_raise_interrupt)
            chat._cancel_turn(loop, _inject_ctx(None), turn, {},
                              ChatRenderer(ansi=False))

            assert turn.done(), "the cancelled turn is still pending on the loop"
        finally:
            loop.close()

    def test_a_hard_cancel_keeps_what_the_turn_spent(self, monkeypatch):
        import agent_system.cli_utils.chat as chat

        monkeypatch.setattr(chat, "_CANCEL_GRACE_S", 0.01)
        loop = asyncio.new_event_loop()
        try:
            turn = loop.create_task(asyncio.sleep(10))
            state = {"usage": {"prompt_tokens": 500}}
            result = chat._cancel_turn(loop, _inject_ctx(None), turn, state,
                                       ChatRenderer(ansi=False))
        finally:
            loop.close()

        assert result["cancelled"] is True
        assert result["usage"] == {"prompt_tokens": 500}

    async def test_the_turn_hands_its_usage_to_the_state_as_it_goes(self):
        agent = _FakeAgent([
            {"type": "start", "request_id": "r", "session_id": "s"},
            {"type": "thinking_complete", "assistant": {"tool_calls": [{"id": "t"}]},
             "usage": {"prompt_tokens": 700, "completion_tokens": 7}},
            {"type": "end"},
        ])
        agent.llm = SimpleNamespace(model="m")
        state = {}
        r, _ = _renderer()
        result = await run_chat_turn(agent, "q", "s", r, state=state)

        assert state["usage"] is result["usage"]
        assert state["usage"]["prompt_tokens"] == 700


class TestTheChatLetsGoOfItsSession:
    def test_even_when_it_fails_before_the_first_prompt(self, monkeypatch):
        """run_chat_loop takes the hold over from the CLI, which no longer
        releases it -- a failure before the loop's try leaked it."""
        import agent_system.cli_utils.chat as chat

        released = []
        monkeypatch.setattr(chat, "_release_session",
                            lambda ctx, sid: released.append(sid))

        def broken(agent):
            raise RuntimeError("plugin commands broke")

        monkeypatch.setattr(chat, "collect_plugin_commands", broken)
        monkeypatch.setattr(chat.sys.stdin, "isatty", lambda: False, raising=False)
        loop = asyncio.new_event_loop()
        try:
            with pytest.raises(RuntimeError, match="plugin commands broke"):
                chat.run_chat_loop(
                    agent=SimpleNamespace(
                        _session_tracker=SimpleNamespace(get_session_messages=lambda sid: []),
                        agent_config=None, llm=SimpleNamespace(model="m")),
                    entry_name="a", session_service=None, session_user="u",
                    session_id="s1", was_new_session=False, llm_profile="p",
                    show_status=False, loop=loop)
        finally:
            loop.close()

        assert released == ["s1"]


class TestSessionTitle:
    def test_the_title_names_the_first_session_once(self, monkeypatch):
        import agent_system.cli_utils.chat as chat

        titles = []

        class _Service:
            async def save_session(self, **kwargs):
                titles.append(kwargs["title"])
                return True

        ctx = _inject_ctx(None)
        ctx.session_service = _Service()
        ctx.session_title = "Mein Titel"
        loop = asyncio.new_event_loop()
        try:
            chat._save_now(loop, ctx)
            chat._save_now(loop, ctx)
        finally:
            loop.close()

        assert titles == ["Mein Titel", None]

    def test_a_new_session_does_not_inherit_it(self, monkeypatch):
        def _run(lines):
            seen = []
            drive_chat_repl(
                monkeypatch, lines, session_title="Mein Titel",
                turn_probe=lambda loop, ctx, task, renderer, editor=None:
                seen.append(ctx.session_title) or {})
            return seen

        # Both runs, because "None" alone is also what a title that never
        # arrived looks like -- the difference is the measurement.
        assert _run(["frage"]) == ["Mein Titel"], "the title never arrived"
        assert _run(["/new", "frage"]) == [None], "/new kept the old title"


class TestWhatTheCommandLineHandsTheRepl:
    """--attach, --llm-params and --session-title end in the chat's context,
    and nothing drove that constructor: every test for the three set the
    attribute by hand afterwards, so dropping the assignment kept them green."""

    def test_all_three_reach_the_first_turn(self, monkeypatch):
        import agent_system.cli_utils.chat as chat

        seen = {}
        monkeypatch.setattr(
            chat, "_task_with_attachments",
            lambda ctx, task, renderer: seen.update(
                attachments=list(ctx.attachments)) or task)
        drive_chat_repl(
            monkeypatch, ["frage"], attachments=["bild.png"],
            llm_params={"thinking_level": "max"}, session_title="Mein Titel",
            turn_probe=lambda loop, ctx, task, renderer, editor=None: seen.update(
                params=dict(ctx.llm_params), title=ctx.session_title) or {})

        assert seen == {"attachments": ["bild.png"],
                        "params": {"thinking_level": "max"},
                        "title": "Mein Titel"}


class TestResumeBringsTheSessionAlong:
    """A session brings its agent and its LLM -- `--session <id>` honoured
    that, /resume loaded any session into the running agent and wrote this
    agent's name over its record on the next save."""

    def _ctx(self, monkeypatch, stored_agent, stored_llm, current="profile_a", stored_params=None,
             record_extra=None, own=None):
        import agent_system.llm.factory as factory
        from agent_system.cli_utils.chat import _ChatContext

        monkeypatch.setattr(
            factory, "create_llm_from_profile",
            lambda config, llm_profile, llm_params=None:
            SimpleNamespace(model="model-of-" + llm_profile, params=llm_params))
        monkeypatch.setattr(
            factory, "resolve_llm_config_for_agent",
            lambda config, agent_config: SimpleNamespace(
                spec=SimpleNamespace(provider="prov", model=agent_config.llm_profile)))

        config = SimpleNamespace(
            plugins=SimpleNamespace(servers={
                "coder": SimpleNamespace(agent_config=object()),
                "writer": SimpleNamespace(agent_config=object())}),
            llm_system=SimpleNamespace(profiles={"profile_a": None, "profile_b": None}))

        class _Manager:
            #: What a person types is an id or the TITLE of a session; the real
            #: one looks it up (SessionManager.resolve_session_ref). Here every
            #: id is its own, except the one name this fixture gave away.
            titles = {"Der Blitter": "s2"}
            #: Two older sessions carry the same title -- the real one names them.
            namesakes = {"Der Blitter": ["s0", "s1b"]}

            async def resolve_session_ref(self, user_id, ref, *, others=None):
                if others is not None:
                    others.extend(self.namesakes.get(ref, []))
                return self.titles.get(ref, ref)

            async def load_session(self, user_id, session_id):
                record = {"agent_name": stored_agent, "llm_profile": stored_llm}
                if stored_params is not None:
                    record["llm_params"] = stored_params
                record.update(record_extra or {})
                return record

        loads = []

        class _Service:
            async def load_and_restore_session(self, agent, user_id, session_id):
                loads.append(session_id)
                return True, 4

        tracker = SimpleNamespace(set_session_metadata=lambda sid, meta: None)
        ctx = _ChatContext(
            agent=SimpleNamespace(system_config=config, _session_tracker=tracker,
                                  llm=SimpleNamespace(model="m"),
                                  agent_config=SimpleNamespace(default_llm_profile=own)),
            entry_name="coder", session_service=_Service(), session_user="u",
            session_id="s1", was_new_session=False, llm_profile=current,
            llm_override=None, llm_profile_info=None, show_status=False,
            session_manager=_Manager())
        return ctx, loads

    async def test_a_session_of_another_agent_is_refused(self, monkeypatch, capsys):
        from agent_system.cli_utils.chat import _resume_session

        ctx, loads = self._ctx(monkeypatch, "writer", "profile_a")

        assert await _resume_session(ctx, "s2") is False

        assert loads == [], "it was loaded into the wrong agent"
        assert ctx.session_id == "s1"
        out = capsys.readouterr().out
        assert "belongs to writer" in out
        assert "--session s2 --agent writer" in out

    async def test_a_title_continues_the_session_it_belongs_to(self, monkeypatch, capsys):
        """Ids are machine-made and cannot be renamed, so /resume takes the
        name the person gave the session with /title -- turned into its id
        BEFORE the hold, or the presence lock names the words typed and the
        session itself stays open to a woken run."""
        import agent_system.cli_utils.chat as chat

        ctx, loads = self._ctx(monkeypatch, "coder", "profile_b")
        held, released = [], []
        monkeypatch.setattr(chat, "_hold_session", lambda ctx, sid: held.append(sid) or True)
        monkeypatch.setattr(chat, "_release_session", lambda ctx, sid: released.append(sid))

        assert await chat._resume_into(ctx, "Der Blitter", "s1") is True

        assert loads == ["s2"], "the title was taken for an id of its own"
        assert ctx.session_id == "s2"
        assert held == ["s2"], "the lock was taken on the title, not on the session"
        assert released == ["s1"]
        out = capsys.readouterr().out
        assert "Der Blitter" in out, "it did not say which session it took"
        # the newest of three was taken -- and it says so, instead of choosing unseen
        assert "newest of 3 with this title" in out, out

    async def test_it_continues_on_its_own_llm(self, monkeypatch):
        from agent_system.cli_utils.chat import _resume_session

        ctx, loads = self._ctx(monkeypatch, "coder", "profile_b")

        assert await _resume_session(ctx, "s2") is True

        assert loads == ["s2"]
        assert ctx.llm_profile == "profile_b"
        assert ctx.llm_override.model == "model-of-profile_b"

    async def test_it_continues_with_its_own_llm_params(self, monkeypatch):
        """A thinking level set in the session (/think, the web chat's button) is the session's, like
        its profile -- on the same profile it was never applied: only a profile change rebuilt."""
        from agent_system.cli_utils.chat import _resume_session

        ctx, _ = self._ctx(monkeypatch, "coder", "profile_a", stored_params={"thinking_level": "high"})

        assert await _resume_session(ctx, "s2") is True

        assert ctx.llm_params == {"thinking_level": "high"}
        assert ctx.llm_override.params == {"thinking_level": "high"}

    async def test_a_session_nobody_picked_a_profile_for_runs_on_the_agents_own(self, monkeypatch, capsys):
        """llm_profile_override null: the session ran on the agent's own. Staying on this chat's /model
        pick wrote it into that record as the session's choice on the next save."""
        from agent_system.cli_utils.chat import _llm_choice, _resume_session

        ctx, _ = self._ctx(monkeypatch, "coder", "profile_a", current="profile_b",
                           record_extra={"llm_profile_override": None}, own="profile_a")
        ctx.llm_override = SimpleNamespace(model="model-of-profile_b")

        assert await _resume_session(ctx, "s2") is True

        assert ctx.llm_profile == "profile_a"
        assert ctx.llm_override is None, "the agent's own client must answer, not a pick"
        assert _llm_choice(ctx) == {"profile": None, "params": {}}
        assert "the agent's own" in capsys.readouterr().out

    async def test_a_session_without_params_leaves_the_chat_its_own(self, monkeypatch):
        """`--llm-params` typed for this chat win over a session at startup; /resume dropped them for
        a session that has none -- every session from before."""
        from agent_system.cli_utils.chat import _resume_session

        ctx, _ = self._ctx(monkeypatch, "coder", "profile_a")
        ctx.llm_params = {"thinking_level": "max"}

        assert await _resume_session(ctx, "s2") is True

        assert ctx.llm_params == {"thinking_level": "max"}

    async def test_a_sessions_own_params_win_over_the_chats(self, monkeypatch):
        from agent_system.cli_utils.chat import _resume_session

        ctx, _ = self._ctx(monkeypatch, "coder", "profile_a", stored_params={"thinking_level": "low"})
        ctx.llm_params = {"thinking_level": "max"}

        assert await _resume_session(ctx, "s2") is True

        assert ctx.llm_params == {"thinking_level": "low"}
        assert ctx.llm_override.params == {"thinking_level": "low"}

    async def test_the_title_of_the_session_left_behind_does_not_follow(
            self, monkeypatch):
        from agent_system.cli_utils.chat import _resume_session

        ctx, _ = self._ctx(monkeypatch, "coder", None)
        ctx.session_title = "Titel der ersten Session"

        assert await _resume_session(ctx, "s2") is True
        assert ctx.session_title is None

    async def test_an_agent_the_config_forgot_does_not_block_it(self, monkeypatch):
        from agent_system.cli_utils.chat import _resume_session

        ctx, loads = self._ctx(monkeypatch, "retired_agent", None)

        assert await _resume_session(ctx, "s2") is True
        assert loads == ["s2"]
        assert ctx.llm_profile == "profile_a"

    async def test_a_session_whose_llm_cannot_be_started_is_refused(
            self, monkeypatch, capsys):
        """Not "staying on the current profile": the session would then RUN on
        this chat's, and the first save writes that over its record -- the
        failed switch destroying the very choice it was honouring."""
        import agent_system.llm.factory as factory
        from agent_system.cli_utils.chat import _resume_session

        ctx, loads = self._ctx(monkeypatch, "coder", "profile_b")

        def no_key(config, llm_profile, llm_params=None):
            raise RuntimeError("OPENAI_API_KEY missing")

        monkeypatch.setattr(factory, "create_llm_from_profile", no_key)

        assert await _resume_session(ctx, "s2") is False

        assert loads == [], "the session was loaded onto the wrong LLM"
        assert ctx.session_id == "s1" and ctx.llm_profile == "profile_a"
        out = capsys.readouterr().out
        assert "OPENAI_API_KEY missing" in out
        assert "--session s2" in out and "--llm" in out


class TestSkillsThatCannotRun:
    def test_only_typeable_skills_that_no_builtin_shadows_are_offered(self, monkeypatch):
        import agent_system.cli_utils.chat as chat

        registry = SimpleNamespace(list_skills=lambda: [
            SimpleNamespace(name=name) for name in ("writer", "3d-print", "tools")])
        monkeypatch.setattr(chat, "_skill_registry", lambda ctx: registry)

        assert chat._available_skills(None) == ["writer"]

    def test_a_skill_file_in_another_encoding_does_not_end_the_chat(
            self, monkeypatch, capsys):
        import agent_system.cli_utils.chat as chat
        import agent_system.skills as skills

        registry = SimpleNamespace(get=lambda name: SimpleNamespace(name=name))
        monkeypatch.setattr(chat, "_skill_registry", lambda ctx: registry)

        def unreadable(skill, arguments):
            raise UnicodeDecodeError("utf-8", b"\xe4", 0, 1, "invalid start byte")

        monkeypatch.setattr(skills, "invoke", unreadable)

        assert chat._expand_skill(None, "writer", "") is None
        assert "Could not read skill 'writer'" in capsys.readouterr().out


class TestTypedAheadPaste:
    async def test_a_pasted_block_opening_with_a_path_is_delivered(self):
        """Mid-turn, every line starting with "/" was refused as a command --
        a pasted stack trace that opens with a path included."""
        agent = _InjectAgent()
        reader = _ScriptedReader([("/usr/lib/x.py line 3\nValueError", "")])
        r, _ = _renderer(width=200)
        state = {"request_id": "req-1"}
        task = asyncio.create_task(
            _poll_typed_input(reader, r, _inject_ctx(agent), state))
        await asyncio.sleep(0.15)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

        assert agent.injected == [("req-1", "/usr/lib/x.py line 3\nValueError")]

    async def test_a_typed_command_is_still_refused(self):
        agent = _InjectAgent()
        reader = _ScriptedReader([("/exit", "")])
        r, out = _renderer(width=200)
        task = asyncio.create_task(
            _poll_typed_input(reader, r, _inject_ctx(agent), {"request_id": "q"}))
        await asyncio.sleep(0.15)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

        assert agent.injected == []
        assert "commands only work at the prompt" in out.getvalue()


class TestTheFooterFollowsTheModelThatRan:
    class _Client:
        def __init__(self, model, window):
            self.model = model
            self.context_window = window

    async def test_the_window_is_the_override_s(self):
        agent = _FakeAgent([
            {"type": "start", "request_id": "r", "session_id": "s"},
            {"type": "thinking_complete", "assistant": {"tool_calls": None},
             "usage": {"prompt_tokens": 9000, "completion_tokens": 300}},
            {"type": "final", "summary": "done",
             "usage": {"prompt_tokens": 9000, "completion_tokens": 300}},
            {"type": "end"},
        ])
        agent.llm = self._Client("agent-model", 1_000_000)
        r, _ = _renderer()
        result = await run_chat_turn(agent, "q", "s", r,
                                     llm_override=self._Client("switched", 64_000))

        assert result["context_window"] == 64_000

    async def test_each_call_is_priced_with_the_model_that_answered(self, monkeypatch):
        import agent_system.cli_utils.chat as chat

        priced = []
        monkeypatch.setattr(chat, "_accumulate_usage",
                            lambda total, usage, model=None, is_batch=False:
                            priced.append((model, is_batch)))
        agent = _FakeAgent([
            {"type": "start", "request_id": "r", "session_id": "s"},
            {"type": "thinking_complete", "assistant": {"tool_calls": None},
             "usage": {"prompt_tokens": 10, "completion_tokens": 1},
             "model": "fallback-model", "batch": False},
            # An extra final call: it names no model, the last call did.
            {"type": "final", "summary": "done",
             "usage": {"prompt_tokens": 20, "completion_tokens": 2}},
            {"type": "end"},
        ])
        agent.llm = self._Client("agent-model", 1_000_000)
        r, _ = _renderer()
        await run_chat_turn(agent, "q", "s", r)

        assert priced == [("fallback-model", False), ("fallback-model", False)]

    async def test_a_fallback_gets_no_fill_percentage(self):
        """The tokens are the fallback's, the window is this client's: the
        percentage would be against a size the answering model never had.
        _format_usage then prints the tokens alone, which is what we know."""
        agent = _FakeAgent([
            {"type": "start", "request_id": "r", "session_id": "s"},
            {"type": "thinking_complete", "assistant": {"tool_calls": None},
             "usage": {"prompt_tokens": 9000, "completion_tokens": 300},
             "model": "fallback-model"},
            {"type": "end"},
        ])
        agent.llm = self._Client("agent-model", 1_000_000)
        r, _ = _renderer()
        result = await run_chat_turn(agent, "q", "s", r)

        assert result["context_tokens"] == 9300, "fixture: the usage never arrived"
        assert "context_window" not in result

    async def test_a_later_call_without_usage_does_not_relabel_the_one_before(self):
        """The server emits thinking_complete WITHOUT usage as well, with its
        own model on it (an empty assistant, a step back on the base model
        after an escalation). Letting that set the key priced the escalated
        call as the base one -- and measured its tokens against the base
        model's window."""
        import agent_system.cli_utils.chat as chat

        priced = []
        agent = _FakeAgent([
            {"type": "start", "request_id": "r", "session_id": "s"},
            {"type": "thinking_complete", "assistant": {"tool_calls": [{"id": "t"}]},
             "usage": {"prompt_tokens": 9000, "completion_tokens": 300},
             "model": "escalated-model"},
            {"type": "thinking_complete", "assistant": {"tool_calls": None},
             "model": "agent-model"},
            {"type": "end"},
        ])
        agent.llm = self._Client("agent-model", 1_000_000)
        r, _ = _renderer()
        original = chat._accumulate_usage

        def record(total, usage, model=None, is_batch=False):
            priced.append(model)
            return original(total, usage, model, is_batch)

        chat._accumulate_usage = record
        try:
            result = await run_chat_turn(agent, "q", "s", r)
        finally:
            chat._accumulate_usage = original

        assert priced == ["escalated-model"], "a call without usage was priced"
        assert "context_window" not in result

    async def test_the_window_stands_when_that_client_answered(self):
        agent = _FakeAgent([
            {"type": "start", "request_id": "r", "session_id": "s"},
            {"type": "thinking_complete", "assistant": {"tool_calls": None},
             "usage": {"prompt_tokens": 9000, "completion_tokens": 300},
             "model": "agent-model"},
            {"type": "end"},
        ])
        agent.llm = self._Client("agent-model", 1_000_000)
        r, _ = _renderer()
        result = await run_chat_turn(agent, "q", "s", r)

        assert result["context_window"] == 1_000_000

    def test_the_event_model_wins_over_the_clients(self):
        agent = SimpleNamespace(llm=SimpleNamespace(model="agent-model"))
        override = SimpleNamespace(model="switched-model")
        event = {"model": "fallback-model", "batch": True}
        assert _call_pricing_key(agent, override, event) == ("fallback-model", True)


def chat_module():
    import agent_system.cli_utils.chat as chat
    return chat


class TestInterruptsInsideTheWork:
    """Review round 2: the Ctrl-C that lands inside the work itself."""

    def test_an_interrupt_inside_the_save_does_not_hang_the_chat(self, capsys):
        """The task ends with the KeyboardInterrupt; waiting on it again hung,
        because asyncio does not stop the loop for such a task."""
        import agent_system.cli_utils.chat as chat

        async def save_that_is_hit(ctx):
            raise KeyboardInterrupt

        ctx = _inject_ctx(None)
        loop = asyncio.new_event_loop()
        original = chat._save_session
        chat._save_session = save_that_is_hit
        try:
            assert chat._save_now(loop, ctx) is False
        finally:
            chat._save_session = original
            loop.close()
        assert ctx.last_saved is None
        assert "(save interrupted)" in capsys.readouterr().err

    def test_a_synchronous_interrupt_is_not_the_end_of_the_chat(self, monkeypatch, capsys):
        """Building a /model client, the skill lookup, a long /history: plain
        Python, no task to cancel. The interrupt left the REPL with a
        traceback -- and what was queued still ran."""
        import agent_system.cli_utils.chat as chat

        real_resolve = chat.resolve_chat_input
        calls = []

        def resolve_hit_once(task, *args):
            calls.append(task)
            if len(calls) == 1:
                raise KeyboardInterrupt
            return real_resolve(task, *args)

        monkeypatch.setattr(chat, "resolve_chat_input", resolve_hit_once)
        turns = []

        def probe(loop, ctx, task, renderer, editor=None):
            turns.append(task)
            return {"typed_queue": ["vorgemerkt"]} if task == "erste" else {}

        drive_chat_repl(monkeypatch, ["zweite"], initial_task="erste", turn_probe=probe)

        assert turns == ["zweite"], "the chat ended, or ran what was hit"
        assert "(interrupted)" in capsys.readouterr().err

    def test_queued_lines_do_not_run_after_it(self, monkeypatch):
        import agent_system.cli_utils.chat as chat

        real_render = chat._render_answer
        turns = []

        def render_hit(renderer, summary):
            if summary == "antwort eins":
                raise KeyboardInterrupt
            return real_render(renderer, summary)

        monkeypatch.setattr(chat, "_render_answer", render_hit)

        def probe(loop, ctx, task, renderer, editor=None):
            turns.append(task)
            if task == "erste":
                return {"summary": "antwort eins", "typed_queue": ["vorgemerkt"]}
            return {}

        drive_chat_repl(monkeypatch, [], initial_task="erste", turn_probe=probe)

        assert turns == ["erste"], "a line queued before the Ctrl-C still ran"

    async def test_an_interrupted_resume_leaves_the_chat_where_it_was(self, monkeypatch):
        """The session's client is built before anything HAPPENS: an interrupt
        there left ctx on the new session, whose hold the loop then released --
        and a build that fails must not even load the session, or the next
        save writes this chat's profile over the record it just read."""
        import agent_system.llm.factory as factory
        from agent_system.cli_utils.chat import _resume_session

        ctx, loads = TestResumeBringsTheSessionAlong()._ctx(
            monkeypatch, "coder", "profile_b")

        def interrupted(config, llm_profile, llm_params=None):
            raise KeyboardInterrupt

        monkeypatch.setattr(factory, "create_llm_from_profile", interrupted)
        with pytest.raises(KeyboardInterrupt):
            await _resume_session(ctx, "s2")

        assert loads == [], "the session was loaded before its LLM stood"
        assert ctx.session_id == "s1"
        assert ctx.llm_profile == "profile_a"


class TestWhatSurvivesAReseed:
    def test_the_repl_marks_every_line_it_did_not_send(self, monkeypatch):
        """Which lines were commands is the REPL's answer: a qualified plugin
        command is one, "/todo:milch kaufen" is a message. Reading their shape
        afterwards got one of the two wrong whichever rule it used."""
        import agent_system.cli_utils.chat as chat

        command = chat.PluginCommand(plugin="context_engineer", name="compact",
                                     summary="", tool="context_engineer_compact",
                                     argument="keep")
        monkeypatch.setattr(chat, "_run_plugin_command",
                            lambda loop, ctx, commands, qualified, payload: None)
        monkeypatch.setattr(chat, "_expand_skill", lambda ctx, name, payload: "skill text")
        editor = _RecordingEditor(["/context_engineer:compact", "/todo:milch kaufen",
                                   "/writer los", "/sessions", "/nonsense"])

        drive_chat_repl(monkeypatch, [], editor=editor, skills=["writer"],
                        plugin_commands=[command])

        assert editor.commands == ["/context_engineer:compact", "/writer los",
                                   "/sessions", "/nonsense"]


class TestTypedAheadCarryingACommand:
    async def test_a_block_that_contains_a_command_line_is_a_message(self):
        """The prompt's own rule, by the same function: several lines are a
        message. Refusing the block because one line reads like a command
        threw away pasted output -- `ls /` alone carries "/tmp" and "/opt" --
        and the same paste at the prompt went through."""
        agent = _InjectAgent()
        reader = _ScriptedReader([("/help\nbitte lesen", "")])
        r, out = _renderer(width=200)
        task = asyncio.create_task(
            _poll_typed_input(reader, r, _inject_ctx(agent), {"request_id": "q"}))
        await asyncio.sleep(0.15)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

        assert agent.injected == [("q", "/help\nbitte lesen")]
        assert "commands only work at the prompt" not in out.getvalue()

    def test_the_repl_hands_its_plugin_commands_to_the_context(self, monkeypatch):
        """Collected once in the loop; the poller needs them to tell a plugin
        command from a message. Never passed on, it read every one of them as
        a message -- and billed it."""
        import agent_system.cli_utils.chat as chat

        command = chat.PluginCommand(plugin="context_engineer", name="compact",
                                     summary="", tool="context_engineer_compact",
                                     argument="keep")
        seen = []
        drive_chat_repl(monkeypatch, ["frage"], plugin_commands=[command],
                        turn_probe=lambda loop, ctx, task, renderer, editor=None:
                        seen.append([c.qualified for c in ctx.plugin_commands]) or {})

        assert seen == [["context_engineer:compact"]]

    async def test_a_qualified_plugin_command_is_refused_too(self):
        """Only `resolve` ever claims "/plugin:command" -- parse_chat_command
        deliberately does not. Reading the line with the smaller function
        sent the spelling that help prints to the model, as a billed
        message."""
        import agent_system.cli_utils.chat as chat

        agent = _InjectAgent()
        ctx = _inject_ctx(agent)
        ctx.plugin_commands = [chat.PluginCommand(
            plugin="context_engineer", name="compact", summary="",
            tool="context_engineer_compact", argument="keep")]
        reader = _ScriptedReader([("/context_engineer:compact", "")])
        r, out = _renderer(width=200)
        task = asyncio.create_task(
            _poll_typed_input(reader, r, ctx, {"request_id": "q"}))
        await asyncio.sleep(0.15)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

        assert agent.injected == []
        assert "commands only work at the prompt" in out.getvalue()

    async def test_an_escaped_message_arrives_unescaped(self):
        """At the prompt "//compact" reaches the agent as "/compact" -- that
        is what the escape is for. Mid-turn the raw line went through."""
        agent = _InjectAgent()
        reader = _ScriptedReader([("//compact", "")])
        r, _ = _renderer(width=200)
        task = asyncio.create_task(
            _poll_typed_input(reader, r, _inject_ctx(agent), {"request_id": "q"}))
        await asyncio.sleep(0.15)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

        assert agent.injected == [("q", "/compact")]

    async def test_what_is_kept_for_the_next_turn_stays_raw(self):
        """The queued line passes the PROMPT again, which unescapes it there.
        Queuing the unescaped one would hand that prompt a command."""
        agent = _InjectAgent()
        agent.accept = False
        reader = _ScriptedReader([("//compact", "")])
        r, _ = _renderer(width=200)
        state = {"request_id": "q"}
        task = asyncio.create_task(_poll_typed_input(reader, r, _inject_ctx(agent), state))
        await asyncio.sleep(0.15)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

        assert state["typed_queue"] == ["//compact"]

    async def test_one_line_that_is_a_command_is_still_refused(self):
        agent = _InjectAgent()
        reader = _ScriptedReader([("/sessions 5", "")])
        r, out = _renderer(width=200)
        task = asyncio.create_task(
            _poll_typed_input(reader, r, _inject_ctx(agent), {"request_id": "q"}))
        await asyncio.sleep(0.15)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

        assert agent.injected == []
        assert "commands only work at the prompt" in out.getvalue()

    # The counter-case -- a pasted block that opens with a path goes through
    # untouched -- is TestTypedAheadPaste's first test, same reader script and
    # same production path. Not written twice.


class TestWorkThatGotThroughAsTheInterruptLanded:
    """asyncio does not stop the loop for a task that finished with a
    KeyboardInterrupt: the result is sitting in the task and waiting again
    hangs. Reading it as "cancelled" threw away work that had already
    happened -- and for /resume that meant letting go of the hold on the
    session the chat had just switched to."""

    def test_a_finished_command_reports_its_result(self):
        import agent_system.cli_utils.chat as chat

        async def work():
            _interrupt_soon(asyncio.get_running_loop())
            return "ergebnis"

        loop = asyncio.new_event_loop()
        try:
            assert chat._run_interruptible(loop, work(), "/sessions") == (True, "ergebnis")
        finally:
            loop.close()

    def test_a_resume_that_finished_keeps_the_session_it_switched_to(self, monkeypatch):
        import agent_system.cli_utils.chat as chat

        released = []
        monkeypatch.setattr(chat, "_release_session",
                            lambda ctx, sid: released.append(sid))

        async def resume_that_finished(ctx, session_id):
            _interrupt_soon(asyncio.get_running_loop())
            ctx.session_id = session_id
            return True

        seen = []
        drive_chat_repl(monkeypatch, ["/resume s2", "frage"],
                        resume=resume_that_finished,
                        turn_probe=lambda loop, ctx, task, renderer, editor=None:
                        seen.append(ctx.session_id) or {})

        assert seen == ["s2"], "the chat was told its resume had been cancelled"
        assert released[:1] == ["s1"], "it let go of the session it switched TO"

    def test_an_abandoned_task_is_never_left_pending(self):
        """Every further Ctrl-C interrupts the wait, and a cancelled task left
        pending on the shared loop runs on inside the NEXT command."""
        import agent_system.cli_utils.chat as chat

        loop = asyncio.new_event_loop()

        async def unwinds_through_a_ctrl_c():
            try:
                await asyncio.sleep(10)
            except asyncio.CancelledError:
                loop.call_soon(_raise_interrupt)  # Ctrl-C while it unwinds
                await asyncio.sleep(0.05)
                raise

        try:
            task = loop.create_task(unwinds_through_a_ctrl_c())
            loop.run_until_complete(asyncio.sleep(0))  # let it reach the sleep
            chat._drain(loop, task, "the save")

            assert task.done(), "it is still pending on the loop"
        finally:
            loop.close()

    def test_a_save_that_ended_in_a_cancellation_did_not_get_through(self, capsys):
        """A task is marked CANCELLED for any CancelledError that escapes its
        coroutine, asked for or not -- and a plugin command lets one through,
        run_plugin_command catches Exception, not BaseException. Reading such
        a task with .exception() RAISES, so without the cancelled check the
        CancelledError leaves _save_now past its own except and ends the chat.
        """
        import agent_system.cli_utils.chat as chat

        async def save_cancelled_as_the_interrupt_lands(ctx):
            # No await in between: the task must be DONE when the callback
            # fires, or the interrupt lands on a task that is merely pending.
            asyncio.get_running_loop().call_soon(_raise_interrupt)
            raise asyncio.CancelledError

        ctx = _inject_ctx(None)
        loop = asyncio.new_event_loop()
        original = chat._save_session
        chat._save_session = save_cancelled_as_the_interrupt_lands
        try:
            assert chat._save_now(loop, ctx) is False
        finally:
            chat._save_session = original
            loop.close()
        assert ctx.last_saved is None
        assert "(save interrupted)" in capsys.readouterr().err


class TestTheFarewell:
    def test_it_names_the_session_and_the_way_back_without_a_save(
            self, monkeypatch, capsys):
        """A chat that never saved still knows which session it was. Saying
        "nothing saved" would be a lie -- the agent writes the session itself
        when it finalises a request -- and withholding the way back is what
        the person complained about."""
        drive_chat_repl(monkeypatch, [])

        err = capsys.readouterr().err
        assert "Session saved:" not in err, "fixture: something WAS saved"
        assert "s1" in err and "Resume with:" in err


class TestASaveThatWonTheRace:
    def test_a_save_that_got_through_counts(self, capsys):
        """The interrupt arrived after the save finished: the task is done
        WITHOUT an exception. Treating that as interrupted left was_new_session
        True, so the next /model skipped its own save as well."""
        import agent_system.cli_utils.chat as chat

        async def save_that_finished(ctx):
            asyncio.get_running_loop().call_soon(_raise_interrupt)
            return True

        ctx = _inject_ctx(None)
        ctx.was_new_session = True
        loop = asyncio.new_event_loop()
        original = chat._save_session
        chat._save_session = save_that_finished
        try:
            assert chat._save_now(loop, ctx) is True
        finally:
            chat._save_session = original
            loop.close()

        assert ctx.last_saved == "s"
        assert ctx.was_new_session is False
        assert "(save interrupted)" not in capsys.readouterr().err


def _raise_interrupt():
    raise KeyboardInterrupt


# --------------------------------------------------------------------------
# What the chat could not do yet: complete a line, continue the last session,
# name one, and change the agent without leaving.
# --------------------------------------------------------------------------


def _completion_ctx(monkeypatch, **extra):
    """A context with a config an agent listing and a profile listing can read."""
    from agent_system.cli_utils.chat import _ChatContext

    profiles = {"fast": SimpleNamespace(description="the cheap one"),
                "deep": SimpleNamespace(description="the good one")}
    servers = {
        "coder": SimpleNamespace(agent_config=object()),
        "writer": SimpleNamespace(agent_config=object()),
        "file_ops": SimpleNamespace(agent_config=None),  # a tool server
    }
    tracker = SimpleNamespace(
        get_session_messages=lambda sid: [],
        get_session_template_vars=lambda sid: {"projekt": "auritale"},
        set_session_metadata=lambda sid, meta: None)
    agent = SimpleNamespace(
        system_config=SimpleNamespace(
            plugins=SimpleNamespace(servers=servers),
            llm_system=SimpleNamespace(profiles=profiles)),
        _session_tracker=tracker, agent_config=None,
        llm=SimpleNamespace(model="m"))
    ctx = _ChatContext(
        agent=agent, entry_name="coder", session_service=None, session_user="u",
        session_id="s1", was_new_session=False, llm_profile="fast",
        llm_override=None, llm_profile_info=None, show_status=False)
    for key, value in extra.items():
        setattr(ctx, key, value)
    return ctx


def _document(text: str):
    """A real prompt_toolkit Document with the cursor at the end.

    Not a stand-in: the completer reads text_before_cursor AND
    text_after_cursor, and a namespace with only the first one made the
    end-of-line case indistinguishable from the middle of a line.
    """
    from prompt_toolkit.document import Document

    return Document(text=text, cursor_position=len(text))


class TestCompletion:
    """Tab at the prompt. The REPL owns what exists; the editor only asks."""

    def _values(self, ctx, line, skills=()):
        from agent_system.cli_utils.chat import _completions_for

        return [value for value, _hint in _completions_for(ctx, skills, line)]

    def test_a_command_word_offers_commands_skills_and_plugin_commands(
            self, monkeypatch):
        import agent_system.cli_utils.chat as chat

        ctx = _completion_ctx(monkeypatch)
        ctx.plugin_commands = [chat.PluginCommand(
            plugin="context_engineer", name="compact", summary="shrink it",
            tool="context_engineer_compact")]

        values = self._values(ctx, "/re", skills=["writer"])

        assert "/resume" in values and "/title" in values
        assert "/compact" in values, "the agent's own commands are missing"
        assert "/writer" in values, "skills are missing"

    def test_a_plain_message_completes_nothing(self, monkeypatch):
        """Tab in the middle of a sentence has to stay a tab. The FIRST word
        is the case that matters: everything after it is read as an argument
        of a command that does not exist, which answers nothing either way."""
        ctx = _completion_ctx(monkeypatch)
        assert self._values(ctx, "wie") == []
        assert self._values(ctx, "wie komme ich") == []

    def test_model_offers_the_profiles(self, monkeypatch):
        assert self._values(_completion_ctx(monkeypatch), "/model d") == ["deep", "fast"]

    def test_agent_offers_only_real_agents(self, monkeypatch):
        """The RAW entry with an agent_config -- a tool server is not an agent."""
        assert self._values(_completion_ctx(monkeypatch), "/agent ") == ["coder", "writer"]

    def test_resume_offers_what_the_last_listing_knew(self, monkeypatch):
        ctx = _completion_ctx(monkeypatch, recent_sessions=[
            {"session_id": "ab12cd34", "title": "Blitter umbauen"},
            {"session_id": "ff00aa11"}])

        assert self._values(ctx, "/resume ") == ["ab12cd34", "ff00aa11"]

    def test_vars_offers_the_ones_this_session_has(self, monkeypatch):
        values = self._values(_completion_ctx(monkeypatch), "/vars ")

        assert values == ["unset", "clear", "projekt="]

    def test_attach_offers_paths(self, monkeypatch, tmp_path):
        (tmp_path / "bild.png").write_bytes(b"x")
        (tmp_path / "unterordner").mkdir()
        monkeypatch.chdir(tmp_path)

        values = self._values(_completion_ctx(monkeypatch), "/attach ")

        assert "bild.png" in values
        assert "unterordner/" in values, "a directory has to stay walkable"

    def test_a_path_typed_with_backslashes_completes_too(self, monkeypatch, tmp_path):
        """The editor offers what STARTS WITH the typed word, and
        "unterordner\\bi" never starts with "unterordner/bild.png" -- on this
        platform that is every second path."""
        (tmp_path / "unterordner").mkdir()
        (tmp_path / "unterordner" / "bild.png").write_bytes(b"x")
        monkeypatch.chdir(tmp_path)

        values = self._values(_completion_ctx(monkeypatch), "/attach unterordner\\bi")

        # "clear" rides along with every /attach; the editor drops it because
        # it does not start with the word.
        assert "unterordner\\bild.png" in values

    def test_a_path_with_a_space_is_one_argument(self, monkeypatch, tmp_path):
        """/attach reads the REST OF THE LINE as one path (Windows paths have
        spaces). Cutting the line at the last space offered entries of the
        current directory as the continuation of "Program Fil" and wrote one
        into the middle of the path."""
        (tmp_path / "Program Files").mkdir()
        (tmp_path / "Program Files" / "ziel.png").write_bytes(b"x")
        monkeypatch.chdir(tmp_path)

        line = "/attach Program Files/zi"
        values = self._values(_completion_ctx(monkeypatch), line)

        assert "Program Files/ziel.png" in values
        # ...and the editor replaces exactly that much of the line.
        from agent_system.cli_utils.chat import _completion_word
        assert _completion_word(line) == "Program Files/zi"

    def test_it_offers_the_files_where_the_person_stands(self, monkeypatch,
                                                         tmp_path):
        """The completer and the commands that consume it have to name the
        same directory. Offering the project's files and then looking for the
        accepted name in the launch directory gives "Not a file" at best --
        and where both trees hold that name, the wrong file without a word.
        """
        from agent_system import paths

        here = tmp_path / "hier"
        here.mkdir()
        project = tmp_path / "projekt"
        project.mkdir()
        (here / "im-startordner.png").write_bytes(b"x")
        (project / "im-projekt.png").write_bytes(b"y")
        monkeypatch.setattr(paths, "_launch_dir", here)
        monkeypatch.chdir(project)

        values = self._values(_completion_ctx(monkeypatch), "/attach im-")

        assert "im-startordner.png" in values
        assert "im-projekt.png" not in values, "it offered the project's files"

    def test_export_completes_paths_as_one_argument_too(self, monkeypatch, tmp_path):
        (tmp_path / "alte transkripte").mkdir()
        monkeypatch.chdir(tmp_path)

        values = self._values(_completion_ctx(monkeypatch), "/export alte tr")

        assert "alte transkripte/" in values

    def test_a_message_word_is_still_cut_at_the_space(self):
        """Only the whole-line commands take the rest; everything else
        completes the last word, or "/model d" would offer nothing."""
        from agent_system.cli_utils.chat import _completion_word

        # TWO words behind the command: with one, "the last word" and "the
        # rest of the line" are the same string and the test measures nothing.
        assert _completion_word("/vars projekt=auritale buch=B8") == "buch=B8"
        assert _completion_word("/model d") == "d"

    def test_resume_does_not_offer_another_agents_session(self, monkeypatch):
        """_resume_session refuses one, so offering it means announcing a
        session and bouncing it in the next line."""
        ctx = _completion_ctx(monkeypatch, recent_sessions=[
            {"session_id": "aa11", "title": "meine", "agent_name": "coder"},
            {"session_id": "bb22", "title": "fremde", "agent_name": "writer"},
            {"session_id": "cc33", "title": "alte, ohne namen"},
        ])

        values = self._values(ctx, "/resume ")

        assert "aa11" in values
        assert "bb22" not in values, "a session of another agent was offered"
        assert "cc33" in values, "an index without the field must not hide a session"

    def test_think_offers_the_levels_the_server_takes(self, monkeypatch):
        from agent_system.llm.factory import THINKING_LEVELS

        values = self._values(_completion_ctx(monkeypatch), "/think ")

        assert values == [*THINKING_LEVELS, "default"]

    def test_the_editor_only_offers_what_starts_with_the_word(self):
        """The completer filters; the REPL answers for the whole context."""
        import agent_system.cli_utils.chat as chat

        completer = chat._build_completer(
            lambda line: [("deep", "the good one"), ("fast", "the cheap one")])

        completions = list(completer.get_completions(_document("/model d"), None))

        assert [c.text for c in completions] == ["deep"]
        assert completions[0].start_position == -1

    def test_a_pasted_block_completes_nothing(self):
        import agent_system.cli_utils.chat as chat

        completer = chat._build_completer(lambda line: [("deep", "")])

        assert list(completer.get_completions(
            _document("zeile eins\n/model d"), None)) == []

    def test_nothing_is_offered_with_the_cursor_inside_the_line(self):
        """The replaced span is measured from the cursor BACKWARDS, so
        completing in the middle left the rest of the word standing:
        "/res|ume abc" became "/resumeume abc"."""
        import agent_system.cli_utils.chat as chat
        from prompt_toolkit.document import Document

        completer = chat._build_completer(lambda line: [("/resume", "")])
        document = Document(text="/resume abc123", cursor_position=len("/res"))

        assert list(completer.get_completions(document, None)) == []

    def test_a_broken_suggestion_does_not_take_the_prompt_down(self):
        """An exception in a completer kills the editor mid-keystroke."""
        import agent_system.cli_utils.chat as chat

        def broken(line):
            raise RuntimeError("registry gone")

        completer = chat._build_completer(broken)

        assert list(completer.get_completions(_document("/mo"), None)) == []

    def test_the_repl_gives_the_editor_its_own_context(self, monkeypatch):
        """Wired through run_chat_loop: the callback has to see THIS chat's
        skills and plugin commands, or it completes for an empty one."""
        editor = _RecordingEditor(["/exit"])
        drive_chat_repl(monkeypatch, [], editor=editor, skills=["writer"])

        assert editor.suggest is not None, "the editor got no completion at all"
        assert "/writer" in [value for value, _ in editor.suggest("/wr")]


class TestResumeWithoutAnId:
    def test_it_takes_the_last_session_this_user_left(self, monkeypatch):
        import agent_system.cli_utils.chat as chat

        resumed = []

        async def resume(ctx, session_id):
            resumed.append(session_id)
            ctx.session_id = session_id
            return True

        monkeypatch.setattr(chat, "_resume_session", resume)
        ctx = _completion_ctx(monkeypatch)
        ctx.session_manager = SimpleNamespace(resolve_session_ref=_own_id, list_root_sessions=_sessions_of(
            [{"session_id": "s1", "title": "die offene"},
             {"session_id": "ab12cd34", "title": "die davor"}]))

        loop = asyncio.new_event_loop()
        try:
            assert loop.run_until_complete(chat._resume_last_session(ctx, "s1")) is True
        finally:
            loop.close()

        assert resumed == ["ab12cd34"], "it resumed the session it was already on"

    def test_nothing_to_continue_says_so(self, monkeypatch, capsys):
        import agent_system.cli_utils.chat as chat

        ctx = _completion_ctx(monkeypatch)
        ctx.session_manager = SimpleNamespace(
            list_root_sessions=_sessions_of([{"session_id": "s1"}]))

        loop = asyncio.new_event_loop()
        try:
            assert loop.run_until_complete(chat._resume_last_session(ctx, "s1")) is False
        finally:
            loop.close()

        assert "No earlier session" in capsys.readouterr().out

    def test_it_skips_a_session_of_another_agent(self, monkeypatch, capsys):
        """The newest session belongs to writer, this chat runs coder: taking
        it would be refused one line later. With two agents in the config that
        is the normal case, not the edge."""
        import agent_system.cli_utils.chat as chat

        resumed = []

        async def resume(ctx, session_id):
            resumed.append(session_id)
            ctx.session_id = session_id
            return True

        monkeypatch.setattr(chat, "_resume_session", resume)
        ctx = _completion_ctx(monkeypatch)          # entry_name="coder"
        ctx.session_manager = SimpleNamespace(resolve_session_ref=_own_id, list_root_sessions=_sessions_of([
            {"session_id": "ff00", "title": "des writers", "agent_name": "writer"},
            {"session_id": "ab12", "title": "meine", "agent_name": "coder"}]))

        loop = asyncio.new_event_loop()
        try:
            assert loop.run_until_complete(chat._resume_last_session(ctx, "s1")) is True
        finally:
            loop.close()

        assert resumed == ["ab12"]

    def test_a_broken_index_is_said_out_loud(self, monkeypatch, capsys):
        """What comes back is the PREVIOUS listing, and a bare /resume acts on
        it -- debug logging is off by then, so silence would make a stale
        answer look like a fresh one."""
        import agent_system.cli_utils.chat as chat

        async def explode(user_id):
            raise OSError("index.json is half written")

        ctx = _completion_ctx(monkeypatch)
        ctx.session_manager = SimpleNamespace(list_root_sessions=explode)

        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(chat._load_recent_sessions(ctx))
        finally:
            loop.close()

        assert "Could not list sessions" in capsys.readouterr().out

    def test_the_listing_is_kept_for_completion(self, monkeypatch):
        import agent_system.cli_utils.chat as chat

        ctx = _completion_ctx(monkeypatch)
        ctx.session_manager = SimpleNamespace(
            list_root_sessions=_sessions_of([{"session_id": "ab12cd34"}]))

        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(chat._load_recent_sessions(ctx))
        finally:
            loop.close()

        assert [e["session_id"] for e in ctx.recent_sessions] == ["ab12cd34"]

    def test_a_hold_taken_for_a_refused_resume_is_given_back(self, monkeypatch):
        """The hold comes before the load; a refusal (another agent's session,
        an LLM that cannot start) must not leave it locked."""
        import agent_system.cli_utils.chat as chat

        released = []
        monkeypatch.setattr(chat, "_hold_session", lambda ctx, sid: True)
        monkeypatch.setattr(chat, "_release_session",
                            lambda ctx, sid: released.append(sid))

        async def refused(ctx, session_id):
            return False

        monkeypatch.setattr(chat, "_resume_session", refused)
        ctx = _completion_ctx(monkeypatch)

        loop = asyncio.new_event_loop()
        try:
            assert loop.run_until_complete(chat._resume_into(ctx, "s2", "s1")) is False
        finally:
            loop.close()

        assert released == ["s2"]

    def test_an_interrupted_load_gives_its_hold_back_too(self, monkeypatch):
        import agent_system.cli_utils.chat as chat

        released = []
        monkeypatch.setattr(chat, "_hold_session", lambda ctx, sid: True)
        monkeypatch.setattr(chat, "_release_session",
                            lambda ctx, sid: released.append(sid))

        async def slow(ctx, session_id):
            await asyncio.sleep(10)
            return True

        monkeypatch.setattr(chat, "_resume_session", slow)
        ctx = _completion_ctx(monkeypatch)

        loop = asyncio.new_event_loop()
        try:
            task = loop.create_task(chat._resume_into(ctx, "s2", "s1"))
            loop.run_until_complete(asyncio.sleep(0))
            task.cancel()
            loop.run_until_complete(asyncio.gather(task, return_exceptions=True))
        finally:
            loop.close()

        assert released == ["s2"], "the hold outlived the cancelled load"


class TestRename:
    def test_a_saved_session_is_renamed_on_disk(self, monkeypatch):
        import agent_system.cli_utils.chat as chat

        renamed = []

        async def rename(user_id, session_id, title):
            renamed.append((user_id, session_id, title))

        ctx = _completion_ctx(monkeypatch)
        ctx.session_manager = SimpleNamespace(rename_session=rename)

        loop = asyncio.new_event_loop()
        try:
            assert loop.run_until_complete(
                chat._set_session_title(ctx, "Blitter umbauen")) is True
        finally:
            loop.close()

        assert renamed == [("u", "s1", "Blitter umbauen")]
        # ...and the next save must not put the old one back: _save_session
        # passes ctx.session_title, which --session-title may have filled.
        assert ctx.session_title == "Blitter umbauen"

    def test_a_failed_rename_leaves_the_title_alone(self, monkeypatch, capsys):
        """Swallowing the error and returning True would look exactly like a
        rename, and the record would keep the old name."""
        import agent_system.cli_utils.chat as chat

        async def explode(user_id, session_id, title):
            raise OSError("index.json is read-only")

        ctx = _completion_ctx(monkeypatch)
        ctx.session_title = "Alt"
        ctx.session_manager = SimpleNamespace(rename_session=explode)

        loop = asyncio.new_event_loop()
        try:
            assert loop.run_until_complete(
                chat._set_session_title(ctx, "Neu")) is False
        finally:
            loop.close()

        assert ctx.session_title == "Alt"
        assert "Could not rename" in capsys.readouterr().out

    def test_a_session_without_a_record_keeps_it_for_the_first_save(
            self, monkeypatch, capsys):
        """There is nothing on disk to rename yet -- the title rides along
        with the first message, exactly as --session-title does."""
        import agent_system.cli_utils.chat as chat

        ctx = _completion_ctx(monkeypatch)
        ctx.was_new_session = True
        ctx.session_manager = SimpleNamespace(rename_session=None)

        loop = asyncio.new_event_loop()
        try:
            assert loop.run_until_complete(
                chat._set_session_title(ctx, "Neue Sache")) is True
        finally:
            loop.close()

        assert ctx.session_title == "Neue Sache"
        assert "first message" in capsys.readouterr().out

    def test_a_stored_title_with_newlines_is_shown_on_one_line(self, monkeypatch, capsys):
        import agent_system.cli_utils.chat as chat

        ctx = _completion_ctx(monkeypatch)
        ctx.session_title = "Bewerte\n  Kapitel 3"

        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(chat._set_session_title(ctx, ""))
        finally:
            loop.close()

        assert "Title: Bewerte Kapitel 3" in capsys.readouterr().out

    def test_a_bare_title_says_the_one_on_disk(self, monkeypatch, capsys):
        """ctx.session_title is dropped once a save wrote it -- the record
        is where a resumed session's title lives."""
        import agent_system.cli_utils.chat as chat

        async def load(user_id, session_id):
            return {"title": "Der Blitter"}

        ctx = _completion_ctx(monkeypatch)
        ctx.was_new_session = False
        ctx.session_title = None
        ctx.session_manager = SimpleNamespace(load_session=load)
        loop = asyncio.new_event_loop()
        try:
            assert loop.run_until_complete(chat._set_session_title(ctx, "")) is False
        finally:
            loop.close()

        out = capsys.readouterr().out
        assert "Title: Der Blitter" in out, out

    def test_an_empty_title_is_refused(self, monkeypatch, capsys):
        import agent_system.cli_utils.chat as chat

        ctx = _completion_ctx(monkeypatch)
        loop = asyncio.new_event_loop()
        try:
            assert loop.run_until_complete(chat._set_session_title(ctx, "")) is False
        finally:
            loop.close()

        assert "Usage: /title" in capsys.readouterr().out


class TestSwitchAgent:
    """A session carries the agent it ran with, so a switch starts a new one."""

    def _patch_factory(self, monkeypatch, built=None):
        import agent_system.cli_utils.chat as chat

        monkeypatch.setattr(
            chat, "_agent_for",
            lambda ctx, name: built or SimpleNamespace(
                agent_config=SimpleNamespace(default_llm_profile="deep"),
                llm=SimpleNamespace(model="model-of-" + name),
                _session_tracker=ctx.agent._session_tracker,
                system_config=ctx.agent.system_config))
        monkeypatch.setattr(chat, "collect_plugin_commands", lambda agent: [])

    def test_a_bare_call_lists_the_agents(self, monkeypatch, capsys):
        import agent_system.cli_utils.chat as chat

        assert chat._switch_agent(_completion_ctx(monkeypatch), "") is False

        out = capsys.readouterr().out
        assert "coder" in out and "writer" in out
        assert "file_ops" not in out, "a tool server was offered as an agent"

    def test_switching_takes_the_new_agent_and_its_own_llm(self, monkeypatch):
        import agent_system.cli_utils.chat as chat

        self._patch_factory(monkeypatch)
        ctx = _completion_ctx(monkeypatch)
        ctx.llm_override = SimpleNamespace(model="das alte override")

        assert chat._switch_agent(ctx, "writer") is True

        assert ctx.entry_name == "writer"
        assert ctx.agent.llm.model == "model-of-writer"
        assert ctx.llm_override is None, "it kept answering on the old agent's LLM"
        assert ctx.llm_profile == "deep"

    def test_the_new_agents_own_commands_replace_the_old_ones(self, monkeypatch):
        """/compact belongs to the agent that has the plugin. Left standing,
        the chat keeps resolving the PREVIOUS agent's commands and dispatches
        a tool the new one does not have."""
        import agent_system.cli_utils.chat as chat

        self._patch_factory(monkeypatch)
        monkeypatch.setattr(chat, "collect_plugin_commands", lambda agent: [
            chat.PluginCommand(plugin="p", name="only-writer-has-this",
                               summary="s", tool="p_t")])
        ctx = _completion_ctx(monkeypatch)
        ctx.plugin_commands = [chat.PluginCommand(
            plugin="q", name="only-coder-had-this", summary="s", tool="q_t")]

        assert chat._switch_agent(ctx, "writer") is True

        assert [c.name for c in ctx.plugin_commands] == ["only-writer-has-this"]

    def test_a_thinking_level_stays_with_the_agent_it_was_set_for(self, monkeypatch):
        """/think on the coder, /agent writer: the writer answered without it, yet the chat still held
        it -- and its save wrote it into the writer's new session."""
        import agent_system.cli_utils.chat as chat

        self._patch_factory(monkeypatch)
        ctx = _completion_ctx(monkeypatch)
        ctx.llm_override = SimpleNamespace(model="m")
        ctx.llm_params = {"thinking_level": "high"}

        assert chat._switch_agent(ctx, "writer") is True

        assert ctx.llm_params == {}

    def test_a_command_line_llm_is_named_when_it_stops_applying(
            self, monkeypatch, capsys):
        """--llm built the client the chat has been answering on. The new
        agent runs on its own, and the banner would otherwise name a profile
        nobody chose here."""
        import agent_system.cli_utils.chat as chat

        self._patch_factory(monkeypatch)
        ctx = _completion_ctx(monkeypatch)
        ctx.llm_override = SimpleNamespace(model="m")
        ctx.llm_profile_info = "gpt-schnell (from --llm)"

        assert chat._switch_agent(ctx, "writer") is True

        assert "gpt-schnell" in capsys.readouterr().out

    def test_an_unknown_agent_changes_nothing(self, monkeypatch, capsys):
        import agent_system.cli_utils.chat as chat

        ctx = _completion_ctx(monkeypatch)

        assert chat._switch_agent(ctx, "writr") is False

        assert ctx.entry_name == "coder"
        out = capsys.readouterr().out
        assert "Unknown agent" in out and "writer" in out, "no suggestion offered"

    def test_a_name_the_factory_refuses_does_not_end_the_chat(self, monkeypatch, capsys):
        import agent_system.cli_utils.chat as chat
        from agent_system.servers.agent.entry import NotAnAgent

        def refuses(ctx, name):
            raise NotAnAgent(f"'{name}' is a tool server, not an agent.", ["coder"])

        monkeypatch.setattr(chat, "_agent_for", refuses)
        ctx = _completion_ctx(monkeypatch)

        assert chat._switch_agent(ctx, "writer") is False
        assert ctx.entry_name == "coder"
        assert "Could not switch to 'writer'" in capsys.readouterr().out

    def test_the_repl_starts_a_new_session_for_it(self, monkeypatch):
        import agent_system.cli_utils.chat as chat

        def switch(ctx, payload):
            ctx.entry_name = payload
            ctx.plugin_commands = ["marker"]
            return True

        monkeypatch.setattr(chat, "_switch_agent", switch)
        seen = []
        editor = _RecordingEditor(["/agent writer", "frage"])
        drive_chat_repl(
            monkeypatch, [], editor=editor,
            turn_probe=lambda loop, ctx, task, renderer, editor=None: seen.append(
                (ctx.entry_name, ctx.session_id, ctx.was_new_session)) or {})

        assert seen and seen[0][0] == "writer"
        assert seen[0][1] != "s1", "the new agent took over the old session"
        assert seen[0][2] is True
        assert editor.seeds, "the history was not swapped with the session"

    def test_the_repl_resolves_against_the_new_agents_commands(
            self, monkeypatch, capsys):
        """The loop keeps a local list for parsing and help. Not rebound, the
        chat answers /help and every typo hint for the agent it just left."""
        import agent_system.cli_utils.chat as chat

        command = chat.PluginCommand(plugin="p", name="only-writer-has-this",
                                     summary="s", tool="p_t")

        def switch(ctx, payload):
            ctx.entry_name = payload
            ctx.plugin_commands = [command]
            return True

        monkeypatch.setattr(chat, "_switch_agent", switch)
        drive_chat_repl(monkeypatch, ["/agent writer", "/help"])

        assert "only-writer-has-this" in capsys.readouterr().out

    def test_an_agent_already_registered_is_reused(self, monkeypatch):
        """Built twice, the second instance would have its own MCP clients and
        its own session tracker while the registry still holds the first."""
        import agent_system.cli_utils.chat as chat
        from agent_system.servers.agent.server import Agent

        registered = Agent.__new__(Agent)
        registry = SimpleNamespace(list=lambda: ["writer"],
                                   get=lambda name: registered)
        ctx = _completion_ctx(monkeypatch)
        ctx.agent.registry = registry
        built = []
        import agent_system.servers.agent.entry as entry
        monkeypatch.setattr(entry, "get_tool_server_config",
                            lambda *a: built.append(a) or "cfg")

        assert chat._agent_for(ctx, "writer") is registered
        assert built == [], "the registered agent was rebuilt"

    def test_an_agent_that_is_not_registered_yet_is_built(self, monkeypatch):
        import agent_system.cli_utils.chat as chat
        import agent_system.servers.agent.entry as entry

        registered = {}
        registry = SimpleNamespace(list=lambda: [], get=lambda name: None,
                                   register=registered.__setitem__)
        ctx = _completion_ctx(monkeypatch)
        ctx.agent.registry = registry
        built = []
        fresh = SimpleNamespace(agent_config=None)
        monkeypatch.setattr(entry, "get_tool_server_config", lambda name, config: "cfg")
        monkeypatch.setattr(entry, "Agent", lambda name, config, server_config, reg, session_service=None:
                            built.append(name) or fresh)

        assert chat._agent_for(ctx, "writer") is fresh
        assert built == ["writer"]
        assert registered == {"writer": fresh}


def _sessions_of(entries):
    async def list_root_sessions(user_id):
        return list(entries)

    return list_root_sessions


async def _own_id(user_id, ref, *, others=None):
    """resolve_session_ref of a store where every ref is an id."""
    return ref


class TestTakingTheLastExchangeBack:
    """The model cannot be asked to do better while its first attempt is
    still in the conversation -- /retry has to CUT it, not add to it."""

    def _messages(self):
        return [
            _Msg("user", "erste frage"),
            _Msg("assistant", "erste antwort"),
            _Msg("user", "schreib die routine"),
            _Msg("assistant", "", tool_calls=[
                {"id": "c1", "function": {"name": "file_ops_write", "arguments": "{}"}}]),
            _Msg("tool", "geschrieben"),
            _Msg("assistant", "fertig"),
        ]

    def test_it_cuts_the_question_and_everything_after_it(self):
        import agent_system.cli_utils.chat as chat

        ctx = _ctx_with(self._messages())

        assert _message_text_of(chat._drop_last_exchange(ctx)) == "schreib die routine"

        left = ctx.agent._session_tracker.get_session_messages("s1")
        assert [m.role for m in left] == ["user", "assistant"]
        assert _message_text_of(left[0]) == "erste frage"

    def test_the_parts_of_an_attached_question_survive(self):
        """Read back as TEXT, a question sent with an image came out as
        "was ist das? [image_url]" -- a billed turn about nothing."""
        import agent_system.cli_utils.chat as chat

        parts = [{"type": "text", "text": "was ist das?"},
                 {"type": "image_url", "image_url": {"url": "data:image/png;base64,xx"}}]
        ctx = _ctx_with([_Msg("user", parts), _Msg("assistant", "ein blitter")])

        dropped = chat._drop_last_exchange(ctx)

        # Read off the result, not compared against the list the test still
        # holds -- that comparison was `parts == parts` and could not fail.
        kinds = [part["type"] for part in dropped.content]
        assert kinds == ["text", "image_url"], "the image became a placeholder"
        assert dropped.content[1]["image_url"]["url"].startswith("data:image/png")

    def test_an_empty_session_has_nothing_to_take_back(self):
        import agent_system.cli_utils.chat as chat

        assert chat._drop_last_exchange(_ctx_with([])) is None

    def test_undo_writes_the_shortened_session(self, monkeypatch):
        """Only the agent's memory was cut, the file kept the dropped turn --
        and `--session <id>` brought it straight back."""
        import agent_system.cli_utils.chat as chat

        saved = []
        monkeypatch.setattr(chat, "_save_now", lambda loop, ctx: saved.append(True))
        monkeypatch.setattr(chat, "_drop_last_exchange", lambda ctx: "die frage")
        turns = []
        drive_chat_repl(monkeypatch, ["/undo"],
                        turn_probe=lambda loop, ctx, task, renderer, editor=None:
                        turns.append(task) or {})

        assert saved == [True]
        assert turns == [], "/undo started a turn"

    def test_retry_asks_the_same_thing_again(self, monkeypatch):
        import agent_system.cli_utils.chat as chat

        monkeypatch.setattr(chat, "_save_now", lambda loop, ctx: True)
        monkeypatch.setattr(chat, "_drop_last_exchange",
                            lambda ctx: _Msg("user", "schreib die routine"))
        turns = []
        drive_chat_repl(monkeypatch, ["/retry"],
                        turn_probe=lambda loop, ctx, task, renderer, editor=None:
                        turns.append(task) or {})

        assert [_message_text_of(task) for task in turns] == ["schreib die routine"]

    def test_retry_hands_over_something_the_agent_can_read(self, monkeypatch):
        """Agent.run_events takes a str or a ChatMessage (servers/agent/
        server.py). The bare content list fell through both: task_text became
        the list, sanitize_for_llm cannot read one and returns "" -- the retry
        billed a full turn about an EMPTY message, image and all."""
        import agent_system.cli_utils.chat as chat
        from agent_system.llm.models import ChatMessage

        parts = [{"type": "text", "text": "was ist das?"},
                 {"type": "image_url", "image_url": {"url": "data:image/png;base64,xx"}}]
        dropped = ChatMessage(role="user", content=parts)
        monkeypatch.setattr(chat, "_save_now", lambda loop, ctx: True)
        monkeypatch.setattr(chat, "_drop_last_exchange", lambda ctx: dropped)
        turns = []
        drive_chat_repl(monkeypatch, ["/retry"],
                        turn_probe=lambda loop, ctx, task, renderer, editor=None:
                        turns.append(task) or {})

        assert len(turns) == 1
        assert isinstance(turns[0], (str, ChatMessage)), (
            f"the agent cannot read a {type(turns[0]).__name__}")
        # ...and the image is still in it, not only the text.
        assert any(getattr(part, "type", None) == "image_url"
                   for part in turns[0].content), "the attachment was dropped"

    def test_a_failed_save_after_undo_is_named(self, monkeypatch, capsys):
        """A session emptied by /undo IS written (SessionTracker.emptied), so
        a save that fails here failed for another reason -- and silence would
        tell the user the turn is gone while `--session <id>` brings it back."""
        import agent_system.cli_utils.chat as chat

        monkeypatch.setattr(chat, "_save_now", lambda loop, ctx: False)
        monkeypatch.setattr(chat, "_drop_last_exchange",
                            lambda ctx: _Msg("user", "die einzige frage"))
        drive_chat_repl(monkeypatch, ["/undo"])

        assert "was NOT written" in capsys.readouterr().err

    def test_nothing_to_take_back_starts_no_turn(self, monkeypatch, capsys):
        import agent_system.cli_utils.chat as chat

        monkeypatch.setattr(chat, "_drop_last_exchange", lambda ctx: None)
        turns = []
        drive_chat_repl(monkeypatch, ["/retry"],
                        turn_probe=lambda loop, ctx, task, renderer, editor=None:
                        turns.append(task) or {})

        assert turns == []
        assert "Nothing to take back" in capsys.readouterr().out


class TestWhatALeftSessionTakesWithIt:
    """Both ways out of a session pass the same note."""

    def test_resume_names_the_title_it_drops(self, monkeypatch, capsys):
        """A /title before the first message parks the title on the context;
        the session it named has no record to write it into. /new says so --
        /resume dropped it without a word."""
        import agent_system.cli_utils.chat as chat

        ctx = _completion_ctx(monkeypatch)
        ctx.session_title = "Blitter umbauen"
        ctx.session_service = SimpleNamespace(
            load_and_restore_session=_restores_session())

        loop = asyncio.new_event_loop()
        try:
            assert loop.run_until_complete(chat._resume_session(ctx, "ab12")) is True
        finally:
            loop.close()

        assert "Blitter umbauen" in capsys.readouterr().out
        assert ctx.session_title is None, "the old title would rename the new session"

    def test_queued_attachments_are_named(self, monkeypatch, capsys):
        """They survive the change -- and are easy to forget once the chat
        says "New session", which is how a file reaches an agent it was
        never meant for."""
        import agent_system.cli_utils.chat as chat

        ctx = _completion_ctx(monkeypatch)
        ctx.attachments = ["/tmp/shot.png"]
        ctx.agent._session_tracker.set_session_messages = lambda sid, msgs: None

        chat._open_fresh_session(ctx, None)

        assert "stay queued" in capsys.readouterr().out
        assert ctx.attachments == ["/tmp/shot.png"], "the queued file was dropped"


def _restores_session():
    async def load_and_restore_session(agent, user_id, session_id):
        return True, 2

    return load_and_restore_session


class TestExport:
    def test_it_writes_the_conversation(self, tmp_path):
        import agent_system.cli_utils.chat as chat

        ctx = _ctx_with([
            _Msg("user", "was macht der blitter?"),
            _Msg("assistant", "er kopiert speicher", tool_calls=[
                {"id": "c1", "function": {"name": "file_ops_read",
                                          "arguments": '{"path": "blitter.c"}'}}]),
            _Msg("tool", "int main(void)"),
        ])
        target = tmp_path / "transcript.md"

        chat._export_transcript(ctx, str(target))

        written = target.read_text(encoding="utf-8")
        assert "was macht der blitter?" in written
        assert "er kopiert speicher" in written
        assert "file_ops_read" in written, "the tool traffic was dropped"
        assert "blitter.c" in written
        assert "int main(void)" in written, "the tool RESULT was dropped"
        # The header says which chat this was -- a transcript without it is
        # one of twenty files called chat-<something>.md.
        assert "s1" in written and "a" in written

    def test_without_a_path_it_writes_next_to_the_session(self, tmp_path, monkeypatch):
        """The name has to carry the session id: every export of every chat
        would otherwise be the same file, and the second one refuses."""
        import agent_system.cli_utils.chat as chat

        monkeypatch.chdir(tmp_path)

        chat._export_transcript(_ctx_with([_Msg("user", "frage")]), "")

        assert (tmp_path / "chat-s1.md").exists(), sorted(
            p.name for p in tmp_path.iterdir())

    def test_a_tilde_name_with_no_home_is_written_and_not_a_crash(
            self, tmp_path, monkeypatch):
        """Path.expanduser() RAISES for a "~name" it cannot resolve --
        `/export ~$notes.md` is the lock file Word leaves next to a document,
        and nothing catches around the dispatch, so it took the chat down.
        It is not a magic name, it is a file name, and it gets written.

        The setenv is what makes this measure anything on a machine where
        USERNAME and the profile directory match -- there it is the
        difference between red and green.
        """
        import agent_system.cli_utils.chat as chat

        monkeypatch.setenv("USERNAME", "jemand_ganz_anderes")
        monkeypatch.chdir(tmp_path)

        chat._export_transcript(_ctx_with([_Msg("user", "frage")]), "~$notes.md")

        assert (tmp_path / "~$notes.md").is_file(), sorted(
            p.name for p in tmp_path.iterdir())

    def test_it_writes_where_the_person_stands_not_where_the_process_runs(
            self, tmp_path, monkeypatch):
        """The chat runs from the project since enter_project(), so a name
        typed at the prompt would land in the checkout."""
        import agent_system.cli_utils.chat as chat
        from agent_system import paths

        here = tmp_path / "wo der mensch steht"
        here.mkdir()
        project = tmp_path / "projekt"
        project.mkdir()
        monkeypatch.setattr(paths, "_launch_dir", here)
        monkeypatch.chdir(project)

        chat._export_transcript(_ctx_with([_Msg("user", "frage")]), "transkript.md")

        assert (here / "transkript.md").is_file()
        assert not (project / "transkript.md").exists(), "written into the project"

    def test_an_existing_file_is_never_overwritten(self, tmp_path, capsys):
        """Without a path every export of a session picks the same name."""
        import agent_system.cli_utils.chat as chat

        target = tmp_path / "transcript.md"
        target.write_text("von gestern", encoding="utf-8")

        chat._export_transcript(_ctx_with([_Msg("user", "frage")]), str(target))

        assert target.read_text(encoding="utf-8") == "von gestern"
        assert "exists already" in capsys.readouterr().out

    def test_an_empty_session_writes_nothing(self, tmp_path, capsys):
        import agent_system.cli_utils.chat as chat

        target = tmp_path / "leer.md"

        chat._export_transcript(_ctx_with([]), str(target))

        assert not target.exists()
        assert "Nothing to export" in capsys.readouterr().out

    def test_the_repl_dispatches_it(self, monkeypatch, tmp_path):
        import agent_system.cli_utils.chat as chat

        asked = []
        monkeypatch.setattr(chat, "_export_transcript",
                            lambda ctx, payload: asked.append(payload))
        drive_chat_repl(monkeypatch, ["/export /tmp/x.md"])

        assert asked == ["/tmp/x.md"]


def _message_text_of(message):
    from agent_system.cli_utils.chat import _message_text

    return _message_text(message)

def _presence_ctx(tmp_path, monkeypatch, session_id="wake1", user="u"):
    """A chat context over a REAL SessionPresence on a temporary sessions dir.

    Not a stand-in: presence_for reads the store's root from
    AGENT_SESSION_STORAGE_PATH at call time, so pointing that at tmp_path
    gives the production object, with production lock and marker files.
    """
    from agent_system.cli_utils.chat import _ChatContext
    from agent_system.config.models import SessionPresenceConfig
    from agent_system.core.session_presence import presence_for

    monkeypatch.setenv("AGENT_SESSION_STORAGE_PATH", str(tmp_path / "sessions"))
    system_config = SimpleNamespace(
        session_presence=SessionPresenceConfig(enabled=True))
    agent = SimpleNamespace(system_config=system_config)
    ctx = _ChatContext(
        agent=agent, entry_name="a", session_service=None, session_user=user,
        session_id=session_id, was_new_session=False, llm_profile="p",
        llm_override=None, llm_profile_info=None, show_status=True,
    )
    presence = presence_for(system_config)
    assert presence is not None, "no presence -- this test would prove nothing"
    return ctx, presence


def _mark_input_waiting(ctx, presence):
    """Set the marker notify() leaves for a session somebody HOLDS.

    Placed here rather than by calling notify(): on an unheld session notify
    SPAWNS a wake run, and on one held by this very process its answer differs
    per platform (Windows byte-range locks belong to the handle, POSIX fcntl
    locks to the process). The marker is the same one file either way, and the
    assertion below reads it back through the production method.
    """
    assert presence.hold(ctx.session_id, ctx.session_user, ctx.entry_name)
    marker = presence.root / ctx.session_user / f"{ctx.session_id}.pending"
    marker.touch()
    assert presence.pending(ctx.session_id, ctx.session_user), \
        "fixture set no mark -- the watcher would have nothing to find"


@pytest.fixture
def repl_loop():
    """The loop the REPL would hand the editor.

    Since the prompt runs ON it (_PromptEditor._ask), a test that leaves it
    out exercises the fallback and not what the chat does.
    """
    loop = asyncio.new_event_loop()
    try:
        yield loop
    finally:
        loop.close()


class TestWakingTheWaitingPrompt:
    """A wake-up has to reach a chat that is sitting at the prompt.

    A held session learns of waiting input from a marker on disk, and the
    marker is picked up on a STEP of the session -- _presence_step does it on
    every LLM call. The chat holds its session across the whole REPL, so
    between turns nothing steps: a sub-agent finished with `wake_when_done`
    sat there unmentioned until the person happened to type something.
    """

    def _read_with_deadline(self, editor, pipe, seconds=8.0):
        """Read the prompt, with a newline in the pipe as a backstop: without
        it a watcher that never fires hangs the suite instead of failing it."""
        def _release():
            time.sleep(seconds)
            try:
                pipe.send_text("\n")
            except Exception:
                pass

        threading.Thread(target=_release, daemon=True).start()
        return editor.read("> ")

    def test_it_cuts_into_the_waiting_prompt(self, pt_prompt, tmp_path,
                                             monkeypatch, repl_loop):
        ctx, presence = _presence_ctx(tmp_path, monkeypatch)
        _mark_input_waiting(ctx, presence)
        editor = _build_prompt_editor([], loop=repl_loop)
        assert editor is not None, "no editor -- nothing to cut into"

        with _watch_for_wake(ctx, editor):
            with pytest.raises(_WokenAtThePrompt):
                self._read_with_deadline(editor, pt_prompt)

    def test_it_leaves_a_half_typed_line_alone(self, pt_prompt, tmp_path,
                                               monkeypatch, repl_loop):
        """exit() throws the buffer away, so a wake-up must not take a message
        being written out of someone's hands. It waits for the next tick -- by
        which time their own line has started a turn that takes the mark."""
        ctx, presence = _presence_ctx(tmp_path, monkeypatch)
        _mark_input_waiting(ctx, presence)
        editor = _build_prompt_editor([], loop=repl_loop)

        def _type_then_send():
            deadline = time.monotonic() + 8.0
            while time.monotonic() < deadline:
                if getattr(editor.app(), "is_running", False):
                    break
                time.sleep(0.01)
            pt_prompt.send_text("halb getippt")
            # Several watcher ticks with the mark set and the buffer not
            # empty; without the wait the read could return before any tick.
            time.sleep(_WAKE_POLL_S * 4)
            pt_prompt.send_text("\n")

        threading.Thread(target=_type_then_send, daemon=True).start()
        with _watch_for_wake(ctx, editor):
            assert editor.read("> ") == "halb getippt"

    def test_a_line_survives_a_focus_change_too(self, pt_prompt, tmp_path,
                                                monkeypatch, repl_loop):
        """Ctrl-R moves the FOCUS to the search buffer, and prompt_toolkit's
        Application.current_buffer follows the focus -- it even hands out an
        empty dummy buffer when nothing focusable has it. Asking that one
        reads "nothing typed" while the line sits in the default buffer, and
        the cut would throw exactly the line away that this guard exists for.
        """
        ctx, presence = _presence_ctx(tmp_path, monkeypatch)
        _mark_input_waiting(ctx, presence)
        editor = _build_prompt_editor([], loop=repl_loop)

        def _type_search_and_send():
            deadline = time.monotonic() + 8.0
            while time.monotonic() < deadline:
                if getattr(editor.app(), "is_running", False):
                    break
                time.sleep(0.01)
            pt_prompt.send_text("wie geht")
            time.sleep(0.2)
            pt_prompt.send_text("\x12")       # Ctrl-R: focus leaves the line
            # Several watcher ticks with the mark set and the focus away.
            time.sleep(_WAKE_POLL_S * 4)
            pt_prompt.send_text("\n")         # accept the (empty) search
            time.sleep(0.2)
            pt_prompt.send_text("\n")         # submit the line itself

        threading.Thread(target=_type_search_and_send, daemon=True).start()
        with _watch_for_wake(ctx, editor):
            assert editor.read("> ") == "wie geht"

    def test_the_mark_is_taken_before_the_turn_runs(self, tmp_path, monkeypatch):
        """A turn that never reaches an LLM call would leave the mark set, and
        the watcher would start the next turn a tick later, and the next."""
        ctx, presence = _presence_ctx(tmp_path, monkeypatch)
        _mark_input_waiting(ctx, presence)

        _take_wake_mark(ctx)

        assert not presence.pending(ctx.session_id, ctx.session_user)

    def test_without_an_editor_it_watches_nothing(self, tmp_path, monkeypatch):
        """The fallback reader is input(), which no thread can interrupt --
        and that is the piped path, where nobody sits at a prompt anyway."""
        ctx, presence = _presence_ctx(tmp_path, monkeypatch)
        _mark_input_waiting(ctx, presence)

        with _watch_for_wake(ctx, None):
            watching = [t for t in threading.enumerate()
                        if t.name == "chat-wake-watch"]
        assert not watching


class TestComposeInEditor:
    """/edit: for the messages a prompt line is the wrong shape for."""

    def _editor(self, monkeypatch, command):
        monkeypatch.delenv("VISUAL", raising=False)
        monkeypatch.setenv("EDITOR", command)

    def test_the_seed_goes_in_and_the_text_comes_back(self, tmp_path,
                                                      monkeypatch):
        script = tmp_path / "fake_editor.py"
        script.write_text(
            "import sys\n"
            "with open(sys.argv[1], 'a', encoding='utf-8') as fh:\n"
            "    fh.write('\\nzweite zeile mit \u00e4\u00f6\u00fc\\n')\n",
            encoding="utf-8")
        self._editor(monkeypatch, f"{sys.executable} {script}")
        expected = "vorgabe\nzweite zeile mit \u00e4\u00f6\u00fc"

        # Both ways round: the REPL runs the editor through the loop now,
        # so a test only on the fallback would stop watching what the
        # chat actually does.
        assert _compose_in_editor("vorgabe") == expected

        loop = asyncio.new_event_loop()
        try:
            assert _compose_in_editor("vorgabe", loop=loop) == expected
        finally:
            loop.close()

    def test_the_loop_keeps_turning_while_the_editor_is_open(self, tmp_path,
                                                            monkeypatch):
        """Writing a message in vim takes minutes. A plain subprocess.run
        blocks the REPL's thread for all of them -- the same parked loop that
        kept a sub-agent from ever finishing at the prompt, just with a longer
        window: composing is exactly when a background job has time to run."""
        script = tmp_path / "slow_editor.py"
        script.write_text("import time\ntime.sleep(0.5)\n",
                          encoding="utf-8")
        self._editor(monkeypatch, f"{sys.executable} {script}")

        loop = asyncio.new_event_loop()
        ticks = []

        async def background():
            while True:
                ticks.append(len(ticks))
                await asyncio.sleep(0.02)

        try:
            job = loop.create_task(background())
            _compose_in_editor("", loop=loop)
            job.cancel()
        finally:
            loop.close()

        assert len(ticks) > 5, f"the loop stood still: {len(ticks)} ticks"

    def test_an_empty_file_is_not_a_message(self, monkeypatch):
        self._editor(monkeypatch, f"{sys.executable} -c pass")
        assert _compose_in_editor("") == ""

    def test_an_editor_that_fails_sends_nothing(self, capsys, monkeypatch):
        """`vi` ending non-zero means the person backed out; sending the file
        anyway would bill the turn they just abandoned. None rather than "",
        so the REPL does not print a second "nothing sent" over this one."""
        self._editor(monkeypatch, f'{sys.executable} -c "raise SystemExit(3)"')

        assert _compose_in_editor("etwas") is None
        assert "3" in capsys.readouterr().out

    def test_the_temporary_file_does_not_stay_behind(self, monkeypatch):
        seen = {}
        self._editor(monkeypatch, f"{sys.executable} -c pass")
        real_run = subprocess.run

        def _spy(argv, **kwargs):
            seen["path"] = argv[-1]
            return real_run(argv, **kwargs)

        monkeypatch.setattr(subprocess, "run", _spy)
        _compose_in_editor("")

        assert seen.get("path"), "the editor was never started"
        assert not os.path.exists(seen["path"])

    def test_it_refuses_when_an_end_is_redirected(self, monkeypatch, capsys):
        """The editor inherits this process's stdout. With it redirected
        (`agent-cli chat > log.txt`) a full-screen editor draws its whole
        screen into the file and reads keys from the tty -- the person sees
        nothing and sits in an invisible vim."""
        import agent_system.cli_utils.chat as chat

        started = []
        monkeypatch.setattr(chat, "_editor_needs_a_terminal", lambda: True)
        monkeypatch.setattr(chat, "_compose_in_editor",
                            lambda seed, loop=None: started.append(seed))
        seen = []

        drive_chat_repl(
            monkeypatch, ["/edit", "/exit"],
            turn_probe=lambda loop, ctx, task, renderer, editor=None:
                seen.append(task) or {})

        assert started == [], "started an editor into a redirect"
        assert seen == []
        assert "terminal" in capsys.readouterr().out

    def test_a_redirected_end_is_what_makes_it_refuse(self, monkeypatch):
        """The gate itself, on the real streams: both ends or nothing."""
        import agent_system.cli_utils.chat as chat

        monkeypatch.setattr(chat.sys.stdin, "isatty", lambda: True,
                            raising=False)
        monkeypatch.setattr(chat.sys.stdout, "isatty", lambda: True,
                            raising=False)
        assert not _editor_needs_a_terminal()

        monkeypatch.setattr(chat.sys.stdout, "isatty", lambda: False,
                            raising=False)
        assert _editor_needs_a_terminal()

    def test_visual_wins_over_editor(self, monkeypatch):
        monkeypatch.setenv("EDITOR", "nano")
        monkeypatch.setenv("VISUAL", "code -w")
        assert _editor_command() == ["code", "-w"]

    @pytest.mark.skipif(os.name != "nt", reason="the Windows splitting rule")
    def test_a_windows_path_survives_both_ways(self, monkeypatch):
        """Measured: posix splitting eats the separators of an unquoted path,
        non-posix leaves the quotes on a quoted one -- and subprocess cannot
        open a file whose name has quotes in it."""
        self._editor(monkeypatch, r"C:\Windows\notepad.exe")
        assert _editor_command() == [r"C:\Windows\notepad.exe"]

        self._editor(monkeypatch, r'"C:\Program Files\np\np.exe" -multiInst')
        assert _editor_command() == [r"C:\Program Files\np\np.exe",
                                     "-multiInst"]


class TestCopyLastAnswer:
    """/copy: the answer as the model wrote it, not as the terminal wrapped it."""

    def _catch(self, monkeypatch):
        copied = {}
        monkeypatch.setattr("agent_system.cli_utils.chat._copy_to_clipboard",
                            lambda text: copied.setdefault("text", text))
        return copied

    def test_it_copies_the_last_answer(self, monkeypatch):
        copied = self._catch(monkeypatch)
        _copy_last_answer(_ctx_with(_TURN))
        assert copied["text"] == "zweite antwort"

    def test_a_tool_only_message_is_not_the_answer(self, monkeypatch):
        """The last assistant message of a turn can be tool calls and nothing
        else; copying "" would silently wipe the clipboard."""
        copied = self._catch(monkeypatch)
        _copy_last_answer(_ctx_with([
            _Msg("user", "frage"),
            _Msg("assistant", "die antwort"),
            _Msg("assistant", "", tool_calls=[
                {"function": {"name": "todo_list", "arguments": "{}"}}]),
        ]))
        assert copied["text"] == "die antwort"

    def test_nothing_to_copy_says_so(self, capsys, monkeypatch):
        monkeypatch.setattr("agent_system.cli_utils.chat._copy_to_clipboard",
                            lambda text: pytest.fail("nothing should be copied"))
        _copy_last_answer(_ctx_with([_Msg("user", "frage")]))
        assert "No answer to copy" in capsys.readouterr().out

    def test_a_failing_clipboard_tool_is_reported(self, capsys, monkeypatch):
        monkeypatch.setattr("agent_system.cli_utils.chat._copy_to_clipboard",
                            lambda text: "xclip: not here")
        _copy_last_answer(_ctx_with(_TURN))
        assert "xclip: not here" in capsys.readouterr().out

    def test_windows_gets_utf16(self, monkeypatch):
        """`clip` reads its stdin in the console codepage, which turns every
        umlaut in an answer into a question mark -- measured. It does
        understand UTF-16LE, which is what it gets."""
        sent = {}

        def _run(argv, **kwargs):
            sent.update(argv=argv, payload=kwargs["input"])
            return SimpleNamespace(returncode=0)

        monkeypatch.setattr(os, "name", "nt")
        monkeypatch.setattr(subprocess, "run", _run)

        assert _copy_to_clipboard("Gr\u00fc\u00dfe") is None
        assert sent["argv"] == ["clip"]
        assert sent["payload"] == "Gr\u00fc\u00dfe".encode("utf-16-le")

    def test_a_tool_that_is_there_but_fails_lets_the_next_one_try(self,
                                                                  monkeypatch):
        """wl-copy is installed on plenty of X11 machines and exits non-zero
        there; stopping at it would skip the xclip that takes the text."""
        tried = []

        def _run(argv, **kwargs):
            tried.append(argv[0])
            if argv[0] == "wl-copy":
                raise subprocess.CalledProcessError(1, argv)
            return SimpleNamespace(returncode=0)

        monkeypatch.setattr(os, "name", "posix")
        monkeypatch.setattr(sys, "platform", "linux")
        monkeypatch.setattr(subprocess, "run", _run)

        assert _copy_to_clipboard("text") is None
        assert tried == ["wl-copy", "xclip"]

    def test_no_clipboard_tool_names_what_it_tried(self, monkeypatch):
        monkeypatch.setattr(os, "name", "posix")
        monkeypatch.setattr(sys, "platform", "linux")

        def _missing(argv, **kwargs):
            raise FileNotFoundError(argv[0])

        monkeypatch.setattr(subprocess, "run", _missing)

        error = _copy_to_clipboard("text")
        assert error and "xclip" in error and "wl-copy" in error

class _EditorThatIsWokenOnce:
    """A prompt that is cut short by a wake-up on its first read.

    What prompt_toolkit does when the watcher calls exit(exception=...) --
    see TestWakingTheWaitingPrompt, which drives the real one.
    """

    def __init__(self, lines):
        self.lines = list(lines)
        self.woken = False
        self.remembered = []
        self.commands = []

    def read(self, prompt):
        if not self.woken:
            self.woken = True
            raise _WokenAtThePrompt
        return self.lines.pop(0)

    def read_continuation(self, prompt):
        return self.lines.pop(0)

    def reseed(self, seed):
        pass

    def remember(self, text):
        self.remembered.append(text)

    def remember_command(self, text):
        self.commands.append(text)

    def app(self):
        return None


class TestTheWokenTurnInTheLoop:
    """Wired through run_chat_loop: a command tested only through its handler
    is a command nobody has ever seen dispatched."""

    def _tasks(self, monkeypatch, editor, **kwargs):
        seen = []

        def _turn(loop, ctx, task, renderer, editor=None):
            seen.append(task)
            return {}

        drive_chat_repl(monkeypatch, [], editor=editor, turn_probe=_turn,
                        **kwargs)
        return seen

    def test_the_prompt_read_is_watched(self, monkeypatch):
        """Without this one, everything below still passes on a repl that
        never starts a watcher at all: the editor there raises by itself."""
        import contextlib

        import agent_system.cli_utils.chat as chat

        watched = []

        @contextlib.contextmanager
        def _watch(ctx, editor):
            watched.append(ctx.session_id)
            yield

        monkeypatch.setattr(chat, "_watch_for_wake", _watch)
        drive_chat_repl(monkeypatch, ["/exit"])

        assert watched == ["s1"]

    def test_a_wake_up_becomes_a_turn(self, monkeypatch):
        """And it reaches the turn as the RUN speaking: the same sentence as a
        `user` turn is indistinguishable from something the person typed, in
        the stored history and for the model, which has to report which of the
        two happened."""
        from agent_system.llm.message_roles import DEVELOPER

        editor = _EditorThatIsWokenOnce(["/exit"])

        task, = self._tasks(monkeypatch, editor)

        assert task.content == WAKE_TASK
        assert task.role == DEVELOPER

    def test_the_woken_turn_does_not_spend_the_queued_attachments(
            self, monkeypatch, tmp_path):
        """/attach queues files for the message the person is WRITING. A turn
        a finished background job started would send them with "You were woken
        because input is waiting" and leave the queue empty."""
        import agent_system.cli_utils.chat as chat

        merged = []
        monkeypatch.setattr(
            chat, "_task_with_attachments",
            lambda ctx, task, renderer: merged.append(task) or task)
        png = tmp_path / "bild.png"
        png.write_bytes(b"x")
        editor = _EditorThatIsWokenOnce(["/exit"])

        tasks = self._tasks(monkeypatch, editor, attachments=[str(png)])

        task, = tasks
        assert task.content == WAKE_TASK
        assert merged == [], "the wake-up spent the person's queued files"

    def test_the_same_sentence_typed_by_a_person_stays_theirs(self, monkeypatch):
        """The conversion asks whether this turn began with a WAKE, not whether
        the text looks like one. Without that, somebody quoting the sentence
        back would have it recorded as something the run said."""
        seen = []

        def _turn(loop, ctx, task, renderer, editor=None):
            seen.append(task)
            return {}

        drive_chat_repl(monkeypatch, [WAKE_TASK, "/exit"], turn_probe=_turn)

        assert seen == [WAKE_TASK], "a typed line is a person talking"

    def test_a_typed_line_still_gets_them(self, monkeypatch, tmp_path):
        """The counter-proof: without it the test above would pass on a chat
        whose attachments never reach any turn at all."""
        import agent_system.cli_utils.chat as chat

        merged = []
        monkeypatch.setattr(
            chat, "_task_with_attachments",
            lambda ctx, task, renderer: merged.append(task) or task)
        png = tmp_path / "bild.png"
        png.write_bytes(b"x")

        self._tasks(monkeypatch, _RecordingEditor(["was ist das", "/exit"]),
                    attachments=[str(png)])

        assert merged == ["was ist das"]


class TestEditAndCopyAreDispatched:
    """Both are wired into the REPL's own dispatch, not only into a handler."""

    def test_copy_runs_without_an_llm_turn(self, monkeypatch):
        import agent_system.cli_utils.chat as chat

        copied = []
        monkeypatch.setattr(chat, "_copy_last_answer",
                            lambda ctx: copied.append(ctx.session_id))
        seen = []

        drive_chat_repl(
            monkeypatch, ["/copy", "/exit"],
            turn_probe=lambda loop, ctx, task, renderer, editor=None:
                seen.append(task) or {})

        assert copied == ["s1"]
        assert seen == [], "/copy billed a turn"

    def test_edit_becomes_the_turn(self, monkeypatch):
        import agent_system.cli_utils.chat as chat

        asked = []

        def _compose(seed, loop=None):
            asked.append(seed)
            return "die lange nachricht"

        monkeypatch.setattr(chat, "_compose_in_editor", _compose)
        seen = []

        editor = drive_chat_repl(
            monkeypatch, ["/edit vorgabe", "/exit"],
            turn_probe=lambda loop, ctx, task, renderer, editor=None:
                seen.append(task) or {})

        assert asked == ["vorgabe"], "the argument did not pre-fill the file"
        assert seen == ["die lange nachricht"]
        # It never passed the prompt, so arrow-up would not have it.
        assert "die lange nachricht" in editor.remembered

    def test_an_empty_edit_sends_nothing(self, monkeypatch):
        import agent_system.cli_utils.chat as chat

        monkeypatch.setattr(chat, "_compose_in_editor",
                            lambda seed, loop=None: "")
        seen = []

        drive_chat_repl(
            monkeypatch, ["/edit", "/exit"],
            turn_probe=lambda loop, ctx, task, renderer, editor=None:
                seen.append(task) or {})

        assert seen == []

    def test_an_editor_that_could_not_run_says_nothing_more(self, monkeypatch,
                                                            capsys):
        """_compose_in_editor has already printed why (no editor, aborted).
        A second "Empty -- nothing sent." under it reads as a second failure,
        which is why it answers None there and "" for an empty file."""
        import agent_system.cli_utils.chat as chat

        monkeypatch.setattr(chat, "_compose_in_editor",
                            lambda seed, loop=None: None)
        seen = []

        drive_chat_repl(
            monkeypatch, ["/edit", "/exit"],
            turn_probe=lambda loop, ctx, task, renderer, editor=None:
                seen.append(task) or {})

        assert seen == []
        assert "Empty" not in capsys.readouterr().out

class TestTheLoopKeepsTurningAtThePrompt:
    """A background job must make progress while the prompt waits.

    `PromptSession.prompt()` is synchronous: it starts a loop of its own and
    blocks the REPL's thread until Enter, so everything on the REPL's loop --
    a sub-agent started with blocking=false, for one -- stood still between
    two turns. Measured on 20.09.2026: a one-step LLM call of a sub-agent sat
    unread for four minutes and completed 0.3 s after somebody typed, because
    typing is what turned the loop again. `wake_when_done` could not work in
    the chat at all that way: the job that sets the wake mark was frozen, so
    the mark the prompt watches for never appeared.
    """

    def _send_after(self, pipe, editor, text, delay=0.6):
        def _run():
            deadline = time.monotonic() + 8.0
            while time.monotonic() < deadline:
                if getattr(editor.app(), "is_running", False):
                    break
                time.sleep(0.01)
            time.sleep(delay)
            pipe.send_text(text)

        threading.Thread(target=_run, daemon=True).start()

    def test_a_task_on_the_loop_runs_while_the_prompt_waits(self, pt_prompt):
        loop = asyncio.new_event_loop()
        ticks = []

        async def background():
            while True:
                ticks.append(len(ticks))
                await asyncio.sleep(0.02)

        try:
            editor = _build_prompt_editor([], loop=loop)
            assert editor is not None
            job = loop.create_task(background())
            self._send_after(pt_prompt, editor, "fertig\n")

            assert editor.read("> ") == "fertig"
            # The prompt waited ~0.6 s; a loop that never turned leaves none.
            assert len(ticks) > 5, f"the loop stood still: {len(ticks)} ticks"

            job.cancel()
        finally:
            loop.close()

    def test_the_repl_hands_its_own_loop_to_the_editor(self, monkeypatch):
        """Not the editor's business to find a loop -- the REPL owns the one
        the sub-agent jobs live on, and that is the one that must turn."""
        import agent_system.cli_utils.chat as chat

        seen = {}

        def _factory(seed, suggest=None, loop=None):
            seen["loop"] = loop
            return _RecordingEditor(["/exit"])

        monkeypatch.setattr(chat, "_build_prompt_editor", _factory)
        monkeypatch.setattr(chat, "collect_plugin_commands", lambda agent_: [])
        monkeypatch.setattr(chat, "_available_skills", lambda ctx: [])
        monkeypatch.setattr(chat.sys.stdin, "isatty", lambda: True, raising=False)
        monkeypatch.setattr(chat.sys.stdout, "isatty", lambda: True, raising=False)

        loop = asyncio.new_event_loop()
        try:
            chat.run_chat_loop(
                agent=SimpleNamespace(_session_tracker=None, agent_config=None,
                                      llm=SimpleNamespace(model="m")),
                entry_name="a", session_service=None, session_user="u",
                session_id="s1", was_new_session=False, llm_profile="p",
                show_status=False, loop=loop)
        finally:
            loop.close()

        assert seen["loop"] is loop

    def test_ctrl_c_still_reaches_the_repl(self, pt_prompt, repl_loop):
        """Running the prompt as a coroutine must not change what the keys
        mean: the REPL counts KeyboardInterrupt at the prompt (twice exits)
        and leaves on EOFError, and both used to come out of a loop of
        prompt_toolkit's own."""
        editor = _build_prompt_editor([], loop=repl_loop)
        self._send_after(pt_prompt, editor, "", delay=0.1)

        with pytest.raises(KeyboardInterrupt):
            editor.read("> ")

    def test_end_of_input_still_reaches_the_repl(self, pt_prompt, repl_loop):
        editor = _build_prompt_editor([], loop=repl_loop)
        # Ctrl-D on an empty line; on Windows Ctrl-Z is bound to the same
        # meaning (_prompt_key_bindings), which is what the REPL leaves on.
        key = "" if os.name == "nt" else ""
        self._send_after(pt_prompt, editor, key, delay=0.1)

        with pytest.raises(EOFError):
            editor.read("> ")

    def test_without_a_loop_it_still_reads(self, pt_prompt):
        """The fallback for a _PromptEditor built outside the REPL. It is the
        old behaviour, loop and all -- which is why the REPL passes one."""
        editor = _build_prompt_editor([])
        self._send_after(pt_prompt, editor, "auch fertig\n", delay=0.1)

        assert editor.read("> ") == "auch fertig"
