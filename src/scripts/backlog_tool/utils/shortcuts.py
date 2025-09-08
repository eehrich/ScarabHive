"""Command shortcut utilities for backlog CLI."""


def handle_command_shortcuts(argv: list[str]) -> list[str]:
    """Convert command shortcuts to full command names."""
    if not argv:
        return argv

    shortcuts = {
        'a': 'add-task',
        'e': 'edit',
        'l': 'list',
        's': 'show'
    }

    first_arg = argv[0]
    if first_arg in shortcuts:
        return [shortcuts[first_arg]] + argv[1:]

    return argv
