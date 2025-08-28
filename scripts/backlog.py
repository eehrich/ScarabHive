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
    # Placeholder validation: ensure backlog file exists and has a header
    import os

    path = args.file or "backlog.md"
    if not os.path.exists(path):
        print(f"ERROR: backlog file not found: {path}", file=sys.stderr)
        return 2
    with open(path, "r", encoding="utf-8") as f:
        data = f.read()
    if "# Backlog" not in data.splitlines()[0]:
        print("ERROR: invalid backlog header", file=sys.stderr)
        return 3
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


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="backlog")
    p.add_argument("--version", action="store_true", help="Show version and exit")
    p.add_argument("--file", help="Backlog file to operate on (default: backlog.md)")

    sub = p.add_subparsers(dest="cmd")

    v = sub.add_parser("validate", help="Validate the backlog file")
    v.set_defaults(func=cmd_validate)

    a = sub.add_parser("add-task", help="Dry-run add a new task")
    a.add_argument("--title", required=True, help="Task title")
    a.add_argument("--notes", help="Optional notes text")
    a.set_defaults(func=cmd_add_task)

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
