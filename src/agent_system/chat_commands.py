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
from dataclasses import dataclass
from typing import Iterable, Literal, Optional, Sequence

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
    ChatCommand("sessions", ("/sessions",), "list recent sessions"),
    ChatCommand("resume", ("/resume",), "continue an earlier session", usage="/resume <id>"),
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
