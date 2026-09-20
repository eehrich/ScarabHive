"""Plugin-declared slash commands: what gets listed, and what runs.

Two properties carry the whole feature. A command must never be OFFERED when
the agent may not call its tool -- help that promises more than the agent has
is worse than no help. And a command must never reach the tool by any path
other than ``dispatch_tool_call``, which is what supplies authorization and the
runtime params (``_session_id``, ``_agent``) the tools rely on.
"""
from __future__ import annotations

import pytest

from agent_system.chat_commands import PluginCommand
from agent_system.plugin_commands import (
    collect_plugin_commands,
    format_command_result,
    help_lines,
    run_plugin_command,
    spellings,
)

COMPACT = {"name": "compact", "description": "shrink it",
           "tool": "context_engineer_compact"}


class _Plugin:
    """A plugin server, as the registry hands it out."""

    def __init__(self, schema, raises=False):
        self._schema = schema
        self._raises = raises

    def get_schema_data(self):
        if self._raises:
            raise RuntimeError("schema.yaml is broken")
        return self._schema


class _Registry:
    def __init__(self, servers):
        self._servers = servers

    def list(self):
        return list(self._servers)

    def get(self, name):
        return self._servers[name]


class _Agent:
    """An agent whose allowlist is a plain set of tool names."""

    def __init__(self, servers, allowed=("context_engineer_compact",), blocked=()):
        self.registry = _Registry(servers)
        self._allowed, self._blocked = set(allowed), set(blocked)
        self.calls = []
        self.denial_calls = []
        self.result = {"status": "success", "tokens_saved": 4200}

    def _resolve_flat_tool_name(self, tool):
        """Longest registered prefix wins -- what the real agent does."""
        for name in sorted(self.registry.list(), key=len, reverse=True):
            if tool == name or tool.startswith(name + "_"):
                return self.registry.get(name), name
        return None, None

    def tool_dispatch_denial(self, tool, server_name):
        self.denial_calls.append((tool, server_name))
        if tool in self._blocked:
            return f"Tool '{tool}' is blocked for this agent."
        if "*" not in self._allowed and tool not in self._allowed:
            return f"Tool '{tool}' is not in this agent's allowed tools."
        return None

    async def dispatch_tool_call(self, tool, params, *, session_id=None, user_id=None):
        self.calls.append((tool, params, session_id, user_id))
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


def _agent(commands=(COMPACT,), name="context_engineer", **kw):
    return _Agent({name: _Plugin({"commands": list(commands)})}, **kw)


class TestCollection:
    def test_a_declared_command_is_collected(self):
        (command,) = collect_plugin_commands(_agent())
        assert (command.plugin, command.name, command.tool) == (
            "context_engineer", "compact", "context_engineer_compact")
        assert command.summary == "shrink it"

    def test_the_plugin_instance_name_is_the_namespace(self):
        """A plugin mounted under a second name yields its own command --
        schema.yaml writes `{{ name }}_compact`, already rendered here.

        Registered b-before-a on purpose: with the dict already in sorted
        order, the sorting in collect_plugin_commands measures nothing."""
        agent = _Agent(
            {"ce_b": _Plugin({"commands": [{**COMPACT, "tool": "ce_b_compact"}]}),
             "ce_a": _Plugin({"commands": [{**COMPACT, "tool": "ce_a_compact"}]})},
            allowed=("ce_a_compact", "ce_b_compact"))
        assert [c.qualified for c in collect_plugin_commands(agent)] == [
            "ce_a:compact", "ce_b:compact"]

    def test_authorization_asks_about_the_server_that_owns_the_tool(self):
        """Not about the plugin that DECLARES the command. dispatch resolves
        the owning server, and two answers to "which server" let the listing
        and the dispatch disagree -- one lists what the other refuses."""
        agent = _Agent(
            {"ce": _Plugin({"commands": [{**COMPACT, "tool": "terminal_exec"}]}),
             "terminal": _Plugin({"tools": []})},
            allowed=("terminal_exec",))
        collect_plugin_commands(agent)
        assert agent.denial_calls == [("terminal_exec", "terminal")]

    def test_a_tool_no_server_provides_is_not_listed(self):
        """Otherwise the command is dropped by authorization without a word and
        the plugin author never learns that `tool:` names nothing."""
        agent = _agent(commands=({**COMPACT, "tool": "nothing_provides_this"},),
                       allowed=("*",))
        assert collect_plugin_commands(agent) == []

    def test_a_command_the_agent_may_not_run_is_not_listed(self):
        assert collect_plugin_commands(_agent(allowed=())) == []

    def test_a_blocked_command_is_not_listed(self):
        assert collect_plugin_commands(_agent(blocked=("context_engineer_compact",))) == []

    def test_an_agent_that_cannot_authorize_gets_nothing(self):
        """Deny on doubt: without the predicate nothing here can establish that
        the agent may run the tool, and guessing 'yes' would hand a restricted
        agent a shortcut its allowlist denies."""
        class _NoAuth:
            registry = _Registry({"context_engineer": _Plugin({"commands": [COMPACT]})})

        assert collect_plugin_commands(_NoAuth()) == []

    def test_a_plugin_without_commands_is_skipped(self):
        agent = _Agent({"plain": _Plugin({"tools": []})})
        assert collect_plugin_commands(agent) == []

    def test_a_broken_schema_does_not_hide_the_other_plugins(self):
        agent = _Agent({"broken": _Plugin(None, raises=True),
                        "context_engineer": _Plugin({"commands": [COMPACT]})})
        assert [c.name for c in collect_plugin_commands(agent)] == ["compact"]

    @pytest.mark.parametrize("entry", [
        {"description": "no name", "tool": "context_engineer_compact"},
        {"name": "compact", "description": "no tool"},
        {"name": "", "tool": "context_engineer_compact"},
        "not even a mapping",
    ])
    def test_an_unusable_entry_is_skipped_not_fatal(self, entry):
        """Allow-all on purpose: with a restricted allowlist the authorization
        filter would drop the broken entry too, and this would pass without the
        schema check ever running (measured -- it did)."""
        agent = _agent(commands=(entry, COMPACT), allowed=("*",))
        assert [c.name for c in collect_plugin_commands(agent)] == ["compact"]

    def test_a_missing_tool_is_named_in_the_log(self, caplog):
        """This guard's value is the MESSAGE, not the skip: without it the
        entry is dropped anyway (no server provides ''), but the author is
        told to look for a missing server instead of a missing `tool:`.
        Measured -- the skip alone survived the mutation."""
        agent = _agent(commands=({"name": "compact", "description": "x"},),
                       allowed=("*",))
        with caplog.at_level("WARNING"):
            collect_plugin_commands(agent)
        assert any("needs 'name' and 'tool'" in r.getMessage()
                   for r in caplog.records), [r.getMessage() for r in caplog.records]

    @pytest.mark.parametrize("name", ["compact now", "compact!", "2fast", "ce:compact"])
    def test_a_name_nobody_can_type_is_rejected(self, name):
        """`/compact now` never parses as a command word -- it would sit in the
        help and answer nothing."""
        agent = _agent(commands=({**COMPACT, "name": name}, COMPACT), allowed=("*",))
        assert [c.name for c in collect_plugin_commands(agent)] == ["compact"]

    @pytest.mark.parametrize("argument", [
        "request_id", "requestId", "requestid", "session_id", "sessionId",
        "agent_name", "_session_id", "_agent"])
    def test_a_reserved_argument_is_rejected(self, argument):
        """Typed text must never become a runtime param. The `_` ones are
        stripped by dispatch, but `request_id` is read AFTER that strip
        (tools/base.py) and becomes _request_id plus the status routing key."""
        agent = _agent(commands=({**COMPACT, "argument": argument},), allowed=("*",))
        assert collect_plugin_commands(agent) == []

    @pytest.mark.parametrize("argument", ["query", "status", "user_id", "path"])
    def test_an_ordinary_argument_still_works(self, argument):
        """Counter-check with the near misses: `status` and `user_id` are read
        as PLAIN tool parameters (todo, lessons_learned, writer_audio), not as
        runtime aliases. A denylist that swallows them blocks `/todo open`."""
        agent = _agent(commands=({**COMPACT, "argument": argument},), allowed=("*",))
        assert collect_plugin_commands(agent)[0].argument == argument

    def test_commands_must_be_a_list(self):
        agent = _Agent({"context_engineer": _Plugin({"commands": {"name": "compact"}})})
        assert collect_plugin_commands(agent) == []

    def test_no_registry_is_not_a_crash(self):
        assert collect_plugin_commands(object()) == []


class TestExecution:
    @pytest.mark.asyncio
    async def test_the_tool_runs_through_dispatch_with_the_session(self):
        agent = _agent()
        (command,) = collect_plugin_commands(agent)
        out = await run_plugin_command(agent, command, session_id="s1", user_id="u1")
        assert agent.calls == [("context_engineer_compact", {}, "s1", "u1")]
        assert "4200" in out

    @pytest.mark.asyncio
    async def test_the_argument_lands_in_the_declared_parameter(self):
        agent = _agent(commands=({**COMPACT, "argument": "query"},))
        (command,) = collect_plugin_commands(agent)
        await run_plugin_command(agent, command, "  budget report ")
        assert agent.calls[0][1] == {"query": "budget report"}

    @pytest.mark.asyncio
    async def test_arguments_to_a_command_that_takes_none_are_refused(self):
        """Dropping them silently would run a compaction the person thought
        they had narrowed down."""
        agent = _agent()
        (command,) = collect_plugin_commands(agent)
        out = await run_plugin_command(agent, command, "only the last hour")
        assert agent.calls == []
        assert "takes no arguments" in out

    @pytest.mark.asyncio
    async def test_an_empty_argument_is_not_passed_as_empty_string(self):
        agent = _agent(commands=({**COMPACT, "argument": "query"},))
        (command,) = collect_plugin_commands(agent)
        await run_plugin_command(agent, command, "   ")
        assert agent.calls[0][1] == {}

    @pytest.mark.asyncio
    async def test_a_failing_tool_is_reported_not_raised(self):
        """The REPL survives its plugins: one throwing tool must not end the
        session the person is in."""
        agent = _agent()
        agent.result = RuntimeError("no session context")
        (command,) = collect_plugin_commands(agent)
        out = await run_plugin_command(agent, command)
        assert "failed" in out and "no session context" in out


class TestResultFormatting:
    def test_an_error_status_is_shown_as_an_error(self):
        assert format_command_result(
            {"status": "error", "error": "no session"}) == "error: no session"

    def test_the_plugins_own_message_wins(self):
        assert format_command_result(
            {"status": "success", "message": "Nothing to compact"}) == "Nothing to compact"

    def test_fields_are_listed_without_the_success_noise(self):
        out = format_command_result(
            {"status": "success", "tokens_saved": 12, "_internal": "x"})
        assert out == "tokens_saved: 12"

    def test_a_result_with_nothing_to_say(self):
        assert format_command_result({"status": "success"}) == "done"

    def test_a_plain_string_passes_through(self):
        assert format_command_result("compacted") == "compacted"

    def test_a_list_of_records_gets_one_line_each(self):
        """Plugin tools answer for the MODEL. /subagents and /mcp both return
        a list of dicts, and printed with str() that is one long Python repr
        -- the person asked to SEE their sub-agents."""
        out = format_command_result({"status": "success", "count": 2, "instances": [
            {"instance_id": "sub-7", "agent_type": "coder", "status": "running"},
            {"instance_id": "sub-8", "agent_type": "writer", "status": "done"}]})

        lines = out.splitlines()
        # Not by index: the order follows the plugin's own dict, which is its
        # business. What matters is that the rows follow their header.
        head = lines.index("instances (2):")
        assert "instance_id=sub-7" in lines[head + 1]
        assert "agent_type=coder" in lines[head + 1]
        assert "instance_id=sub-8" in lines[head + 2]
        assert "count: 2" in out
        assert "[{" not in out, out

    def test_empty_fields_of_a_record_are_left_out(self):
        """A row of twelve fields, eight of them None, is not a listing."""
        out = format_command_result({"rows": [
            {"name": "figma", "connected": False, "error": None, "note": ""}]})

        assert "error" not in out and "note" not in out
        assert "name=figma" in out

    def test_a_long_value_is_cut_not_wrapped(self):
        out = format_command_result({"rows": [{"task": "x" * 400}]})

        assert len(out.splitlines()) == 2
        assert "…" in out

    def test_a_long_listing_says_how_much_it_left_out(self):
        out = format_command_result({"rows": [{"n": i} for i in range(25)]})

        assert "... 5 more" in out
        assert len(out.splitlines()) == 22   # header + 20 rows + the note

    def test_a_list_that_is_not_records_is_left_alone(self):
        """Only a list of dicts is a listing; a list of ids is a value."""
        out = format_command_result({"ids": ["a", "b"]})

        assert out == "ids: ['a', 'b']"


class TestSpellingsAndHelp:
    def test_a_unique_name_is_spelled_bare(self):
        assert spellings([PluginCommand("ce", "compact", "", "t")]) == ["compact"]

    def test_an_ambiguous_name_is_spelled_qualified(self):
        """The help must show the only spelling that actually resolves."""
        commands = [PluginCommand("a", "compact", "", "t"),
                    PluginCommand("b", "compact", "", "t")]
        assert spellings(commands) == ["a:compact", "b:compact"]

    def test_help_is_empty_without_commands(self):
        assert help_lines([]) == []

    def test_help_is_a_headed_block_of_command_then_summary(self):
        """Asserted as whole lines: substring checks passed with the header
        gone, the column gone, and command and summary swapped (measured)."""
        lines = help_lines([
            PluginCommand("ce", "find", "search the archive", "t",
                          argument="query", argument_hint="<words>"),
            PluginCommand("ce", "compact", "shrink it", "t"),
        ])
        assert lines == [
            "",
            "Plugin commands:",
            "  /find <words>   search the archive",
            "  /compact        shrink it",
        ]


# ---------------------------------------------------------------------------
# The wiring in the chat REPL. Measured to be worth it: with the module-level
# functions tested alone, five mutations of the wiring survived -- including
# deleting the dispatch branch, which silently turns "/compact" into a PAID
# LLM turn.
# ---------------------------------------------------------------------------

import asyncio
import base64
import builtins
from types import SimpleNamespace

from agent_system.cli_utils.chat import (
    _ChatContext,
    _help_text,
    _run_plugin_command,
    run_chat_loop,
)


def _ctx(agent):
    return _ChatContext(
        agent=agent, entry_name="a", session_service=None, session_user="u",
        session_id="s1", was_new_session=False, llm_profile="p",
        llm_override=None, llm_profile_info=None, show_status=False)


class TestHelpText:
    def test_plugin_commands_appear_in_help(self):
        text = _help_text((), [PluginCommand("ce", "compact", "shrink it", "t")])
        assert "Plugin commands:" in text
        assert "/compact" in text and "shrink it" in text

    def test_help_without_plugin_commands_has_no_empty_section(self):
        assert "Plugin commands:" not in _help_text(())


class TestRunPluginCommandOnTheLoop:
    def test_the_result_is_printed(self, capsys):
        loop = asyncio.new_event_loop()
        try:
            agent = _agent()
            (command,) = collect_plugin_commands(agent)
            _run_plugin_command(loop, _ctx(agent), [command], command.qualified, "")
        finally:
            loop.close()
        assert "4200" in capsys.readouterr().out

    def test_ctrl_c_cancels_the_command_instead_of_leaving_it_pending(self, capsys):
        """run_until_complete leaves the future PENDING on KeyboardInterrupt.
        An uncancelled compaction finishes inside the NEXT turn and rewrites the
        message list that turn is reading -- after the person was told it was
        interrupted."""
        loop = asyncio.new_event_loop()
        finished = []

        class _Slow(_Agent):
            async def dispatch_tool_call(self, tool, params, **kw):
                await asyncio.sleep(5)
                finished.append(tool)
                return {"status": "success"}

        try:
            agent = _Slow({"context_engineer": _Plugin({"commands": [COMPACT]})})
            (command,) = collect_plugin_commands(agent)
            real = loop.run_until_complete
            seen = []

            def interrupt_the_first_wait(awaitable):
                seen.append(awaitable)
                if len(seen) == 1:
                    raise KeyboardInterrupt
                return real(awaitable)

            loop.run_until_complete = interrupt_the_first_wait
            _run_plugin_command(loop, _ctx(agent), [command], command.qualified, "")
            loop.run_until_complete = real

            assert seen[0].cancelled(), "the command task was left on the loop"
            assert finished == [], "the cancelled command ran to completion anyway"
            assert not [t for t in asyncio.all_tasks(loop) if not t.done()]
        finally:
            loop.close()
        assert "cancelled" in capsys.readouterr().err


def _drive_repl(monkeypatch, lines, agent, loop=None, seen_loops=None):
    """Run the real run_chat_loop over *lines*, return the tasks that became
    LLM turns. _execute_turn is stubbed because it is the part that spends
    money -- which is exactly what several of these tests assert about.

    ``loop``/``seen_loops``: pass a loop through to run_chat_loop and collect
    the loop each turn actually ran on -- the seam the borrowed-loop tests
    need."""
    turns = []

    def _fake_turn(loop, ctx, task, renderer, editor=None):
        if seen_loops is not None:
            seen_loops.append(loop)
        turns.append(task)
        return {}

    monkeypatch.setattr("agent_system.cli_utils.chat._execute_turn", _fake_turn)
    monkeypatch.setattr("agent_system.cli_utils.chat._available_skills",
                        lambda ctx: [])
    fed = iter(lines)

    def fake_input(prompt=""):
        try:
            return next(fed)
        except StopIteration:
            raise EOFError

    monkeypatch.setattr(builtins, "input", fake_input)
    # Feeding builtins.input is only enough while the REPL reads through it.
    # On a real terminal it builds a prompt_toolkit editor instead and would
    # block on the console -- green under pytest (stdin is not a tty), hanging
    # under `pytest -s`. Force the input() path so the driver means the same
    # thing wherever it runs.
    monkeypatch.setattr("agent_system.cli_utils.chat._build_prompt_editor",
                        lambda seed, **kw: None)
    agent.llm = SimpleNamespace(model="m")
    run_chat_loop(
        agent=agent, entry_name="a", session_service=None, session_user="u",
        session_id="s1", was_new_session=False, llm_profile="p",
        show_status=False, loop=loop)
    return turns


class TestChatBorrowedLoop:
    """run_chat_loop must be able to run on a loop the caller keeps alive.

    The CLI hands over its shared bootstrap loop because the external MCP
    connections made during bootstrap only make progress while that loop
    runs. Until 2026-09-02 chat always built a private loop -- the bootstrap
    connections then reported connected=True (their task was parked, not
    done) and every external call ran into the submit timeout.
    """

    def test_turns_run_on_the_borrowed_loop_and_it_stays_open(self, monkeypatch):
        agent = _agent()
        loop = asyncio.new_event_loop()
        try:
            seen = []
            turns = _drive_repl(monkeypatch, ["hallo"], agent,
                                loop=loop, seen_loops=seen)
            assert turns == ["hallo"]
            assert seen and all(entry is loop for entry in seen), (
                "the turn ran on a different loop than the one handed in")
            assert not loop.is_closed(), (
                "chat tore down a loop it does not own -- the CLI still needs "
                "it for shutdown_tools/shutdown_batch_system")
        finally:
            if not loop.is_closed():
                loop.close()

    def test_without_a_loop_chat_still_cleans_up_its_own(self, monkeypatch):
        agent = _agent()
        seen = []
        _drive_repl(monkeypatch, ["hallo"], agent, seen_loops=seen)
        assert seen, "no turn ran"
        assert seen[0].is_closed(), (
            "the private loop must be closed on the way out -- that teardown "
            "is what keeps standalone use free of closed-pipe cascades")


class TestTheReplDispatchesToThePlugin:
    def _run(self, monkeypatch, lines, agent):
        return _drive_repl(monkeypatch, lines, agent)

    def test_a_plugin_command_runs_the_plugin_and_costs_no_turn(
            self, monkeypatch, capsys):
        agent = _agent()
        turns = self._run(monkeypatch, ["/compact"], agent)
        assert agent.calls, "the plugin tool was never dispatched"
        assert turns == [], "the command was sent to the LLM as a paid turn"
        assert "4200" in capsys.readouterr().out

    def test_an_ordinary_message_still_becomes_a_turn(self, monkeypatch):
        """Counter-check: without it the assertion above passes for an agent
        that simply never runs anything."""
        agent = _agent()
        assert self._run(monkeypatch, ["wie geht es dir"], agent) == [
            "wie geht es dir"]

    def test_the_command_is_listed_in_help(self, monkeypatch, capsys):
        agent = _agent()
        self._run(monkeypatch, ["/help"], agent)
        assert "Plugin commands:" in capsys.readouterr().out

    def test_a_typo_suggests_the_plugin_command(self, monkeypatch, capsys):
        agent = _agent()
        self._run(monkeypatch, ["/compa"], agent)
        out = capsys.readouterr().out
        assert "Unknown command" in out and "/compact" in out


class TestTheEscapeReachesTheAgent:
    """The terminal used to forward "//compact" raw while the web surface sent
    "/compact" -- the same keystrokes meant two different things."""

    def test_an_escaped_command_word_arrives_unescaped(self, monkeypatch):
        assert _drive_repl(monkeypatch, ["//compact"], _agent()) == ["/compact"]

    def test_a_pasted_comment_keeps_both_slashes(self, monkeypatch):
        assert _drive_repl(monkeypatch, ["// TODO: fix"], _agent()) == [
            "// TODO: fix"]

    def test_the_escape_does_not_run_the_plugin_command(self, monkeypatch):
        agent = _agent()
        _drive_repl(monkeypatch, ["//compact"], agent)
        assert agent.calls == []


class TestAttachCommand:
    """/attach queues files for the NEXT message and sends them as one
    multimodal ChatMessage -- the CLI-chat half of what agent-cli run's
    --attach and the HTTP API's multipart upload already do."""

    PNG = base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAIAAACQd1PeAAAADElEQVR4nGNg"
        "+M/AAAACAQEAqCJhkAAAAABJRU5ErkJggg==")

    def _png(self, tmp_path):
        target = tmp_path / "sketch.png"
        target.write_bytes(self.PNG)
        return target

    @staticmethod
    def _wav(tmp_path):
        """A real, minimal WAV -- the validator opens it, a stub would not do."""
        import wave

        target = tmp_path / "ton.wav"
        with wave.open(str(target), "wb") as handle:
            handle.setnchannels(1)
            handle.setsampwidth(2)
            handle.setframerate(8000)
            handle.writeframes(b"\x00\x00" * 8)
        return target

    def _text(self, tmp_path):
        target = tmp_path / "notiz.md"
        target.write_text("die geheime zahl ist 47", encoding="utf-8")
        return target

    def test_attached_file_rides_on_the_next_message_then_queue_clears(
            self, monkeypatch, tmp_path):
        agent = _agent()
        png = self._png(tmp_path)
        turns = _drive_repl(monkeypatch,
                            [f"/attach {png}", "build this", "and this"],
                            agent)
        from agent_system.llm.models import ChatMessage
        first, second = turns
        assert isinstance(first, ChatMessage), "attachment turn must be a ChatMessage"
        kinds = [getattr(c, "type", None) for c in first.content]
        # image_url is the builder's (OpenAI-style) spelling, same as the API path
        assert kinds == ["text", "image_url"]
        assert first.content[0].text == "build this"
        assert second == "and this", "queue must be empty again after sending"

    @pytest.mark.parametrize("maker,part_type,label", [
        ("_png", "image_url", "image"),
        ("_text", "text_file", "text"),
        ("_wav", "audio", "audio"),
    ])
    def test_every_kind_reaches_the_message_in_its_own_bucket(
            self, monkeypatch, tmp_path, capsys, maker, part_type, label):
        """One bucket per kind, and the confirmation names the kind.

        Only the image bucket was pinned: the audio and text arguments of the
        builder call could both be replaced by None and the whole suite stayed
        green (measured), and so could the kind in the /attach line.
        """
        agent = _agent()
        target = getattr(self, maker)(tmp_path)

        turns = _drive_repl(monkeypatch, [f"/attach {target}", "sieh dir das an"],
                            agent)

        assert f"[{label}]" in capsys.readouterr().out, (
            "the /attach confirmation names the wrong kind")
        (message,) = turns
        assert [getattr(c, "type", None) for c in message.content] == [
            "text", part_type]

    def test_a_missing_file_is_refused_at_attach_time(self, monkeypatch,
                                                      tmp_path, capsys):
        agent = _agent()
        turns = _drive_repl(monkeypatch,
                            [f"/attach {tmp_path / 'nope.png'}", "hi"], agent)
        assert turns == ["hi"], "nothing may ride along"
        assert "Not a file" in capsys.readouterr().out

    def test_a_tilde_name_that_is_no_home_does_not_kill_the_session(
            self, monkeypatch, capsys):
        """`~$notes.md` is the lock file Word leaves next to a document.

        Path.expanduser() RAISES for a ~name it cannot resolve, and nothing
        catches around the /attach dispatch -- the whole chat died on a file
        name. The condition (USERNAME != profile directory) is forced here,
        because on a machine where they match the bug is invisible.
        """
        monkeypatch.setenv("USERNAME", "jemand_ganz_anderes")
        agent = _agent()

        turns = _drive_repl(monkeypatch, ["/attach ~$notes.md", "hi"], agent)

        assert turns == ["hi"], "the session did not survive the attach"
        assert "Not a file" in capsys.readouterr().out

    def test_clear_empties_the_queue(self, monkeypatch, tmp_path):
        agent = _agent()
        png = self._png(tmp_path)
        turns = _drive_repl(monkeypatch,
                            [f"/attach {png}", "/attach clear", "hi"], agent)
        assert turns == ["hi"]

    def test_capability_refusal_blocks_the_send_and_keeps_the_queue(
            self, monkeypatch, tmp_path, capsys):
        """A text-only model must produce OUR message before any tokens are
        spent -- and the queue survives so the person can switch profiles
        without re-attaching (second try refuses again == still queued)."""
        agent = _agent()
        png = self._png(tmp_path)
        import agent_system.llm.capabilities as caps
        monkeypatch.setattr(caps, "ensure_model_supports",
                            lambda model, **k: "model m cannot take images")
        turns = _drive_repl(monkeypatch,
                            [f"/attach {png}", "try one", "try two"], agent)
        assert turns == [], "no turn may run against the refusal"
        out = capsys.readouterr().out
        assert out.count("Not sent:") == 2, "queue was dropped after first refusal"



class TestFixedParameters:
    """A tool with an `operation` of nine values is reachable as a command
    only if the command can say WHICH one. sub_agent_manager is exactly that
    shape, and without fixed params no chat could ever list its sub-agents.
    """

    LIST = {"name": "subagents", "description": "what runs",
            "tool": "context_engineer_compact", "params": {"operation": "list"}}

    @pytest.mark.asyncio
    async def test_they_reach_the_tool(self):
        agent = _agent(commands=(self.LIST,))
        (command,) = collect_plugin_commands(agent)

        await run_plugin_command(agent, command, "")

        assert agent.calls[0][1] == {"operation": "list"}

    @pytest.mark.asyncio
    async def test_the_typed_argument_joins_them(self):
        raw = dict(self.LIST, name="cancel", params={"operation": "cancel"},
                   argument="instance_id")
        agent = _agent(commands=(raw,))
        (command,) = collect_plugin_commands(agent)

        await run_plugin_command(agent, command, "sub-7")

        assert agent.calls[0][1] == {"operation": "cancel", "instance_id": "sub-7"}

    def test_params_that_are_not_a_mapping_drop_the_command(self):
        """Half a call would run the wrong operation -- worse than no command."""
        assert collect_plugin_commands(
            _agent(commands=(dict(self.LIST, params=["operation=list"]),))) == []

    def test_a_reserved_parameter_drops_the_command(self):
        assert collect_plugin_commands(
            _agent(commands=(dict(self.LIST, params={"session_id": "x"}),))) == []

    def test_fixing_the_parameter_the_argument_binds_drops_the_command(self):
        """One of the two would win silently, and which is a detail nobody
        should have to know."""
        assert collect_plugin_commands(_agent(commands=(
            dict(self.LIST, argument="operation", params={"operation": "list"}),))) == []

    def test_a_command_without_params_still_calls_with_none(self):
        """The whole catalogue predates this field."""
        agent = _agent()
        (command,) = collect_plugin_commands(agent)
        assert dict(command.params) == {}


class TestTheSubAgentManagerReally:
    """The schema on disk, not a fixture: the three lines that make sub-agents
    visible from a chat are worth an assertion of their own."""

    def _schema(self):
        import yaml

        from pathlib import Path

        path = (Path(__file__).parents[2] / "src" / "plugins" /
                "sub_agent_manager" / "schema.yaml")
        return yaml.safe_load(path.read_text(encoding="utf-8"))

    def test_it_declares_listing_and_cancelling(self):
        declared = {c["name"]: c for c in self._schema().get("commands") or []}

        assert set(declared) == {"subagents", "cancel"}
        assert declared["subagents"]["params"] == {"operation": "list"}
        assert declared["cancel"]["params"] == {"operation": "cancel"}
        assert declared["cancel"]["argument"] == "instance_id"

    def test_both_run_a_tool_the_plugin_really_has(self):
        """A command naming a tool nobody provides is dropped by
        authorization without a word -- it would simply never appear."""
        schema = self._schema()
        tools = {t["function"]["name"] for t in schema["tools"]}

        for command in schema["commands"]:
            assert command["tool"] in tools, command["name"]

    def test_the_operations_exist(self):
        """`operation` is an enum in the tool's own schema; a value outside it
        is a runtime error the person would only see as a failed command."""
        schema = self._schema()
        (tool,) = [t for t in schema["tools"]
                   if t["function"]["name"].endswith("_manage_sub_agent")]
        allowed = tool["function"]["parameters"]["properties"]["operation"]["enum"]

        for command in schema["commands"]:
            assert command["params"]["operation"] in allowed, command["name"]
