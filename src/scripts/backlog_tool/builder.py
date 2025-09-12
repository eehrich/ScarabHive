"""Markdown building functionality for backlog serialization.

This module contains functions for converting Backlog objects back to
markdown format.
"""
import re
from typing import Any, Dict, List, cast, Optional

from .models import Backlog
from . import values


def build_markdown(backlog: Backlog) -> str:
    """Convert a Backlog object back to markdown format.

    Args:
        backlog: The backlog to convert to markdown

    Returns:
        Markdown string representation of the backlog
    """
    lines: List[str] = []

    # Helper to remove raw_blocks corresponding to modeled fields
    def _strip_modeled_blocks_global(raw_lines: list[str], modeled_keys: Optional[set[str]] = None) -> list[str]:
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
        status_lower = (e.status or '').strip().lower()
        for k, v in sym_map.items():
            if isinstance(v, list):
                if status_lower in v:
                    sym = k
                    break
            elif v == status_lower:
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
                # Only add non-empty notes to avoid extra blank lines
                if n.strip():
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
            status_lower = (t.status or '').strip().lower()
            for k, v in sym_map.items():
                if isinstance(v, list):
                    if status_lower in v:
                        task_sym = k
                        break
                elif v == status_lower:
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
                    # Only add non-empty notes to avoid extra blank lines
                    if n.strip():
                        lines.append(f"        - {n}")

            # Preserve any raw_lines after structured fields for tasks
            if t.raw_lines:
                modeled_task = {'status', 'added', 'closed', 'description', 'notes'}
                rl = _strip_modeled_blocks_global(list(t.raw_lines), modeled_task)
                for line in rl:
                    lines.append(f"      {line}")
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
        status_lower = (e.status or '').strip().lower()
        for k, v in sym_map.items():
            if isinstance(v, list):
                if status_lower in v:
                    sym = k
                    break
            elif v == status_lower:
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
                # Only add non-empty notes to avoid extra blank lines
                if n.strip():
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
            while rl and rl[-1].strip() == "":
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
            status_lower = (t.status or '').strip().lower()
            for k, v in sym_map.items():
                if isinstance(v, list):
                    if status_lower in v:
                        task_sym = k
                        break
                elif v == status_lower:
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
                    # Only add non-empty notes to avoid extra blank lines
                    if n.strip():
                        lines.append(f"        - {n}")

            # Preserve any raw_lines after structured fields for tasks
            if t.raw_lines:
                modeled_task = {'status', 'added', 'closed', 'description', 'notes'}
                rl = _strip_modeled_blocks_global(list(t.raw_lines), modeled_task)
                for line in rl:
                    lines.append(f"      {line}")
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
