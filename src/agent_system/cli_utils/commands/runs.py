"""`agent-cli runs`: what the agents are doing now -- the runs at work as a tree, each with the step it is on, what
waits for its person, what just ended. With ``--watch`` it stays and follows.

The terminal's reading of the list the web shell shows above its sessions, from the same run events
(``agent_system.run_events``, ``agent_system.run_tree``): every process writes them into one store, so a turn in
the browser shows here and a turn of this terminal there. Read-only: nothing is started, no agent is built, and
no run is ended for a process this one does not see. Whoever sits at the machine sees every user's runs, as they
can read any user's sessions (``--session-user``).
"""
from __future__ import annotations

import shutil
import sys
import time
from typing import Any

from ...run_events import run_event_reader
from ...run_tree import MARKS, render
from ..common import ansi_capable_stdout

#: The runs read, those not ended first -- as many as a page of the web shell starts with.
RUNS = 60
#: How often ``--watch`` reads again, in seconds.
WATCH_S = 2.0


def _plain(out: Any) -> bool:
    """Whether the terminal cannot show the marks (a console on a legacy code page)."""
    try:
        "".join(MARKS.values()).encode(getattr(out, "encoding", None) or "utf-8")
    except (UnicodeError, LookupError):
        return True
    return False


def runs_text(out: Any = None) -> str:
    """The runs as they stand, for the terminal ``out`` (its width and what it can show)."""
    out = out or sys.stdout
    width = shutil.get_terminal_size((100, 24)).columns
    return "\n".join(render(run_event_reader().runs(None, RUNS), width=max(40, width - 1), plain=_plain(out)))


def run_runs_command(args: Any, config: Any) -> None:
    """Print the runs; with ``--watch`` redraw them every WATCH_S until Ctrl-C."""
    if not getattr(args, "watch", False):
        print(runs_text())
        return
    # home and clear where the terminal does that; else (a file, a console that prints the codes) a line between
    clear = "\x1b[H\x1b[2J" if ansi_capable_stdout() else "-" * 40 + "\n"
    try:
        while True:
            # one write, so the screen does not flicker between the clearing and the list
            sys.stdout.write(clear + runs_text() + "\n")
            sys.stdout.flush()
            time.sleep(WATCH_S)
    except KeyboardInterrupt:
        print()
