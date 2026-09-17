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
    apply_vars,
    commands_for,
    parse_vars,
    group_tools_by_server,
    needs_escape,
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
        # browser tab, which has the multipart upload instead. model and agent
        # switch what the running chat talks to, which the browser does in its
        # own selectors; rename belongs on its session list, not in the
        # message box.
        # undo/retry/export rewrite or read what the AGENT holds in memory;
        # the browser reloads the session from disk on every message, and
        # export would write on the server's disk, not the viewer's.
        assert cli - web == {"exit", "attach", "model", "agent", "rename",
                             "undo", "retry", "export"}


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

    The first attempt widened the command-word regex to allow a colon, so
    "/todo:milch kaufen" became an "unknown command" at the prompt and was
    rejected on the web surface where plugin commands do not even exist. Now a
    colon word is claimed only when it really names a declared command.
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

    def test_a_multiline_message_never_runs_the_qualified_command(self):
        """Multi-line input is a message whichever spelling opens it -- the
        bare name already was, the qualified one ran the plugin with the
        whole paragraph as its argument."""
        command = PluginCommand(plugin="context_engineer", name="compact",
                                summary="", tool="context_engineer_compact",
                                argument="keep")
        for line in ("/context_engineer:compact keep\nmore",
                     "/compact keep\nmore"):
            assert resolve(line, (), [command]).kind == "message", line

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


class TestTheEscapeIsTheInverseOfTheUnescape:
    """``needs_escape`` decides what the input history hands back.

    Its only correctness condition is that it is the exact inverse of the
    unescape above: a message the history escapes must be unescaped again on
    the way in, or the agent receives one slash more than it was sent -- and
    one it does NOT escape must not be re-read as a command.
    """

    @pytest.mark.parametrize("stored", [
        "/help", "/compact now", "/plug:cmd", "/unbekannt bitte", "/?",
        "/etc",
    ])
    def test_an_escaped_message_round_trips_byte_for_byte(self, stored):
        assert needs_escape(stored) is True
        assert parse_chat_command("/" + stored) == (None, stored)

    @pytest.mark.parametrize("stored", [
        "/3d drucker bauen",        # head is not command-word shaped
        "/2fa aktivieren",
        "/-x",
        "/",
        "/etc/nginx/nginx.conf lesen",
        "nur text",
        "/erste zeile\nzweite",     # multi-line is always a message
    ])
    def test_what_needs_no_escape_already_survives_as_it_stands(self, stored):
        # These reach the agent unchanged when handed back RAW, so escaping
        # them would be the corruption: the "//" strip does not fire for a
        # head like "/3d", and the extra slash would go through.
        assert needs_escape(stored) is False
        assert parse_chat_command(stored) == (None, stored)


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


class TestVars:
    """The ``/vars`` grammar, which both surfaces read through this parser.

    Three of these pin bugs the first implementation really had: a quoted
    value fell apart, a Windows path lost its backslashes, and a line with
    errors reported itself as a harmless query.
    """

    def test_vars_is_offered_on_both_surfaces(self):
        for surface in (CLI, WEB):
            assert "vars" in {c.name for c in commands_for(surface)}

    def test_a_bare_line_only_asks(self):
        request = parse_vars("")
        assert request.is_query
        assert not request.assign and not request.unset and not request.clear

    def test_assignments_read_like_the_command_line_flag(self):
        request = parse_vars("lang=German user_name=Alice")
        assert dict(request.assign) == {"lang": "German", "user_name": "Alice"}
        assert not request.errors

    def test_a_quoted_value_keeps_its_spaces(self):
        """shlex in non-POSIX mode split this into 'greeting="hallo' + 'welt"'."""
        request = parse_vars('greeting="hallo welt" lang=de')
        assert dict(request.assign) == {"greeting": "hallo welt", "lang": "de"}
        assert not request.errors

    def test_a_windows_path_keeps_its_backslashes(self):
        r"""POSIX shlex would eat these and store C:tmpx.

        The repo is Windows-primary, so a silently mangled path is the more
        likely damage of the two -- and the escape character is switched off
        precisely to prevent it.
        """
        request = parse_vars(r"path=C:\tmp\x")
        assert dict(request.assign) == {"path": r"C:\tmp\x"}

    def test_an_empty_value_is_a_value_not_a_removal(self):
        """KEY= sets the empty string; removal has its own word."""
        request = parse_vars("note=")
        assert dict(request.assign) == {"note": ""}
        assert not request.unset

    def test_an_unbalanced_quote_is_refused_not_stored(self):
        request = parse_vars('broken="unbalanced')
        assert request.errors
        assert not request.assign

    @pytest.mark.parametrize("bad", ["8ball=x", "my-var=1", "=lonely"])
    def test_a_name_jinja_cannot_address_is_refused(self, bad):
        """`{{ my-var }}` is a subtraction: accepting the name would store a
        variable that never renders."""
        request = parse_vars(bad)
        assert request.errors, f"{bad} should not be accepted"
        assert not request.assign

    def test_a_word_without_an_equals_sign_is_refused(self):
        request = parse_vars("oops")
        assert request.errors and not request.assign

    def test_clear_with_arguments_clears_nothing(self):
        """Destructive and ambiguous: refuse instead of guessing."""
        request = parse_vars("clear junk")
        assert request.clear is False
        assert request.errors

    def test_clear_on_its_own_clears(self):
        assert parse_vars("clear").clear is True

    def test_unset_needs_a_name(self):
        request = parse_vars("unset")
        assert request.errors and not request.unset

    def test_a_line_with_errors_is_not_a_query(self):
        """A caller that checks is_query first must not answer a typo with a
        listing, as though nothing had been wrong with the line."""
        assert parse_vars("oops").is_query is False
        assert parse_vars("clear junk").is_query is False

    def test_a_hash_is_part_of_the_value_not_a_comment(self):
        """shlex treats '#' as a comment by default. The first version cleared
        `escape` but not `commenters`, so `color=#ff0000` stored the EMPTY
        string with no error, and everything after a '#' was dropped. Hex
        colours, URL fragments and issue numbers are ordinary values, and
        `--vars` (a plain partition on argv) keeps them."""
        request = parse_vars("color=#ff0000 issue=#42")
        assert dict(request.assign) == {"color": "#ff0000", "issue": "#42"}
        assert not request.errors

    def test_a_hash_does_not_swallow_the_rest_of_the_line(self):
        request = parse_vars("a=1 b=#x c=3")
        assert dict(request.assign) == {"a": "1", "b": "#x", "c": "3"}

    def test_only_the_first_equals_separates(self):
        """Otherwise a URL or a base64 value cannot be stored: the key would
        be everything up to the LAST '=' and get refused as a bad name."""
        request = parse_vars("url=http://x/?a=b&c=d")
        assert dict(request.assign) == {"url": "http://x/?a=b&c=d"}

    def test_unset_takes_more_than_one_name(self):
        assert parse_vars("unset a b").unset == ("a", "b")

    def test_apply_refuses_a_request_that_carries_errors(self):
        """`unset a 8b` is refused as a line -- but the valid half must not be
        applied anyway. Both surfaces check errors first, so this pins the
        guard in the shared function where a third caller cannot miss it."""
        assert apply_vars({"a": "1"}, parse_vars("unset a 8b")) == {"a": "1"}
        assert apply_vars({"a": "1"}, parse_vars("clear junk")) == {"a": "1"}

    def test_apply_sets_unsets_and_clears(self):
        current = {"a": "1", "b": "2"}
        assert apply_vars(current, parse_vars("b=3 c=4")) == {"a": "1", "b": "3", "c": "4"}
        assert apply_vars(current, parse_vars("unset a")) == {"b": "2"}
        assert apply_vars(current, parse_vars("clear")) == {}

    def test_apply_leaves_the_input_alone(self):
        """The tracker hands out its INTERNAL dict for a known session and a
        throwaway {} for an unknown one -- an in-place edit would work for the
        first case and silently do nothing for the second."""
        current = {"a": "1"}
        result = apply_vars(current, parse_vars("b=2"))
        assert current == {"a": "1"}
        assert result is not current

    def test_unsetting_something_absent_is_not_an_error(self):
        assert apply_vars({"a": "1"}, parse_vars("unset gone")) == {"a": "1"}
