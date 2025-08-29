"""Minimal backlog CLI dispatcher for the project.

This is intentionally tiny: it provides a thin entry point `main()` and
supports a couple of smoke commands used by tests and CI: `--version`,
`validate`, and `add-task --title` (dry-run).

The full tool will live under `scripts/backlog_tool/` and this module will
be the console entrypoint that imports the library code.
"""
from __future__ import annotations

import argparse
import sys
from datetime import date
import os
import shutil

__version__ = "0.1.0"


def cmd_validate(args: argparse.Namespace) -> int:
    # Use the library validator for richer checks
    import os
    from scripts.backlog_tool import parser as bl

    path = args.file or "backlog.md"
    if not os.path.exists(path):
        print(f"ERROR: backlog file not found: {path}", file=sys.stderr)
        return 2
    lines = bl.read_file(path)
    backlog = bl.parse(lines)
    errors = bl.validate_backlog(backlog)
    if errors:
        for e in errors:
            print(f"ERROR: {e}", file=sys.stderr)
        return 1
    print("OK: backlog validated")
    return 0


def cmd_add_task(args: argparse.Namespace) -> int:
    # Dry-run add: print a formatted snippet that would be inserted
    now = date.today().isoformat()
    entry = []
    entry.append(f"- \u2610 Task XXXX: {args.title}")
    entry.append("  - status: open")
    entry.append(f"  - added: {now}")
    if getattr(args, "notes", None):
        # Allow CLI users to pass literal '\\n' sequences which should
        # be interpreted as real newlines. Normalize here for preview.
        notes_raw = args.notes.replace('\\n', '\n')
        entry.append("  - Notes:")
        for line in notes_raw.splitlines():
            entry.append(f"    - {line}")
    # Show preview only for dry-run mode
    if not getattr(args, 'write', False):
        print("Dry-run: task entry to insert:")
        print("\n".join(entry))

    if getattr(args, "write", False):
        # when persisting changes, an epic id is required
        if not getattr(args, "epic", None):
            print("ERROR: --epic is required when using --write", file=sys.stderr)
            return 2
        from scripts.backlog_tool import parser as bl
        path = args.file or "backlog.md"
        # create backlog from bundled template if it does not exist
        if not os.path.exists(path):
            tpl = os.path.join(os.path.dirname(__file__), 'backlog_tool', 'template.md')
            if not os.path.exists(tpl):
                print(f"ERROR: template not found: {tpl}", file=sys.stderr)
                return 2
            shutil.copy2(tpl, path)
            print(f"Created backlog from template: {path}")
        lines = bl.read_file(path)
        backlog = bl.parse(lines)
        try:
            epic_id = _pad_id_input(getattr(args, 'epic', None))
            forced = _pad_id_input(getattr(args, 'forced_id', None))
            # Normalize literal '\\n' sequences and strip any leading
            # list marker from user-supplied lines so the writer does not
            # produce nested '- - ...' bullets.
            def _normalize_notes(s: str | None) -> str | None:
                if s is None:
                    return None
                s2 = s.replace('\\n', '\n')
                lines = []
                for ln in s2.splitlines():
                    l = ln
                    if l.lstrip().startswith('- '):
                        # remove the first hyphen and following space
                        idx = l.find('- ')
                        l = l[:idx] + l[idx+2:]
                    lines.append(l.rstrip())
                return '\n'.join(lines)

            notes_arg = _normalize_notes(getattr(args, "notes", None))
            t = bl.add_task_to_epic(backlog, epic_id, args.title, notes_arg, forced_id=forced)
        except KeyError as e:
            print(f"ERROR: {e}", file=sys.stderr)
            return 2
        except ValueError as e:
            print(f"ERROR: {e}", file=sys.stderr)
            return 2
        bak = bl.make_backup(path)
        bl.safe_write(path, bl.build_markdown(backlog))
        print(f"Created task {t.id} under epic {epic_id}; backup: {bak}")
    return 0


def cmd_move_task(args: argparse.Namespace) -> int:
    from scripts.backlog_tool import parser as bl

    path = args.file or "backlog.md"
    lines = bl.read_file(path)
    backlog = bl.parse(lines)
    task_id = _pad_id_input(getattr(args, 'task', None))
    to_epic = _pad_id_input(getattr(args, 'to_epic', None))
    try:
        moved = bl.move_task(backlog, task_id, to_epic)
    except KeyError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 2
    print(f"Dry-run: moved task {task_id} -> epic {to_epic} (new id: {moved.id})")
    if getattr(args, "write", False):
        bak = bl.make_backup(path)
        bl.safe_write(path, bl.build_markdown(backlog))
        print(f"Wrote changes to {path}; backup: {bak}")
    return 0


def cmd_add_epic(args: argparse.Namespace) -> int:
    from scripts.backlog_tool import parser as bl
    path = args.file or "backlog.md"
    # Show intent. If --write was passed, this is not a dry-run.
    if args.write:
        print(f"Create epic -> title: {args.title}")
    else:
        print(f"Dry-run: create epic -> title: {args.title}")
    if args.write:
        # create from template if missing
        if not os.path.exists(path):
            tpl = os.path.join(os.path.dirname(__file__), 'backlog_tool', 'template.md')
            if not os.path.exists(tpl):
                print(f"ERROR: template not found: {tpl}", file=sys.stderr)
                return 2
            # copy the template first
            shutil.copy2(tpl, path)
            print(f"Created backlog from template: {path}")
            # Directly insert the new epic text at the '## 1. Epics - open' marker
            # but compute a real unique epic id from the (empty) template so
            # subsequent `add-task --epic` calls find it.
            lines_orig = bl.read_file(path)
            backlog_obj = bl.parse(lines_orig)
            # find next available epic id
            existing = {e.id for e in backlog_obj.epics_open + backlog_obj.epics_finished}
            new_id = None
            for i in range(0, 10000):
                cand = f"{i:04d}"
                if cand not in existing:
                    new_id = cand
                    break
            if new_id is None:
                print("ERROR: no available epic ids", file=sys.stderr)
                return 3

            # Use the parser API to add the epic to the freshly copied template.
            backlog_obj = bl.parse(lines_orig)
            forced = _pad_id_input(getattr(args, 'forced_id', None))
            try:
                e = bl.add_epic_to_backlog(backlog_obj, args.title, forced_id=forced)
            except ValueError as ve:
                print(f"ERROR: {ve}", file=sys.stderr)
                return 2
            bak = bl.make_backup(path)
            bl.safe_write(path, bl.build_markdown(backlog_obj))
            print(f"Created epic {e.id}; backup: {bak}")
        else:
            lines = bl.read_file(path)
            backlog = bl.parse(lines)
            forced = _pad_id_input(getattr(args, 'forced_id', None))
            try:
                e = bl.add_epic_to_backlog(backlog, args.title, forced_id=forced)
            except ValueError as ve:
                print(f"ERROR: {ve}", file=sys.stderr)
                return 2
            bak = bl.make_backup(path)
            bl.safe_write(path, bl.build_markdown(backlog))
            print(f"Created epic {e.id}; backup: {bak}")
    return 0


def cmd_edit(args: argparse.Namespace) -> int:
    """Edit fields on an Epic or Task.

    Usage: backlog edit <id> --set key=value [--set key=value ...] [--write]

    Keys supported for tasks: title, status, added, closed, notes
    Keys supported for epics: title, status, and arbitrary epic-level
    fields (stored in raw_lines) such as description.
    """
    from scripts.backlog_tool import parser as bl
    import re

    path = args.file or "backlog.md"
    lines = bl.read_file(path)
    backlog = bl.parse(lines)

    # single id expected
    ident = args.id[0] if isinstance(args.id, (list, tuple)) and args.id else getattr(args, 'legacy_id', [None])[0]
    ident = _pad_id_input(ident)
    if not ident:
        print('ERROR: no id provided', file=sys.stderr)
        return 2

    # collect sets
    sets = {}
    for s in getattr(args, 'set', []) or []:
        if '=' not in s:
            print(f"ERROR: invalid --set value (expected key=value): {s}", file=sys.stderr)
            return 2
        k, v = s.split('=', 1)
        sets[k.strip().lower()] = v

    if not sets:
        print('Nothing to change; provide --set key=value', file=sys.stderr)
        return 2

    # Prefer editing a Task if the id corresponds to a task; otherwise try epic.
    try:
        epic, task = bl.find_task(backlog, ident)
    except KeyError:
        # Fallback: try to locate an epic with this id first, then try to
        # locate a task by scanning all epics and matching numeric ids
        epic = next((e for e in backlog.epics_open + backlog.epics_finished if e.id == ident), None)
        task = None

        def _canon_id(s: str) -> str:
            s2 = str(s).strip()
            if s2.isdigit():
                # canonicalize numeric ids by removing leading zeros
                return str(int(s2))
            return s2

        try:
            ident_canon = _canon_id(ident)
        except Exception:
            ident_canon = str(ident)

        # scan for task matches (ignore zero-padding differences)
        for e in backlog.epics_open + backlog.epics_finished:
            for t in getattr(e, 'tasks', []) or []:
                t_id = getattr(t, 'id', None)
                if t_id is None:
                    continue
                try:
                    t_canon = _canon_id(t_id)
                except Exception:
                    t_canon = str(t_id)
                if ident_canon == t_canon or str(t_id) == str(ident) or str(t_id).lstrip('0') == str(ident).lstrip('0'):
                    epic = e
                    task = t
                    break
            if task is not None:
                break

    if task is not None:
        # Only allow explicit, known task-level keys to be edited.
        allowed_task_keys = {"title", "status", "added", "closed", "notes", "description"}
        invalid = [k for k in sets.keys() if k not in allowed_task_keys]
        if invalid:
            print(f"ERROR: invalid task field(s): {', '.join(sorted(invalid))}", file=sys.stderr)
            return 2

        for k, v in sets.items():
            if k == 'title':
                task.title = v
            elif k == 'status':
                # Use the parser helper to apply status changes so closed date
                # logic is applied consistently.
                task = bl.update_task_status(backlog, task.id, v)
            elif k == 'added':
                task.added = v
            elif k == 'closed':
                task.closed = v
            elif k == 'notes':
                # Support literal '\\n' sequences and strip accidental
                # leading list markers '- ' so we don't end up with nested
                # bullets when writing back to markdown.
                vv = v.replace('\\n', '\n')
                normalized = []
                for ln in vv.splitlines():
                    l = ln
                    if l.lstrip().startswith('- '):
                        idx = l.find('- ')
                        l = l[:idx] + l[idx+2:]
                    normalized.append(l.rstrip())
                task.notes = normalized
            elif k == 'description':
                vv = v.replace('\\n', '\n')
                normalized = []
                for ln in vv.splitlines():
                    l = ln
                    if l.lstrip().startswith('- '):
                        idx = l.find('- ')
                        l = l[:idx] + l[idx+2:]
                    normalized.append(l.rstrip())
                task.description = normalized

        if getattr(args, 'write', False):
            # perform write and report the update
            bak = bl.make_backup(path)
            bl.safe_write(path, bl.build_markdown(backlog))
            print(f"Updated task {task.id} (Epic {epic.id})")
            print(f"Wrote changes to {path}; backup: {bak}")
        else:
            print(f"Dry-run: updated task {task.id} (Epic {epic.id})")
        return 0

    if epic:
        # Only allow explicit, known epic-level keys to be edited. Writing
        # arbitrary keys into raw_lines is error-prone; require consumers to
        # edit only modeled fields.
        allowed_epic_keys = {"title", "status", "added", "closed", "notes", "description"}
        invalid = [k for k in sets.keys() if k not in allowed_epic_keys]
        if invalid:
            print(f"ERROR: invalid epic field(s): {', '.join(sorted(invalid))}", file=sys.stderr)
            return 2

        def _strip_raw_block(raw_lines: list[str], key: str) -> list[str]:
            """Remove any raw_lines block that starts with '- key:' and following indented lines."""
            out: list[str] = []
            i = 0
            key_re = re.compile(rf"^\s*-\s*{re.escape(key)}:\b", flags=re.I)
            while i < len(raw_lines):
                ln = raw_lines[i]
                if key_re.match(ln):
                    # skip this line and any immediately following lines that
                    # are indented more than this line.
                    base_indent = len(ln) - len(ln.lstrip(' '))
                    i += 1
                    while i < len(raw_lines):
                        nxt = raw_lines[i]
                        nxt_indent = len(nxt) - len(nxt.lstrip(' '))
                        if nxt.strip() == '':
                            # blank lines are part of the block; skip
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

        for k, v in sets.items():
            if k == 'title':
                epic.title = v
            elif k == 'status':
                epic.status = v
            elif k == 'added':
                epic.added = v
            elif k == 'closed':
                epic.closed = v
            elif k == 'notes':
                # Normalize and strip accidental list markers
                vv = v.replace('\\n', '\n')
                normalized = []
                for ln in vv.splitlines():
                    l = ln
                    if l.lstrip().startswith('- '):
                        idx = l.find('- ')
                        l = l[:idx] + l[idx+2:]
                    normalized.append(l.rstrip())
                epic.notes = normalized
                epic.raw_lines = _strip_raw_block(epic.raw_lines, 'notes')
            elif k == 'description':
                vv = v.replace('\\n', '\n')
                normalized = []
                for ln in vv.splitlines():
                    l = ln
                    if l.lstrip().startswith('- '):
                        idx = l.find('- ')
                        l = l[:idx] + l[idx+2:]
                    normalized.append(l.rstrip())
                epic.description = normalized
                epic.raw_lines = _strip_raw_block(epic.raw_lines, 'description')

        if getattr(args, 'write', False):
            bak = bl.make_backup(path)
            bl.safe_write(path, bl.build_markdown(backlog))
            print(f"Updated epic {epic.id}")
            print(f"Wrote changes to {path}; backup: {bak}")
        else:
            print(f"Dry-run: updated epic {epic.id}")
        return 0

    print(f"ERROR: id {ident} not found", file=sys.stderr)
    return 2


def cmd_backup(args: argparse.Namespace) -> int:
    from scripts.backlog_tool import parser as bl
    import os

    path = args.file or "backlog.md"
    if not os.path.exists(path):
        print(f"ERROR: backlog file not found: {path}", file=sys.stderr)
        return 2
    if getattr(args, "prune", False):
        # pruning behavior
        keep = getattr(args, "keep", None)
        older = getattr(args, "older_than", None)
        if getattr(args, "dry_run", False):
            removed = bl.prune_backups(path, keep=keep, older_than_days=older)
            print("Dry-run: backups that would be removed:")
            for r in removed:
                print(r)
            return 0
        if not getattr(args, "yes", False):
            print("Prune backups will remove files. Re-run with --yes to confirm.")
            return 3
        removed = bl.prune_backups(path, keep=keep, older_than_days=older)
        print("Pruned backups:")
        for r in removed:
            print(r)
        return 0

    bak = bl.make_backup(path)
    print(f"Created backup: {bak}")
    return 0


def cmd_init(args: argparse.Namespace) -> int:
    """Create a new backlog file from the bundled template if it does not exist."""
    import os
    path = args.file or "backlog.md"
    if os.path.exists(path):
        print(f"Backlog already exists: {path}")
        return 1
    tpl = os.path.join(os.path.dirname(__file__), 'backlog_tool', 'template.md')
    if not os.path.exists(tpl):
        print(f"ERROR: template not found: {tpl}", file=sys.stderr)
        return 2
    try:
        shutil.copy2(tpl, path)
    except Exception as e:
        print(f"ERROR: failed to create backlog from template: {e}", file=sys.stderr)
        return 3
    print(f"Created backlog from template: {path}")
    return 0


def cmd_check_ids(args: argparse.Namespace) -> int:
    from scripts.backlog_tool import parser as bl
    path = args.file or "backlog.md"
    lines = bl.read_file(path)
    backlog = bl.parse(lines)
    # detect duplicate task ids
    task_ids: list[str] = []
    epic_ids: list[str] = []
    for e in backlog.epics_open + backlog.epics_finished:
        epic_ids.append(e.id)
        for t in e.tasks:
            task_ids.append(t.id)
    # Treat numeric ids with/without leading zeros as the same id for
    # duplicate detection (e.g., '13' and '0013'). Canonicalize by
    # converting numeric ids to their integer representation as strings.
    def _canon(i: str) -> str:
        s = str(i).strip()
        if s.isdigit():
            try:
                return str(int(s))
            except ValueError:
                return s
        return s

    canon_tasks = [_canon(i) for i in task_ids]
    dup_tasks = {i for i in canon_tasks if canon_tasks.count(i) > 1}
    canon_epics = [_canon(i) for i in epic_ids]
    # collisions where an id appears both as epic and task
    cross = set(canon_tasks) & set(canon_epics)

    if dup_tasks or cross:
        if dup_tasks:
            print("Duplicate task ids:")
            for c in sorted(dup_tasks):
                print(f"  {int(c):04d}" if c.isdigit() else c)
        if cross:
            print("ID collisions between epics and tasks:")
            for c in sorted(cross):
                print(f"  {int(c):04d}" if c.isdigit() else c)
        return 1
    print("No duplicate task ids found")
    return 0


def cmd_fix_format(args: argparse.Namespace) -> int:
    from scripts.backlog_tool import parser as bl

    path = args.file or "backlog.md"
    lines = bl.read_file(path)
    backlog = bl.parse(lines)
    id_changes = bl.reassign_duplicate_task_ids(backlog)
    collision_changes = bl.reassign_epic_task_collisions(backlog)
    norm_changes = bl.normalize_backlog_format(backlog)
    if not id_changes and not norm_changes and not collision_changes:
        print("No formatting or id issues found")
        return 0
    print("Planned changes:")
    for old, new in id_changes:
        print(f"reassign: {old} -> {new}")
    for old, new in collision_changes:
        print(f"reassign collision: {old} -> {new}")
    for c in norm_changes:
        print(c)
    if getattr(args, "write", False):
        bak = bl.make_backup(path)
        # If ids-only was requested, apply targeted textual replacements so
        # we preserve all authoring and formatting. Otherwise fall back to
        # full reserialization from the normalized model (respecting the
        # parser/writer rules).
        if getattr(args, 'ids_only', False):
            import re as _re
            with open(path, 'r', encoding='utf-8') as _f:
                _text = _f.read()

            def _replace_with_new(old_id, new_id):
                return lambda m: m.group(1) + new_id

            for old, new in list(id_changes) + list(collision_changes):
                _text = _re.sub(rf'(\bEpic\s+){_re.escape(old)}(?=\s*:)', _replace_with_new(old, new), _text)
                _text = _re.sub(rf'(\bTask\s+){_re.escape(old)}(?=\s*:)', _replace_with_new(old, new), _text)

            bl.safe_write(path, _text)
            print(f"Applied id-only fixes; backup: {bak}")
            if collision_changes:
                for old, new in collision_changes:
                    print(f"reassign collision: {old} -> {new}")
        else:
            # full reserialize path: write canonicalized markdown from model
            bl.safe_write(path, bl.build_markdown(backlog))
            print(f"Applied fixes; backup: {bak}")
            if collision_changes:
                for old, new in collision_changes:
                    print(f"reassign collision: {old} -> {new}")
    return 0


def cmd_undo(args: argparse.Namespace) -> int:
    from scripts.backlog_tool import parser as bl
    import os

    path = args.file or "backlog.md"
    if not os.path.exists(path):
        print(f"ERROR: backlog file not found: {path}", file=sys.stderr)
        return 2
    backups = bl.list_backups(path)
    if not backups:
        print("No backups found", file=sys.stderr)
        return 3
    # support listing, explicit restore, interactive choose, or default restore
    if getattr(args, "list", False):
        for i, b in enumerate(backups, 1):
            print(f"{i}: {b}")
        return 0

    # explicit backup path provided
    if getattr(args, "backup", None):
        chosen = args.backup
        if chosen not in backups:
            print(f"ERROR: specified backup not found: {chosen}", file=sys.stderr)
            return 4
        bl.restore_backup(path, chosen)
        print(f"Restored backup: {chosen}")
        return 0

    if getattr(args, "choose", False):
        # interactive choose
        for i, b in enumerate(backups, 1):
            print(f"{i}: {b}")
        sel = input("Choose backup number to restore (empty to cancel): ")
        if not sel:
            print("Cancelled")
            return 0
        try:
            idx = int(sel) - 1
            if idx < 0 or idx >= len(backups):
                print("Invalid selection", file=sys.stderr)
                return 5
            chosen = backups[idx]
        except ValueError:
            print("Invalid selection", file=sys.stderr)
            return 5
        bl.restore_backup(path, chosen)
        print(f"Restored backup: {chosen}")
        return 0

    # default: restore last backup
    backup_path = backups[-1]
    bl.restore_backup(path, backup_path)
    print(f"Restored backup: {backup_path}")
    return 0


def _normalize_status(s: str) -> str | None:
    if not s:
        return None
    s0 = s.strip().lower()
    from scripts.backlog_tool import values
    # prefer escaped codepoints to avoid duplicated literal glyphs being treated as repeated keys
    SYM = values.get('symbol_map', {
        '\u2610': 'open', '\u2705': 'done', '\u274c': 'failed', '\u23f3': 'in progress'
    })
    if s0 in SYM:
        return SYM[s0]
    import re
    s_clean = re.sub(r"[^a-z0-9 ]+", '', s0)
    WORD_MAP = values.get('word_map', {
        'done': 'done', 'implemented': 'done', 'finished': 'done', 'resolved': 'done', 'closed': 'done', 'completed': 'done',
        'open': 'open', 'in progress': 'in progress', 'started': 'in progress',
        'failed': 'failed', 'reverted': 'reverted', 'revert': 'reverted',
        'rejected': 'rejected', 'reject': 'rejected',
        'cancelled': 'cancelled', 'canceled': 'cancelled', 'cancel': 'cancelled', 'aborted': 'cancelled'
    })
    if s_clean in WORD_MAP:
        return WORD_MAP[s_clean]
    first = s_clean.split()[0] if s_clean else ''
    return WORD_MAP.get(first)


def _ansi(text: str, code: str | None) -> str:
    if not code:
        return text
    return f"\x1b[{code}m{text}\x1b[0m"


def _pad_id_input(ident: str | None) -> str | None:
    """Pad numeric id inputs to four digits when plausible.

    Examples: '13' -> '0013', '0001' -> '0001', non-numeric strings are
    returned unchanged.
    """
    if ident is None:
        return None
    s = str(ident).strip()
    if not s:
        return s
    if s.isdigit():
        # pad small numeric ids to 4 digits
        try:
            n = int(s)
        except ValueError:
            return s
        if 0 <= n <= 9999:
            return f"{n:04d}"
    return s


def cmd_list(args: argparse.Namespace) -> int:
    """List all epic and task ids with titles.

    Supports optional ANSI colorization with --color.
    """
    from scripts.backlog_tool import parser as bl
    path = args.file or "backlog.md"
    lines = bl.read_file(path)
    backlog = bl.parse(lines)
    # Determine color usage: explicit flag wins, otherwise auto-detect TTY
    use_color_flag = getattr(args, "color", None)
    if use_color_flag is None:
        use_color = sys.stdout.isatty()
    else:
        use_color = bool(use_color_flag)
    # On Windows, enable ANSI handling in interactive TTYs via colorama.
    # Avoid initializing colorama when stdout is being captured by tests
    # (StringIO) since it can wrap streams and hide raw escape sequences.
    if use_color and sys.stdout.isatty():
        try:
            import colorama
            colorama.init()
        except Exception:
            pass

    # Determine which epics to inspect based on state
    state = getattr(args, "state", "open") or "open"
    only = getattr(args, "only", "epics") or "epics"
    ids_only = getattr(args, "ids_only", False)

    epics: list = []
    if state in ("open", "all"):
        epics.extend(backlog.epics_open)
    if state in ("finished", "all"):
        epics.extend(backlog.epics_finished)

    # Default behavior: only epics and open (handled by defaults above)

    # If ids-only requested, print numeric ids (epics and tasks) one per line
    if ids_only:
        printed = set()
        # If the user requested state=all but left --only as the default (epics),
        # they likely want all ids; handle that case by printing both epics and tasks.
        effective_print_tasks = (only in ("tasks", "all")) or (only == "epics" and state == "all")
        # Print epic ids
        if only in ("epics", "all"):
            for e in epics:
                if e.id not in printed:
                    print(e.id)
                    printed.add(e.id)
        # Print task ids when appropriate
        if effective_print_tasks:
            for e in epics:
                for t in e.tasks:
                    if t.id not in printed:
                        print(t.id)
                        printed.add(t.id)
        return 0

    # Print epics when requested
    if only in ("epics", "all"):
        print("Epics:")
        for e in epics:
            eid = _ansi(e.id, "36;1" if use_color else None)
            title = _ansi(e.title, "32" if use_color else None)
            print(f"  Epic {eid}: {title}")
        print("")

    # Print tasks when requested
    if only in ("tasks", "all"):
        print("Tasks:")
        for e in epics:
            for t in e.tasks:
                tid = _ansi(t.id, "36;1" if use_color else None)
                title = _ansi(t.title, "33" if use_color else None)
                print(f"  Task {tid}: {title}  (Epic {e.id})")
    return 0


def cmd_show(args: argparse.Namespace) -> int:
    """Show a detailed view of an epic or task by numeric id.

    --id accepts either an epic id or a task id.
    """
    from scripts.backlog_tool import parser as bl
    path = args.file or "backlog.md"
    lines = bl.read_file(path)
    backlog = bl.parse(lines)
    # Merge positional ids and legacy --id (stored in legacy_id) for
    # backwards-compatibility. Ensure we have at least one id to show.
    legacy = getattr(args, 'legacy_id', None) or []
    positional = getattr(args, 'id', None) or []
    ids = list(positional) + list(legacy)
    if not ids:
        print('ERROR: no id provided', file=sys.stderr)
        return 2
    use_color_flag = getattr(args, "color", None)
    if use_color_flag is None:
        use_color = sys.stdout.isatty()
    else:
        use_color = bool(use_color_flag)
    if use_color and sys.stdout.isatty():
        try:
            import colorama
            colorama.init()
        except Exception:
            pass

    missing = False
    for ident in ids:
        ident = _pad_id_input(ident)
        # Try epic
        found = False
        for e in backlog.epics_open + backlog.epics_finished:
            if e.id == ident:
                found = True
                print(_ansi(f"Epic {e.id}: {e.title}", "32;1" if use_color else None))
                print(f"  status: {e.status}")
                if e.raw_lines:
                    print("  (extra lines preserved)")
                print("  - tasks:")
                for t in e.tasks:
                    tid = _ansi(t.id, "36;1" if use_color else None)
                    ttitle = _ansi(t.title, "33" if use_color else None)
                    print(f"    - Task {tid}: {ttitle}")
                    print(f"      - status: {t.status}")
                    if t.added:
                        print(f"      - added: {t.added}")
                    if t.closed:
                        print(f"      - closed: {t.closed}")
                break

        if found:
            continue

        # Try task
        try:
            epic, task = bl.find_task(backlog, ident)
        except KeyError:
            print(f"ERROR: id {ident} not found", file=sys.stderr)
            missing = True
            continue

        print(_ansi(f"Task {task.id}: {task.title}", "33;1" if use_color else None))
        print(f"  status: {task.status}")
        if task.added:
            print(f"  added: {task.added}")
        if task.closed:
            print(f"  closed: {task.closed}")
        print(f"  Parent Epic: {epic.id}: {epic.title}")

    return 1 if missing else 0


def cmd_update(args: argparse.Namespace) -> int:
    """Validate and move finished epics (compat shim for legacy updater).

    Respects BACKLOG_MD env var for tests; otherwise uses --file if provided.
    """
    import os
    from pathlib import Path
    import re
    from datetime import date

    ppath = os.environ.get('BACKLOG_MD')
    if ppath:
        p = Path(ppath)
    else:
        p = Path(args.file or 'backlog.md')
    if not p.exists():
        print('backlog.md not found at', p)
        return 4

    # validate
    txt = p.read_text(encoding='utf-8')
    if txt.count('# Backlog') != 1:
        print('Expected single "# Backlog" header')
        return 2
    ids = re.findall(r"^\s*(?:☐|✅|❌|⏳)?\s*(?:Epic|Task)\s+(\d{4})\b", txt, flags=re.M)
    dup = {i for i in ids if ids.count(i) > 1}
    if dup:
        print('Duplicate numeric IDs found:', ', '.join(sorted(dup)))
        return 3

    # We'll validate status values, but only inside the Epics sections.
    # The file can (and does) contain example/template blocks before the
    # "## 1. Epics - open" header. Restricting the search to the two Epics
    # sections prevents template placeholders (e.g. "<status> (mandatory)")
    # from being treated as real status lines.
    from scripts.backlog_tool import values as bl_values
    start_open = txt.find('## 1. Epics - open')
    start_finished = txt.find('## 2. Epics - finished')
    # If the open Epics section is missing, there is nothing to validate or move.
    if start_open == -1:
        return 0
    # Don't require the finished section to exist for status validation —
    # tests and minimal backlog files may omit it. Validation will scan
    # from the open section onward and will still detect bad status tokens.

    # Consider only the text that contains the open + finished epic sections
    relevant_text = txt[start_open:]
    status_lines = re.findall(r"^\s*-\s*status:\s*(.+)$", relevant_text, flags=re.M)
    bad = []
    canonical = set(bl_values.get('allowed_statuses', ['done', 'open', 'failed', 'in progress', 'reverted', 'rejected', 'cancelled']))
    for s in status_lines:
        norm = _normalize_status(s)
        if not norm or norm not in canonical:
            bad.append(s)
    if bad:
        msg = 'Found unknown status values: ' + ', '.join(sorted(set(bad)))
        try:
            print(msg)
        except UnicodeEncodeError:
            safe = msg.encode('ascii', errors='backslashreplace').decode('ascii')
            print(safe)
        return 5

    # move finished epics
    full = txt
    start_open = full.find('## 1. Epics - open')
    start_finished = full.find('## 2. Epics - finished')
    if start_open == -1 or start_finished == -1:
        return 0
    prefix = full[:start_open]
    open_text = full[start_open:start_finished]
    finished_text = full[start_finished:]

    lines = open_text.splitlines(keepends=True)
    # accept optional leading '-' (markdown list) and optional symbol like '☐'
    epic_header_re = re.compile(r"^\s*(?:-\s*)?(?:☐|✅|❌|⏳)?\s*Epic\s+(\d{4}):")
    epic_indices = [i for i, line in enumerate(lines) if epic_header_re.match(line)]
    if not epic_indices:
        return 0
    blocks = []
    for idx, start in enumerate(epic_indices):
        end = epic_indices[idx + 1] if idx + 1 < len(epic_indices) else len(lines)
        blocks.append((start, end))

    moved_blocks = []
    acceptable_terminal = set(bl_values.get('acceptable_terminal', ['done', 'reverted', 'rejected', 'cancelled', 'implemented', 'fixed']))
    for start, end in blocks:
        block_text = ''.join(lines[start:end])
        # Strict: only accept the canonical 'tasks:' heading (no Subtasks synonyms)
        subtasks_match = re.search(r"-\s*tasks:\s*", block_text, flags=re.I)
        if subtasks_match:
            subtasks_part = block_text[subtasks_match.end():]
            status_lines = re.findall(r"^\s*-\s*status:\s*(.+)$", subtasks_part, flags=re.M)
        else:
            status_lines = re.findall(r"^\s*-\s*status:\s*(.+)$", block_text, flags=re.M)
        if not status_lines:
            continue
        norms = [_normalize_status(s) for s in status_lines]
        m = epic_header_re.search(block_text)
        if norms and all((n in acceptable_terminal) for n in norms):
            moved_blocks.append((start, end, block_text, norms, status_lines))

    if not moved_blocks:
        return 0

    keep_lines = list(lines)
    for start, end, *_ in reversed(moved_blocks):
        del keep_lines[start:end]
    new_open_text = ''.join(keep_lines)

    appended = ''
    today = date.today().isoformat()
    for _, _, block, *_ in moved_blocks:
        if '- updated:' not in block:
            parts = block.splitlines(keepends=True)
            if len(parts) >= 1:
                parts.insert(1, f" - updated: {today}\n")
            block = ''.join(parts)
        appended += '\n' + block

    if not new_open_text.endswith('\n'):
        new_open_text += '\n'

    m = re.search(r"^##\s*2\.\s*Epics\s*-\s*finished.*?$", full, flags=re.M)
    if not m:
        new_txt = prefix + new_open_text + finished_text + appended + '\n'
    else:
        header_end = m.end()
        insertion_pos = header_end
        while insertion_pos < len(full) and full[insertion_pos] in ('\n', '\r'):
            insertion_pos += 1
        new_txt = prefix + new_open_text + full[start_finished:insertion_pos] + appended + full[insertion_pos:]

    # write atomically
    import tempfile
    import os
    dirp = p.parent
    fd, tmppath = tempfile.mkstemp(dir=dirp)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as fh:
            fh.write(new_txt)
        os.replace(tmppath, str(p))
    finally:
        if os.path.exists(tmppath):
            try:
                os.remove(tmppath)
            except OSError:
                pass
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="backlog")
    p.add_argument("--version", action="store_true", help="Show version and exit")
    p.add_argument("--file", help="Backlog file to operate on (default: backlog.md)")

    sub = p.add_subparsers(dest="cmd")

    v = sub.add_parser("validate", help="Validate the backlog file")
    v.add_argument("--file", help="Backlog file to operate on (default: backlog.md)")
    v.set_defaults(func=cmd_validate)

    a = sub.add_parser("add-task", help="add a new task")
    a.add_argument("--title", required=True, help="Task title")
    # make --epic optional for dry-run compatibility; required when --write is used
    a.add_argument("--epic", required=False, help="Epic id to add the task under")
    a.add_argument("--id", dest="forced_id", help="Force a specific Task id (numeric or string). Will error if id exists")
    a.add_argument("--notes", help="Optional notes text")
    a.add_argument("--file", help="Backlog file to operate on (default: backlog.md)")
    a.add_argument("--write", action="store_true", help="Persist changes to file")
    a.set_defaults(func=cmd_add_task)

    ae = sub.add_parser("add-epic", help="Create a new epic")
    ae.add_argument("--title", required=True, help="Epic title")
    ae.add_argument("--id", dest="forced_id", help="Force a specific Epic id (numeric or string). Will error if id exists")
    ae.add_argument("--file", help="Backlog file to operate on (default: backlog.md)")
    ae.add_argument("--write", action="store_true", help="Persist changes to file")
    ae.set_defaults(func=cmd_add_epic)

    m = sub.add_parser("move-task", help="Move a task to another epic")
    m.add_argument("--task", required=True, help="Task id to move")
    m.add_argument("--to-epic", required=True, help="Destination epic id")
    m.add_argument("--file", help="Backlog file to operate on (default: backlog.md)")
    m.add_argument("--write", action="store_true", help="Persist changes to file")
    m.set_defaults(func=cmd_move_task)

    # Replace legacy update-status with a more general `edit` command that
    # can set arbitrary fields on epics or tasks.
    u = sub.add_parser("edit", help="Edit epic or task fields (replaces update-status)")
    u.add_argument("id", nargs="+", help="Epic or Task numeric id(s) (0001)")
    u.add_argument("--set", dest="set", action="append", help="Set a field: --set key=value")
    u.add_argument("--file", help="Backlog file to operate on (default: backlog.md)")
    u.add_argument("--write", action="store_true", help="Persist changes to file")
    u.set_defaults(func=cmd_edit)

    b = sub.add_parser("backup", help="Create a timestamped backup of the backlog file")
    b.add_argument("--file", help="Backlog file to operate on (default: backlog.md)")
    b.add_argument("--prune", action="store_true", help="Prune old backups instead of creating a new one")
    b.add_argument("--keep", type=int, help="When pruning, keep the newest N backups (default: 10)")
    b.add_argument("--older-than", type=int, help="When pruning, remove backups older than N days")
    b.add_argument("--dry-run", action="store_true", help="Show which backups would be removed")
    b.add_argument("--yes", action="store_true", help="Confirm destructive prune without prompt")
    b.set_defaults(func=cmd_backup)

    r = sub.add_parser("undo", help="Restore the last backup of the backlog file")
    r.add_argument("--file", help="Backlog file to operate on (default: backlog.md)")
    r.add_argument("--list", action="store_true", help="List available backups and exit")
    r.add_argument("--choose", action="store_true", help="Interactively choose a backup to restore")
    r.add_argument("--backup", help="Restore a specific backup file path (exact match from --list)")
    r.set_defaults(func=cmd_undo)

    c = sub.add_parser("check-ids", help="Check for duplicate task ids")
    c.add_argument("--file", help="Backlog file to operate on (default: backlog.md)")
    c.set_defaults(func=cmd_check_ids)

    f = sub.add_parser("fix-format", help="Normalize status tokens and reassign duplicate ids")
    f.add_argument("--file", help="Backlog file to operate on (default: backlog.md)")
    f.add_argument("--write", action="store_true", help="Apply fixes and persist to file")
    f.add_argument("--ids-only", action="store_true", dest="ids_only",
                   help="When writing, only rewrite numeric Task/Epic ids and leave formatting intact")
    f.set_defaults(func=cmd_fix_format)

    # legacy compatibility: expose the `update` command used by older scripts/tests
    up = sub.add_parser("update", help="Validate and move finished epics")
    up.add_argument("--file", help="Backlog file to operate on (default: backlog.md)")
    up.set_defaults(func=cmd_update)

    ini = sub.add_parser("init", help="Create a new backlog.md from the bundled template if missing")
    ini.add_argument("--file", help="Backlog file to create (default: backlog.md)")
    ini.set_defaults(func=lambda args: cmd_init(args))

    ls = sub.add_parser("list", help="List all epic and task ids with titles")
    ls.add_argument("--file", help="Backlog file to operate on (default: backlog.md)")
    # color tri-state: --color, --no-color; default None means auto-detect tty
    g = ls.add_mutually_exclusive_group()
    g.add_argument("--color", dest="color", action="store_true", help="Enable ANSI colorized output")
    g.add_argument("--no-color", dest="color", action="store_false", help="Disable ANSI colorized output")
    ls.set_defaults(color=True)
    ls.add_argument("--state", choices=["open", "finished", "all"], default="open",
                    help="Filter by epic state (default: open)")
    ls.add_argument("--only", choices=["epics", "tasks", "all"], default="epics",
                    help="Show only epics, only tasks, or all (default: epics)")
    ls.add_argument("--ids-only", action="store_true", dest="ids_only",
                    help="Print only numeric ids, one per line")
    ls.set_defaults(func=cmd_list)

    sh = sub.add_parser("show", help="Show details for an epic or task by id")
    # Accept one or more numeric ids as positional arguments, e.g.:
    #   backlog show 0001 0002 0123
    sh.add_argument("id", nargs="*", help="Epic or Task numeric id(s) (0001)")
    # Backwards-compatibility: accept legacy `--id` into `legacy_id` and
    # merge with positional ids inside `cmd_show`.
    sh.add_argument("--id", dest="legacy_id", nargs="+", help=argparse.SUPPRESS)
    sh.add_argument("--file", help="Backlog file to operate on (default: backlog.md)")
    g2 = sh.add_mutually_exclusive_group()
    g2.add_argument("--color", dest="color", action="store_true", help="Enable ANSI colorized output")
    g2.add_argument("--no-color", dest="color", action="store_false", help="Disable ANSI colorized output")
    sh.set_defaults(color=True)
    sh.set_defaults(func=cmd_show)

    return p


def main(argv: list[str] | None = None) -> int:
    argv = list(argv or sys.argv[1:])
    parser = build_parser()
    if not argv:
        parser.print_help()
        return 0
    # handle version specially
    if "--version" in argv:
        print(__version__)
        return 0
    args = parser.parse_args(argv)
    func = getattr(args, "func", None)
    if func is None:
        parser.print_help()
        return 0
    return func(args)


if __name__ == "__main__":
    raise SystemExit(main())
