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
    entry.append(f"- ☐ Task XXXX: {args.title}")
    entry.append(f"  - status: open")
    entry.append(f"  - added: {now}")
    if args.notes:
        entry.append("  - Notes:")
        for line in args.notes.splitlines():
            entry.append(f"    - {line}")
    print("Dry-run: task entry to insert:")
    print("\n".join(entry))
    return 0


def cmd_move_task(args: argparse.Namespace) -> int:
    # Move a task between epics (dry-run by default)
    from scripts.backlog_tool import parser as bl

    path = args.file or "backlog.md"
    lines = bl.read_file(path)
    backlog = bl.parse(lines)
    try:
        moved = bl.move_task(backlog, args.task, args.to_epic)
    except KeyError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 2
    print(f"Dry-run: moved task {args.task} -> epic {args.to_epic} (new id: {moved.id})")
    if args.write:
        bl.safe_write(path, bl.build_markdown(backlog))
        print(f"Wrote changes to {path}")
    return 0


def cmd_update_status(args: argparse.Namespace) -> int:
    from scripts.backlog_tool import parser as bl

    path = args.file or "backlog.md"
    lines = bl.read_file(path)
    backlog = bl.parse(lines)
    try:
        updated = bl.update_task_status(backlog, args.task, args.status)
    except KeyError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 2
    print(f"Dry-run: updated task {args.task} status -> {updated.status} (closed: {updated.closed})")
    if args.write:
        bl.safe_write(path, bl.build_markdown(backlog))
        print(f"Wrote changes to {path}")
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

    # allow selecting last by default
    backup_path = backups[-1]
    bl.restore_backup(path, backup_path)
    print(f"Restored backup: {backup_path}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="backlog")
    p.add_argument("--version", action="store_true", help="Show version and exit")
    p.add_argument("--file", help="Backlog file to operate on (default: backlog.md)")

    sub = p.add_subparsers(dest="cmd")

    v = sub.add_parser("validate", help="Validate the backlog file")
    v.add_argument("--file", help="Backlog file to operate on (default: backlog.md)")
    v.set_defaults(func=cmd_validate)

    a = sub.add_parser("add-task", help="Dry-run add a new task")
    a.add_argument("--title", required=True, help="Task title")
    a.add_argument("--notes", help="Optional notes text")
    a.add_argument("--file", help="Backlog file to operate on (default: backlog.md)")
    a.set_defaults(func=cmd_add_task)

    m = sub.add_parser("move-task", help="Move a task to another epic")
    m.add_argument("--task", required=True, help="Task id to move")
    m.add_argument("--to-epic", required=True, help="Destination epic id")
    m.add_argument("--file", help="Backlog file to operate on (default: backlog.md)")
    m.add_argument("--write", action="store_true", help="Persist changes to file")
    m.add_argument("--dry-run", action="store_true", help="Explicitly do not persist changes (default)")
    m.set_defaults(func=cmd_move_task)

    u = sub.add_parser("update-status", help="Update a task's status")
    u.add_argument("--task", required=True, help="Task id to update")
    u.add_argument("--status", required=True, help="New status value")
    u.add_argument("--file", help="Backlog file to operate on (default: backlog.md)")
    u.add_argument("--write", action="store_true", help="Persist changes to file")
    u.add_argument("--dry-run", action="store_true", help="Explicitly do not persist changes (default)")
    u.set_defaults(func=cmd_update_status)

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
