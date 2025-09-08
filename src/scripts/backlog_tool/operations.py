"""CRUD operations for backlog management.

This module contains functions for creating, reading, updating, and deleting
epics and tasks in a backlog.
"""
from datetime import date
from typing import Optional, Tuple

from .models import Backlog, Epic, Task
from . import values


def add_task_to_epic(backlog: Backlog, epic_id: str, title: str, notes: Optional[str] = None, forced_id: Optional[str] = None) -> Task:
    """Add a new task to an existing epic.

    Args:
        backlog: The backlog to modify
        epic_id: ID of the epic to add the task to
        title: Title of the new task
        notes: Optional notes for the task
        forced_id: Optional specific ID to use for the task

    Returns:
        The newly created Task object

    Raises:
        KeyError: If epic_id is not found
        ValueError: If forced_id is invalid or already exists
        RuntimeError: If no available task IDs remain
    """
    # Build a global set of ids (epic + task) to avoid collisions across epics and tasks
    existing_ids = {e.id for e in backlog.epics_open + backlog.epics_finished}
    existing_ids.update(t.id for ep in backlog.epics_open + backlog.epics_finished for t in ep.tasks)
    # normalize forced id if given (pad numeric form to 4 digits)
    if forced_id is not None:
        # Only numeric ids are allowed for consistency.
        if not str(forced_id).isdigit():
            raise ValueError("id must be numeric")
        forced_id = f"{int(str(forced_id)):04d}"

    for e in backlog.epics_open:
        if e.id == epic_id:
            # If a forced id was provided, validate uniqueness and use it.
            if forced_id:
                if forced_id in existing_ids:
                    raise ValueError(f"id {forced_id} already exists")
                new_id = forced_id
            else:
                # choose a monotonic id: max(existing canonical numeric ids) + 1
                # Only consider numeric ids that are 4 digits or less (canonical backlog ids).
                # Migration artifacts or auxiliary ids may be longer (e.g. '900100') and
                # should not be used to compute the next 4-digit id — they would push
                # the starting point beyond the 4-digit range and make allocation fail.
                numeric_ids = [int(x) for x in existing_ids if x.isdigit() and len(x) <= 4]
                start = (max(numeric_ids) + 1) if numeric_ids else 0
                for i in range(start, 10000):
                    cand = f"{i:04d}"
                    if cand not in existing_ids:
                        new_id = cand
                        break
                else:
                    raise RuntimeError("no available task ids")

            t = Task(id=new_id, title=title, status="open", added=date.today().isoformat())
            if notes:
                t.notes = notes.splitlines()
            e.tasks.append(t)
            return t
    raise KeyError(f"epic {epic_id} not found")


def add_epic_to_backlog(backlog: Backlog, title: str, status: str = 'open', forced_id: Optional[str] = None) -> Epic:
    """Create a new epic with a unique zero-padded 4-digit id and append to epics_open.

    The id generator finds the next unused numeric id (0000..9999) not present
    in existing epics.

    Args:
        backlog: The backlog to modify
        title: Title of the new epic
        status: Status of the new epic ('open' or 'finished')
        forced_id: Optional specific ID to use for the epic

    Returns:
        The newly created Epic object

    Raises:
        ValueError: If forced_id is invalid or already exists
        RuntimeError: If no available epic IDs remain
    """
    # Use a shared id pool between epics and tasks
    existing = {e.id for e in backlog.epics_open + backlog.epics_finished}
    existing.update(t.id for ep in backlog.epics_open + backlog.epics_finished for t in ep.tasks)

    # normalize forced id if given (pad numeric form to 4 digits) and enforce numeric-only ids
    if forced_id is not None:
        if not str(forced_id).isdigit():
            raise ValueError("id must be numeric")
        forced_id = f"{int(str(forced_id)):04d}"
        if forced_id in existing:
            raise ValueError(f"id {forced_id} already exists")
        new_id = forced_id
    else:
        # find next available numeric id
        for i in range(0, 10000):
            cand = f"{i:04d}"
            if cand not in existing:
                new_id = cand
                break
        else:
            raise RuntimeError("no available epic ids")

    e = Epic(id=new_id, title=title, status=status)
    e.tasks = []

    if status == 'open':
        backlog.epics_open.append(e)
    else:
        backlog.epics_finished.append(e)

    return e


def find_task(backlog: Backlog, task_id: str) -> Tuple[Epic, Task]:
    """Find a task by its ID.

    Args:
        backlog: The backlog to search
        task_id: ID of the task to find

    Returns:
        Tuple of (epic, task) containing the found task

    Raises:
        KeyError: If task_id is not found
    """
    for e in backlog.epics_open + backlog.epics_finished:
        for t in e.tasks:
            if t.id == task_id:
                return e, t
    raise KeyError(f"task {task_id} not found")


def move_task(backlog: Backlog, task_id: str, to_epic_id: str) -> Task:
    """Move a task to a different epic.

    If the task id conflicts in the destination epic, a new id is generated.

    Args:
        backlog: The backlog to modify
        task_id: ID of the task to move
        to_epic_id: ID of the destination epic

    Returns:
        The moved Task object (possibly with an updated id)

    Raises:
        KeyError: If task_id or to_epic_id is not found
    """
    src_epic, task = find_task(backlog, task_id)

    # find destination epic
    dest_epic: Optional[Epic] = None
    for e in backlog.epics_open + backlog.epics_finished:
        if e.id == to_epic_id:
            dest_epic = e
            break
    if dest_epic is None:
        raise KeyError(f"epic {to_epic_id} not found")

    # remove from source
    src_epic.tasks = [t for t in src_epic.tasks if t.id != task_id]

    # ensure unique id in destination; if conflict, generate a new one
    existing = {t.id for t in dest_epic.tasks}
    if task.id in existing:
        i = 0
        while True:
            cand = f"{int(to_epic_id) + i:04d}"
            if cand not in existing:
                task.id = cand
                break
            i += 1
    dest_epic.tasks.append(task)
    return task


def update_task_status(backlog: Backlog, task_id: str, new_status: str) -> Task:
    """Update the status of a task.

    If moving to a closed/done state, set closed date.

    Args:
        backlog: The backlog to modify
        task_id: ID of the task to update
        new_status: New status for the task

    Returns:
        The updated Task object

    Raises:
        KeyError: If task_id is not found
    """
    _, task = find_task(backlog, task_id)
    task.status = new_status
    lower = (new_status or "").strip().lower()
    terminal_list = set(values.get('acceptable_terminal', ["done", "reverted", "rejected", "cancelled", "implemented", "fixed", "failed"]))
    if lower in terminal_list:
        if not task.closed:
            task.closed = date.today().isoformat()
        # For terminal states, preserve existing closed date if present
    else:
        # opening a task clears closed date
        task.closed = None
    return task
