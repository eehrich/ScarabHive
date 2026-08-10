"""The shared slash-command layer: catalogue, parsing, skill invocation.

These pin the contract both surfaces depend on. The terminal chat used to own
this logic and the web UI had none; if the two ever drift apart again, it will
show up here first.
"""
from __future__ import annotations

import pytest

from agent_system.chat_commands import (
    BUILTIN_COMMANDS,
    CLI,
    WEB,
    commands_for,
    looks_like_command,
    parse_chat_command,
    resolve,
    suggest_command,
)
from agent_system.skills.invocation import expand, split_arguments


class TestCatalogue:
    def test_every_alias_maps_to_exactly_one_command(self):
        """A duplicate alias would make one command silently unreachable."""
        seen = {}
        for command in BUILTIN_COMMANDS:
            for alias in command.aliases:
                assert alias not in seen, f"{alias} claimed by {seen.get(alias)} and {command.name}"
                seen[alias] = command.name

    def test_aliases_are_slash_prefixed_and_lowercase(self):
        for command in BUILTIN_COMMANDS:
            for alias in command.aliases:
                assert alias.startswith("/")
                assert alias == alias.lower()

    def test_exit_is_terminal_only(self):
        """There is no terminal to leave in a browser tab."""
        assert "exit" in {c.name for c in commands_for(CLI)}
        assert "exit" not in {c.name for c in commands_for(WEB)}

    def test_both_surfaces_share_the_rest(self):
        cli = {c.name for c in commands_for(CLI)}
        web = {c.name for c in commands_for(WEB)}
        assert web < cli
        assert cli - web == {"exit"}


class TestParsing:
    @pytest.mark.parametrize("line,expected", [
        ("/exit", "exit"), ("/quit", "exit"), ("/q", "exit"),
        ("/new", "new"), ("/sessions", "sessions"), ("/hist", "history"),
        ("/?", "help"), ("/H", "help"),
    ])
    def test_aliases_resolve(self, line, expected):
        assert parse_chat_command(line)[0] == expected

    def test_payload_is_split_off(self):
        assert parse_chat_command("/resume abc-123") == ("resume", "abc-123")

    def test_a_path_is_a_message_not_a_typo(self):
        """A sysadmin agent gets paths typed at it; those are not commands."""
        command, payload = parse_chat_command("/etc/nginx/nginx.conf lesen")
        assert command is None
        assert payload == "/etc/nginx/nginx.conf lesen"

    def test_double_slash_escapes(self):
        assert parse_chat_command("//exit means quit") == (None, "/exit means quit")

    def test_command_shaped_typo_is_flagged(self):
        assert parse_chat_command("/sesions") == ("unknown", "/sesions")

    def test_multiline_input_is_a_message_even_if_it_opens_like_a_command(self):
        """A pasted paragraph must not be eaten as a command.

        "/last Woche haben wir X besprochen\\nbitte fasse zusammen" resolved to
        the /last command and everything after the first line was discarded
        without a word. Both surfaces can produce multi-line input.
        """
        line = "/last Woche haben wir X besprochen\nbitte fasse zusammen"
        command, payload = parse_chat_command(line)
        assert command is None
        assert payload == line


class TestResolution:
    def test_builtins_win_over_skills(self):
        """A skill named 'help' must not take over the way out."""
        assert resolve("/help", ["help"]).kind == "command"

    def test_skill_is_found_with_its_arguments(self):
        r = resolve("/writer analysiere Kapitel 3", ["writer", "verify"])
        assert (r.kind, r.name, r.payload) == ("skill", "writer", "analysiere Kapitel 3")

    def test_skill_without_arguments(self):
        r = resolve("/verify", ["verify"])
        assert (r.kind, r.name, r.payload) == ("skill", "verify", "")

    def test_skill_lookup_is_case_insensitive(self):
        assert resolve("/Writer", ["writer"]).name == "writer"

    def test_unknown_stays_unknown(self):
        assert resolve("/nope", ["writer"]).kind == "unknown"

    def test_plain_message_passes_through(self):
        r = resolve("was kostet das?", ["writer"])
        assert (r.kind, r.payload) == ("message", "was kostet das?")


class TestSuggestions:
    def test_prefix_beats_fuzzy(self):
        assert suggest_command("/h") == "/h"

    def test_typo_gets_a_hint(self):
        assert suggest_command("/sesions") == "/sessions"

    def test_skills_compete_for_suggestions(self):
        """A mistyped skill should be suggested too, not just built-ins."""
        assert suggest_command("/writr", ["writer"]) == "/writer"

    def test_nonsense_gets_none(self):
        assert suggest_command("/zzzzqqqq") is None


class TestLooksLikeCommand:
    def test_command_word(self):
        assert looks_like_command("/sessions")

    def test_message(self):
        assert not looks_like_command("bitte /sessions zeigen")

    def test_path_is_not_a_command(self):
        assert not looks_like_command("/etc/hosts")


class TestArgumentSplitting:
    def test_quoted_group_stays_together(self):
        assert split_arguments('eins "zwei drei" vier') == ["eins", "zwei drei", "vier"]

    def test_unbalanced_quotes_fall_back_instead_of_raising(self):
        """A stray apostrophe must not turn a turn into an error message."""
        assert split_arguments("don't crash") == ["don't", "crash"]

    def test_empty(self):
        assert split_arguments("   ") == []


class TestExpansion:
    def test_arguments_placeholder(self):
        assert expand("Fix issue $ARGUMENTS now", "123") == "Fix issue 123 now"

    def test_positional_placeholders(self):
        body = "from $1 to $2"
        assert expand(body, "alt neu") == "from alt to neu"

    def test_indexed_form(self):
        assert expand("take $ARGUMENTS[2]", "a b c") == "take b"

    def test_missing_position_is_empty_not_an_error(self):
        """An optional second argument is legitimate."""
        assert expand("$1|$2", "nur-eins") == "nur-eins|"

    def test_input_is_appended_when_the_skill_declares_no_placeholder(self):
        """Typed input must never vanish silently."""
        out = expand("Do the thing.", "mit Nachdruck")
        assert out.startswith("Do the thing.")
        assert out.endswith("ARGUMENTS: mit Nachdruck")

    def test_no_arguments_no_footer(self):
        assert expand("Do the thing.", "") == "Do the thing."

    def test_placeholder_skill_without_arguments_stays_clean(self):
        assert expand("Fix $ARGUMENTS", "") == "Fix "

    @pytest.mark.parametrize("body", [
        "Never exceed $100 per run.",
        "dc.w $0180,$0F00   ; COLOR00 = red",
        "awk '{print $1000}' file",
        "Budget: $50",
    ])
    def test_dollar_digits_in_prose_are_left_alone(self, body):
        """A price or a hex constant is not a parameter.

        An unbounded ``$(\\d+)`` ate them: "never exceed $100" became "never
        exceed ", and — worse — the match counted as "placeholder used", which
        suppressed the ARGUMENTS footer, so the typed instruction vanished too.
        """
        assert expand(body, "") == body

    def test_typed_input_survives_a_body_full_of_dollar_amounts(self):
        out = expand("Flag anything above $100 per run.", "pruefe Kapitel 3")
        assert "$100" in out
        assert out.endswith("ARGUMENTS: pruefe Kapitel 3")

    def test_single_digit_placeholders_still_work_next_to_prose(self):
        assert expand("copy $1 -> $2 (max $500)", "alt neu") == "copy alt -> neu (max $500)"

    def test_windows_paths_survive_positional_substitution(self):
        r"""shlex's POSIX mode ate the backslashes: C:\Users\me -> C:Usersme."""
        assert expand("Read $1", r"C:\Users\enric\notes.md") == r"Read C:\Users\enric\notes.md"

    def test_quoted_group_still_arrives_unquoted(self):
        assert expand("[$1]", '"zwei woerter" rest') == "[zwei woerter]"

    def test_huge_arguments_do_not_go_quadratic(self):
        """shlex is a char-at-a-time state machine; 900 KB blocked the loop.

        Above the limit the split is plain whitespace — nobody addresses ``$3``
        with a megabyte, and ``$ARGUMENTS`` still receives everything.
        """
        import time

        huge = ('"' + "x" * 40 + '" ') * 20000  # ~840 KB, quoting-heavy
        started = time.monotonic()
        parts = split_arguments(huge)
        assert time.monotonic() - started < 1.0
        assert len(parts) > 1000
