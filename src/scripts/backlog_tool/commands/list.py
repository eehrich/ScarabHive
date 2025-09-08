"""List-related commands for the backlog CLI."""
import argparse
import sys
from pathlib import Path
from typing import List, Tuple

from .. import parser as bl
from ..parser import Backlog, Epic, Task


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
    """Format an epic line for output."""
    if color:
        # Use color codes for status
        status_symbol = '☐' if epic.status == 'open' else '✅'
        if epic.status == 'open':
            return f"\033[1;34m{status_symbol} Epic {epic.id}: {epic.title}\033[0m"
        else:
            return f"\033[1;32m{status_symbol} Epic {epic.id}: {epic.title}\033[0m"
    else:
        status_symbol = '☐' if epic.status == 'open' else '✅'
        return f"{status_symbol} Epic {epic.id}: {epic.title}"


def _format_task_line(task: Task, epic: Epic, color: bool) -> str:
    """Format a task line for output."""
    if color:
        # Use color codes for status
        status_symbol = '☐' if task.status == 'open' else '✅'
        if task.status == 'open':
            return f"\033[1;33m{status_symbol} Task {task.id}: {task.title}\033[0m (Epic {epic.id})"
        else:
            return f"\033[1;32m{status_symbol} Task {task.id}: {task.title}\033[0m (Epic {epic.id})"
    else:
        status_symbol = '☐' if task.status == 'open' else '✅'
        return f"{status_symbol} Task {task.id}: {task.title} (Epic {epic.id})"


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
            print(_format_epic_line(epic, color))

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
                print(_format_task_line(task, epic, color))

    return 0
