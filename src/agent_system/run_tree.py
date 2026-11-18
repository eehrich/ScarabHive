"""The runs as a person reads them in a terminal: what works now as a tree, what waits for them, what just ended.

The same reading as the web shell's list above its sessions (``static/js/shell/activity.js``), from the same rows
(``RunEventStore.runs``): every agent's turn with the sub-agents below it, each with the step it is on. A run
stands below the run of the session it works below while that one is at work too; else on its own. Used by
``agent-cli runs`` and the chat's ``/runs``.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional

from agent_system.run_events import ACTIVE, FINAL

#: The ended runs listed, the newest -- and only those that ended within this many seconds: "just".
ENDED = 6
ENDED_S = 3600
#: The marks of a run at work, gone quiet, waiting for its person, done, stopped, failed -- and their plain spelling
#: for a terminal that cannot show the first.
MARKS = {"working": "●", "quiet": "◌", "needs": "?", "done": "✓", "cancelled": "■", "failed": "✕"}
PLAIN = {"working": "*", "quiet": "~", "needs": "?", "done": "+", "cancelled": "-", "failed": "x"}


def working(runs: list[dict[str, Any]]) -> list[tuple[dict[str, Any], int]]:
    """The runs at work as a tree, flat in the order it is read: (run, depth). Oldest first: nothing jumps."""
    active = sorted((run for run in runs if run["state"] in ACTIVE), key=lambda run: run.get("started") or "")
    ids = {run["run_id"] for run in active}
    below: dict[str, list[dict[str, Any]]] = {}
    for run in active:
        parent = run.get("parent_session") or ""
        below.setdefault(parent if parent in ids and parent != run["run_id"] else "", []).append(run)
    out: list[tuple[dict[str, Any], int]] = []

    def walk(parent: str, depth: int) -> None:
        # from the runs on their own down: runs that name each other in a circle hang on none of them, and are
        # not reached -- the walk ends
        for run in below.get(parent, []):
            out.append((run, depth))
            walk(run["run_id"], depth + 1)

    walk("", 0)
    return out


def waits_since(run: dict[str, Any]) -> str:
    """Since when a run waits for its person: its question's time, its end (a failure), else its last word (a
    result to check). Not its last sign of life -- a run that asks goes on saying that it lives."""
    return run.get("asked") or run.get("ended") or run.get("updated") or ""


def waiting(runs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The runs that wait for their person, the one that came to wait last first."""
    return sorted((run for run in runs if run.get("needs_you")), key=waits_since, reverse=True)


def ended(runs: list[dict[str, Any]], limit: int = ENDED, now: Optional[datetime] = None) -> list[dict[str, Any]]:
    """What just ended and needs nobody, the newest first -- not the sub-agents: their end is their caller's to
    hear of; and not what ended longer ago than ENDED_S."""
    now = now or datetime.now(timezone.utc)
    done = [run for run in runs if run["state"] in FINAL and not run.get("needs_you") and run.get("source") != "sub_agent"
            and 0 <= _seconds_since(run.get("ended"), now) < ENDED_S]
    return sorted(done, key=lambda run: run.get("ended") or "", reverse=True)[:limit]


def _seconds_since(iso: Optional[str], now: datetime) -> float:
    """The seconds since ``iso``, -1 where it is no time."""
    try:
        start = datetime.fromisoformat(str(iso or "").replace("Z", "+00:00"))
    except ValueError:
        return -1.0
    if start.tzinfo is None:
        start = start.replace(tzinfo=timezone.utc)
    return max(0.0, (now - start).total_seconds())


def since(iso: Optional[str], now: Optional[datetime] = None) -> str:
    """"now", "12 min", "1 h 05" -- how long since ``iso``."""
    seconds = _seconds_since(iso, now or datetime.now(timezone.utc))
    if seconds < 0:
        return ""
    minutes = int(seconds // 60)
    if minutes < 1:
        return "now"
    return f"{minutes} min" if minutes < 60 else f"{minutes // 60} h {minutes % 60:02d}"


def says(run: dict[str, Any], lang: str = "en") -> str:
    """What a run says of itself in a line: the step it is on, what it asks, why it failed -- and nothing where its
    mark says it all (done, stopped)."""
    state = run["state"]
    if state in ACTIVE:
        step = (run.get("step_labels") or {}).get(lang) or run.get("step_label") or (
            "Waiting to start" if state == "queued" else "At work")
        return f"{step} -- no sign of life for 30 s" if run.get("silent") else step
    if state == "needs_input":
        return f"Asks: {(run.get('question') or {}).get('question') or 'a question'}"
    if state == "review":
        return "Done -- please check the result"
    if state in ("done", "cancelled"):
        return ""
    why = f": {run['reason']}" if run.get("reason") else ""
    return ("Handed over to you" if state == "handed_over" else "Failed") + why


def _mark(run: dict[str, Any], marks: dict[str, str]) -> str:
    if run["state"] in ACTIVE:
        return marks["quiet" if run.get("silent") else "working"]
    if run.get("needs_you"):
        return marks["needs"]
    return marks["done" if run["state"] == "done" else "cancelled" if run["state"] == "cancelled" else "failed"]


def render(runs: list[dict[str, Any]], *, now: Optional[datetime] = None, width: int = 100, lang: str = "en",
           plain: bool = False) -> list[str]:
    """The runs as lines for a terminal ``width`` wide: three groups, each only when it has something."""
    marks = PLAIN if plain else MARKS
    dot, cut = (" - ", "...") if plain else (" · ", "…")
    lines: list[str] = []

    def row(run: dict[str, Any], depth: int = 0) -> None:
        indent = "  " * (depth + 1)
        time = since(run.get("ended") if run["state"] in FINAL else run.get("started"), now)
        head = f"{indent}{_mark(run, marks)} {run.get('agent') or 'agent'}"
        if run.get("title"):
            head += f"{dot}{run['title']}"
        room = max(10, width - len(time) - 2)
        head = head if len(head) <= room else head[: room - len(cut)] + cut
        lines.append(f"{head:<{room}}  {time}".rstrip())
        said = says(run, lang)
        if said:
            said = f"{indent}    {said}"
            lines.append(said if len(said) <= width else said[: width - len(cut)] + cut)

    def group(title: str, rows: list[tuple[dict[str, Any], int]]) -> None:
        if rows:
            lines.append(f"{title} ({len(rows)})")
            for run, depth in rows:
                row(run, depth)

    group("At work", working(runs))
    group("Needs you", [(run, 0) for run in waiting(runs)])
    group("Just ended", [(run, 0) for run in ended(runs, now=now)])
    return lines or ["Nothing at work."]
