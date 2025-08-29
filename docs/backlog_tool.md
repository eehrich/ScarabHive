# Backlog CLI (backlog)

A short reference for the project's minimal backlog CLI. The console entrypoint is the `backlog` script (module `scripts.backlog`). This document lists commands, flags, and examples for common workflows.

## Purpose

The backlog CLI provides a lightweight, test-friendly interface to read, inspect, and mutate the project's `backlog.md` file. It is intentionally conservative: most operations are dry-run by default and writes create timestamped backups in a `.backups/` folder.

## Location

- CLI entrypoint: `src/scripts/backlog.py`
- Parser/writer library: `src/scripts/backlog_tool/parser.py`
- Backlog file: by default `backlog.md` in the repository root (override with `--file <path>`)

## Common flags

- `--file <path>`  — operate on a specific backlog file (default: `backlog.md`)
- `--write`        — persist changes to disk (most commands default to dry-run)
- `--id <id>`      — for `add-task` / `add-epic` this forces a specific numeric id (see notes)

Notes on ids
- Forced ids must be numeric; the tool pads numeric ids to 4 digits (e.g. `13` → `0013`).
- If a forced id already exists in the backlog (epic or task), the add operation fails with an error.

Backups
- Any command that writes will create a timestamped backup under `.backups/<filename>.<YYYYMMDD_HHMMSS>.bak` before replacing the target file.

## Commands

All commands are available via `backlog <command> [options]`.

## Running the CLI

On Windows development machines we recommend using the project's virtual environment executables directly to avoid PATH or activation issues.

- Use the bundled Python executable in the virtual env to run the module:

```bash
.venv/Scripts/python.exe -m scripts.backlog --help
```

- Or use the `backlog` console script executable (if created) directly from the venv Scripts directory:

```bash
.venv/Scripts/backlog.exe <command> [options]
```

Both approaches are useful in CI or automation where activating the venv isn't desirable. On POSIX systems the equivalent is `.venv/bin/python` or `.venv/bin/backlog`.


### validate

Usage

    backlog validate [--file <path>]

Description
- Performs validation checks on `backlog.md` (IDs uniqueness, date formats, known status tokens).
- Returns non-zero on validation errors and prints details.

### add-task

Usage

    backlog add-task --title <title> [--epic <epic-id>] [--id <id>] [--notes <text>] [--file <path>] [--write]

Description
- Creates a new task under an epic.
- Dry-run by default (prints the snippet it would insert).
- When `--write` is provided the CLI will insert the task into the backlog and write the updated file (with backup).
- `--epic` must be provided when using `--write`.
- `--id` forces a specific numeric id (error if the id exists already).
- `--notes` accepts a string; use `\n` to include literal newlines in the CLI (the code converts `\n` into real newlines for modeled fields).

Example

    backlog add-task --title "Fix login" --epic 0001 --notes "Investigate\nAdd tests" --write

Or force id (numeric-only):

    backlog add-task --title "Hotfix" --epic 0001 --id 1234 --write

### add-epic

Usage

    backlog add-epic --title <title> [--id <id>] [--file <path>] [--write]

Description
- Create a new epic and append it to the `## 1. Epics - open` section.
- `--id` forces a numeric id (errors on collision); otherwise the tool picks the next unused numeric id.
- Dry-run unless `--write` is specified.

Example

    backlog add-epic --title "New Integration" --write
    backlog add-epic --title "Urgent" --id 2000 --write

### move-task

Usage

    backlog move-task --task <task-id> --to-epic <epic-id> [--file <path>] [--write]

Description
- Dry-run by default; with `--write` persists the change.
- If the destination epic already contains a task with the same id, the CLI will generate a new unique id for the moved task.

### edit

Usage

    backlog edit <id> --set key=value [--set key=value ...] [--file <path>] [--write]

Supported keys for tasks: `title`, `status`, `added`, `closed`, `notes`, `description`.
Supported keys for epics: `title`, `status`, `added`, `closed`, `notes`, `description`.

Notes
- CLI `--set` values may contain literal `\n` sequences which are translated into real newlines for modeled fields like `notes` and `description`.
- For epics, editing modeled fields removes corresponding raw blocks to avoid duplication.

Example

    backlog edit 0002 --set status=done --set closed=2025-08-29 --write

### backup

Usage

    backlog backup [--file <path>]
    backlog backup --prune [--keep N] [--older-than DAYS] [--yes]

Description
- Create a timestamped backup of the backlog file.
- `--prune` removes old backups (use `--dry-run` to preview, `--yes` to confirm destructive prune).

### undo

Usage

    backlog undo [--file <path>] [--list] [--choose] [--backup <path>]

Description
- Restore a previous backup. With `--list` prints available backups. With `--choose` interactively select one to restore.

### check-ids

Usage

    backlog check-ids [--file <path>]

Description
- Reports duplicate task ids and collisions between epic and task ids.

### fix-format

Usage

    backlog fix-format [--file <path>] [--write] [--ids-only]

Description
- Normalizes some status tokens, reassigns duplicate task ids, and can optionally rewrite the file into a canonical layout.
- By default the command only reports planned changes. Use `--write` to apply them.
- `--ids-only` (when used with `--write`) performs targeted textual replacements of `Epic <old>` and `Task <old>` ids only, preserving author formatting and raw content. This is the safe option when you only want to update numeric ids without canonicalizing the entire file.

Example

    # Safely update duplicate ids only
    backlog fix-format --file backlog.md --write --ids-only

    # Apply full canonical reserialization
    backlog fix-format --write

### list

Usage

    backlog list [--file <path>] [--state open|finished|all] [--only epics|tasks|all] [--ids-only] [--color|--no-color]

Description
- Print epics and/or tasks. `--ids-only` prints numeric ids one per line (useful for scripting).

### show

Usage

    backlog show <id> [<id> ...] [--file <path>] [--color|--no-color]

Description
- Show detailed information for one or more epic/task ids.

### update

Usage

    backlog update [--file <path>]

Description
- Legacy helper: validate and move finished epics from `1. Epics - open` into `2. Epics - finished` when all contained tasks are in a terminal state.

### init

Usage

    backlog init [--file <path>]

Description
- Create a new `backlog.md` from the bundled template if missing.

## Behavior notes and implementation details

- Numeric ids are considered the canonical id form and are zero-padded by the CLI/library to 4 digits for display and comparison.
- Backups are created automatically before any write. Backups live in a `.backups/` subdirectory next to the file being modified.
- `--ids-only` mode for `fix-format` performs safe, minimal textual id replacements; use it when the goal is only to update ids.
- The parser preserves unknown/extra lines in `raw_lines` so the writer can round-trip non-modeled content where possible. However, full canonicalization (no `--ids-only`) may reformat modeled fields to a canonical layout.

## Examples

Preview a new task without writing:

    backlog add-task --title "Add telemetry" --epic 0001

Add the task and persist, letting the tool choose an id:

    backlog add-task --title "Add telemetry" --epic 0001 --write

Force a specific (numeric) id for a new epic:

    backlog add-epic --title "Urgent ops" --id 3000 --write

Safely update ids only (recommended when you don't want formatting changes):

    backlog fix-format --file backlog.md --write --ids-only

List all ids for automation:

    backlog list --state all --only all --ids-only

Show details for a task or epic:

    backlog show 0001

## Contributing / Tests

- New behavior must be covered with unit tests under `tests/` and a successful `pytest` run.
- The parser and CLI modules are intentionally small and easy to test against small sample backlog snippets.

## Where to change behaviour

- Parser and writer: `src/scripts/backlog_tool/parser.py`
- CLI entrypoint: `src/scripts/backlog.py`

If you'd like, I can:
- Add a short README section with these examples, or
- Add CLI integration tests that run a subprocess and validate exit codes and outputs.
