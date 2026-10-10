"""Session presence: which sessions run right now, and waking an idle one.

A process holds a session while the conversation is in its hands: the agent
loop for each request (servers/agent/mixins/run.py), agent-cli run and agent-run
through their save after the run, agent-cli chat while the session is open.
Holding is an OS lock on <sessions>/<user>/<session>.lock, next to the
session file. The OS lets go of it however the process ends, so a crashed run
never looks running, and whoever meets a lock file nobody holds deletes it:
the directory keeps only the sessions that run.

Whoever wants a session another process holds is told so (SessionBusy) rather
than running it too: both would write the conversation, and the last save
would win.

notify() is for input that waits for a session -- a direct message, say. A
held session reads it on its next step; input that arrives after its last
LLM call leaves <session>.pending, and letting go of the session wakes it. A
session nobody holds is woken right away: agent-cli continues it from its
file, on its stored agent and profile. The marker stays until that run takes
it, so a run that finds none knows somebody else got there first. A turn
started BECAUSE input waits -- a woken run, a woken agent-cli chat prompt --
takes it and changes <session>.woken: that turn is told, so whoever still
rings for news that was there before it stops (wake_session).

A woken run carries its depth in HIVE_WAKE_DEPTH; at max_wake_depth nobody is
woken, so sessions that answer each other cannot start each other forever.
Sub-agents' sessions are never woken and never listed: they belong to the run
that spawned them.

A session whose last run its user stopped starts again only when somebody
starts it: not for input left waiting, not for work of the stopped run that
ends later and rings for it (wake_session knows that work by the run it came
from). The input waits in its store for the next run. A stop is noted where it
is made (note_stop: the web chat's Stop, Ctrl-C in a CLI), not asked of the
run, which may let go before it hears of it or after it can be told. The run
that took the session LAST decides: the holding process answers from memory,
and its last hold leaves <session>.stopped. The next run lifts the mark as it
takes the session; a hold that starts no run (/undo, an append, a woken run
that steps aside) leaves it.

The package, from the bottom up: lockfile.py holds the OS locks on the lock
files (every platform difference of the locking), process.py whether a process
still runs and how a woken run is started, presence.py SessionPresence -- holds,
the markers beside the lock file, notify() and the stop marks -- with
presence_for(), and wake.py wake_blocked() and wake_session() for work that
outlives its turn. What the rest of the system uses is imported from here.
"""
from .presence import (
    STOPPED,
    SessionBusy,
    SessionPresence,
    forget_stop,
    note_stop,
    presence_for,
    sessions_dir,
    stopped_by_user,
)
from .process import WAKE_DEPTH_ENV, WAKE_TASK, alive, spawn_wake, wake_command, wake_depth
from .wake import PRESENCE_OFF, wake_blocked, wake_session

__all__ = [
    "PRESENCE_OFF",
    "STOPPED",
    "SessionBusy",
    "SessionPresence",
    "WAKE_DEPTH_ENV",
    "WAKE_TASK",
    "alive",
    "forget_stop",
    "note_stop",
    "presence_for",
    "sessions_dir",
    "spawn_wake",
    "stopped_by_user",
    "wake_blocked",
    "wake_command",
    "wake_depth",
    "wake_session",
]
