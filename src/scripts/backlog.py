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
    """Validate backlog file with comprehensive checks and detailed reporting."""
    import os
    from scripts.backlog_tool import parser as bl

    path = args.file or "backlog.md"
    if not os.path.exists(path):
        print(f"ERROR: backlog file not found: {path}", file=sys.stderr)
        return 2

    try:
        lines = bl.read_file(path)
        backlog = bl.parse(lines)
        errors = bl.validate_backlog(backlog)

        # Count items for summary
        open_epics = len(backlog.epics_open)
        finished_epics = len(backlog.epics_finished)
        total_epics = open_epics + finished_epics
        total_tasks = sum(len(e.tasks) for e in backlog.epics_open + backlog.epics_finished)

        if errors:
            print(f"[ERROR] Validation failed with {len(errors)} error(s):", file=sys.stderr)
            print(file=sys.stderr)

            # Group errors by type for better readability
            error_types = {}
            for error in errors:
                error_type = error.split(':')[0] if ':' in error else 'other'
                if error_type not in error_types:
                    error_types[error_type] = []
                error_types[error_type].append(error)

            for error_type, type_errors in error_types.items():
                print(f"[ERROR] {error_type.upper()}:", file=sys.stderr)
                for error in type_errors:
                    print(f"   - {error}", file=sys.stderr)
                print(file=sys.stderr)

            print(f"[INFO] Summary: {total_epics} epics ({open_epics} open, {finished_epics} finished), {total_tasks} tasks", file=sys.stderr)
            return 1
        else:
            # Success case with detailed summary
            print("[SUCCESS] Backlog validation successful!")
            print()
            print("[INFO] Summary:")
            print(f"   - Total epics: {total_epics} ({open_epics} open, {finished_epics} finished)")
            print(f"   - Total tasks: {total_tasks}")

            # Show some additional stats if requested
            if getattr(args, 'verbose', False):
                print()
                print("[INFO] Details:")

                # Count tasks by status
                status_counts = {}
                for epic in backlog.epics_open + backlog.epics_finished:
                    for task in epic.tasks:
                        status = task.status or 'unknown'
                        status_counts[status] = status_counts.get(status, 0) + 1

                if status_counts:
                    print("   - Task status distribution:")
                    for status, count in sorted(status_counts.items()):
                        print(f"     - {status}: {count} task(s)")

                # Check for recent activity
                recent_tasks = []
                for epic in backlog.epics_open + backlog.epics_finished:
                    for task in epic.tasks:
                        if task.added:
                            recent_tasks.append((task.added, task.id))

                if recent_tasks:
                    recent_tasks.sort(reverse=True)
                    latest_date = recent_tasks[0][0]
                    print(f"   - Latest task added: {latest_date} (Task {recent_tasks[0][1]})")

            print()
            print("[SUCCESS] All validation checks passed!")
            return 0

    except Exception as e:
        print(f"ERROR: Validation failed with exception: {e}", file=sys.stderr)
        return 2


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
                    line = ln
                    if line.lstrip().startswith('- '):
                        # remove the first hyphen and following space
                        idx = line.find('- ')
                        line = line[:idx] + line[idx+2:]
                    lines.append(line.rstrip())
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
    if getattr(args, "write", False):
        # perform the move and persist
        bak = bl.make_backup(path)
        bl.safe_write(path, bl.build_markdown(backlog))
        print(f"Moved task {task_id} -> epic {to_epic} (new id: {moved.id})")
        print(f"Wrote changes to {path}; backup: {bak}")
    else:
        print(f"Dry-run: moved task {task_id} -> epic {to_epic} (new id: {moved.id})")
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
    """Edit fields on one or more Epics or Tasks.

    Now supports multiple ids in a single invocation. All provided ids
    receive the same set of key=value updates. The command remains
    idempotent for each id and performs a single write/backup when
    ``--write`` is supplied.

    Usage: backlog edit <id> [<id> ...] --set key=value [--set key=value ...] [--write]

    Keys supported for tasks: title, status, added, closed, notes, description
    Keys supported for epics: title, status, added, closed, notes, description
    """
    from scripts.backlog_tool import parser as bl

    path = args.file or "backlog.md"
    lines = bl.read_file(path)
    backlog = bl.parse(lines)

    # Support one or more ids (existing parser already allows `nargs='+'`).
    raw_ids = list(getattr(args, 'id', []) or [])
    if not raw_ids:
        print('ERROR: no id provided', file=sys.stderr)
        return 2
    idents = []
    for rid in raw_ids:
        pid = _pad_id_input(rid)
        if pid:
            idents.append(pid)

    if not idents:
        print('ERROR: no valid ids provided', file=sys.stderr)
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

    # Helpers reused for each id.
    def _find_epic_or_task(identifier: str):
        # Try task first
        try:
            e, t = bl.find_task(backlog, identifier)
            return e, t
        except KeyError:
            pass
        # Epic direct
        for e in backlog.epics_open + backlog.epics_finished:
            if e.id == identifier:
                return e, None
        # Fallback numeric canonical comparison for tasks
        def _canon_id(s: str) -> str:
            s2 = str(s).strip()
            if s2.isdigit():
                return str(int(s2))
            return s2
        try:
            ident_canon = _canon_id(identifier)
        except Exception:
            ident_canon = str(identifier)
        for e in backlog.epics_open + backlog.epics_finished:
            for t in getattr(e, 'tasks', []) or []:
                t_id = getattr(t, 'id', None)
                if t_id is None:
                    continue
                try:
                    t_canon = _canon_id(t_id)
                except Exception:
                    t_canon = str(t_id)
                if ident_canon == t_canon or str(t_id) == str(identifier) or str(t_id).lstrip('0') == str(identifier).lstrip('0'):
                    return e, t
        raise KeyError(identifier)

    allowed_task_keys = {"title", "status", "added", "closed", "notes", "description"}
    allowed_epic_keys = {"title", "status", "added", "closed", "notes", "description"}

    import re as _re

    def _strip_raw_block(raw_lines: list[str], key: str) -> list[str]:
        out: list[str] = []
        i = 0
        key_re = _re.compile(rf"^\s*-\s*{_re.escape(key)}:\b", flags=_re.I)
        while i < len(raw_lines):
            ln = raw_lines[i]
            if key_re.match(ln):
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

    updated_tasks: list[str] = []
    updated_epics: list[str] = []
    missing: list[str] = []

    for ident in idents:
        try:
            epic, task = _find_epic_or_task(ident)
        except KeyError:
            missing.append(ident)
            continue

        if task is not None:
            invalid = [k for k in sets.keys() if k not in allowed_task_keys]
            if invalid:
                print(f"ERROR: invalid task field(s): {', '.join(sorted(invalid))}", file=sys.stderr)
                return 2
            for k, v in sets.items():
                if k == 'title':
                    task.title = v
                elif k == 'status':
                    task = bl.update_task_status(backlog, task.id, v)
                elif k == 'added':
                    task.added = v
                elif k == 'closed':
                    task.closed = v
                elif k == 'notes':
                    vv = v.replace('\\n', '\n')
                    normalized = []
                    for ln in vv.splitlines():
                        line = ln
                        if line.lstrip().startswith('- '):
                            idx = line.find('- ')
                            line = line[:idx] + line[idx+2:]
                        normalized.append(line.rstrip())
                    task.notes = normalized
                elif k == 'description':
                    vv = v.replace('\\n', '\n')
                    normalized = []
                    for ln in vv.splitlines():
                        line = ln
                        if line.lstrip().startswith('- '):
                            idx = line.find('- ')
                            line = line[:idx] + line[idx+2:]
                        normalized.append(line.rstrip())
                    task.description = normalized
            updated_tasks.append(task.id)
            continue

        # epic path
        invalid = [k for k in sets.keys() if k not in allowed_epic_keys]
        if invalid:
            print(f"ERROR: invalid epic field(s): {', '.join(sorted(invalid))}", file=sys.stderr)
            return 2
        for k, v in sets.items():
            if k == 'title':
                epic.title = v
            elif k == 'status':
                epic.status = v
            elif k == 'added':
                epic.added = v
                epic.raw_lines = _strip_raw_block(epic.raw_lines, 'added')
            elif k == 'closed':
                epic.closed = v
                epic.raw_lines = _strip_raw_block(epic.raw_lines, 'closed')
            elif k == 'notes':
                vv = v.replace('\\n', '\n')
                normalized = []
                for ln in vv.splitlines():
                    line = ln
                    if line.lstrip().startswith('- '):
                        idx = line.find('- ')
                        line = line[:idx] + line[idx+2:]
                    normalized.append(line.rstrip())
                epic.notes = normalized
                epic.raw_lines = _strip_raw_block(epic.raw_lines, 'notes')
            elif k == 'description':
                vv = v.replace('\\n', '\n')
                normalized = []
                for ln in vv.splitlines():
                    line = ln
                    if line.lstrip().startswith('- '):
                        idx = line.find('- ')
                        line = line[:idx] + line[idx+2:]
                    normalized.append(line.rstrip())
                epic.description = normalized
                epic.raw_lines = _strip_raw_block(epic.raw_lines, 'description')
        updated_epics.append(epic.id)

    if not updated_tasks and not updated_epics and not missing:
        print('Nothing updated')
        return 0

    single_mode = len(idents) == 1
    # Record type for single legacy message formatting
    single_kind: str | None = None
    if single_mode:
        # Peek classification without mutating
        try:
            e_tmp, t_tmp = _find_epic_or_task(idents[0])
            single_kind = 'task' if t_tmp is not None else 'epic'
        except KeyError:
            single_kind = None
    if getattr(args, 'write', False) and (updated_tasks or updated_epics):
        bak = bl.make_backup(path)
        bl.safe_write(path, bl.build_markdown(backlog))
        if single_mode and single_kind == 'task' and updated_tasks:
            parent_epic = None
            for e in backlog.epics_open + backlog.epics_finished:
                if any(t.id == updated_tasks[0] for t in e.tasks):
                    parent_epic = e.id
                    break
            print(f"Updated task {updated_tasks[0]} (Epic {parent_epic})")
        elif single_mode and single_kind == 'epic' and updated_epics:
            print(f"Updated epic {updated_epics[0]}")
        else:
            if updated_epics:
                print("Updated epics: " + ', '.join(sorted(updated_epics)))
            if updated_tasks:
                print("Updated tasks: " + ', '.join(sorted(updated_tasks)))
        print(f"Wrote changes to {path}; backup: {bak}")
    else:
        if single_mode and single_kind == 'task' and updated_tasks:
            parent_epic = None
            for e in backlog.epics_open + backlog.epics_finished:
                if any(t.id == updated_tasks[0] for t in e.tasks):
                    parent_epic = e.id
                    break
            print(f"Dry-run: updated task {updated_tasks[0]} (Epic {parent_epic})")
        elif single_mode and single_kind == 'epic' and updated_epics:
            print(f"Dry-run: updated epic {updated_epics[0]}")
        else:
            if updated_epics:
                print("Dry-run: would update epics: " + ', '.join(sorted(updated_epics)))
            if updated_tasks:
                print("Dry-run: would update tasks: " + ', '.join(sorted(updated_tasks)))

    if missing:
        for m in missing:
            print(f"ERROR: id {m} not found", file=sys.stderr)
        return 2
    return 0


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
    date_changes = bl.auto_fix_date_formats(backlog)
    id_format_changes = bl.auto_fix_id_formats(backlog)
    epic_completion_changes = bl.auto_complete_epics(backlog)
    
    all_changes = id_changes + collision_changes + norm_changes + date_changes + id_format_changes + epic_completion_changes
    
    if not all_changes:
        print("No formatting or id issues found")
        return 0
        
    print("Planned changes:")
    for old, new in id_changes:
        print(f"reassign: {old} -> {new}")
    for old, new in collision_changes:
        print(f"reassign collision: {old} -> {new}")
    for c in norm_changes:
        print(f"normalize: {c}")
    for c in date_changes:
        print(f"date fix: {c}")
    for c in id_format_changes:
        print(f"id format: {c}")
    for c in epic_completion_changes:
        print(f"epic completion: {c}")
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
                # Match both normalized (0001) and original (1, 01, 001) ID formats
                # The old_id from the model is normalized, but text may have original format
                old_patterns = [old]  # Start with normalized format
                
                # Also try shorter versions of the ID if it's numeric
                if old.isdigit():
                    num = int(old)
                    if num < 1000:  # Only for 4-digit or less
                        old_patterns.extend([f"{num:01d}", f"{num:02d}", f"{num:03d}"])
                
                for old_id_pattern in old_patterns:
                    # Match the actual format: optional status symbols + "Epic/Task" + ID + ":"
                    # Use count=1 to replace only one occurrence at a time
                    epic_pattern = rf'((?:☐|✅|❌|⏳|\[ ?\])?\s*Epic\s+){_re.escape(old_id_pattern)}(?=\s*:)'
                    task_pattern = rf'((?:☐|✅|❌|⏳|\[ ?\])?\s*Task\s+){_re.escape(old_id_pattern)}(?=\s*:)'
                    
                    # Replace one occurrence at a time to avoid replacing all duplicates
                    _text, epic_count = _re.subn(epic_pattern, _replace_with_new(old_id_pattern, new), _text, count=1)
                    _text, task_count = _re.subn(task_pattern, _replace_with_new(old_id_pattern, new), _text, count=1)
                    
                    if epic_count > 0 or task_count > 0:
                        break  # Successfully replaced one occurrence, move to next change

            bl.safe_write(path, _text)
            print(f"Applied id-only fixes; backup: {bak}")
            if collision_changes:
                for old, new in collision_changes:
                    print(f"reassign collision: {old} -> {new}")
        else:
            # full reserialize path: write canonicalized markdown from model
            bl.safe_write(path, bl.build_markdown(backlog))
            print(f"Applied all fixes; backup: {bak}")
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
        # When moving a finished epic, ensure we record a closing date on
        # the epic (use '- closed: YYYY-MM-DD') rather than an undefined
        # '- updated:' field. Insert the closed date after the epic header
        # line if it isn't already present.
        if '- closed:' not in block:
            parts = block.splitlines(keepends=True)
            if len(parts) >= 1:
                # use two-space indentation consistent with other epic fields
                parts.insert(1, f"  - closed: {today}\n")
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
    p = argparse.ArgumentParser(
        prog="backlog",
        description="Backlog CLI - A lightweight tool for managing project backlogs in Markdown format.",
        epilog="""
EXAMPLES:
  Basic Operations:
    backlog validate                    # Check backlog for errors
    backlog list                        # Show open epics
    backlog show 0001                   # View epic details

  Task Management:
    backlog add-task --title "Fix bug" --epic 0001 --write
    backlog edit 0002 --set status=done --write
    backlog move-task --task 0003 --to-epic 0004 --write

  Safety & Recovery:
    backlog backup --dry-run            # Preview backup creation
    backlog undo --list                 # See available backups
    backlog undo --choose               # Interactive restore

SAFETY: Use --write to persist changes. All operations create backups automatically.
COLOR: Auto-detected; use --color/--no-color to override.
FILES: Default is backlog.md; use --file to specify alternative.
""",
        formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument("--version", action="store_true", help="Show version and exit")
    p.add_argument("--file", help="Backlog file to operate on (default: backlog.md)")

    sub = p.add_subparsers(dest="cmd", metavar="COMMAND", help="Available commands:")

    v = sub.add_parser("validate", 
                      help="🔍 Validate backlog file for errors and inconsistencies",
                      description="Validate the backlog file for common issues like duplicate IDs, invalid dates, and malformed entries.")
    # Standardized option ordering: positional → required → optional → file → safety → output
    v.add_argument("--file", help="Backlog file to operate on (default: backlog.md)")
    v.add_argument("--verbose", action="store_true", help="Show detailed validation statistics")
    v.set_defaults(func=cmd_validate)

    a = sub.add_parser("add-task", 
                       help="➕ Add a new task to an epic",
                       description="Add a new task to an existing epic. Use --write to persist changes. The task will be added with 'open' status and today's date.")
    # Standardized option ordering: positional → required → optional → file → safety → output
    a.add_argument("--title", required=True, help="Task title")
    a.add_argument("--epic", required=False, help="Epic id to add the task under (required with --write)")
    a.add_argument("--notes", help="Optional notes text (use \\n for line breaks)")
    a.add_argument("--id", dest="forced_id", help="Force a specific Task id (numeric or string). Will error if id exists")
    a.add_argument("--file", help="Backlog file to operate on (default: backlog.md)")
    a.add_argument("--write", action="store_true", help="⚠️  Persist changes to file (creates backup)")
    a.set_defaults(func=cmd_add_task)

    ae = sub.add_parser("add-epic", 
                       help="📋 Create a new epic",
                       description="Create a new epic and add it to the backlog. Use --write to persist changes. The epic will be added with 'open' status and today's date.")
    # Standardized option ordering: positional → required → optional → file → safety → output
    ae.add_argument("--title", required=True, help="Epic title")
    ae.add_argument("--id", dest="forced_id", help="Force a specific Epic id (numeric or string). Will error if id exists")
    ae.add_argument("--file", help="Backlog file to operate on (default: backlog.md)")
    ae.add_argument("--write", action="store_true", help="⚠️  Persist changes to file (creates backup)")
    ae.set_defaults(func=cmd_add_epic)

    m = sub.add_parser("move-task", 
                      help="↔️  Move a task between epics",
                      description="Move an existing task from one epic to another. Use --write to persist changes.")
    # Standardized option ordering: positional → required → optional → file → safety → output
    m.add_argument("--task", required=True, help="Task id to move")
    m.add_argument("--to-epic", required=True, help="Destination epic id")
    m.add_argument("--file", help="Backlog file to operate on (default: backlog.md)")
    m.add_argument("--write", action="store_true", help="⚠️  Persist changes to file (creates backup)")
    m.set_defaults(func=cmd_move_task)

    # Replace legacy update-status with a more general `edit` command that
    # can set arbitrary fields on epics or tasks.
    u = sub.add_parser("edit", 
                      help="✏️  Edit epic or task fields",
                      description="Update fields on one or more epics/tasks. Supports bulk updates with --set key=value. Use multiple --set for multiple fields.")
    # Standardized option ordering: positional → required → optional → file → safety → output
    u.add_argument("id", nargs="+", help="Epic or Task numeric id(s) (0001)")
    u.add_argument("--set", dest="set", action="append", help="Set a field: --set key=value (can be used multiple times)")
    u.add_argument("--file", help="Backlog file to operate on (default: backlog.md)")
    u.add_argument("--write", action="store_true", help="⚠️  Persist changes to file (creates backup)")
    u.set_defaults(func=cmd_edit)

    b = sub.add_parser("backup", 
                      help="💾 Create or manage backups",
                      description="Create timestamped backups of the backlog file or manage existing backups. Use --prune with --keep or --older-than to clean up old backups.")
    # Standardized option ordering: positional → required → optional → file → safety → output
    b.add_argument("--file", help="Backlog file to operate on (default: backlog.md)")
    b.add_argument("--prune", action="store_true", help="Remove old backups instead of creating a new one")
    b.add_argument("--keep", type=int, help="When pruning, keep the newest N backups (default: 10)")
    b.add_argument("--older-than", type=int, help="When pruning, remove backups older than N days")
    b.add_argument("--dry-run", action="store_true", help="Show which backups would be removed (with --prune)")
    b.add_argument("--yes", action="store_true", help="Confirm destructive prune without prompt")
    b.set_defaults(func=cmd_backup)

    r = sub.add_parser("undo", 
                      help="↶ Restore from backup",
                      description="Restore the backlog file from a previous backup. Use --list to see available backups, --choose for interactive selection, or --backup for specific file.")
    # Standardized option ordering: positional → required → optional → file → safety → output
    r.add_argument("--file", help="Backlog file to operate on (default: backlog.md)")
    r.add_argument("--list", action="store_true", help="List available backups and exit")
    r.add_argument("--choose", action="store_true", help="Interactively choose a backup to restore")
    r.add_argument("--backup", help="Restore a specific backup file path (exact match from --list)")
    r.set_defaults(func=cmd_undo)

    c = sub.add_parser("check-ids", 
                      help="🔍 Check for duplicate IDs",
                      description="Scan the backlog for duplicate task IDs and epic/task ID collisions.")
    # Standardized option ordering: positional → required → optional → file → safety → output
    c.add_argument("--file", help="Backlog file to operate on (default: backlog.md)")
    c.set_defaults(func=cmd_check_ids)

    f = sub.add_parser("fix-format", 
                      help="🔧 Auto-fix formatting issues",
                      description="Normalize status tokens, fix date formats, and reassign duplicate IDs. Use --ids-only for safe ID-only fixes.")
    # Standardized option ordering: positional → required → optional → file → safety → output
    f.add_argument("--file", help="Backlog file to operate on (default: backlog.md)")
    f.add_argument("--ids-only", action="store_true", dest="ids_only",
                   help="When writing, only rewrite numeric Task/Epic ids and leave formatting intact")
    f.add_argument("--write", action="store_true", help="⚠️  Apply fixes and persist to file (creates backup)")
    f.set_defaults(func=cmd_fix_format)

    # legacy compatibility: expose the `update` command used by older scripts/tests
    up = sub.add_parser("update", 
                       help="📦 Move finished epics",
                       description="Legacy command: validate and move finished epics from open to finished section.")
    # Standardized option ordering: positional → required → optional → file → safety → output
    up.add_argument("--file", help="Backlog file to operate on (default: backlog.md)")
    up.set_defaults(func=cmd_update)

    ini = sub.add_parser("init", 
                        help="📄 Create new backlog file",
                        description="Create a new backlog.md file from the bundled template if it doesn't exist.")
    # Standardized option ordering: positional → required → optional → file → safety → output
    ini.add_argument("--file", help="Backlog file to create (default: backlog.md)")
    ini.set_defaults(func=lambda args: cmd_init(args))

    ls = sub.add_parser("list", 
                       help="📋 List epics and tasks",
                       description="List all epic and task IDs with their titles. Use filters to show specific subsets. Combine --state and --only for precise filtering.")
    # Standardized option ordering: positional → required → optional → file → safety → output
    ls.add_argument("--file", help="Backlog file to operate on (default: backlog.md)")
    ls.add_argument("--state", choices=["open", "finished", "all"], default="open",
                    help="Filter by epic state (default: open)")
    ls.add_argument("--only", choices=["epics", "tasks", "all"], default="epics",
                    help="Show only epics, only tasks, or all (default: epics)")
    ls.add_argument("--ids-only", action="store_true", dest="ids_only",
                    help="Print only numeric ids, one per line")
    # color tri-state: --color, --no-color; default None means auto-detect tty
    g = ls.add_mutually_exclusive_group()
    g.add_argument("--color", dest="color", action="store_true", help="Enable ANSI colorized output")
    g.add_argument("--no-color", dest="color", action="store_false", help="Disable ANSI colorized output")
    ls.set_defaults(color=True)
    ls.set_defaults(func=cmd_list)

    sh = sub.add_parser("show", 
                       help="👀 Show detailed information",
                       description="Show detailed information for one or more epic/task IDs. Accepts multiple IDs and supports both epic and task identifiers.")
    # Standardized option ordering: positional → required → optional → file → safety → output
    sh.add_argument("id", nargs="*", help="Epic or Task numeric id(s) (0001)")
    # Backwards-compatibility: accept legacy `--id` into `legacy_id` and
    # merge with positional ids inside `cmd_show`.
    sh.add_argument("--id", dest="legacy_id", nargs="+", help=argparse.SUPPRESS)
    sh.add_argument("--file", help="Backlog file to operate on (default: backlog.md)")
    # color tri-state: --color, --no-color; default None means auto-detect tty
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
