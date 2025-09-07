"""A robust, tolerant markdown backlog parser and writer with comprehensive error handling.

The parser is conservative: it identifies top-level sections (Epics - open,
Epics - finished) and parses epics/tasks with minimal structure. It intentionally
keeps unknown content as raw lines so re-serialization preserves non-modeled
content.

Features:
- Robust error handling and recovery
- Comprehensive validation
- Atomic file operations
- Backup and restore capabilities
- Detailed logging for debugging
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple, cast
import re
import os
import logging
from datetime import date
from . import values
import shutil
import time

# Set up logger
logger = logging.getLogger(__name__)


@dataclass
class Task:
    id: str
    title: str
    status: str
    # multiline description (optional)
    description: List[str] = field(default_factory=list)
    # timestamps
    added: Optional[str] = None
    closed: Optional[str] = None
    # list of note lines
    notes: List[str] = field(default_factory=list)
    # preserve any unknown lines so we can round-trip
    raw_lines: List[str] = field(default_factory=list)


@dataclass
class Epic(Task):
    # the tasks contained by this epic
    tasks: List[Task] = field(default_factory=list)


@dataclass
class Backlog:
    header: List[str]
    epics_open: List[Epic]
    epics_finished: List[Epic]
    footer: List[str]


RE_EPIC_LINE = re.compile(r"^\s*(?:-\s*)?(?:☐|✅|❌|⏳|\[ ?\])?\s*Epic\s+(\d+):\s*(.*)$")
RE_TASK_LINE = re.compile(r"^\s*(?:-\s*)?(?:☐|✅|❌|⏳|\[ ?\])?\s*Task\s+(\d+):\s*(.*)$")
RE_FIELD_LINE = re.compile(r"^\s*-\s*(\w+):\s*(.*)$")


def read_file(path: str) -> List[str]:
    """Read a file and return its lines with robust error handling.

    Args:
        path: Path to the file to read

    Returns:
        List of lines from the file

    Raises:
        FileNotFoundError: If file doesn't exist
        IOError: If reading fails
        UnicodeDecodeError: If file encoding is invalid
    """
    if not isinstance(path, str) or not path.strip():
        raise ValueError("Invalid file path provided")

    logger.debug(f"Reading file: {path}")

    try:
        with open(path, 'r', encoding='utf-8') as f:
            lines = f.readlines()

        # Ensure all lines end with newlines for consistency
        processed_lines = []
        for line in lines:
            if not line.endswith('\n'):
                line += '\n'
            processed_lines.append(line)

        logger.info(f"Successfully read {len(processed_lines)} lines from {path}")
        return processed_lines

    except FileNotFoundError:
        logger.error(f"File not found: {path}")
        raise
    except UnicodeDecodeError as e:
        logger.error(f"Encoding error reading {path}: {e}")
        raise
    except IOError as e:
        logger.error(f"IO error reading {path}: {e}")
        raise
    except Exception as e:
        logger.error(f"Unexpected error reading {path}: {e}")
        raise IOError(f"Failed to read file {path}: {e}") from e


def parse(backlog_lines: List[str]) -> Backlog:
    """Parse backlog markdown lines into a Backlog object with robust error handling.

    Args:
        backlog_lines: List of markdown lines to parse

    Returns:
        Backlog object with parsed content

    Raises:
        ValueError: If input is severely malformed and cannot be parsed
    """
    logger.debug(f"Starting parse of {len(backlog_lines)} lines")

    # Input validation
    if not isinstance(backlog_lines, list):
        raise ValueError("backlog_lines must be a list of strings")

    if not backlog_lines:
        logger.warning("Empty backlog_lines provided, returning empty backlog")
        return Backlog(header=[], epics_open=[], epics_finished=[], footer=[])

    # Validate that all lines are strings
    for i, line in enumerate(backlog_lines):
        if not isinstance(line, str):
            logger.error(f"Line {i} is not a string: {type(line)}")
            raise ValueError(f"All lines must be strings, but line {i} is {type(line)}")

    header: List[str] = []
    footer: List[str] = []
    epics_open: List[Epic] = []
    epics_finished: List[Epic] = []

    section = "header"
    seen_epics_section = False
    current_epic: Optional[Epic] = None
    current_task: Optional[Task] = None
    current_collect: Optional[Tuple[str, int, str]] = None
    parse_errors: List[str] = []

    try:
        for line_num, ln in enumerate(backlog_lines, 1):
            try:
                s = ln.strip()

                # detect section headers
                if s.startswith('## 1. Epics - open'):
                    section = 'epics_open'
                    seen_epics_section = True
                    logger.debug(f"Found epics_open section at line {line_num}")
                    continue
                if s.startswith('## 2. Epics - finished'):
                    section = 'epics_finished'
                    seen_epics_section = True
                    logger.debug(f"Found epics_finished section at line {line_num}")
                    continue

                # Any other top-level '##' header after we've seen the Epics sections
                # should be treated as the start of the footer
                if s.startswith('## ') and seen_epics_section:
                    section = 'footer'
                    logger.debug(f"Found footer section at line {line_num}")
                    footer.append(ln)
                    current_epic = None
                    current_task = None
                    continue

                m = RE_EPIC_LINE.match(ln)
                if m:
                    try:
                        eid_raw, title = m.group(1), m.group(2)
                        # normalize numeric ids to zero-padded 4-digit form when possible
                        if eid_raw.isdigit():
                            eid = f"{int(eid_raw):04d}"
                        else:
                            eid = eid_raw

                        current_epic = Epic(id=eid, title=title.strip(), status="open")
                        if section == "header":
                            section = "epics_open"

                        if section == "epics_open":
                            epics_open.append(current_epic)
                            logger.debug(f"Added epic {eid} to open section")
                        elif section == "epics_finished":
                            epics_finished.append(current_epic)
                            logger.debug(f"Added epic {eid} to finished section")
                        else:
                            logger.warning(f"Epic {eid} found in unexpected section '{section}' at line {line_num}")

                        # reset current task context when a new epic starts
                        current_task = None
                        continue
                    except (ValueError, IndexError) as e:
                        parse_errors.append(f"Failed to parse epic at line {line_num}: {e}")
                        logger.error(f"Epic parsing error at line {line_num}: {e}")
                        continue

                m2 = RE_TASK_LINE.match(ln)
                if m2 and current_epic is not None:
                    try:
                        tid_raw, title = m2.group(1), m2.group(2)
                        if tid_raw.isdigit():
                            tid = f"{int(tid_raw):04d}"
                        else:
                            tid = tid_raw

                        current_task = Task(id=tid, title=title.strip(), status="open")
                        current_epic.tasks.append(current_task)
                        logger.debug(f"Added task {tid} to epic {current_epic.id}")
                        continue
                    except (ValueError, IndexError) as e:
                        parse_errors.append(f"Failed to parse task at line {line_num}: {e}")
                        logger.error(f"Task parsing error at line {line_num}: {e}")
                        continue
                elif m2 and current_epic is None:
                    parse_errors.append(f"Task found at line {line_num} but no current epic")
                    logger.warning(f"Orphaned task at line {line_num}: {ln.strip()}")
                    continue

                # fields under task or epic
                indent = len(ln) - len(ln.lstrip(' '))
                m3 = RE_FIELD_LINE.match(ln.strip())
                if m3:
                    try:
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
                                if val.strip():
                                    current_task.notes = [val.strip()]
                                    current_collect = None
                                else:
                                    current_task.notes = []
                                    current_collect = ("notes", indent, "task")
                            elif key == "description":
                                if val.strip():
                                    current_task.description = [val.strip()]
                                    current_collect = None
                                else:
                                    current_task.description = []
                                    current_collect = ("description", indent, "task")
                            continue

                        # treat as epic-level field if indent < 4 and we have a current epic
                        if indent < 4 and current_epic is not None:
                            if key == "status":
                                current_epic.status = val.strip()
                            elif key == "added":
                                current_epic.added = val.strip()
                            elif key == "closed":
                                current_epic.closed = val.strip()
                            elif key == "notes":
                                if val.strip():
                                    current_epic.notes = [val.strip()]
                                    current_collect = None
                                else:
                                    current_epic.notes = []
                                    current_collect = ("notes", indent, "epic")
                            elif key == "description":
                                if val.strip():
                                    current_epic.description = [val.strip()]
                                    current_collect = None
                                else:
                                    current_epic.description = []
                                    current_collect = ("description", indent, "epic")
                            else:
                                # preserve other epic-level fields in raw_lines
                                if not ln.strip().lower().startswith("- tasks:"):
                                    current_epic.raw_lines.append(ln)
                            # clear current task context after epic-level field
                            current_task = None
                            continue
                    except (ValueError, IndexError) as e:
                        parse_errors.append(f"Failed to parse field at line {line_num}: {e}")
                        logger.error(f"Field parsing error at line {line_num}: {e}")
                        continue

                # Handle multiline field collection
                if current_collect is not None:
                    try:
                        name, col_indent, target = current_collect

                        if ln.strip() == "":
                            if target == "task" and current_task is not None:
                                if name == "notes":
                                    current_task.notes.append("")
                                else:
                                    current_task.description.append("")
                                continue
                            if target == "epic" and current_epic is not None:
                                if name == "notes":
                                    current_epic.notes.append("")
                                else:
                                    current_epic.description.append("")
                                continue

                        if indent > col_indent:
                            text = ln.strip()
                            if name == "notes" and text.startswith("- "):
                                content = text[2:].strip()
                            else:
                                content = text

                            if target == "task" and current_task is not None:
                                if name == "notes":
                                    current_task.notes.append(content)
                                else:
                                    current_task.description.append(content)
                                continue
                            if target == "epic" and current_epic is not None:
                                if name == "notes":
                                    current_epic.notes.append(content)
                                else:
                                    current_epic.description.append(content)
                                continue
                        else:
                            # ended collection
                            current_collect = None
                    except Exception as e:
                        parse_errors.append(f"Failed to collect multiline field at line {line_num}: {e}")
                        logger.error(f"Multiline collection error at line {line_num}: {e}")
                        current_collect = None
                        continue

                # fallback: preserve in raw_lines or header/footer
                if section == 'footer':
                    footer.append(ln)
                    continue

                if current_epic is not None:
                    current_task = None
                    if not ln.strip().lower().startswith("- tasks:"):
                        current_epic.raw_lines.append(ln)
                else:
                    header.append(ln)

            except Exception as e:
                parse_errors.append(f"Unexpected error at line {line_num}: {e}")
                logger.error(f"Unexpected parsing error at line {line_num}: {e}")
                continue

    except Exception as e:
        logger.error(f"Critical parsing error: {e}")
        raise ValueError(f"Failed to parse backlog: {e}") from e

    # Log parsing summary
    total_epics = len(epics_open) + len(epics_finished)
    total_tasks = sum(len(e.tasks) for e in epics_open + epics_finished)
    logger.info(f"Parsed {total_epics} epics with {total_tasks} tasks")

    if parse_errors:
        logger.warning(f"Encountered {len(parse_errors)} parsing errors: {parse_errors[:3]}...")
        # Don't fail completely, but log the issues

    return Backlog(header=header, epics_open=epics_open, epics_finished=epics_finished, footer=footer)


def add_task_to_epic(backlog: Backlog, epic_id: str, title: str, notes: Optional[str] = None, forced_id: Optional[str] = None) -> Task:
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
    e.raw_lines = []
    backlog.epics_open.append(e)
    return e


def build_markdown(backlog: Backlog) -> str:
    lines: List[str] = []
    # Helper to remove raw_blocks corresponding to modeled fields
    def _strip_modeled_blocks_global(raw_lines: list[str], modeled_keys: set[str] | None = None) -> list[str]:
        """Remove raw_lines blocks that correspond to modeled fields.

        Only removes blocks for keys that are present in `modeled_keys`.
        This preserves explicit raw blocks like a `- notes:` block when the
        epic/task does not have a modeled `notes` field parsed.
        """
        out: list[str] = []
        i = 0
        # If no modeled keys provided, return raw lines unchanged
        if not modeled_keys:
            return list(raw_lines)
        key_pattern = '|'.join(re.escape(k) for k in modeled_keys)
        key_re = re.compile(rf"^\s*-\s*({key_pattern}):", flags=re.I)
        while i < len(raw_lines):
            ln = raw_lines[i]
            if key_re.match(ln.strip()):
                base_indent = len(ln) - len(ln.lstrip(' '))
                i += 1
                while i < len(raw_lines):
                    nxt = raw_lines[i]
                    nxt_indent = len(nxt) - len(nxt.lstrip(' '))
                    if nxt.strip() == '':
                        i += 1
                        continue
                    if nxt_indent > base_indent:
                        i += 1
                        continue
                    break
                continue
            out.append(ln)
            i += 1
        return out
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
        # emit epic-level structured fields if present
        if e.added:
            lines.append(f"  - added: {e.added}")
        if e.closed:
            lines.append(f"  - closed: {e.closed}")
        if e.description:
            # If description is a single short line, prefer inline form to
            # preserve authoring style (avoid converting inline -> block).
            if len(e.description) == 1 and e.description[0] != "" and "\n" not in e.description[0]:
                lines.append(f"  - description: {e.description[0]}")
            else:
                lines.append("  - description:")
                for d in e.description:
                    if d == "":
                        lines.append("")
                    else:
                        lines.append(f"    {d}")
        if e.notes:
            lines.append("  - notes:")
            for n in e.notes:
                # render an explicit blank line between note list items
                if n == "":
                    lines.append("")
                else:
                    lines.append(f"    - {n}")
        # Preserve any raw_lines after structured fields
        if e.raw_lines:
            modeled = set()
            for k in ('notes', 'description', 'added', 'closed'):
                if getattr(e, k, None):
                    modeled.add(k)
            rl = _strip_modeled_blocks_global(list(e.raw_lines), modeled)
            while rl and rl[0].strip() == "":
                rl.pop(0)
            while rl and rl[-1].strip() == "":
                rl.pop()
            prev_blank = False
            for raw in rl:
                is_blank = raw.strip() == ""
                if is_blank and prev_blank:
                    continue
                lines.append(raw)
                prev_blank = is_blank
        # Always emit the tasks section as the last modeled element for the epic
        lines.append("  - tasks:")
        for t in e.tasks:
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
            if t.description:
                # prefer inline when single-line
                if len(t.description) == 1 and t.description[0] != "" and "\n" not in t.description[0]:
                    lines.append(f"      - description: {t.description[0]}")
                else:
                    lines.append("      - description:")
                    for d in t.description:
                        if d == "":
                            lines.append("")
                        else:
                            lines.append(f"        {d}")
            if t.notes:
                lines.append("      - notes:")
                for n in t.notes:
                    if n == "":
                        lines.append("")
                    else:
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
        # emit epic-level structured fields if present (same as open epics)
        if e.added:
            lines.append(f"  - added: {e.added}")
        if e.closed:
            lines.append(f"  - closed: {e.closed}")
        if e.description:
            if len(e.description) == 1 and e.description[0] != "" and "\n" not in e.description[0]:
                lines.append(f"  - description: {e.description[0]}")
            else:
                lines.append("  - description:")
                for d in e.description:
                    if d == "":
                        lines.append("")
                    else:
                        lines.append(f"    {d}")
        if e.notes:
            lines.append("  - notes:")
            for n in e.notes:
                if n == "":
                    lines.append("")
                else:
                    lines.append(f"    - {n}")

        # preserve epic-level raw lines (strip modeled blocks like notes/description/added/closed)
        if e.raw_lines:
            modeled = set()
            for k in ('notes', 'description', 'added', 'closed'):
                if getattr(e, k, None):
                    modeled.add(k)
            rl = _strip_modeled_blocks_global(list(e.raw_lines), modeled)
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
        for t in e.tasks:
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
            if t.description:
                lines.append("      - description:")
                for d in t.description:
                    if d == "":
                        lines.append("")
                    else:
                        lines.append(f"        {d}")
            if t.notes:
                lines.append("      - notes:")
                for n in t.notes:
                    if n == "":
                        lines.append("")
                    else:
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
    """Atomically write text to file with backup and error handling.

    Args:
        path: File path to write to
        text: Content to write

    Raises:
        IOError: If writing fails
        OSError: If file system operations fail
    """
    if not isinstance(path, str) or not path.strip():
        raise ValueError("Invalid file path provided")

    if not isinstance(text, str):
        raise ValueError("Text content must be a string")

    tmp = path + ".tmp"
    logger.debug(f"Starting atomic write to {path}")

    try:
        # Ensure the directory exists (only if path contains directories)
        dir_path = os.path.dirname(path)
        if dir_path and dir_path != '.':
            os.makedirs(dir_path, exist_ok=True)

        # Write to temporary file first
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(text)

        # Atomic replace
        os.replace(tmp, path)
        logger.info(f"Successfully wrote {len(text)} characters to {path}")

    except Exception as e:
        # Clean up temp file if it exists
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except Exception:
            pass  # Ignore cleanup errors

        logger.error(f"Failed to write to {path}: {e}")
        raise IOError(f"Failed to write file {path}: {e}") from e


def make_backup(path: str) -> str:
    """Create a timestamped backup of `path` under a .backups directory.

    Args:
        path: File path to backup

    Returns:
        Path to the created backup file

    Raises:
        IOError: If backup creation fails
        FileNotFoundError: If source file doesn't exist
    """
    if not isinstance(path, str) or not path.strip():
        raise ValueError("Invalid file path provided")

    if not os.path.exists(path):
        raise FileNotFoundError(f"Source file does not exist: {path}")

    try:
        p = os.path.abspath(path)
        d = os.path.dirname(p)
        backups_dir = os.path.join(d, '.backups')
        os.makedirs(backups_dir, exist_ok=True)

        ts = time.strftime('%Y%m%d_%H%M%S')
        base = os.path.basename(p)
        bak = f"{base}.{ts}.bak"
        dest = os.path.join(backups_dir, bak)

        shutil.copy2(p, dest)
        logger.info(f"Created backup: {dest}")
        return dest

    except Exception as e:
        logger.error(f"Failed to create backup for {path}: {e}")
        raise IOError(f"Failed to create backup: {e}") from e


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
        for t in e.tasks:
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
    """Run comprehensive validation rules and return list of error strings.

    Validation rules:
    - Epic ids must be unique across open and finished lists.
    - Task ids must be unique across all epics.
    - Date fields (added, closed) must be ISO dates YYYY-MM-DD when present.
    - Status values must be among allowed set.
    - Epics with all tasks finished should be marked as finished.
    - ID format validation (4-digit numeric preferred).
    - Required fields presence.

    Args:
        backlog: Backlog object to validate

    Returns:
        List of error messages (empty if valid)
    """
    if not isinstance(backlog, Backlog):
        return ["Invalid backlog object provided"]

    errors: list[str] = []
    logger.debug("Starting backlog validation")

    try:
        # Check epic ids uniqueness
        epic_ids = [e.id for e in backlog.epics_open + backlog.epics_finished]
        dup_epics = {i for i in epic_ids if epic_ids.count(i) > 1}
        for de in sorted(dup_epics):
            errors.append(f"duplicate epic id: {de}")

        # Check task ids uniqueness
        task_ids: list[str] = []
        for e in backlog.epics_open + backlog.epics_finished:
            for t in e.tasks:
                task_ids.append(t.id)
        dup_tasks = {i for i in task_ids if task_ids.count(i) > 1}
        for dt in sorted(dup_tasks):
            errors.append(f"duplicate task id: {dt}")

        # Date format validation
        import datetime
        def is_iso_date(s: Optional[str]) -> bool:
            if not s:
                return True
            try:
                datetime.date.fromisoformat(s)
                return True
            except Exception:
                return False

        # Validate epic fields
        for e in backlog.epics_open + backlog.epics_finished:
            if not e.id or not e.id.strip():
                errors.append(f"epic missing id: {e.title}")
            if not e.title or not e.title.strip():
                errors.append(f"epic {e.id} missing title")

            if not is_iso_date(e.added):
                errors.append(f"bad date (added) for epic {e.id}: {e.added}")
            if not is_iso_date(e.closed):
                errors.append(f"bad date (closed) for epic {e.id}: {e.closed}")

            # Validate task fields
            for t in e.tasks:
                if not t.id or not t.id.strip():
                    errors.append(f"task in epic {e.id} missing id: {t.title}")
                if not t.title or not t.title.strip():
                    errors.append(f"task {t.id} in epic {e.id} missing title")

                if not is_iso_date(t.added):
                    errors.append(f"bad date (added) for task {t.id}: {t.added}")
                if not is_iso_date(t.closed):
                    errors.append(f"bad date (closed) for task {t.id}: {t.closed}")

        # Status values validation
        allowed = set(values.get('allowed_statuses', ["open", "done", "closed", "complete", "finished", "resolved", "in progress", "todo"]))
        for e in backlog.epics_open + backlog.epics_finished:
            if e.status and e.status.strip().lower() not in allowed:
                errors.append(f"unknown epic status for {e.id}: {e.status}")
            for t in e.tasks:
                if t.status and t.status.strip().lower() not in allowed:
                    errors.append(f"unknown task status for {t.id}: {t.status}")

        # Check if epics should be finished
        finish_list = set(values.get('finish_statuses', ["done", "closed", "complete", "finished"]))
        for e in backlog.epics_open + backlog.epics_finished:
            if not getattr(e, 'tasks', None):
                continue

            all_finished = True
            for t in e.tasks:
                st = (t.status or '').strip().lower()
                if st not in finish_list:
                    all_finished = False
                    break

            if all_finished:
                est = (e.status or '').strip().lower()
                if est not in finish_list:
                    errors.append(f"epic {e.id} not finished but all tasks are finished")

        # ID format validation (prefer 4-digit numeric)
        for e in backlog.epics_open + backlog.epics_finished:
            if e.id and e.id.isdigit() and len(e.id) != 4:
                errors.append(f"epic id {e.id} should be 4 digits")
            for t in e.tasks:
                if t.id and t.id.isdigit() and len(t.id) != 4:
                    errors.append(f"task id {t.id} should be 4 digits")

        logger.info(f"Validation completed: {len(errors)} errors found")

    except Exception as e:
        errors.append(f"Validation failed with exception: {e}")
        logger.error(f"Validation exception: {e}")

    return errors


def reassign_duplicate_task_ids(backlog: Backlog) -> list[Tuple[str, str]]:
    """Find duplicate task ids and reassign new unique ids.

    Returns list of tuples (old_id, new_id) for changed tasks.
    """
    # Build a list of all task ids and detect duplicates among tasks only
    all_task_ids: list[str] = []
    for e in backlog.epics_open + backlog.epics_finished:
        for t in e.tasks:
            all_task_ids.append(t.id)
    dup = {i for i in all_task_ids if all_task_ids.count(i) > 1}
    changed: list[Tuple[str, str]] = []
    if not dup:
        return changed

    # existing pool must include epic ids as well to avoid collisions
    existing = {e.id for e in backlog.epics_open + backlog.epics_finished} | set(all_task_ids)

    # choose start = max numeric existing id + 1 for monotonic allocation
    # Only consider numeric ids that are 4 digits or less (canonical backlog ids).
    # Migration artifacts or auxiliary ids may be longer (e.g. '900100') and
    # should not be used to compute the next 4-digit id — they would push the
    # starting point beyond the 4-digit range and make allocation fail.
    numeric_existing = [int(x) for x in existing if x.isdigit() and len(x) <= 4]
    start = (max(numeric_existing) + 1) if numeric_existing else 0

    def next_id(start_idx=start):
        for i in range(start_idx, 10000):
            cand = f"{i:04d}"
            if cand not in existing:
                existing.add(cand)
                return cand
        raise RuntimeError("no available task ids")

    for e in backlog.epics_open + backlog.epics_finished:
        for t in e.tasks:
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
    task_ids = [t.id for e in backlog.epics_open + backlog.epics_finished for t in e.tasks]
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
        for t in e.tasks:
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
    for t in e.tasks:
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


def auto_fix_date_formats(backlog: Backlog) -> list[str]:
    """Auto-fix common date format issues by converting to ISO format.
    
    Handles formats like:
    - MM/DD/YYYY -> YYYY-MM-DD
    - DD-MM-YYYY -> YYYY-MM-DD  
    - YYYY/MM/DD -> YYYY-MM-DD
    - Month DD, YYYY -> YYYY-MM-DD
    
    Returns list of change descriptions.
    """
    changes: list[str] = []
    import re
    from datetime import datetime
    
    def parse_and_convert_date(date_str: str) -> str | None:
        """Try to parse various date formats and return ISO format."""
        date_str = date_str.strip()
        
        # Already ISO format
        if re.match(r'^\d{4}-\d{2}-\d{2}$', date_str):
            return None
            
        # MM/DD/YYYY
        match = re.match(r'^(\d{1,2})/(\d{1,2})/(\d{4})$', date_str)
        if match:
            month, day, year = match.groups()
            try:
                # Validate the date
                datetime(int(year), int(month), int(day))
                return f"{year}-{month.zfill(2)}-{day.zfill(2)}"
            except ValueError:
                return None
                
        # DD-MM-YYYY
        match = re.match(r'^(\d{1,2})-(\d{1,2})-(\d{4})$', date_str)
        if match:
            day, month, year = match.groups()
            try:
                datetime(int(year), int(month), int(day))
                return f"{year}-{month.zfill(2)}-{day.zfill(2)}"
            except ValueError:
                return None
                
        # YYYY/MM/DD
        match = re.match(r'^(\d{4})/(\d{1,2})/(\d{1,2})$', date_str)
        if match:
            year, month, day = match.groups()
            try:
                datetime(int(year), int(month), int(day))
                return f"{year}-{month.zfill(2)}-{day.zfill(2)}"
            except ValueError:
                return None
                
        # Try to parse with dateutil if available (more flexible)
        try:
            import dateutil.parser
            parsed = dateutil.parser.parse(date_str)
            return parsed.date().isoformat()
        except (ImportError, ValueError):
            pass
            
        return None
    
    for e in backlog.epics_open + backlog.epics_finished:
        if e.added:
            converted = parse_and_convert_date(e.added)
            if converted and converted != e.added:
                changes.append(f"epic {e.id} added date: {e.added} -> {converted}")
                e.added = converted
                
        if e.closed:
            converted = parse_and_convert_date(e.closed)
            if converted and converted != e.closed:
                changes.append(f"epic {e.id} closed date: {e.closed} -> {converted}")
                e.closed = converted
                
        for t in e.tasks:
            if t.added:
                converted = parse_and_convert_date(t.added)
                if converted and converted != t.added:
                    changes.append(f"task {t.id} added date: {t.added} -> {converted}")
                    t.added = converted
                    
            if t.closed:
                converted = parse_and_convert_date(t.closed)
                if converted and converted != t.closed:
                    changes.append(f"task {t.id} closed date: {t.closed} -> {converted}")
                    t.closed = converted
                    
    return changes


def auto_fix_id_formats(backlog: Backlog) -> list[str]:
    """Auto-fix ID format issues by converting non-4-digit numeric IDs to 4-digit format.
    
    Returns list of change descriptions.
    """
    changes: list[str] = []
    
    # Collect all existing IDs to avoid collisions
    existing_ids = {e.id for e in backlog.epics_open + backlog.epics_finished}
    for e in backlog.epics_open + backlog.epics_finished:
        for t in e.tasks:
            existing_ids.add(t.id)
    
    def normalize_id(id_str: str) -> str | None:
        """Convert numeric ID to 4-digit format if needed."""
        if not id_str or not id_str.isdigit():
            return None
        if len(id_str) == 4:
            return None  # Already correct
        if len(id_str) > 4:
            return None  # Don't auto-fix long IDs
            
        # Pad with zeros
        normalized = id_str.zfill(4)
        
        # Check for collision
        if normalized in existing_ids and normalized != id_str:
            return None  # Can't fix due to collision
            
        return normalized
    
    for e in backlog.epics_open + backlog.epics_finished:
        normalized = normalize_id(e.id)
        if normalized and normalized != e.id:
            changes.append(f"epic id format: {e.id} -> {normalized}")
            existing_ids.remove(e.id)
            existing_ids.add(normalized)
            e.id = normalized
            
        for t in e.tasks:
            normalized = normalize_id(t.id)
            if normalized and normalized != t.id:
                changes.append(f"task id format: {t.id} -> {normalized}")
                existing_ids.remove(t.id)
                existing_ids.add(normalized)
                t.id = normalized
                
    return changes


def auto_complete_epics(backlog: Backlog) -> list[str]:
    """Auto-complete epics that have all tasks finished but epic not marked as finished.
    
    Returns list of change descriptions.
    """
    changes: list[str] = []
    from . import values as _values
    
    finish_list = set(_values.get('finish_statuses', ["done", "closed", "complete", "finished"]))
    
    for e in backlog.epics_open + backlog.epics_finished:
        if not getattr(e, 'tasks', None) or not e.tasks:
            continue
            
        # Check if all tasks are finished
        all_finished = True
        for t in e.tasks:
            st = (t.status or '').strip().lower()
            if st not in finish_list:
                all_finished = False
                break
                
        if all_finished:
            est = (e.status or '').strip().lower()
            if est not in finish_list:
                # Auto-complete the epic
                new_status = 'done'  # Default completion status
                changes.append(f"epic {e.id} auto-completed: {e.status or 'open'} -> {new_status}")
                e.status = new_status
                
                # Move to finished epics if it's currently in open
                if e in backlog.epics_open:
                    backlog.epics_open.remove(e)
                    backlog.epics_finished.append(e)
                    changes.append(f"epic {e.id} moved to finished section")
                    
    return changes

