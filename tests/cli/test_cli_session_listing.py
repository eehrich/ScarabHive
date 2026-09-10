"""The session listing: one line per session, and no sub-agents.

Both CLI entry points used to print four lines plus a blank per session over
the MERGED index -- for cli_user that is 2915 real sessions and 31086
sub-sessions, so `--list-sessions` produced roughly 170.000 lines of output for
a question whose answer is "which session do I continue".

The listing runs against a real SessionManager here on purpose: whether a
sub-session shows up is decided by which index partition it is written to, and
that only happens on the real write path.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from agent_system.cli_utils.session_listing import (
    DEFAULT_LIMIT,
    format_session_line,
    parse_limit,
    print_sessions,
)
from agent_system.services.session_manager import SessionManager

USER = "cli_user"


@pytest.fixture
def manager(tmp_path):
    """A store with three top-level sessions and one sub-agent below the first."""
    mgr = SessionManager(storage_path=str(tmp_path / "sessions"))

    async def seed():
        parent = await mgr.create_session(
            user_id=USER, title="the parent chat", agent_name="basic_agent")
        for i in range(2):
            await mgr.create_session(
                user_id=USER, title=f"another chat {i}", agent_name="sysadmin_agent")
        await mgr.create_session(
            user_id=USER, title="spawned by the parent", agent_name="v6_psychologe",
            parent_session_id=parent["session_id"])
        return parent["session_id"]

    parent_id = asyncio.run(seed())
    mgr.clear_cache()
    return mgr, parent_id


class TestTheListing:
    def test_one_line_per_session(self, manager, capsys):
        mgr, _ = manager
        asyncio.run(print_sessions(mgr, USER))
        lines = capsys.readouterr().out.strip().splitlines()
        # header + 3 top-level sessions, nothing else (no footer requested)
        assert len(lines) == 4, lines

    def test_a_sub_agent_session_is_not_listed(self, manager, capsys):
        mgr, _ = manager
        asyncio.run(print_sessions(mgr, USER))
        out = capsys.readouterr().out
        assert "spawned by the parent" not in out, (
            "a sub-agent session reached the listing")
        assert "v6_psychologe" not in out
        assert "the parent chat" in out, "fixture seeded nothing visible"

    def test_a_multiline_title_stays_one_line(self, manager, capsys):
        mgr, _ = manager

        async def retitle():
            sessions = await mgr.list_root_sessions(USER)
            data = await mgr.load_session(USER, sessions[0]["session_id"])
            data["title"] = "[Debate-Forum #90001]\n\n\n## new posts"
            await mgr.save_session(data)

        asyncio.run(retitle())
        mgr.clear_cache()

        asyncio.run(print_sessions(mgr, USER))
        lines = capsys.readouterr().out.strip().splitlines()
        assert len(lines) == 4, lines
        assert "[Debate-Forum #90001] ## new posts" in "\n".join(lines)

    def test_the_limit_caps_the_output_and_says_so(self, manager, capsys):
        mgr, _ = manager
        asyncio.run(print_sessions(mgr, USER, limit=2, more_hint="pass a count"))
        out = capsys.readouterr().out
        lines = out.strip().splitlines()
        assert len(lines) == 4, lines  # header + 2 + the "more" line
        assert "(2 of 3)" in lines[0]
        assert "... 1 more -- pass a count" in out

    def test_zero_means_all(self, manager, capsys):
        mgr, _ = manager
        asyncio.run(print_sessions(mgr, USER, limit=0, more_hint="pass a count"))
        out = capsys.readouterr().out
        assert "(3 of 3)" in out
        assert "more --" not in out

    def test_the_current_session_is_marked(self, manager, capsys):
        mgr, parent_id = manager
        asyncio.run(print_sessions(mgr, USER, current_session_id=parent_id))
        marked = [l for l in capsys.readouterr().out.splitlines()
                  if l.startswith(" *")]
        assert len(marked) == 1 and parent_id in marked[0], marked

    def test_the_newest_sessions_are_the_ones_that_survive_the_cap(
            self, manager, capsys):
        # Without an order, a cap is an arbitrary sample: showing the 20
        # OLDEST of 2915 answers "which session do I continue" wrongly every
        # time, and nothing else in the repo pins list_root_sessions' sort.
        mgr, _ = manager

        async def touch_the_oldest_one():
            oldest = (await mgr.list_root_sessions(USER))[-1]
            data = await mgr.load_session(USER, oldest["session_id"])
            await mgr.save_session(data)  # rewrites updated_at to now
            return oldest["session_id"], oldest["title"]

        touched_id, touched_title = asyncio.run(touch_the_oldest_one())
        mgr.clear_cache()

        asyncio.run(print_sessions(mgr, USER, limit=1))
        out = capsys.readouterr().out
        assert touched_id in out, "the cap kept the oldest session, not the newest"
        assert touched_title in out

    def test_the_footer_tells_the_user_what_to_do_with_an_id(self, manager, capsys):
        mgr, _ = manager
        asyncio.run(print_sessions(mgr, USER, footer="Use /resume <id>."))
        assert capsys.readouterr().out.strip().endswith("Use /resume <id>.")

    def test_an_empty_store_says_so(self, tmp_path, capsys):
        mgr = SessionManager(storage_path=str(tmp_path / "empty"))
        asyncio.run(print_sessions(mgr, "nobody"))
        assert "No sessions for user 'nobody'." in capsys.readouterr().out

    def test_a_broken_store_does_not_kill_the_cli(self, capsys):
        class Exploding:
            async def list_root_sessions(self, user_id):
                raise RuntimeError("index gone")

        asyncio.run(print_sessions(Exploding(), USER))
        assert "Could not list sessions: index gone" in capsys.readouterr().out

    def test_no_session_manager_is_not_a_crash(self, capsys):
        asyncio.run(print_sessions(None, USER))
        assert "Session listing is unavailable." in capsys.readouterr().out


class TestTheLine:
    def test_it_carries_what_the_listing_promises(self):
        line = format_session_line({
            "session_id": "a1b2c3d4e5",
            "updated_at": "2026-09-10T12:16:17.470030+00:00",
            "message_count": 14,
            "agent_name": "v6_synopsis_writer",
            "title": "idea-typ-normalkette",
        })
        assert "a1b2c3d4e5" in line
        assert "14 msg" in line
        assert "v6_synopsis_writer" in line
        assert "idea-typ-normalkette" in line
        assert "\n" not in line

    def test_the_time_is_the_local_wall_clock_not_the_stored_utc(self):
        # The store keeps UTC and this machine runs UTC+2, so printing the
        # stamp's first 16 characters showed a session that ran at 14:16 as
        # 12:16 -- in the one column the session is picked by. Built against
        # an offset five hours from this machine's, so the test measures the
        # conversion on any machine, including one running UTC.
        local_now = datetime.now().astimezone()
        far_away = timezone(local_now.utcoffset() + timedelta(hours=5))
        stamp = local_now.astimezone(far_away)

        line = format_session_line({"session_id": "x",
                                    "updated_at": stamp.isoformat()})

        assert local_now.strftime("%Y-%m-%d %H:%M") in line
        assert stamp.strftime("%Y-%m-%d %H:%M") not in line, (
            "the stored offset was thrown away")

    @pytest.mark.parametrize("stamp", [
        "kaputt", "", None, 12345,
        # astimezone() goes through the platform's local-time API, and on
        # Windows a stamp outside ~1970..3001 raises OSError -- which is
        # neither TypeError nor ValueError. The print loop has no try around
        # it, so this ended the listing halfway down with a traceback.
        "1969-07-20T20:17:00+00:00",
        "3200-01-01T00:00:00+00:00",
    ])
    def test_an_unreadable_timestamp_does_not_take_the_line_down(self, stamp):
        line = format_session_line({"session_id": "x", "updated_at": stamp})
        assert "x" in line and "\n" not in line

    def test_a_listing_survives_a_record_with_an_impossible_timestamp(self,
                                                                      capsys):
        class OneOddRecord:
            async def list_root_sessions(self, user_id):
                return [{"session_id": "good1", "updated_at": "2026-09-10T10:00:00+00:00"},
                        {"session_id": "ancient", "updated_at": "1969-07-20T20:17:00+00:00"},
                        {"session_id": "good2", "updated_at": "2026-09-09T10:00:00+00:00"}]

        asyncio.run(print_sessions(OneOddRecord(), USER))
        out = capsys.readouterr().out
        assert "good2" in out, "the listing stopped at the odd record"

    # The id shapes the store actually holds: as generated, hand-picked via
    # `--session <id>`, and the web API's ephemeral ones -- 85 of the 3372 real
    # entries are NOT the 10-character kind, and the ten-character case is the
    # only one that fits the budget by itself.
    @pytest.mark.parametrize("session_id", [
        "a1b2c3d4e5",
        "looptest-1788301000",
        "ephemeral-71bc299c-c707-4b83-aa71-4c91440cee00",
        "x",
    ])
    def test_the_line_fits_a_console(self, session_id):
        # 74 fixed columns plus a 48-char title ran to 125, and 831 of the 2915
        # real entries wrapped on a 120-column terminal -- a session over two
        # lines is what this module replaced.
        line = format_session_line({
            "session_id": session_id,
            "updated_at": "2026-09-10T12:16:17.470030+00:00",
            "message_count": 9999,
            "agent_name": "v5b_synopsis_audit_revalidator",
            "title": "x" * 80,
        })
        assert len(line) <= 100, f"{len(line)} columns: {line}"

    def test_a_long_id_costs_the_title_not_the_id(self):
        # The footer offers `--session <id>`; half an id resumes nothing.
        long_id = "ephemeral-71bc299c-c707-4b83-aa71-4c91440cee00"
        line = format_session_line({"session_id": long_id, "title": "x" * 80,
                                    "agent_name": "a", "message_count": 1})
        assert long_id in line, "the id was cut -- the footer's offer is void"

    def test_a_record_without_a_count_falls_back_to_the_messages(self):
        line = format_session_line({"session_id": "x", "messages": [1, 2, 3]})
        assert "3 msg" in line

    def test_a_long_agent_name_does_not_push_the_title_out_of_line(self):
        short = format_session_line({"session_id": "x", "agent_name": "a", "title": "T"})
        long = format_session_line({
            "session_id": "x", "agent_name": "v5b_synopsis_audit_revalidator_and_more",
            "title": "T"})
        assert short.index("T") == long.index("T")


class TestAgentRunWiring:
    """`--list-sessions` used to be a store_true flag.

    It now takes an optional count, and that turns every truthiness check into
    a trap: `--list-sessions 0` means "all of them", not "no listing".
    """

    @pytest.fixture(autouse=True)
    def no_config_load(self, monkeypatch):
        import agent_system.agent_run as run

        def boom(*a, **k):
            raise AssertionError(
                "the config was loaded although only a listing was asked for")

        monkeypatch.setattr(run, "load_settings", boom)
        return run

    @pytest.fixture
    def recorded(self, monkeypatch, no_config_load):
        calls = {}

        async def fake_print(manager, user_id, **kwargs):
            calls.update(kwargs)
            calls["user_id"] = user_id

        monkeypatch.setattr(no_config_load, "print_sessions", fake_print)
        return calls

    def _run(self, monkeypatch, argv):
        import agent_system.agent_run as run
        monkeypatch.setattr("sys.argv", argv)
        run.main()

    def test_a_bare_flag_lists_the_default_count(self, monkeypatch, recorded):
        self._run(monkeypatch, ["agent-run", "--list-sessions"])
        assert recorded["limit"] == DEFAULT_LIMIT

    def test_a_count_is_honoured(self, monkeypatch, recorded):
        self._run(monkeypatch, ["agent-run", "--list-sessions", "7"])
        assert recorded["limit"] == 7

    def test_zero_is_a_count_not_an_off_switch(self, monkeypatch, recorded):
        self._run(monkeypatch, ["agent-run", "--list-sessions", "0"])
        assert recorded["limit"] == 0, "0 was read as 'no listing'"

    def test_a_request_after_the_flag_still_lists(self, monkeypatch, recorded,
                                                  capsys):
        # argparse fills an optional's slot from the next token BEFORE
        # converting it, so with type=int this died on `int("was laeuft?")`
        # -- exit 2 where the store_true version listed the sessions.
        self._run(monkeypatch, ["agent-run", "--list-sessions", "was laeuft?"])
        assert recorded["limit"] == DEFAULT_LIMIT
        assert "Ignoring 'was laeuft?'" in capsys.readouterr().out

    def test_the_footer_and_the_hint_reach_the_listing(self, monkeypatch,
                                                       recorded):
        self._run(monkeypatch, ["agent-run", "--list-sessions"])
        assert recorded["footer"] == "Continue one with: --session <id>"
        assert "--list-sessions 0 for all" in recorded["more_hint"]

    def test_the_session_being_continued_is_the_marked_one(self, monkeypatch,
                                                           recorded):
        self._run(monkeypatch, ["agent-run", "--list-sessions",
                                "--session", "s1"])
        assert recorded["current_session_id"] == "s1"

    def test_without_the_flag_a_request_is_still_required(self, monkeypatch,
                                                          no_config_load):
        monkeypatch.setattr("sys.argv", ["agent-run"])
        with pytest.raises(SystemExit) as exit_info:
            no_config_load.main()
        assert exit_info.value.code == 2


class TestParseLimit:
    @pytest.mark.parametrize("value,expected", [
        ("5", 5), (5, 5), ("0", 0), (0, 0), ("  7 ", 7),
    ])
    def test_a_count_is_a_count(self, value, expected):
        assert parse_limit(value) == (expected, None)

    @pytest.mark.parametrize("value", ["", None, "   "])
    def test_nothing_typed_means_the_default(self, value):
        assert parse_limit(value) == (DEFAULT_LIMIT, None)

    @pytest.mark.parametrize("value", ["abc", "2o", "all", "1.5", "eine frage"])
    def test_what_is_not_a_count_is_reported_not_swallowed(self, value):
        limit, complaint = parse_limit(value)
        assert limit == DEFAULT_LIMIT
        assert complaint == value, (
            "a discarded argument that still prints a plausible listing is "
            "indistinguishable from a honoured one")

    @pytest.mark.parametrize("value", ["-1", "-5", -3])
    def test_a_negative_count_is_a_complaint_not_all_of_them(self, value):
        # `-1` for "the last one" is a common reflex; mapping it to 0 would
        # answer it with all 2915 lines -- the loudest possible reading of a
        # request to see fewer.
        limit, complaint = parse_limit(value)
        assert limit == DEFAULT_LIMIT
        assert complaint == str(value)
