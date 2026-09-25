"""One-line session listings, shared by the chat REPL and both CLI entry points.

Sub-agent sessions are left out on purpose. They outnumber the real ones by an
order of magnitude -- measured for ``cli_user``: 31086 sub-sessions against 2915
top-level ones -- so a listing that shows them buries what the user started
himself. ``list_root_sessions`` reads only the main index, and that is exactly
where the sub-sessions are not: they live in per-parent
``.subs.<parent>.index.json`` partitions.

The stored ``depth`` is deliberately NOT the filter. 224 of those 2915
main-index entries carry ``depth: 1`` although their session file has no depth
field at all -- that is ``create_session``'s index default, not a statement
about the session -- and plain agent-cli chats are among them. Filtering on it
would hide real sessions.

Known gap, inherited from ``list_root_sessions`` and kept on purpose: a main
index that parses to an EMPTY dict is trusted, so sessions whose files are on
disk without an index entry stay invisible here (restoring a backup without its
index is the way to get there). Healing that would mean rebuilding the index on
every empty listing, and for cli_user that is a 3.2 GB scan of 34002 files --
which is the cost ``list_root_sessions`` exists to avoid. Delete the index file
instead of emptying it and the next call rebuilds it.
"""
from __future__ import annotations

import logging
from collections import Counter
from datetime import datetime
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)

DEFAULT_LIMIT = 20

# The line has to survive a console. 74 fixed columns plus a 48-char title ran
# to 125, and 831 of the 2915 real entries wrapped on this 120-column terminal
# -- one session over two lines is exactly what this module replaced. 100
# columns is the budget; the agent column pays for it (24 of 85 distinct agent
# names are longer than 20 characters, so a few get cut rather than every row
# padded for them).
#
# The session id is the one column that is never cut: the footer offers
# `--session <id>`, and half an id does not resume anything. Ids are 10
# characters as generated, but the store holds 46-character
# ``ephemeral-<uuid>`` ones from the web API and hand-picked ones from
# `--session looptest-1788301000` -- 85 of the 3372 real entries. Those rows
# spend the budget on the id, so the TITLE gives way, down to nothing.
_SID_WIDTH = 10
_AGENT_WIDTH = 22
_LINE_BUDGET = 100


#: The word that lists every session, runs of pipeline agents included.
EVERYTHING = "all"


def in_chat_selector(runtime: Any, keep: tuple = ()) -> Optional[Callable[[str], bool]]:
    """Whether a session's agent is one meant for chat.

    The listing is for the conversations its person started, and those run on
    agents meant for chat -- ``visibility`` ui or both, the same answer the
    panel's agent dropdown gives (runtime.ServerDecl.visibility, manifest
    fallback included). Everything else is a run some pipeline started under
    the same user: measured for cli_user on 25.09.2026, 4562 of 5054
    top-level sessions, so that 18 of the newest 20 lines were
    ``v4_continuity_scorer_bench_*`` and a person's own chats sank below them.
    A name the configuration no longer declares (a renamed agent, a benchmark
    instance) is not meant for chat either.

    The terminal reaches further than the panel -- ``--agent`` and ``/agent``
    run a ``tool`` or ``private`` agent too -- so the agents in *keep* (the
    one this chat or run is on) are always shown: a listing that hid the
    conversation being had would be the worst answer it could give. A session
    with no agent name cannot be judged and is shown. None when there is no
    runtime to ask: then nothing is filtered.
    """
    if runtime is None:
        return None

    def shown(agent_name: str) -> bool:
        if not agent_name or agent_name in keep:
            return True
        declared = runtime.describe(agent_name)
        return declared is not None and declared.visibility in ("ui", "both")
    return shown


def parse_limit(value: Any, default: int = DEFAULT_LIMIT) -> tuple[int, Optional[str]]:
    """Read a count. Returns ``(limit, complaint)``; 0 means "all of them".

    The complaint is the offending text, and the caller is expected to print
    it: `/sessions 2o` silently listing the default is indistinguishable from
    a count that was honoured, because the header says "(20 of 2915)" either
    way. A negative number is a complaint too -- ``-1`` for "the last one" is
    a common reflex, and mapping it to 0 would answer it with all 2915 lines.
    """
    text = "" if value is None else str(value).strip()
    if not text:
        return default, None
    try:
        count = int(text)
    except ValueError:
        return default, text
    if count < 0:
        return default, text
    return count, None


def newest_of(others: list, everything_hint: str) -> str:
    """What to add when a title named several sessions and the newest was taken.

    Empty when it named one: then there was no choice to mention.
    """
    if not others:
        return ""
    return (f"   (the newest of {len(others) + 1} with this title -- "
            f"{everything_hint} lists every id)")


def parse_listing(value: Any, default: int = DEFAULT_LIMIT) -> tuple[int, bool, Optional[str]]:
    """``(limit, everything, complaint)`` for a listing's argument.

    A count as parse_limit reads it, or ``all``: every session, the runs of
    pipeline agents included, without a limit -- the way to dig, not to look.
    """
    if str(value or "").strip().lower() == EVERYTHING:
        return 0, True, None
    limit, complaint = parse_limit(value, default)
    return limit, False, complaint


def _when(raw: Any) -> str:
    """The timestamp in LOCAL time -- the store keeps UTC.

    Measured: all 2915 index entries end in ``+00:00`` and this machine runs
    UTC+2, so printing the first 16 characters showed a session that ran at
    14:16 as 12:16 -- in the one column the session is picked by.
    """
    try:
        return datetime.fromisoformat(raw).astimezone().strftime("%Y-%m-%d %H:%M")
    except (TypeError, ValueError, OSError):
        # OSError is not paranoia: astimezone() goes through the platform's
        # local-time API, and on Windows a stamp before 1970 or after ~3001
        # raises Errno 22. The loop that prints these lines sits outside
        # print_sessions' try, so one hand-edited stamp would end the listing
        # with a traceback halfway down.
        return str(raw or "")[:16].replace("T", " ")


def format_session_line(entry: dict, current_session_id: Optional[str] = None) -> str:
    """One session, one line -- whatever the record holds.

    Titles are cut from the first request, so 63 of the stored ones carry
    newlines. Collapsing the whitespace is what keeps "one line" true.
    """
    sid = entry.get("session_id") or "?"
    marker = "*" if current_session_id and sid == current_session_id else " "
    when = _when(entry.get("updated_at"))
    count = entry.get("message_count")
    if count is None:
        count = len(entry.get("messages") or [])
    agent = (entry.get("agent_name") or "?")[:_AGENT_WIDTH]
    head = (f" {marker} {sid:<{_SID_WIDTH}} {when:<16} {count:>4} msg  "
            f"{agent:<{_AGENT_WIDTH}} ")
    # The title takes whatever is left of the budget -- 36 columns for an id
    # of the generated length, less for a longer one, nothing for the longest.
    room = max(0, _LINE_BUDGET - len(head))
    title = " ".join((entry.get("title") or "Untitled").split())[:room]
    return (head + title).rstrip()


async def print_sessions(
    session_manager: Any,
    user_id: str,
    *,
    limit: int = DEFAULT_LIMIT,
    current_session_id: Optional[str] = None,
    more_hint: str = "",
    footer: str = "",
    shown: Optional[Callable[[str], bool]] = None,
    everything_hint: str = "",
) -> list[dict]:
    """Print this user's top-level sessions, newest first, one line each.

    With *shown* (in_chat_selector), only sessions whose agent passes are
    listed -- and one line says how many runs were left out, of which agents,
    and *everything_hint* how to list them: a listing that drops rows without
    saying so reads as "that is all there is".

    Returns what it read, so a caller that also needs the records does not
    walk the index a second time -- the listing stats every session for
    children, and doing that twice per `/sessions` was measurable.
    """
    if session_manager is None:
        print("Session listing is unavailable.")
        return []
    try:
        sessions = await session_manager.list_root_sessions(user_id)
    except Exception as e:  # noqa: BLE001 -- a broken index must not kill the CLI
        logger.error("Failed to list sessions for %s: %s", user_id, e, exc_info=True)
        print(f"Could not list sessions: {e}")
        return []

    if not sessions:
        print(f"No sessions for user '{user_id}'.")
        return []

    listable, left_out = list(sessions), []
    if shown is not None:
        # The session this chat is in stays whatever its agent: it is marked,
        # and a listing without its own "*" row misstates where the person is.
        def listed(s: dict) -> bool:
            return (s.get("session_id") == current_session_id
                    or shown(s.get("agent_name") or ""))
        listable = [s for s in sessions if listed(s)]
        left_out = [s for s in sessions if not listed(s)]
    printed = listable if limit <= 0 else listable[:limit]
    print(f"Sessions for '{user_id}' ({len(printed)} of {len(listable)}):")
    for entry in printed:
        print(format_session_line(entry, current_session_id))
    rest = len(listable) - len(printed)
    if rest > 0 and more_hint:
        print(f"   ... {rest} more -- {more_hint}")
    if left_out:
        # One line within the budget: how many, the agent most of them ran on,
        # and the word that lists them. The agent gives way, as a session
        # line's title does -- five-digit counts ran to 102 columns.
        (agent, count), = Counter(s.get("agent_name") or "?" for s in left_out).most_common(1)
        head = f"   ({len(left_out)} more on agents not meant for chat, most "
        tail = f" {count}" + (f" -- {everything_hint}" if everything_hint else "") + ")"
        room = max(0, min(_AGENT_WIDTH, _LINE_BUDGET - len(head) - len(tail)))
        print(head + agent[:room] + tail)
    if footer:
        print(footer)
    return list(sessions)
