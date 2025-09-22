"""Add-related commands for the backlog CLI."""
import argparse
import csv
import json
import os
import shutil
import sys
from datetime import date
from typing import Optional

from ..utils import ProgressBar


def _pad_id_input(ident: Optional[str]) -> Optional[str]:
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


def _normalize_notes(s: Optional[str]) -> Optional[str]:
    """Normalize notes by handling literal \\n sequences and stripping list markers."""
    if s is None:
        return None
    s2 = s.replace('\\n', '\n')
    lines = []
    for ln in s2.splitlines():
        line = ln
        # Only remove the first '- ' if it's a list marker, not '--' or other patterns
        stripped = line.lstrip()
        if stripped.startswith('- ') and not stripped.startswith('--'):
            # remove the first hyphen and following space
            idx = line.find('- ')
            line = line[:idx] + line[idx+2:]
        lines.append(line.rstrip())
    return '\n'.join(lines)


def _cmd_add_task_bulk(args: argparse.Namespace) -> int:
    """Handle bulk task addition from file."""
    file_path = args.from_file
    if not os.path.exists(file_path):
        print(f"ERROR: File not found: {file_path}", file=sys.stderr)
        return 2

    # Determine file type and parse
    if file_path.endswith('.json'):
        try:
            with open(file_path, 'r', encoding='utf-8') as f:
                data = json.load(f)
        except json.JSONDecodeError as e:
            print(f"ERROR: Invalid JSON file: {e}", file=sys.stderr)
            return 2
    elif file_path.endswith('.csv'):
        try:
            with open(file_path, 'r', encoding='utf-8') as f:
                reader = csv.DictReader(f)
                data = list(reader)
        except csv.Error as e:
            print(f"ERROR: Invalid CSV file: {e}", file=sys.stderr)
            return 2
    else:
        print("ERROR: File must be .csv or .json", file=sys.stderr)
        return 2

    if not data:
        print("ERROR: File contains no data", file=sys.stderr)
        return 2

    # Validate data structure
    required_fields = ['title', 'epic']
    for item in data:
        missing = [field for field in required_fields if field not in item or not item[field]]
        if missing:
            print(f"ERROR: Missing required fields in data: {missing}", file=sys.stderr)
            return 2

    from .. import parser as bl
    path = args.file or "backlog.md"

    # create backlog from bundled template if it does not exist
    if not os.path.exists(path):
        tpl = os.path.join(os.path.dirname(__file__), '..', 'template.md')
        if not os.path.exists(tpl):
            print(f"ERROR: template not found: {tpl}", file=sys.stderr)
            return 2
        shutil.copy2(tpl, path)
        print(f"Created backlog from template: {path}")

    lines = bl.read_file(path)
    backlog = bl.parse(lines)

    created_tasks = []
    errors = []

    # Show progress for bulk operations with many items
    show_progress = len(data) > 5
    if show_progress:
        progress = ProgressBar(len(data), "Processing tasks")

    for i, item in enumerate(data):
        try:
            epic_id = _pad_id_input(item['epic'])
            if epic_id is None:
                errors.append(f"Row {i+1}: Invalid epic ID")
                continue
            forced = _pad_id_input(item.get('id')) if item.get('id') else None

            notes_arg = _normalize_notes(item.get('notes'))
            t = bl.add_task_to_epic(backlog, epic_id, item['title'], notes_arg, forced_id=forced)
            created_tasks.append((t.id, epic_id))

        except KeyError:
            errors.append(f"Row {i+1}: Epic '{item.get('epic', 'unknown')}' not found")
        except ValueError as e:
            errors.append(f"Row {i+1}: {e}")
        except Exception as e:
            errors.append(f"Row {i+1}: Unexpected error: {e}")

        if show_progress:
            progress.update()

    # Report results
    if created_tasks:
        print(f"Successfully created {len(created_tasks)} tasks:")
        for task_id, epic_id in created_tasks:
            print(f"  - Task {task_id} under epic {epic_id}")

    if errors:
        print(f"\nErrors encountered ({len(errors)}):", file=sys.stderr)
        for error in errors:
            print(f"  - {error}", file=sys.stderr)

    if getattr(args, "write", False) and created_tasks:
        backup_dir = getattr(args, "backup_dir", None)
        max_backups = getattr(args, "max_backups", None)
        bak = bl.make_backup(path, backup_dir, max_backups)
        bl.safe_write(path, bl.build_markdown(backlog))
        print(f"\nWrote changes to {path}; backup: {bak}")
    elif not getattr(args, "write", False):
        print(f"\nDry-run: would create {len(created_tasks)} tasks")

    # Return error code if any tasks failed
    return 1 if errors else 0


def cmd_add_task(args: argparse.Namespace) -> int:
    """Add a new task to an epic."""
    # Check if we're doing bulk add from file
    if getattr(args, 'from_file', None):
        return _cmd_add_task_bulk(args)

    # Validate required arguments for single task
    if not getattr(args, 'title', None):
        print("ERROR: --title is required when not using --from-file", file=sys.stderr)
        return 2

    # Dry-run add: print a formatted snippet that would be inserted
    now = date.today().isoformat()
    entry = []
    entry.append(f"- \u2610 Task XXXX: {args.title}")
    entry.append("  - status: open")
    entry.append(f"  - added: {now}")
    if getattr(args, "description", None):
        # Allow CLI users to pass literal '\\n' sequences which should
        # be interpreted as real newlines. Normalize here for preview.
        description_raw = args.description.replace('\\n', '\n')
        entry.append("  - Description:")
        for line in description_raw.splitlines():
            entry.append(f"    - {line}")
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
        from .. import parser as bl
        path = args.file or "backlog.md"
        # create backlog from bundled template if it does not exist
        if not os.path.exists(path):
            tpl = os.path.join(os.path.dirname(__file__), '..', 'template.md')
            if not os.path.exists(tpl):
                print(f"ERROR: template not found: {tpl}", file=sys.stderr)
                return 2
            shutil.copy2(tpl, path)
            print(f"Created backlog from template: {path}")
        lines = bl.read_file(path)
        backlog = bl.parse(lines)
        try:
            epic_id = _pad_id_input(getattr(args, 'epic', None))
            if epic_id is None:
                print("ERROR: Invalid epic ID", file=sys.stderr)
                return 2
            forced = _pad_id_input(getattr(args, 'forced_id', None))
            notes_arg = _normalize_notes(getattr(args, "notes", None))
            description_arg = _normalize_notes(getattr(args, "description", None))
            t = bl.add_task_to_epic(backlog, epic_id, args.title, notes_arg, description_arg, forced_id=forced)
        except KeyError:
            print(f"ERROR: Epic '{epic_id}' not found. Use 'backlog list' to see available epics.", file=sys.stderr)
            return 2
        except ValueError as e:
            print(f"ERROR: {e}. Check task title and id format.", file=sys.stderr)
            return 2
        backup_dir = getattr(args, "backup_dir", None)
        max_backups = getattr(args, "max_backups", None)
        bak = bl.make_backup(path, backup_dir, max_backups)
        bl.safe_write(path, bl.build_markdown(backlog))
        print(f"Created task {t.id} under epic {epic_id}; backup: {bak}")
    return 0


def _cmd_add_epic_bulk(args: argparse.Namespace) -> int:
    """Handle bulk epic addition from file."""
    file_path = args.from_file
    if not os.path.exists(file_path):
        print(f"ERROR: File not found: {file_path}", file=sys.stderr)
        return 2

    # Determine file type and parse
    if file_path.endswith('.json'):
        try:
            with open(file_path, 'r', encoding='utf-8') as f:
                data = json.load(f)
        except json.JSONDecodeError as e:
            print(f"ERROR: Invalid JSON file: {e}", file=sys.stderr)
            return 2
    elif file_path.endswith('.csv'):
        try:
            with open(file_path, 'r', encoding='utf-8') as f:
                reader = csv.DictReader(f)
                data = list(reader)
        except csv.Error as e:
            print(f"ERROR: Invalid CSV file: {e}", file=sys.stderr)
            return 2
    else:
        print("ERROR: File must be .csv or .json", file=sys.stderr)
        return 2

    if not data:
        print("ERROR: File contains no data", file=sys.stderr)
        return 2

    # Validate data structure
    for item in data:
        if 'title' not in item or not item['title']:
            print("ERROR: Missing required 'title' field in data", file=sys.stderr)
            return 2

    from .. import parser as bl
    path = args.file or "backlog.md"

    # create backlog from bundled template if it does not exist
    if not os.path.exists(path):
        tpl = os.path.join(os.path.dirname(__file__), '..', 'template.md')
        if not os.path.exists(tpl):
            print(f"ERROR: template not found: {tpl}", file=sys.stderr)
            return 2
        shutil.copy2(tpl, path)
        print(f"Created backlog from template: {path}")

    lines = bl.read_file(path)
    backlog = bl.parse(lines)

    created_epics = []
    created_tasks = []
    errors = []

    # Show progress for bulk operations with many items
    show_progress = len(data) > 5
    if show_progress:
        progress = ProgressBar(len(data), "Creating epics")

    for i, item in enumerate(data):
        try:
            forced = _pad_id_input(item.get('id')) if item.get('id') else None
            e = bl.add_epic_to_backlog(backlog, item['title'], forced_id=forced)
            created_epics.append(e.id)

            # If the epic JSON includes a 'tasks' array, add those tasks under the newly created epic
            tasks_list = item.get('tasks') or []
            if isinstance(tasks_list, list) and tasks_list:
                for ti, task_item in enumerate(tasks_list):
                    if not isinstance(task_item, dict) or 'title' not in task_item or not task_item['title']:
                        errors.append(f"Row {i+1} task {ti+1}: missing required 'title' field")
                        continue
                    try:
                        task_forced = _pad_id_input(task_item.get('id')) if task_item.get('id') else None
                        notes_arg = _normalize_notes(task_item.get('notes')) if task_item.get('notes') else None
                        description_arg = _normalize_notes(task_item.get('description')) if task_item.get('description') else None
                        t = bl.add_task_to_epic(backlog, e.id, task_item['title'], notes_arg, description_arg, forced_id=task_forced)
                        created_tasks.append((t.id, e.id))
                    except Exception as te:
                        errors.append(f"Row {i+1} task {ti+1}: Unexpected error: {te}")

        except ValueError as ve:
            errors.append(f"Row {i+1}: {ve}")
        except Exception as e:
            errors.append(f"Row {i+1}: Unexpected error: {e}")

        if show_progress:
            progress.update()

    # Report results
    if created_epics:
        print(f"Successfully created {len(created_epics)} epics:")
        for epic_id in created_epics:
            print(f"  - Epic {epic_id}")

    if created_tasks:
        print(f"Successfully created {len(created_tasks)} tasks alongside epics:")
        for task_id, epic_id in created_tasks:
            print(f"  - Task {task_id} under epic {epic_id}")

    if errors:
        print(f"\nErrors encountered ({len(errors)}):", file=sys.stderr)
        for error in errors:
            print(f"  - {error}", file=sys.stderr)

    if getattr(args, "write", False) and created_epics:
        backup_dir = getattr(args, "backup_dir", None)
        max_backups = getattr(args, "max_backups", None)
        bak = bl.make_backup(path, backup_dir, max_backups)
        bl.safe_write(path, bl.build_markdown(backlog))
        print(f"\nWrote changes to {path}; backup: {bak}")
    elif not getattr(args, "write", False):
        print(f"\nDry-run: would create {len(created_epics)} epics")

    # Return error code if any epics failed
    return 1 if errors else 0


def cmd_add_epic(args: argparse.Namespace) -> int:
    """Create a new epic."""
    # Check if we're doing bulk add from file
    if getattr(args, 'from_file', None):
        return _cmd_add_epic_bulk(args)

    # Validate required arguments for single epic
    if not getattr(args, 'title', None):
        print("ERROR: --title is required when not using --from-file", file=sys.stderr)
        return 2

    from .. import parser as bl
    path = args.file or "backlog.md"
    # Show intent. If --write was passed, this is not a dry-run.
    if args.write:
        print(f"Create epic -> title: {args.title}")
    else:
        print(f"Dry-run: create epic -> title: {args.title}")
    if args.write:
        # create from template if missing
        if not os.path.exists(path):
            tpl = os.path.join(os.path.dirname(__file__), '..', 'template.md')
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
            description_arg = _normalize_notes(getattr(args, "description", None))
            try:
                e = bl.add_epic_to_backlog(backlog_obj, args.title, description=description_arg, forced_id=forced)
            except ValueError as ve:
                print(f"ERROR: {ve}", file=sys.stderr)
                return 2
            backup_dir = getattr(args, "backup_dir", None)
            max_backups = getattr(args, "max_backups", None)
            bak = bl.make_backup(path, backup_dir, max_backups)
            bl.safe_write(path, bl.build_markdown(backlog_obj))
            print(f"Created epic {e.id}; backup: {bak}")
        else:
            lines = bl.read_file(path)
            backlog = bl.parse(lines)
            forced = _pad_id_input(getattr(args, 'forced_id', None))
            description_arg = _normalize_notes(getattr(args, "description", None))
            try:
                e = bl.add_epic_to_backlog(backlog, args.title, description=description_arg, forced_id=forced)
            except ValueError as ve:
                print(f"ERROR: {ve}", file=sys.stderr)
                return 2
            backup_dir = getattr(args, "backup_dir", None)
            max_backups = getattr(args, "max_backups", None)
            bak = bl.make_backup(path, backup_dir, max_backups)
            bl.safe_write(path, bl.build_markdown(backlog))
            print(f"Created epic {e.id}; backup: {bak}")
    return 0
