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
_COMMAND_WORD = re.compile(r"^/[A-Za-z?][A-Za-z0-9_-]*$")

Kind = Literal["command", "skill", "message", "unknown"]


@dataclass(frozen=True)
class Resolution:
    """What a submitted line turned out to be."""

    kind: Kind
    #: Command name, skill name, or None for a plain message.
    name: Optional[str]
    #: Arguments for a command/skill, or the message itself.
    payload: str


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
        return None, stripped[1:]
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


def resolve(line: str, skill_names: Sequence[str] = ()) -> Resolution:
    """Classify a submitted line as command, skill, message or unknown.

    Built-ins win over skills: a skill called ``help`` must not shadow
    ``/help``, or a bundle dropped into a skills folder could take over the
    only way out of a confusing state.
    """
    command, payload = parse_chat_command(line)
    if command is None:
        return Resolution("message", None, payload)
    if command != "unknown":
        return Resolution("command", command, payload)

    word = payload[1:]  # payload is the "/word" token for unknowns
    for name in skill_names:
        if name.lower() == word.lower():
            _, _, rest = line.strip().partition(" ")
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
    """
    stripped = text.strip()
    return bool(stripped) and bool(_COMMAND_WORD.match(stripped.split(" ")[0]))
