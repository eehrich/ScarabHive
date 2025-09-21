"""Show-related commands for the backlog CLI."""
import argparse
import sys
from typing import Optional

from .. import parser as bl


def _ansi(text: str, code: Optional[str]) -> str:
    """Apply ANSI color codes to text."""
    if not code:
        return text
    return f"\x1b[{code}m{text}\x1b[0m"


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


def cmd_show(args: argparse.Namespace) -> int:
    """Show a detailed view of an epic or task by numeric id.

    --id accepts either an epic id or a task id.
    """
    path = args.file or "backlog.md"
    lines = bl.read_file(path)
    backlog = bl.parse(lines)

    # Merge positional ids and legacy --id (stored in legacy_id) for
    # backwards-compatibility. Ensure we have at least one id to show.
    legacy = getattr(args, 'legacy_id', None) or []
    positional = getattr(args, 'id', None) or []
    ids = list(positional) + list(legacy)
    if not ids and not getattr(args, 'interactive', False):
        print('ERROR: no id provided', file=sys.stderr)
        return 2

    # Interactive mode: prompt user to select items
    if not ids and getattr(args, 'interactive', False):
        print("Available items:")
        all_items = []
        idx = 1
        for e in backlog.epics_open + backlog.epics_finished:
            print(f"{idx}. Epic {e.id}: {e.title}")
            all_items.append(('epic', e.id))
            idx += 1
            for t in e.tasks:
                print(f"{idx}. Task {t.id}: {t.title}")
                all_items.append(('task', t.id))
                idx += 1
        try:
            selections = input("Enter item numbers to show (comma-separated, e.g. 1,3,5): ").strip()
            if not selections:
                print("No selection made.")
                return 0
            selected_indices = [int(x.strip()) - 1 for x in selections.split(',') if x.strip().isdigit()]
            ids = [all_items[i][1] for i in selected_indices if 0 <= i < len(all_items)]
        except (ValueError, IndexError, EOFError):
            print("Invalid input or no items selected.")
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
                    # Print multiline description if present
                    if getattr(t, 'description', None):
                        print("      - description:")
                        for line in t.description:
                            print(f"        {line}")
                break

        if found:
            continue

        # Try task
        try:
            epic, task = bl.find_task(backlog, ident)
        except KeyError:
            print(f"ERROR: id '{ident}' not found. Use 'backlog list' to see available items.", file=sys.stderr)
            missing = True
            continue

        print(_ansi(f"Task {task.id}: {task.title}", "33;1" if use_color else None))
        print(f"  status: {task.status}")
        if task.added:
            print(f"  added: {task.added}")
        if task.closed:
            print(f"  closed: {task.closed}")
        print(f"  Parent Epic: {epic.id}: {epic.title}")
        # Print multiline description if present
        if getattr(task, 'description', None):
            print("  - description:")
            for line in task.description:
                print(f"    {line}")

    return 1 if missing else 0
