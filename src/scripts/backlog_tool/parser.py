"""A tiny, tolerant markdown backlog parser and writer.

The parser is conservative: it identifies top-level sections (Epics - open,
Epics - finished) and parses epics/tasks with minimal structure. It intentionally
keeps unknown content as raw lines so re-serialization preserves non-modeled
content.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple, cast
import re
import os
from datetime import date
from . import values
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


RE_EPIC_LINE = re.compile(r"^\s*(?:-\s*)?(?:☐|✅|❌|⏳|\[ ?\])?\s*Epic\s+(\d{1,4}):\s*(.*)$")
RE_TASK_LINE = re.compile(r"^\s*(?:-\s*)?(?:☐|✅|❌|⏳|\[ ?\])?\s*Task\s+(\d{1,4}):\s*(.*)$")
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
    seen_epics_section = False
    current_epic: Optional[Epic] = None
    current_task: Optional[Task] = None

    for ln in backlog_lines:
        s = ln.strip()
        # detect section headers
        if s.startswith('## 1. Epics - open'):
            section = 'epics_open'
            seen_epics_section = True
            continue
        if s.startswith('## 2. Epics - finished'):
            section = 'epics_finished'
            seen_epics_section = True
            continue
        # Any other top-level '##' header after we've seen the Epics sections
        # should be treated as the start of the footer (do not attempt to
        # parse epics or tasks past this point).
        if s.startswith('## ') and seen_epics_section:
            section = 'footer'
            # append this heading to footer and skip parsing as epic/task
            footer.append(ln)
            current_epic = None
            current_task = None
            continue
        m = RE_EPIC_LINE.match(ln)
        if m:
            eid_raw, title = m.group(1), m.group(2)
            # normalize numeric ids to zero-padded 4-digit form when possible
            eid = f"{int(eid_raw):04d}" if eid_raw.isdigit() else eid_raw
            current_epic = Epic(id=eid, title=title.strip(), status="open")
            if section == "header":
                section = "epics_open"
            if section == "epics_open":
                epics_open.append(current_epic)
            elif section == "epics_finished":
                epics_finished.append(current_epic)
            # reset current task context when a new epic starts
            current_task = None
            continue
        m2 = RE_TASK_LINE.match(ln)
        if m2 and current_epic is not None:
            tid_raw, title = m2.group(1), m2.group(2)
            tid = f"{int(tid_raw):04d}" if tid_raw.isdigit() else tid_raw
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
                    # preserve other epic-level fields in raw_lines, but
                    # avoid preserving a 'tasks:' heading which will be
                    # rendered by the writer.
                    if not ln.strip().lower().startswith("- tasks:"):
                        current_epic.raw_lines.append(ln)
                # clear current task context after epic-level field
                current_task = None
                continue
        # fallback: preserve in raw_lines of current epic if present
        if section == 'footer':
            footer.append(ln)
            # ensure we don't accidentally treat footer lines as header
            continue

        if current_epic is not None:
            # clear current_task context so subsequent epic-level fields
            # (e.g., '  - status: open') are not mistakenly applied to the
            # previous task
            current_task = None
            # Avoid preserving a redundant 'tasks:' heading in raw_lines.
            if not ln.strip().lower().startswith("- tasks:"):
                current_epic.raw_lines.append(ln)
        else:
            header.append(ln)

    # as a simple model, put footer empty
    return Backlog(header=header, epics_open=epics_open, epics_finished=epics_finished, footer=footer)


def add_task_to_epic(backlog: Backlog, epic_id: str, title: str, notes: Optional[str] = None) -> Task:
    # Build a global set of ids (epic + task) to avoid collisions across epics and tasks
    existing_ids = {e.id for e in backlog.epics_open + backlog.epics_finished}
    existing_ids.update(t.id for ep in backlog.epics_open + backlog.epics_finished for t in ep.subtasks)
    for e in backlog.epics_open:
        if e.id == epic_id:
            # choose a monotonic id: max(existing numeric ids) + 1
            numeric_ids = [int(x) for x in existing_ids if x.isdigit()]
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
            e.subtasks.append(t)
            return t
    raise KeyError(f"epic {epic_id} not found")


def add_epic_to_backlog(backlog: Backlog, title: str, status: str = 'open') -> Epic:
    """Create a new epic with a unique zero-padded 4-digit id and append to epics_open.

    The id generator finds the next unused numeric id (0000..9999) not present
    in existing epics.
    """
    # Use a shared id pool between epics and tasks
    existing = {e.id for e in backlog.epics_open + backlog.epics_finished}
    existing.update(t.id for ep in backlog.epics_open + backlog.epics_finished for t in ep.subtasks)
    # find next available numeric id
    for i in range(0, 10000):
        cand = f"{i:04d}"
        if cand not in existing:
            new_id = cand
            break
    else:
        raise RuntimeError("no available epic ids")

    e = Epic(id=new_id, title=title, status=status, subtasks=[], raw_lines=[])
    backlog.epics_open.append(e)
    return e


def build_markdown(backlog: Backlog) -> str:
    lines: List[str] = []
    # If the original header contains explicit Epics section headings (from
    # the template), preserve their position by splicing our generated epic
    # content into the header in-place. This avoids moving the Epics sections
    # after an EOF marker or other footer content.
    hdr = backlog.header or []
    # find template markers if present
    idx1 = next((i for i, line in enumerate(hdr) if line.strip().startswith("## 1. Epics - open")), None)
    idx2 = next((i for i, line in enumerate(hdr) if line.strip().startswith("## 2. Epics - finished")), None)
    # detect EOF-like marker in header which should be treated as a footer boundary
    eof_idx = next((i for i, line in enumerate(hdr) if line.strip() in ("EOF", "---")), None)

    if idx1 is not None:
        # If an EOF marker appears *before* the epics marker in the header,
        # prefer to place the generated epics *before* that EOF so the EOF
        # remains at the end of the document (i.e., do not leave the Epics
        # sections after the EOF). In that case, treat the eof index as the
        # split point for the header tail.
        if eof_idx is not None and eof_idx < idx1:
            split_idx = eof_idx
            emit_header_head = hdr[:split_idx]
            emit_header_tail = hdr[split_idx:]
        else:
            # emit header up to the Epics - open marker
            emit_header_head = hdr[:idx1]
            # we'll treat the header tail as the content after the finished marker
            emit_footer_after_idx = (idx2 if idx2 is not None else idx1)
            emit_header_tail = hdr[emit_footer_after_idx + 1 :]
        lines.extend(emit_header_head)
        # ensure blank separator
        if lines and lines[-1].strip() != "":
            lines.append("")
    else:
        # no explicit epics markers found in the parsed header. However,
        # the original header may include an EOF/footer marker (e.g., "EOF"
        # or "---"). If present, emit header up to the EOF and treat the
        # remainder as the header tail so we insert Epics before the EOF.
        if eof_idx is not None:
            lines.extend(hdr[:eof_idx])
            emit_header_tail = hdr[eof_idx:]
        else:
            # otherwise emit the entire header and append epics after it
            lines.extend(hdr)
            emit_header_tail = []

    # Emit the Epics - open section (canonicalized)
    if not lines or lines[-1].strip() != "":
        lines.append("")
    lines.append("## 1. Epics - open")
    lines.append("")
    for e in backlog.epics_open:
        # Use configured symbol for open epic where available
        sym = None
        sym_map = cast(Dict[str, Any], values.get('symbol_map', {}) or {})
        for k, v in sym_map.items():
            if v == (e.status or '').strip().lower():
                sym = k
                break
        sym = sym or '☐'
        lines.append(f"- {sym} Epic {e.id}: {e.title}")
        lines.append(f"  - status: {e.status}")
        # Emit any preserved epic-level raw lines (description/notes) before the tasks list.
        # Normalize raw_lines by trimming leading/trailing blank lines and
        # collapsing consecutive blank lines to a single blank line so
        # adding new epics does not grow spurious vertical whitespace.
        if e.raw_lines:
            # normalize: strip leading/trailing blank lines
            rl = list(e.raw_lines)
            # remove leading blanks
            while rl and rl[0].strip() == "":
                rl.pop(0)
            # remove trailing blanks
            while rl and rl[-1].strip() == "":
                rl.pop()
            # collapse consecutive blank lines
            prev_blank = False
            for raw in rl:
                is_blank = raw.strip() == ""
                if is_blank and prev_blank:
                    # skip duplicate blank
                    continue
                lines.append(raw)
                prev_blank = is_blank
        # Always emit the tasks section as the last modeled element for the epic
        lines.append("  - tasks:")
        for t in e.subtasks:
            # task symbol resolved from status
            task_sym = None
            for k, v in sym_map.items():
                if v == (t.status or '').strip().lower():
                    task_sym = k
                    break
            task_sym = task_sym or '\u2610'
            lines.append(f"    - {task_sym} Task {t.id}: {t.title}")
            lines.append(f"      - status: {t.status}")
            if t.added:
                lines.append(f"      - added: {t.added}")
            if t.closed:
                lines.append(f"      - closed: {t.closed}")
            if t.notes:
                lines.append("      - Notes:")
                for n in t.notes:
                    lines.append(f"        - {n}")
        # separate epics with a blank line
        lines.append("")
    # Emit the Epics - finished section (render finished epics similar to open epics).
    lines.append("")
    lines.append("## 2. Epics - finished")
    lines.append("")
    for e in backlog.epics_finished:
        # resolve symbol for epic status (default to done/checkmark)
        sym = None
        sym_map = cast(Dict[str, Any], values.get('symbol_map', {}) or {})
        for k, v in sym_map.items():
            if v == (e.status or '').strip().lower():
                sym = k
                break
        sym = sym or '\u2705'
        lines.append(f"- {sym} Epic {e.id}: {e.title}")
        lines.append(f"  - status: {e.status}")
        # preserve epic-level raw lines
        if e.raw_lines:
            rl = list(e.raw_lines)
            while rl and rl[0].strip() == "":
                rl.pop(0)
            while rl and rl and rl[-1].strip() == "":
                rl.pop()
            prev_blank = False
            for raw in rl:
                is_blank = raw.strip() == ""
                if is_blank and prev_blank:
                    continue
                lines.append(raw)
                prev_blank = is_blank
        # emit tasks for finished epic
        lines.append("  - tasks:")
        for t in e.subtasks:
            task_sym = None
            for k, v in sym_map.items():
                if v == (t.status or '').strip().lower():
                    task_sym = k
                    break
            task_sym = task_sym or '\u2610'
            lines.append(f"    - {task_sym} Task {t.id}: {t.title}")
            lines.append(f"      - status: {t.status}")
            if t.added:
                lines.append(f"      - added: {t.added}")
            if t.closed:
                lines.append(f"      - closed: {t.closed}")
            if t.notes:
                lines.append("      - Notes:")
                for n in t.notes:
                    lines.append(f"        - {n}")
        # separate epics with a blank line
        lines.append("")

    # Now append any header tail (the original content that followed the
    # Epics markers in the template, e.g., Ideas/EOF or other footer lines)
    if emit_header_tail:
        lines.extend(emit_header_tail)
    else:
        # If no header tail was present, append the explicit backlog.footer
        lines.extend(backlog.footer)

    # Collapse runs of blank lines to at most one to avoid excessive vertical
    # whitespace caused by assembling header/raw_lines/tasks/footer pieces.
    compact: List[str] = []
    blank_count = 0
    for ln in lines:
        if ln.strip() == "":
            blank_count += 1
            if blank_count <= 1:
                compact.append("")
            else:
                # skip extra blank
                continue
        else:
            blank_count = 0
            compact.append(ln)

    return "\n".join(compact)


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
    finish_list = set(values.get('finish_statuses', ["done", "closed", "complete", "finished"]))
    if lower in finish_list:
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
    allowed = set(values.get('allowed_statuses', ["open", "done", "closed", "complete", "finished", "resolved", "in progress", "todo"]))
    for e in backlog.epics_open + backlog.epics_finished:
        if e.status and e.status.strip().lower() not in allowed:
            errors.append(f"unknown epic status for {e.id}: {e.status}")
        for t in e.subtasks:
            if t.status and t.status.strip().lower() not in allowed:
                errors.append(f"unknown task status for {t.id}: {t.status}")

    return errors


def reassign_duplicate_task_ids(backlog: Backlog) -> list[Tuple[str, str]]:
    """Find duplicate task ids and reassign new unique ids.

    Returns list of tuples (old_id, new_id) for changed tasks.
    """
    all_ids: list[str] = []
    for e in backlog.epics_open + backlog.epics_finished:
        for t in e.subtasks:
            all_ids.append(t.id)
    dup = {i for i in all_ids if all_ids.count(i) > 1}
    changed: list[Tuple[str, str]] = []
    if not dup:
        return changed

    existing = set(all_ids)
    # helper to make next available id (0000..9999)
    def next_id(start=0):
        for i in range(start, 10000):
            cand = f"{i:04d}"
            if cand not in existing:
                existing.add(cand)
                return cand
        raise RuntimeError("no available task ids")

    for e in backlog.epics_open + backlog.epics_finished:
        for t in e.subtasks:
            if t.id in dup:
                newid = next_id()
                changed.append((t.id, newid))
                t.id = newid
    return changed


def reassign_epic_task_collisions(backlog: Backlog) -> list[Tuple[str, str]]:
    """Detect ids used both for epics and tasks and reassign task ids to unique values.

    Returns list of (old_task_id, new_task_id).
    """
    changes: list[Tuple[str, str]] = []
    epic_ids = {e.id for e in backlog.epics_open + backlog.epics_finished}
    task_ids = [t.id for e in backlog.epics_open + backlog.epics_finished for t in e.subtasks]
    collisions = {tid for tid in task_ids if tid in epic_ids}
    if not collisions:
        return changes

    existing = set(epic_ids) | set(task_ids)

    def next_id(start=0):
        for i in range(start, 10000):
            cand = f"{i:04d}"
            if cand not in existing:
                existing.add(cand)
                return cand
        raise RuntimeError("no available ids")

    for e in backlog.epics_open + backlog.epics_finished:
        for t in e.subtasks:
            if t.id in collisions:
                new = next_id()
                changes.append((t.id, new))
                t.id = new
    return changes


def normalize_backlog_format(backlog: Backlog) -> list[str]:
    """Normalize status tokens and empty/placeholder dates; returns list of change descriptions."""
    changes: list[str] = []
    from . import values as _values
    for e in backlog.epics_open + backlog.epics_finished:
        # normalize epic status word
        if e.status:
            norm = e.status.strip()
            n = norm.lower()
            # map symbols or words
            mapped = None
            sym_map = cast(Dict[str, Any], _values.get('symbol_map', {}) or {})
            for k, v in sym_map.items():
                if k == norm or k == norm.strip():
                    mapped = v
                    break
            if not mapped:
                word_map = cast(Dict[str, str], _values.get('word_map', {}) or {})
                mapped = word_map.get(n, n)
            if mapped != e.status:
                changes.append(f"epic {e.id} status: {e.status} -> {mapped}")
                e.status = mapped
        for t in e.subtasks:
            if t.status:
                n = t.status.strip().lower()
                word_map = cast(Dict[str, str], _values.get('word_map', {}) or {})
                mapped = word_map.get(n, t.status)
                if mapped != t.status:
                    changes.append(f"task {t.id} status: {t.status} -> {mapped}")
                    t.status = mapped
            # normalize placeholder closed dates like em-dash '—' or dash
            if t.closed and t.closed.strip() in ('—', '-', '—'):
                changes.append(f"task {t.id} closed: {t.closed} -> ''")
                t.closed = None
    return changes

