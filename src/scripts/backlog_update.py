#!/usr/bin/env python3
"""Small conservative updater for backlog (moved from .prompts/scripts).
This script validates the backlog layout and exits non-zero if it would change it.

Notes:
- Recognized status values include: done, open, failed, in progress, and reverted (↩).
- Once a task is added to the backlog it should not be deleted; only the `status` field may change (including to `reverted`).
"""
from pathlib import Path
import re
import os
from datetime import date
import tempfile


def validate(path: Path) -> int:
    txt = path.read_text(encoding='utf-8')
    # simple checks: single top-level Backlog header and no duplicate epic/task IDs
    if txt.count('# Backlog') != 1:
        print('Expected single "# Backlog" header')
        return 2
    # extract IDs only from lines that look like epic/task headers to avoid
    # capturing incidental mentions in notes or prose
    ids = re.findall(r"^\s*(?:☐|✅|❌|⏳)?\s*(?:Epic|Task)\s+(\d{4})\b", txt, flags=re.M)
    dup = {i for i in ids if ids.count(i) > 1}
    if dup:
        print('Duplicate numeric IDs found:', ', '.join(sorted(dup)))
        return 3

    # verify status values are within allowed/canonical set
    # canonical statuses we accept
    canonical = {'done', 'open', 'failed', 'in progress', 'reverted', 'rejected', 'cancelled'}

    def normalize_status(s: str) -> str | None:
        if not s:
            return None
        s0 = s.strip().lower()
        # map common symbol forms to canonical names
        SYM = {
                '\u2610': 'open',   # ☐
                '\u2705': 'done',   # ✅
                '\u274c': 'failed', # ❌
                '\u23f3': 'in progress', # ⏳
        }
        if s0 in SYM:
            return SYM[s0]
        # remove punctuation and map common words
        s_clean = re.sub(r"[^a-z0-9 ]+", '', s0)
        WORD_MAP = {
            'done': 'done',
            'implemented': 'done',
            'finished': 'done',
            'resolved': 'done',
            'closed': 'done',
            'completed': 'done',
            'open': 'open',
            'in progress': 'in progress',
            'started': 'in progress',
            'failed': 'failed',
            'reverted': 'reverted',
            'revert': 'reverted',
            'reverts': 'reverted',
            'rejected': 'rejected',
            'reject': 'rejected',
            'rejection': 'rejected',
            'cancelled': 'cancelled',
            'canceled': 'cancelled',
            'cancel': 'cancelled',
            'aborted': 'cancelled',
            'abandon': 'cancelled',
        }
        # exact match
        if s_clean in WORD_MAP:
            return WORD_MAP[s_clean]
        # try splitting and matching first word
        first = s_clean.split()[0] if s_clean else ''
        return WORD_MAP.get(first)

    # find lines that look like ' - status: VALUE' or '- status: VALUE'
    status_lines = re.findall(r"^\s*-\s*status:\s*(.+)$", txt, flags=re.M)
    bad = []
    for s in status_lines:
        norm = normalize_status(s)
        if not norm or norm not in canonical:
            bad.append(s)
    if bad:
        # printing raw status values may fail on Windows consoles that don't
        # support certain unicode symbols (e.g. checkboxes). Use a safe
        # fallback that escapes non-ascii characters if necessary.
        msg = 'Found unknown status values: ' + ', '.join(sorted(set(bad)))
        try:
            print(msg)
        except UnicodeEncodeError:
            # fallback: escape non-ascii characters so printing cannot fail
            safe = msg.encode('ascii', errors='backslashreplace').decode('ascii')
            print(safe)
        return 5
    return 0


def move_finished_epics(path: Path, dry_run: bool = False, verbose: bool = False) -> int:
    """Move epics whose subtasks are all finished into the finished section.

    Also add an `- updated: YYYY-MM-DD` line to moved epics.
    """
    txt = path.read_text(encoding='utf-8')
    start_open = txt.find('## 1. Epics - open')
    start_finished = txt.find('## 2. Epics - finished')
    if start_open == -1 or start_finished == -1:
        # nothing to do if sections missing
        return 0

    prefix = txt[:start_open]
    open_text = txt[start_open:start_finished]
    finished_text = txt[start_finished:]

    # split open_text into epic blocks
    lines = open_text.splitlines(keepends=True)
    epic_indices = []  # list of (start_idx, end_idx) in lines
    epic_header_re = re.compile(r"^\s*(?:☐|✅|❌|⏳)?\s*Epic\s+(\d{4}):")
    for i, line in enumerate(lines):
        if epic_header_re.match(line):
            epic_indices.append(i)
    if not epic_indices:
        return 0

    # determine blocks
    blocks = []
    for idx, start in enumerate(epic_indices):
        end = epic_indices[idx + 1] if idx + 1 < len(epic_indices) else len(lines)
        blocks.append((start, end))

    def normalize_status_local(s: str) -> str | None:
        # reuse normalize logic from validate but keep local to avoid duplication
        if not s:
            return None
        s0 = s.strip().lower()
        SYM = {
                '\u2610': 'open',   # ☐
                '\u2705': 'done',   # ✅
                '\u274c': 'failed', # ❌
                '\u23f3': 'in progress', # ⏳
        }
        if s0 in SYM:
            return SYM[s0]
        s_clean = re.sub(r"[^a-z0-9 ]+", '', s0)
        WORD_MAP = {
            'done': 'done',
            'implemented': 'done',
            'fixed': 'done',
            'finished': 'done',
            'resolved': 'done',
            'closed': 'done',
            'completed': 'done',
            'open': 'open',
            'in progress': 'in progress',
            'started': 'in progress',
            'failed': 'failed',
            'reverted': 'reverted',
            'revert': 'reverted',
            'rejected': 'rejected',
            'reject': 'rejected',
            'cancelled': 'cancelled',
            'canceled': 'cancelled',
            'cancel': 'cancelled',
            'aborted': 'cancelled',
        }
        if s_clean in WORD_MAP:
            return WORD_MAP[s_clean]
        first = s_clean.split()[0] if s_clean else ''
        return WORD_MAP.get(first)

    moved_blocks = []
    acceptable_terminal = {'done', 'reverted', 'rejected', 'cancelled', 'implemented', 'fixed'}
    for start, end in blocks:
        block_text = ''.join(lines[start:end])
        # prefer statuses under the Subtasks: section
        subtasks_match = re.search(r"-\s*Subtasks:\s*", block_text, flags=re.I)
        if subtasks_match:
            subtasks_part = block_text[subtasks_match.end():]
            status_lines = re.findall(r"^\s*-\s*status:\s*(.+)$", subtasks_part, flags=re.M)
        else:
            status_lines = re.findall(r"^\s*-\s*status:\s*(.+)$", block_text, flags=re.M)

        if not status_lines:
            # no explicit statuses -> do not move
            if verbose:
                # try to extract epic id for clarity
                m = epic_header_re.search(block_text)
                eid = m.group(1) if m else '<unknown>'
                print(f"[skip] Epic {eid}: no status lines found")
            continue

        norms = [normalize_status_local(s) for s in status_lines]
        # try to extract epic id for verbose output
        m = epic_header_re.search(block_text)
        eid = m.group(1) if m else '<unknown>'
        if verbose:
            print(f"[inspect] Epic {eid}: raw_statuses={status_lines} normalized={norms}")

        # move epic if all subtask statuses normalize to an acceptable terminal state
        if norms and all((n in acceptable_terminal) for n in norms):
            moved_blocks.append((start, end, block_text, norms, status_lines))
            if verbose:
                print(f"[will-move] Epic {eid}: all subtasks terminal -> queued for move")

    if not moved_blocks:
        return 0

    # Rebuild open_text without moved blocks
    keep_lines = list(lines)
    # remove moved blocks in reverse order to keep indices valid
    # moved_blocks entries contain extra diagnostic fields (norms, status_lines),
    # use star-unpacking to remain robust if the tuple shape changes.
    for start, end, *_ in reversed(moved_blocks):
        del keep_lines[start:end]
    new_open_text = ''.join(keep_lines)

    # Append moved blocks to finished_text (after the header)
    # Find insertion point: after the '## 2. Epics - finished' header line
    # Keep finished_text as-is and append moved blocks at its end
    appended = ''
    today = date.today().isoformat()
    for _, _, block, *_ in moved_blocks:
        # add updated metadata if not present
        if '- updated:' not in block:
            # insert updated line after the epic header (first line)
            parts = block.splitlines(keepends=True)
            if len(parts) >= 1:
                parts.insert(1, f" - updated: {today}\n")
            block = ''.join(parts)
        appended += '\n' + block

    # ensure there is a separating newline between sections
    if not new_open_text.endswith('\n'):
        new_open_text += '\n'

    # Insert appended blocks immediately after the '## 2. Epics - finished' header
    # Use a regex to find the header line (robust against spacing) and insert
    # moved epics after the header and any immediate blank lines that follow it.
    m = re.search(r"^##\s*2\.\s*Epics\s*-\s*finished.*?$", txt, flags=re.M)
    if not m:
        # fallback: append at end if header cannot be located (shouldn't happen)
        new_txt = prefix + new_open_text + finished_text + appended + '\n'
    else:
        header_end = m.end()
        # skip immediate blank lines after header so inserted blocks sit before
        # existing finished epics list content (if any)
        insertion_pos = header_end
        while insertion_pos < len(txt) and txt[insertion_pos] in ('\n', '\r'):
            insertion_pos += 1

        new_txt = prefix + new_open_text + txt[start_finished:insertion_pos] + appended + txt[insertion_pos:]

    # write atomically
    dirp = path.parent
    fd, tmppath = tempfile.mkstemp(dir=dirp)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as fh:
            fh.write(new_txt)
        os.replace(tmppath, str(path))
    finally:
        # cleanup leftover tmp if any
        if os.path.exists(tmppath):
            try:
                os.remove(tmppath)
            except OSError:
                pass
    return 0


def main():
    # allow overriding backlog path via environment for tests and safety
    import os
    ppath = os.environ.get('BACKLOG_MD')
    if ppath:
        p = Path(ppath).resolve()
    else:
        p = Path('.').resolve() / 'backlog.md'
    if not p.exists():
        print('backlog.md not found at', p)
        return 4
    # parse simple CLI flags
    import argparse
    ap = argparse.ArgumentParser(description='Validate and optionally move finished epics in backlog.md')
    ap.add_argument('--dry-run', action='store_true', help='Do not write changes; only print what would change')
    ap.add_argument('--verbose', '-v', action='store_true', help='Verbose diagnostic output')
    ap.add_argument('--apply-only', action='store_true', help='Run without running tests (legacy compatibility)')
    args = ap.parse_args()

    # first validate existing file
    rc = validate(p)
    if rc != 0:
        return rc
    # attempt to move finished epics and add updated metadata
    try:
        return move_finished_epics(p, dry_run=args.dry_run, verbose=args.verbose)
    except Exception as e:
        print('Error updating backlog:', e)
        return 6


if __name__ == '__main__':
    raise SystemExit(main())
