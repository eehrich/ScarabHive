"""Backup-related commands for the backlog CLI."""
import argparse
import os
import sys

from .. import parser as bl
from ..utils import ProgressBar


def cmd_backup(args: argparse.Namespace) -> int:
    """Create or manage backups of the backlog file."""
    path = args.file or "backlog.md"
    if not os.path.exists(path):
        print(f"ERROR: backlog file not found: {path}", file=sys.stderr)
        return 2
    if getattr(args, "prune", False):
        # pruning behavior
        keep = getattr(args, "keep", None)
        # Use max_backups config as default if keep not specified
        if keep is None:
            keep = getattr(args, "max_backups", 10)
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

        print("Analyzing backups to prune...")
        removed = bl.prune_backups(path, keep=keep, older_than_days=older)

        if removed:
            print(f"Pruning {len(removed)} backup files...")
            # Show progress for bulk file operations
            if len(removed) > 5:
                progress = ProgressBar(len(removed), "Removing backups")
                for i, r in enumerate(removed):
                    print(r)
                    progress.update()
            else:
                print("Pruned backups:")
                for r in removed:
                    print(r)
        else:
            print("No backups to prune.")

        return 0

    bak = bl.make_backup(path)
    print(f"Created backup: {bak}")
    return 0
