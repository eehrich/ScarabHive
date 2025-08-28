"""A tiny, tolerant markdown backlog parser and writer.

The parser is conservative: it identifies top-level sections (Epics - open,
Epics - finished) and parses epics/tasks with minimal structure. It intentionally
keeps unknown content as raw lines so re-serialization preserves non-modeled
content.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional, Dict, Tuple
import re
import os
from datetime import date
import shutil
import time


@dataclass
class Task:
    id: str
    title: str
    status: str
    added: Optional[str] = None
    closed: Optional[str] = None
    notes: List[str] = field(default_factory=list)


@dataclass
class Epic:
    id: str
    title: str
    status: str
    subtasks: List[Task] = field(default_factory=list)
    raw_lines: List[str] = field(default_factory=list)


@dataclass
class Backlog:
    header: List[str]
    epics_open: List[Epic]
    epics_finished: List[Epic]
    footer: List[str]


RE_EPIC_LINE = re.compile(r"^\s*(?:-\s*)?(?:☐|✅|❌|⏳)\s*Epic\s+(\d{4}):\s*(.*)$")
RE_TASK_LINE = re.compile(r"^\s*(?:-\s*)?(?:☐|✅|❌|⏳)\s*Task\s+(\d{4}):\s*(.*)$")
RE_FIELD_LINE = re.compile(r"^\s*-\s*(\w+):\s*(.*)$")


def read_file(path: str) -> List[str]:
    with open(path, "r", encoding="utf-8") as f:
        return f.read().splitlines()


def parse(backlog_lines: List[str]) -> Backlog:
    header: List[str] = []
    footer: List[str] = []
    epics_open: List[Epic] = []
    epics_finished: List[Epic] = []

    section = "header"
    current_epic: Optional[Epic] = None
    current_task: Optional[Task] = None

    for ln in backlog_lines:
        m = RE_EPIC_LINE.match(ln)
        if m:
            eid, title = m.group(1), m.group(2)
            current_epic = Epic(id=eid, title=title.strip(), status="open")
            if section == "header":
                section = "epics_open"
            epics_open.append(current_epic)
            continue
        m2 = RE_TASK_LINE.match(ln)
        if m2 and current_epic is not None:
            tid, title = m2.group(1), m2.group(2)
            current_task = Task(id=tid, title=title.strip(), status="open")
            current_epic.subtasks.append(current_task)
            continue
        # fields under task or epic (e.g., - status: open)
        # Decide by indentation: task fields are indented more (built with 4/6 spaces)
        indent = len(ln) - len(ln.lstrip(' '))
        m3 = RE_FIELD_LINE.match(ln.strip())
        if m3:
            key, val = m3.group(1), m3.group(2)
            key = key.strip().lower()
            # treat as task-level field if indent >= 4 and we have a current task
            if indent >= 4 and current_task is not None:
                if key == "status":
                    current_task.status = val.strip()
                elif key == "added":
                    current_task.added = val.strip()
                elif key == "closed":
                    current_task.closed = val.strip()
                elif key == "notes":
                    pass
                continue
            # treat as epic-level field if indent < 4 and we have a current epic
            if indent < 4 and current_epic is not None:
                if key == "status":
                    current_epic.status = val.strip()
                else:
                    # preserve other epic-level fields in raw_lines
                    current_epic.raw_lines.append(ln)
                # clear current task context after epic-level field
                current_task = None
                continue
        # fallback: preserve in raw_lines of current epic if present
        if current_epic is not None:
            # clear current_task context so subsequent epic-level fields
            # (e.g., '  - status: open') are not mistakenly applied to the
            # previous task
            current_task = None
            current_epic.raw_lines.append(ln)
        else:
            header.append(ln)

    # as a simple model, put footer empty
    return Backlog(header=header, epics_open=epics_open, epics_finished=epics_finished, footer=footer)


def add_task_to_epic(backlog: Backlog, epic_id: str, title: str, notes: Optional[str] = None) -> Task:
    for e in backlog.epics_open:
        if e.id == epic_id:
            # generate placeholder id XXXX -> find next available
            existing = {t.id for t in e.subtasks}
            new_id = "0000"
            i = 0
            while True:
                cand = f"{int(epic_id) + i:04d}"
                if cand not in existing:
                    new_id = cand
                    break
                i += 1
            t = Task(id=new_id, title=title, status="open", added=date.today().isoformat())
            if notes:
                t.notes = notes.splitlines()
            e.subtasks.append(t)
            return t
    raise KeyError(f"epic {epic_id} not found")


def build_markdown(backlog: Backlog) -> str:
    lines: List[str] = []
    lines.extend(backlog.header)
    # Avoid duplicating the 'Epics - open' heading if it's already in the header
    if not any(l.strip().startswith("## 1. Epics - open") for l in backlog.header):
        lines.append("")
        lines.append("## 1. Epics - open")
        lines.append("")
    for e in backlog.epics_open:
        lines.append(f"- ☐ Epic {e.id}: {e.title}")
        lines.append(f"  - status: {e.status}")
        lines.append(f"  - Subtasks:")
        for t in e.subtasks:
            lines.append(f"    - ☐ Task {t.id}: {t.title}")
            lines.append(f"      - status: {t.status}")
            if t.added:
                lines.append(f"      - added: {t.added}")
            if t.closed:
                lines.append(f"      - closed: {t.closed}")
            if t.notes:
                lines.append(f"      - Notes:")
                for n in t.notes:
                    lines.append(f"        - {n}")
        lines.extend(e.raw_lines)
        lines.append("")
    # Avoid duplicating the 'Epics - finished' heading if it's already in the footer
    if not any(l.strip().startswith("## 2. Epics - finished") for l in backlog.footer):
        lines.append("")
        lines.append("## 2. Epics - finished")
        lines.append("")
    lines.extend(backlog.footer)
    return "\n".join(lines)


def safe_write(path: str, text: str) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(tmp, path)


def make_backup(path: str) -> str:
    """Create a timestamped backup of `path` under a .backups directory.

    Returns the backup file path as a string.
    """
    p = os.path.abspath(path)
    d = os.path.dirname(p)
    backups_dir = os.path.join(d, '.backups')
    os.makedirs(backups_dir, exist_ok=True)
    ts = time.strftime('%Y%m%d_%H%M%S')
    base = os.path.basename(p)
    bak = f"{base}.{ts}.bak"
    dest = os.path.join(backups_dir, bak)
    shutil.copy2(p, dest)
    return dest


def list_backups(path: str) -> list[str]:
    p = os.path.abspath(path)
    d = os.path.dirname(p)
    backups_dir = os.path.join(d, '.backups')
    if not os.path.isdir(backups_dir):
        return []
    files = [os.path.join(backups_dir, f) for f in os.listdir(backups_dir)
             if f.startswith(os.path.basename(p) + '.') and f.endswith('.bak')]
    # sort by modification time ascending (oldest first)
    files.sort(key=lambda f: os.path.getmtime(f))
    return files


def restore_backup(path: str, backup_path: str) -> None:
    """Restore backup_path over path atomically."""
    if not os.path.exists(backup_path):
        raise FileNotFoundError(backup_path)
    # atomic replace
    tmp = path + '.restore.tmp'
    shutil.copy2(backup_path, tmp)
    os.replace(tmp, path)


def prune_backups(path: str, keep: int | None = None, older_than_days: int | None = None) -> list[str]:
    """Prune backups for `path` by keeping the newest `keep` files and/or removing files older than `older_than_days`.

    Returns a list of removed file paths.
    """
    files = list_backups(path)
    if not files:
        return []

    # build list of candidates with mtimes
    files_with_mtime = [(f, os.path.getmtime(f)) for f in files]
    # newest first
    files_with_mtime.sort(key=lambda t: t[1], reverse=True)

    to_remove = set()
    if keep is not None and keep >= 0:
        # keep newest `keep` files
        for f, _ in files_with_mtime[keep:]:
            to_remove.add(f)

    if older_than_days is not None and older_than_days > 0:
        cutoff = time.time() - (older_than_days * 86400)
        for f, m in files_with_mtime:
            if m < cutoff:
                to_remove.add(f)

    # If no criteria given, default to keep 10
    if keep is None and older_than_days is None:
        default_keep = 10
        for f, _ in files_with_mtime[default_keep:]:
            to_remove.add(f)

    removed = []
    for f in sorted(to_remove):
        try:
            os.remove(f)
            removed.append(f)
        except Exception:
            # on failure, skip and continue
            continue
    return removed


def find_task(backlog: Backlog, task_id: str) -> Tuple[Epic, Task]:
    """Return (epic, task) for the given task_id or raise KeyError."""
    for e in backlog.epics_open + backlog.epics_finished:
        for t in e.subtasks:
            if t.id == task_id:
                return e, t
    raise KeyError(f"task {task_id} not found")


def move_task(backlog: Backlog, task_id: str, to_epic_id: str) -> Task:
    """Move a task identified by task_id into the epic with id to_epic_id.

    If the task id conflicts in the destination epic, a new id is generated.
    Returns the moved Task object (possibly with an updated id).
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
    src_epic.subtasks = [t for t in src_epic.subtasks if t.id != task_id]

    # ensure unique id in destination; if conflict, generate a new one
    existing = {t.id for t in dest_epic.subtasks}
    if task.id in existing:
        i = 0
        while True:
            cand = f"{int(to_epic_id) + i:04d}"
            if cand not in existing:
                task.id = cand
                break
            i += 1
    dest_epic.subtasks.append(task)
    return task


def update_task_status(backlog: Backlog, task_id: str, new_status: str) -> Task:
    """Update the status of a task; if moving to a closed/done state, set closed date.

    new_status is stored verbatim. A closed date is added when new_status
    looks like a finishing state (done/closed/complete/finished).
    """
    _, task = find_task(backlog, task_id)
    task.status = new_status
    lower = (new_status or "").strip().lower()
    if lower in ("done", "closed", "complete", "finished"):
        if not task.closed:
            task.closed = date.today().isoformat()
    else:
        # opening a task clears closed date
        task.closed = None
    return task


def validate_backlog(backlog: Backlog) -> list[str]:
    """Run lightweight validation rules and return list of error strings.

    Rules implemented:
    - Epic ids must be unique across open and finished lists.
    - Task ids must be unique across all epics.
    - Date fields (added, closed) must be ISO dates YYYY-MM-DD when present.
    - Status values must be among a permissive allowed set.
    """
    errors: list[str] = []
    # check epic ids
    epic_ids = [e.id for e in backlog.epics_open + backlog.epics_finished]
    dup_epics = {i for i in epic_ids if epic_ids.count(i) > 1}
    for de in sorted(dup_epics):
        errors.append(f"duplicate epic id: {de}")

    # check task ids
    task_ids: list[str] = []
    for e in backlog.epics_open + backlog.epics_finished:
        for t in e.subtasks:
            task_ids.append(t.id)
    dup_tasks = {i for i in task_ids if task_ids.count(i) > 1}
    for dt in sorted(dup_tasks):
        errors.append(f"duplicate task id: {dt}")

    # date format check
    import datetime
    def is_iso_date(s: Optional[str]) -> bool:
        if not s:
            return True
        try:
            # datetime.date.fromisoformat enforces YYYY-MM-DD
            datetime.date.fromisoformat(s)
            return True
        except Exception:
            return False

    for e in backlog.epics_open + backlog.epics_finished:
        for t in e.subtasks:
            if not is_iso_date(t.added):
                errors.append(f"bad date (added) for task {t.id}: {t.added}")
            if not is_iso_date(t.closed):
                errors.append(f"bad date (closed) for task {t.id}: {t.closed}")

    # status values
    allowed = {"open", "done", "closed", "complete", "finished", "resolved", "in progress", "todo"}
    for e in backlog.epics_open + backlog.epics_finished:
        if e.status and e.status.strip().lower() not in allowed:
            errors.append(f"unknown epic status for {e.id}: {e.status}")
        for t in e.subtasks:
            if t.status and t.status.strip().lower() not in allowed:
                errors.append(f"unknown task status for {t.id}: {t.status}")

    return errors

