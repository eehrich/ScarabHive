"""The shared slash-command layer: catalogue, parsing, skill invocation.

These pin the contract both surfaces depend on. The terminal chat used to own
this logic and the web UI had none; if the two ever drift apart again, it will
show up here first.
"""
from __future__ import annotations

import pytest

from agent_system.chat_commands import (
    BUILTIN_COMMANDS,
    PluginCommand,
    CLI,
    UNKNOWN_SERVER,
    WEB,
    commands_for,
    group_tools_by_server,
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
        # attach takes paths on the server's own disk -- meaningless in a
        # browser tab, which has the multipart upload instead.
        assert cli - web == {"exit", "attach"}


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

    def test_multiline_message_is_not_a_command(self):
        """parse_chat_command treats multiline input as a message; the filter
        must mirror that, or /history and /last hide real turns."""
        assert not looks_like_command("/new plan fuer die woche\nzweite zeile")


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


class TestPluginCommands:
    """Plugins are the third source in this namespace, between built-ins and
    skills. What matters here is precedence and the ambiguity rule -- who wins
    when two sources claim the same word."""

    @staticmethod
    def _command(plugin="context_engineer", name="compact", **kw):
        return PluginCommand(plugin=plugin, name=name, summary="", tool=f"{plugin}_{name}", **kw)

    def test_a_declared_command_resolves(self):
        commands = [self._command()]
        result = resolve("/compact", (), commands)
        assert (result.kind, result.name) == ("plugin", "context_engineer:compact")

    def test_arguments_are_the_rest_of_the_line(self):
        commands = [self._command(name="find", argument="query")]
        assert resolve("/find  two words ", (), commands).payload == "two words"

    def test_the_qualified_spelling_resolves(self):
        commands = [self._command()]
        result = resolve("/context_engineer:compact now", (), commands)
        assert result.kind == "plugin"
        assert result.name == "context_engineer:compact"
        assert result.payload == "now"

    def test_builtins_are_not_shadowed(self):
        """A plugin declaring 'help' must not take over the way out."""
        result = resolve("/help", (), [self._command(name="help")])
        assert (result.kind, result.name) == ("command", "help")

    def test_plugin_commands_beat_skills(self):
        result = resolve("/compact", ["compact"], [self._command()])
        assert result.kind == "plugin"

    def test_a_skill_still_resolves_next_to_plugin_commands(self):
        """Counter-check for the precedence test above: the skill branch must
        still be reachable, or the assertion there would pass for free."""
        result = resolve("/writer x", ["writer"], [self._command()])
        assert (result.kind, result.name, result.payload) == ("skill", "writer", "x")

    def test_an_ambiguous_bare_name_does_not_pick_one(self):
        """Two plugins, same command name: running the first by registration
        order would silently do the wrong thing."""
        commands = [self._command(plugin="a"), self._command(plugin="b")]
        assert resolve("/compact", (), commands).kind == "unknown"

    def test_an_ambiguous_name_stays_reachable_qualified(self):
        commands = [self._command(plugin="a"), self._command(plugin="b")]
        result = resolve("/b:compact", (), commands)
        assert (result.kind, result.name) == ("plugin", "b:compact")

    def test_an_unknown_command_is_still_unknown(self):
        assert resolve("/nope", (), [self._command()]).kind == "unknown"

    def test_case_is_ignored(self):
        assert resolve("/COMPACT", (), [self._command()]).kind == "plugin"


class TestColonWordsStayMessages:
    """The qualified spelling `/plugin:name` must not make ordinary prose look
    like a command.

    The first attempt widened the command-word regex to allow a colon. That
    regex ALSO filters stored messages (`looks_like_command`), so
    "/todo:milch kaufen" became an "unknown command" at the prompt, was
    rejected on the web surface where plugin commands do not even exist, and
    vanished retroactively from /history and /last. Now a colon word is claimed
    only when it really names a declared command.
    """

    @pytest.mark.parametrize("line", [
        "/todo:kaufen milch",
        "/note:morgen einkaufen",
        "/ref:ABC-123 bitte pruefen",
        "/note: still open",
        "/note:",
        "/etc/nginx/nginx.conf",
    ])
    def test_a_colon_word_is_an_ordinary_message(self, line):
        assert parse_chat_command(line)[0] is None
        assert resolve(line).kind == "message"

    @pytest.mark.parametrize("line", ["/todo:kaufen milch", "/note:morgen"])
    def test_and_stays_visible_in_history(self, line):
        """looks_like_command filters stored messages out of /history and
        /last. Whatever it calls a command disappears from the transcript."""
        assert looks_like_command(line) is False

    def test_an_undeclared_qualified_word_is_a_message(self):
        """Nothing declares it, so it is prose -- not "unknown command"."""
        assert resolve("/plug:cmd", (), []).kind == "message"

    def test_a_declared_qualified_word_is_claimed(self):
        command = PluginCommand("plug", "cmd", "", "plug_cmd")
        assert resolve("/plug:cmd", (), [command]).kind == "plugin"

    def test_the_escape_still_wins_over_a_declared_command(self):
        command = PluginCommand("plug", "cmd", "", "plug_cmd")
        result = resolve("//plug:cmd", (), [command])
        assert (result.kind, result.payload) == ("message", "/plug:cmd")


class TestBuiltinsCannotBeAdvertisedAway:
    def test_a_plugin_command_named_like_a_builtin_needs_qualifying(self):
        """/history is a built-in; a plugin command of that name is reachable
        only as /plugin:history, and the help must say so."""
        from agent_system.plugin_commands import spellings

        assert spellings([PluginCommand("mem", "history", "", "mem_history")]) == [
            "mem:history"]


class TestTheDoubleSlashEscape:
    """`//text` sends a message that starts with a command word.

    Two halves were wrong. The terminal never applied the escape at all -- it
    forwarded the raw line while the web surface stripped it, so the same
    keystrokes meant different things on the two surfaces. And the strip was
    unconditional, which quietly ate a slash from any pasted line beginning
    with `//`.
    """

    @pytest.mark.parametrize("line,expected", [
        ("//help", "/help"),
        ("//compact now", "/compact now"),
        ("//plug:cmd", "/plug:cmd"),
        ("//unbekannt bitte", "/unbekannt bitte"),
    ])
    def test_a_command_word_is_unescaped(self, line, expected):
        assert parse_chat_command(line) == (None, expected)
        assert resolve(line).payload == expected

    @pytest.mark.parametrize("line", [
        "// TODO: das noch fixen",
        "//192.168.1.1/share",
        "// eslint-disable-next-line",
        "//",
    ])
    def test_anything_else_keeps_both_slashes(self, line):
        """No escape was needed, so none is applied -- eating a slash here
        corrupts the message the person actually sent."""
        assert parse_chat_command(line) == (None, line)

    def test_an_escaped_message_stays_visible_in_history(self):
        """It is a message, not a command. Hiding it left the agent's answer
        in /history with no question above it."""
        assert looks_like_command("/unbekannt bitte") is False

    def test_a_real_command_is_still_filtered_out_of_history(self):
        """Counter-check: the leftovers this filter exists for must still go."""
        assert looks_like_command("/sessions") is True
        assert looks_like_command("/hist") is True


class TestToolGrouping:
    """``/tools`` groups by the server that provides a tool -- in the terminal
    and in the browser, from one rule."""

    def test_the_longest_server_name_wins(self):
        """``coder_file_ops_read_file`` belongs to ``coder_file_ops``. Under
        the shorter match it lands beside a ``coder`` it never came from."""
        groups = dict(group_tools_by_server(
            [{"name": "coder_file_ops_read_file"}, {"name": "coder_run"}],
            ["coder", "coder_file_ops"]))

        assert [t["name"] for t in groups["coder_file_ops"]] == ["coder_file_ops_read_file"]
        assert [t["name"] for t in groups["coder"]] == ["coder_run"]

    def test_a_single_tool_server_carries_its_bare_name(self):
        """sequential_thinking and todo ARE their tool -- no underscore to
        match on, so equality has to count."""
        groups = dict(group_tools_by_server([{"name": "todo"}], ["todo"]))

        assert [t["name"] for t in groups["todo"]] == ["todo"]

    def test_no_group_is_invented_that_no_registry_holds(self):
        """The bug the rule replaced: splitting the name on "_" filed
        ``sequential_thinking``'s tool under a ``sequential`` that was never
        registered. Only a name the registry really has may become a group;
        anything else is named as unclaimed."""
        groups = dict(group_tools_by_server(
            [{"name": "sequential_thinking"}], ["file_ops"]))

        assert list(groups) == [UNKNOWN_SERVER]
        assert [t["name"] for t in groups[UNKNOWN_SERVER]] == ["sequential_thinking"]

    def test_the_order_is_the_display_order(self):
        """Both surfaces print what this returns, so the sorting belongs here
        rather than twice in two languages."""
        groups = group_tools_by_server(
            [{"name": "zeta_one"}, {"name": "alpha_one"}, {"name": "zeta_two"}],
            ["zeta", "alpha"])

        assert [server for server, _ in groups] == ["alpha", "zeta"]
        assert [t["name"] for t in dict(groups)["zeta"]] == ["zeta_one", "zeta_two"]
