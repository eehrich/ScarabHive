"""Slash commands for the chat surfaces — one catalogue, one parser.

Every chat front end needs the same three answers: is this line a command, is
it a skill, or is it a message for the agent? The terminal chat had its own
copy of that logic, and the web UI had none at all, which is why `/help` and
`/sessions` existed in one place and not the other.

So the *knowledge* lives here — which commands exist, what they are called,
how a line is parsed, what a typo probably meant — while each surface keeps its
own *execution*, because listing sessions in a terminal and in a browser have
nothing in common but the name.

Skills join the same namespace: anything that is not a built-in is looked up in
the skill registry, so `/writer` runs the writer skill exactly the way
`/sessions` lists sessions. That mirrors how the ecosystem (Claude Code, Codex)
behaves, and it matches what the agent can already reach — `skills_list` never
filtered by agent, so restricting the human here would leave the person at the
keyboard with less reach than the model.

Plugins are the third source: a plugin declares `commands:` in its schema.yaml
and `/compact` runs context_engineer's compaction. They sit BETWEEN built-ins
and skills — a skill folder someone drops in must not shadow shipped code, and
neither may take over `/help`. See docs/plugin_commands_design.md.
"""
from __future__ import annotations

import difflib
import re
import shlex
from dataclasses import dataclass
from typing import Any, Iterable, Literal, Mapping, Optional, Sequence

#: Surfaces a command can appear on. The web UI has no terminal to leave, so
#: `/exit` is not offered there -- claiming otherwise would be a dead entry.
CLI = "cli"
WEB = "web"


@dataclass(frozen=True)
class ChatCommand:
    """One built-in command, as every surface should present it."""

    name: str
    aliases: tuple[str, ...]
    summary: str
    usage: str = ""
    surfaces: tuple[str, ...] = (CLI, WEB)

    @property
    def display(self) -> str:
        """What to show in help: the usage line, or all spellings."""
        return self.usage or ", ".join(self.aliases)


BUILTIN_COMMANDS: tuple[ChatCommand, ...] = (
    ChatCommand("exit", ("/exit", "/quit", "/q", "/bye"),
                "end the chat (Ctrl-D / Ctrl-Z+Enter work too)",
                usage="/exit, /quit, /q", surfaces=(CLI,)),
    ChatCommand("new", ("/new",), "start a fresh session (current one stays saved)"),
    ChatCommand("session", ("/session",), "show the current session and how to resume it"),
    ChatCommand("sessions", ("/sessions",), "list recent sessions (0 = all)",
                usage="/sessions [count]"),
    ChatCommand("resume", ("/resume",), "continue an earlier session", usage="/resume <id>"),
    # No "/var" alias, though /cost and /hist set that precedent: "/var" is
    # also the head of a path a sysadmin agent gets typed at, and the short
    # form buys nothing the long one does not already give.
    # `usage` stays short: both help renderers pad EVERY row to the longest
    # display string, so spelling the full grammar here widened the whole
    # table by 16 columns and pushed it past 80. The summary carries the rest.
    ChatCommand("vars", ("/vars",),
                "session variables: list, KEY=VALUE sets, 'unset KEY', 'clear'",
                usage="/vars [KEY=VALUE ...]"),
    # Terminal-only: the browser has no LLM picker to keep in step with, and a
    # command that silently disagrees with a selector is worse than no command.
    ChatCommand("model", ("/model", "/llm"),
                "LLM of this session: bare lists, a name switches",
                usage="/model [profile]", surfaces=(CLI,)),
    ChatCommand("tools", ("/tools",), "tools this agent really has (not what it claims)",
                usage="/tools [filter]"),
    ChatCommand("skills", ("/skills",), "skills you can run, and what this agent loads"),
    ChatCommand("costs", ("/costs", "/cost"), "session cost so far, including sub-agents"),
    ChatCommand("history", ("/history", "/hist"), "show the last n exchanges (default 6)",
                usage="/history [n]"),
    ChatCommand("last", ("/last",), "tool calls and results of the last turn, in full"),
    ChatCommand("attach", ("/attach",),
                "attach a file to the NEXT message (repeat for more; "
                "'/attach' lists, '/attach clear' empties)",
                usage="/attach [<path> | clear]", surfaces=(CLI,)),
    ChatCommand("help", ("/help", "/h", "/?"), "this help", usage="/help, /h"),
)

#: alias -> canonical name, built once from the catalogue above.
_COMMAND_ALIASES: dict[str, str] = {
    alias: command.name for command in BUILTIN_COMMANDS for alias in command.aliases
}

# A command word: a single "/name" token, no further slash, no dot. That is
# what separates a mistyped command from a path -- "/h" is a typo the user
# wants flagged, "/etc/nginx/nginx.conf" is ordinary input for a sysadmin
# agent and firing an LLM turn on either extreme is wrong.
_NAME = r"[A-Za-z][A-Za-z0-9_-]*"
_COMMAND_WORD = re.compile(r"^/[A-Za-z?][A-Za-z0-9_-]*$")
_COMMAND_NAME = re.compile(rf"^{_NAME}$")

# The qualified plugin spelling, "/context_engineer:compact". Deliberately NOT
# part of _COMMAND_WORD: that regex also filters stored messages
# (looks_like_command), and widening it turned "/todo:milch kaufen" into an
# "unknown command" -- on the web surface too, where plugin commands do not
# even exist. A colon word is claimed only when it really names a declared
# command; anything else stays the message it always was.
_QUALIFIED_WORD = re.compile(rf"^/{_NAME}:{_NAME}$")

Kind = Literal["command", "skill", "plugin", "message", "unknown"]


@dataclass(frozen=True)
class Resolution:
    """What a submitted line turned out to be."""

    kind: Kind
    #: Command name, skill name, ``plugin:command``, or None for a plain message.
    name: Optional[str]
    #: Arguments for a command/skill/plugin command, or the message itself.
    payload: str


@dataclass(frozen=True)
class PluginCommand:
    """A slash command a plugin declares in its ``schema.yaml``.

    What a person sees follows the ecosystem: a name, one line of help, and a
    hint for the arguments (Claude Code spells that ``argument-hint``). What it
    RUNS is one of the plugin's own tools -- so a command can never reach past
    what this agent is already allowed to call, and the plugin does not get a
    second, unguarded entry point next to its tools.
    """

    plugin: str
    name: str
    summary: str
    #: Flat tool name, already rendered ("context_engineer_compact").
    tool: str
    #: Tool parameter that receives the rest of the line. None = no arguments.
    argument: Optional[str] = None
    argument_hint: str = ""

    @property
    def qualified(self) -> str:
        """``plugin:name`` -- the spelling that is never ambiguous."""
        return f"{self.plugin}:{self.name}"


def commands_for(surface: str) -> tuple[ChatCommand, ...]:
    """Built-ins that make sense on *surface*."""
    return tuple(c for c in BUILTIN_COMMANDS if surface in c.surfaces)


def parse_chat_command(line: str) -> tuple[Optional[str], str]:
    """Split a prompt line into (command, payload).

    Returns ("unknown", line) for something that LOOKS like a command but
    isn't one, so the caller can say so instead of silently spending a turn on
    it. Anything else starting with "/" is a normal message. "//" is the
    literal escape for a message that really has to start with a command word.
    """
    stripped = line.strip()
    if stripped.startswith("//"):
        # Only where an escape is NEEDED. "//compact" has to reach the agent
        # as "/compact" -- that is what the escape is for. But "// TODO: fix"
        # is a pasted comment and "//192.168.1.1/share" is a UNC path; eating
        # a slash there corrupts the message the person actually sent.
        escaped = stripped[1:]
        head = escaped.split()
        if head and (_COMMAND_WORD.match(head[0]) or _QUALIFIED_WORD.match(head[0])):
            return None, escaped
        return None, stripped
    if not stripped.startswith("/"):
        return None, stripped
    if "\n" in stripped:
        # A pasted paragraph that happens to open with a command word is a
        # message. Treating it as a command threw the rest away without a
        # word -- and both surfaces can produce multi-line input (the terminal
        # through its """ fence, the browser because Enter is a newline).
        return None, stripped
    word, _, rest = stripped.partition(" ")
    command = _COMMAND_ALIASES.get(word.lower())
    if command is not None:
        return command, rest.strip()
    if _COMMAND_WORD.match(word):
        return "unknown", word
    return None, stripped


def is_builtin_command(name: str) -> bool:
    """Whether ``/name`` is taken by a built-in and can never reach anything else."""
    return f"/{name.lower()}" in _COMMAND_ALIASES


def is_typeable_command_name(name: str) -> bool:
    """Whether ``/name`` would be recognised as a command word at all.

    A plugin declaring ``name: "compact now"`` gets a help entry nobody can
    invoke -- the parser never sees a command there, it sees a message.
    """
    return bool(_COMMAND_NAME.match(name))


def match_plugin_command(
    word: str, plugin_commands: Sequence[PluginCommand]
) -> Optional[PluginCommand]:
    """The plugin command a typed word (without the slash) means, or None.

    The qualified spelling always wins. A BARE name only resolves while it is
    unique: two plugins may each call a command ``compact``, and picking the
    first would run the wrong one on the strength of registration order. Those
    stay reachable as ``plugin:name``, which is what help then shows.
    """
    lowered = word.lower()
    for command in plugin_commands:
        if command.qualified.lower() == lowered:
            return command
    named = [c for c in plugin_commands if c.name.lower() == lowered]
    return named[0] if len(named) == 1 else None


def resolve(line: str, skill_names: Sequence[str] = (),
            plugin_commands: Sequence[PluginCommand] = ()) -> Resolution:
    """Classify a submitted line as command, plugin command, skill, message or
    unknown.

    Built-ins win over everything: a skill or plugin called ``help`` must not
    shadow ``/help``, or a bundle dropped into a folder could take over the
    only way out of a confusing state. Plugin commands come before skills for
    the same reason one step down -- a plugin ships with the system, a skill
    directory is whatever happens to lie on disk.
    """
    command, payload = parse_chat_command(line)
    word, _, rest = line.strip().partition(" ")
    if command is None:
        # "/plugin:name" is not a command WORD (see _QUALIFIED_WORD): it is
        # claimed only when it really names a declared command, so an ordinary
        # "/todo:milch kaufen" remains the message it looks like.
        if _QUALIFIED_WORD.match(word):
            qualified = match_plugin_command(word[1:], plugin_commands)
            if qualified is not None:
                return Resolution("plugin", qualified.qualified, rest.strip())
        return Resolution("message", None, payload)
    if command != "unknown":
        return Resolution("command", command, payload)

    unknown_word = payload[1:]  # payload is the "/word" token for unknowns
    plugin_command = match_plugin_command(unknown_word, plugin_commands)
    if plugin_command is not None:
        return Resolution("plugin", plugin_command.qualified, rest.strip())
    for name in skill_names:
        if name.lower() == unknown_word.lower():
            return Resolution("skill", name, rest.strip())
    return Resolution("unknown", None, payload)


def suggest_command(word: str, extra: Iterable[str] = ()) -> Optional[str]:
    """Closest known command for a typo, or None.

    Prefixes first: "/h" is the common abbreviation-style slip, and difflib
    scores it far below any cutoff against "/help" (2 chars against 5).
    *extra* takes skill names (without the slash) so they compete too.
    """
    lowered = word.lower()
    candidates = list(_COMMAND_ALIASES) + [f"/{name}" for name in extra]
    prefixed = sorted((c for c in candidates if c.startswith(lowered)), key=len)
    if prefixed:
        return prefixed[0]
    matches = difflib.get_close_matches(lowered, candidates, n=1, cutoff=0.6)
    return matches[0] if matches else None


def needs_escape(text: str) -> bool:
    """Whether a stored message has to be re-escaped before it is offered back.

    A message sent as "//compact" is stored as "/compact". Handed to a prompt
    raw it would RUN the command instead of being sent again, so the input
    history puts the escape back.

    This is the exact inverse of the unescape in parse_chat_command, and it
    has to stay that way: escaping a head that would NOT be unescaped there
    delivers the extra slash to the agent. "/3d drucker" and a bare "/" are
    messages on both paths and must be left alone.
    """
    stripped = text.strip()
    if not stripped.startswith("/") or stripped.startswith("//"):
        return False
    if "\n" in stripped:
        return False  # multi-line is always a message, never a command
    head = stripped.split()
    return bool(head and (_COMMAND_WORD.match(head[0])
                          or _QUALIFIED_WORD.match(head[0])))


def looks_like_command(text: str) -> bool:
    """Whether a stored user message is really a slash command.

    Commands never reach the agent -- but before "/h" became an alias, unknown
    ones were passed through as messages and are now sitting in old sessions.
    They are not part of the conversation and would only add noise.

    Only a word that IS a command counts, not everything shaped like one: a
    message escaped with "//" is stored with a SINGLE slash, exactly as the
    person meant it, and hiding those left the agent's answer standing in
    /history with no question above it.
    """
    stripped = text.strip()
    if "\n" in stripped:
        # parse_chat_command treats anything multiline as a MESSAGE, never a
        # command. Mirror that rule -- otherwise a multiline turn whose first
        # line looks like "/word ..." is hidden by /history and /last.
        return False
    return stripped.split(" ")[0].lower() in _COMMAND_ALIASES


# A template variable, as Jinja can actually address it. Hyphens are rejected
# on purpose: `{{ my-var }}` is a subtraction, so a variable named that way
# would be accepted here and then never render -- the kind of silent nothing
# that costs an hour to find. _NAME above is deliberately NOT reused; it allows
# hyphens because command words may have them.
_VAR_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


@dataclass(frozen=True)
class VarsRequest:
    """What a ``/vars`` line asks for, once read.

    A request with nothing in it is a QUESTION -- bare ``/vars`` lists. That
    keeps the common case free of a subcommand nobody would remember.
    """

    assign: Mapping[str, str]
    unset: tuple[str, ...]
    clear: bool
    errors: tuple[str, ...]

    @property
    def is_query(self) -> bool:
        """Whether this only asks, so a surface can skip the write path.

        A line with errors is NOT a query -- it is a refusal. Saying otherwise
        would let a caller that checks this first answer a typo with a listing,
        as if nothing had been wrong with it.
        """
        return not (self.assign or self.unset or self.clear or self.errors)


def _split_assignments(text: str) -> list[str]:
    """Split a ``/vars`` line into tokens: quotes group, backslashes survive.

    Neither ``shlex`` default does both. POSIX mode groups ``greeting="a b"``
    correctly but eats backslashes, so a Windows value arrives as ``C:tmpx``;
    non-POSIX mode keeps the backslashes but only groups when a token STARTS
    with a quote, so ``greeting="a b"`` silently becomes two tokens and the
    second is reported as junk. Both were measured, not assumed.

    So: POSIX quoting with the escape character switched off. Raises
    ``ValueError`` on an unbalanced quote, which is a real answer -- the
    non-POSIX split accepted ``x="unbalanced`` and stored the quote.

    ``commenters`` has to go the same way, and forgetting it cost more than
    the escapes would have: shlex treats ``#`` as a comment by default, so
    ``color=#ff0000`` parsed to an EMPTY value with no error at all, and
    ``a=1 #x b=2`` silently dropped ``b``. Hex colours, URL fragments and
    issue numbers are ordinary values, and ``--vars`` (a plain partition on
    argv) keeps them -- the two spellings have to mean the same thing.
    """
    lexer = shlex.shlex(text, posix=True)
    lexer.whitespace_split = True
    lexer.escape = ""
    lexer.commenters = ""
    return list(lexer)


def parse_vars(payload: str) -> VarsRequest:
    """Read the rest of a ``/vars`` line into a request.

    The grammar is the one ``--vars`` already taught: whitespace-separated
    ``KEY=VALUE``. Two words are reserved as the first token, ``clear`` and
    ``unset``, because "empty this" and "remove one" have no spelling in
    KEY=VALUE form -- ``KEY=`` sets an empty string, which is a different
    thing and occasionally what someone means.

    Never raises: a malformed line comes back as ``errors`` for the surface to
    print, because a chat command that traps the REPL is worse than a bad line.
    """
    text = payload.strip()
    if not text:
        return VarsRequest({}, (), False, ())
    try:
        tokens = _split_assignments(text)
    except ValueError as exc:
        return VarsRequest({}, (), False, (f"could not read the line: {exc}",))
    if not tokens:
        return VarsRequest({}, (), False, ())

    head = tokens[0].lower()
    if head == "clear":
        extra = " ".join(tokens[1:])
        errors = (f"/vars clear takes no arguments (got {extra})",) if extra else ()
        return VarsRequest({}, (), not errors, errors)
    if head == "unset":
        names: list[str] = []
        errors_list: list[str] = []
        for token in tokens[1:]:
            if _VAR_NAME.match(token):
                names.append(token)
            else:
                errors_list.append(f"not a variable name: {token}")
        if not names and not errors_list:
            errors_list.append("/vars unset needs at least one name")
        return VarsRequest({}, tuple(names), False, tuple(errors_list))

    assign: dict[str, str] = {}
    errors_list = []
    for token in tokens:
        key, sep, value = token.partition("=")
        if not sep:
            errors_list.append(f"expected KEY=VALUE, got: {token}")
            continue
        name = key.strip()
        if not _VAR_NAME.match(name):
            errors_list.append(
                f"not a usable variable name: {name or '(empty)'} "
                f"-- letters, digits and _ only, not starting with a digit")
            continue
        assign[name] = value
    return VarsRequest(assign, (), False, tuple(errors_list))


def apply_vars(current: Mapping[str, Any], request: VarsRequest) -> dict[str, Any]:
    """The variables a session should hold after *request*, as a NEW dict.

    Pure on purpose: both surfaces read their variables from the same session
    tracker and write them back the same way, so the only part worth sharing is
    this one -- and it can be tested without an agent, a session or a browser.

    Returning a fresh dict rather than mutating matters: the tracker hands out
    its internal dict for a known session but a throwaway ``{}`` for an unknown
    one, so an in-place edit would silently do nothing for the second case.

    A request carrying errors changes NOTHING. Both surfaces check that before
    calling, but the guard belongs here: half-applying a refused line is the
    one outcome nobody could explain, and a third caller should not have to
    know that.
    """
    if request.errors:
        return dict(current)
    if request.clear:
        return {}
    result = dict(current)
    for name in request.unset:
        result.pop(name, None)
    result.update(request.assign)
    return result


async def store_vars(tracker: Any, session_manager: Any, user_id: str,
                     session_id: str, new_vars: Mapping[str, Any]) -> bool:
    """Write *new_vars* as the session's complete variable set. Returns whether
    it also reached disk.

    This is execution rather than knowledge, which the rest of this module
    avoids -- but the two surfaces do the identical thing here, and the two
    halves are exactly where they drifted apart when written twice:

    * The tracker is CLEARED before it is set. ``set_session_template_vars``
      only ever ``update()``s, so without the clear a removal leaves the old
      value in place while the caller's own return value looks right.
    * The persisted ``context_vars`` are REPLACED. Everything else in the
      system only adds variables, so the runtime->disk sync in
      ``session_service`` merges; a merge cannot express a removal, and on the
      web every message reloads the session from disk, which merged the
      deleted variable straight back in.

    Nothing is swallowed: a permission error or a broken write propagates, so
    the surface can say the change did not stick instead of showing a listing
    that only exists in memory.
    """
    tracker.clear_session_template_vars(session_id)
    if new_vars:
        tracker.set_session_template_vars(session_id, dict(new_vars))
    if session_manager is None or not user_id:
        return False
    # Called by name, not through getattr(..., None): a renamed method would
    # otherwise turn persistence off silently and every test would stay green,
    # which is exactly the failure this round was fixing.
    return bool(await session_manager.replace_session_context_vars(
        user_id, session_id, dict(new_vars)))


#: What a tool is filed under when no registered server claims its name.
UNKNOWN_SERVER = "(unknown server)"


def group_tools_by_server(
    tools: Sequence[dict], server_names: Iterable[str]
) -> list[tuple[str, list[dict]]]:
    """Group the tools of ``/tools`` under the server that provides them.

    A tool name carries its server as a prefix, so the LONGEST registered
    server name that matches wins: ``coder_file_ops_read_file`` belongs to
    ``coder_file_ops``, not to a shorter ``coder``. Equality covers
    single-tool servers, where the tool carries the server's bare name
    (``sequential_thinking``, ``todo``).

    Splitting on "_" instead invented groups that do not exist ("sequential"
    next to "sequential_thinking"), which is why a tool no registered server
    claims is named as such rather than filed somewhere plausible.

    Returns the groups in display order (by server name), so the terminal and
    the browser show the same list instead of two orderings of one truth.
    """
    known = sorted(server_names, key=len, reverse=True)
    groups: dict[str, list[dict]] = {}
    for tool in tools:
        name = tool.get("name", "?")
        server = next(
            (s for s in known if name == s or name.startswith(s + "_")),
            UNKNOWN_SERVER,
        )
        groups.setdefault(server, []).append(tool)
    return [(server, groups[server]) for server in sorted(groups)]
