"""List-related commands for the backlog CLI."""
import argparse
import sys
from typing import List, Tuple, Optional

from .. import parser as bl
from ..parser import Backlog, Epic, Task


def _ansi(text: str, code: Optional[str]) -> str:
    """Apply ANSI color codes to text."""
    if not code:
        return text
    return f"\x1b[{code}m{text}\x1b[0m"


def _sanitize(text: str, max_len: int = 120) -> str:
    """Normalize whitespace and truncate long titles for single-line output.

    This keeps `backlog list` compact and prevents long or multiline titles
    from merging with subsequent items in the summary view.
    """
    # Defensive check for None values, though type annotation suggests this won't happen
    if text is None:
        return ""
    # Replace newlines and carriage returns with spaces
    s = text.replace("\r", " ").replace("\n", " ")
    # Collapse repeated whitespace
    s = " ".join(s.split())
    if len(s) > max_len:
        return s[: max_len - 1].rstrip() + "…"
    return s


def _get_epics_and_tasks(backlog: Backlog, state: str, only: str) -> Tuple[List[Epic], List[Task]]:
    """Get filtered epics and tasks based on state and type filters."""
    epics_open = backlog.epics_open if state in ('open', 'all') else []
    epics_finished = backlog.epics_finished if state in ('finished', 'all') else []

    all_epics = epics_open + epics_finished
    all_tasks = []
    for epic in all_epics:
        all_tasks.extend(epic.tasks)

    if only == 'epics':
        return all_epics, []
    elif only == 'tasks':
        return [], all_tasks
    else:  # 'all'
        return all_epics, all_tasks


def _format_epic_line(epic: Epic, color: bool) -> str:
    """Format an epic line for output.

    Match `show.py` styling: ID -> light cyan (36;1), title -> yellow (33).
    """
    if color:
        # In `show.py` the epic header is printed in green bold for the whole
        # line. Mirror that here for exact parity with the canonical output,
        # but sanitize the title so the summary is always a single line.
        tid = _sanitize(epic.id, max_len=20)
        title = _sanitize(epic.title, max_len=120)
        return _ansi(f"Epic {tid}: {title}", "32;1")
    return f"Epic {epic.id}: {epic.title}"


def _format_task_line(task: Task, epic: Epic, color: bool) -> str:
    """Format a task line for output.

    Match `show.py` styling: ID -> light cyan (36;1), title -> yellow (33).
    """
    if color:
        tid = _ansi(_sanitize(task.id, max_len=20), "36;1")
        ttitle = _ansi(_sanitize(task.title, max_len=100), "33")
        return f"Task {tid}: {ttitle}"
    return f"Task {_sanitize(task.id, max_len=20)}: {_sanitize(task.title, max_len=100)}"


def _format_epic_inline(epic: Epic, color: bool) -> str:
    """Format an epic as an inline summary (ID cyan, title yellow).

    This is used for the compact `--only epics` view to match task styling.
    """
    if color:
        eid = _ansi(_sanitize(epic.id, max_len=20), "36;1")
        title = _ansi(_sanitize(epic.title, max_len=100), "33")
        return f"Epic {eid}: {title}"
    return f"Epic {epic.id}: {epic.title}"


def cmd_list(args: argparse.Namespace) -> int:
    """List epics and tasks from the backlog.

    Lists all epic and task IDs with their titles. Use filters to show specific subsets.
    Combine --state and --only for precise filtering.
    """
    path = args.file or "backlog.md"

    try:
        backlog_lines = bl.read_file(path)
        backlog = bl.parse(backlog_lines)
    except Exception as e:
        print(f"ERROR: Failed to parse backlog file '{path}': {e}", file=sys.stderr)
        return 1

    # Get filtered epics and tasks
    # Special case: ids-only mode shows all items regardless of --only setting
    if getattr(args, 'ids_only', False):
        epics, tasks = _get_epics_and_tasks(backlog, args.state, 'all')
    else:
        epics, tasks = _get_epics_and_tasks(backlog, args.state, args.only)

    # Determine color setting
    color = args.color
    if color is None:  # Auto-detect
        color = sys.stdout.isatty()
    if color and sys.stdout.isatty():
        try:
            import colorama
            colorama.init()
        except Exception:
            pass

    # Handle ids-only mode
    if getattr(args, 'ids_only', False):
        ids = []
        for epic in epics:
            ids.append(epic.id)
        for task in tasks:
            ids.append(task.id)

        for id_val in sorted(ids):
            print(id_val)
        return 0

    # Regular output mode
    if epics:
        if args.only == 'all' or (args.only == 'epics' and not getattr(args, 'ids_only', False)):
            print("Epics:")
        for epic in epics:
            # Default to inline epic formatting (ID cyan + title yellow) to
            # keep the summary/list views visually consistent with task lines.
            # The legacy full-green header helper `_format_epic_line` is still
            # available for callers that explicitly need the `show.py` style.
            line = _format_epic_inline(epic, color)
            print(line)
            # print single-line description if present (keep it short)
            if getattr(epic, 'description', None):
                desc = _sanitize(' '.join(epic.description), max_len=120)
                print(f"  {desc}")

    if tasks:
        if args.only == 'all' and epics:
            print("\nTasks:")
        elif args.only == 'tasks' and not getattr(args, 'ids_only', False):
            print("Tasks:")
        # No header for tasks-only in ids-only mode

        # Group tasks by epic for better organization
        task_by_epic: dict[str, tuple[Epic, list[Task]]] = {}
        for epic in backlog.epics_open + backlog.epics_finished:
            for task in epic.tasks:
                if task in tasks:
                    if epic.id not in task_by_epic:
                        task_by_epic[epic.id] = (epic, [])
                    task_by_epic[epic.id][1].append(task)

        for epic_id, (epic, epic_tasks) in sorted(task_by_epic.items()):
            for task in epic_tasks:
                line = _format_task_line(task, epic, color)
                print(line)
                # print a short single-line description if present
                if getattr(task, 'description', None):
                    desc = _sanitize(' '.join(task.description), max_len=100)
                    print(f"  {desc}")

    return 0
