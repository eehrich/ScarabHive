# Backlog CLI (backlog)

A short reference for the project's minimal backlog CLI. The console entrypoint is the `backlog` script (module `scripts.backlog`). This document lists commands, flags, and examples for common wor### edit

Usage

    backlog edit <id> [<id> ...] --set key=value [--set key=value ...] [--file <path>] [--interactive] [--write]

Description
- Update fields on one or more epics/tasks in a single command. All ids receive the same set of key=value updates.
- Performs a single write + backup when `--write` is provided (atomic across all ids).
- With `--interactive`, if no `--set` options are provided, prompts the user to enter fields and values interactively.

Supported keys for tasks: title, status, added, closed, notes, description
Supported keys for epics: title, status, added, closed, notes, descriptionQuick Start

Notes handling
- By default, `--set notes="..."` will append the provided lines to existing `notes` for the target epic/task.
- To replace existing notes instead of appending, use `--replace-notes`.

### First Time Setup
```bash
# Create a new backlog file
backlog init

# Validate the new backlog
backlog validate

# Add your first epic
backlog add-epic --title "Project Setup" --write

# Add your first epic with a description (multiline supported using \n)
backlog add-epic --title "Project Setup" --description "High-level goals for the project\nScope and milestones" --write

# Add your first task
backlog add-task --title "Set up development environment" --epic 0001 --write
```

### Bulk Operations Quick Start
```bash
# Create a CSV file with multiple tasks
echo "title,epic,notes
Setup CI/CD pipeline,0001,Automate deployment
Write documentation,0001,User and API docs
Add unit tests,0001,Increase code coverage" > tasks.csv

# Bulk add all tasks at once
backlog add-task --from-file tasks.csv --write
```

### Common Workflows

#### Adding Tasks
```bash
# Preview a new task (dry-run)
backlog add-task --title "Implement user authentication" --epic 0001

# Add the task for real
backlog add-task --title "Implement user authentication" --epic 0001 --write

# Add task with notes and custom ID
backlog add-task --title "Fix login bug" --epic 0001 --id 0123 --notes "Issue reported by user\nNeed to test edge cases" --write

# Add task with a multiline description (use literal \n for line breaks)
backlog add-task --title "Design auth API" --epic 0001 --description "Describe endpoints and flows\nInclude error cases and examples" --write

# Dry-run preview showing description (no --write)
backlog add-task --title "Design auth API" --epic 0001 --description "Describe endpoints and flows\nInclude error cases and examples"
```

#### Updating Tasks
```bash
# Mark task as in progress
backlog edit 0123 --set status="in progress" --write

# Mark task as completed
backlog edit 0123 --set status=done --set closed=2025-09-07 --write

# Bulk update multiple tasks
backlog edit 0123 0124 0125 --set status=done --write
```

#### Bulk Operations
```bash
# Bulk add tasks from CSV file
backlog add-task --from-file tasks.csv --write

# Bulk add epics from JSON file
backlog add-epic --from-file epics.json --write

# Bulk move tasks from CSV file
backlog move-task --from-file moves.csv --write

# Preview bulk operations (dry-run)
backlog add-task --from-file tasks.csv
```

#### Progress Indicators

For long-running operations, the CLI displays progress indicators to keep you informed:

```bash
# Bulk operations with 6+ items show progress bars
backlog add-task --from-file large_tasks.csv --write
# Shows: Processing tasks [████████░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░] 40.0% (12/30) ETA 2.1s

# Validation shows progress messages
backlog validate
# Shows: Reading backlog file...
#        Parsing 150 lines...
#        Validating backlog structure...

# Backup pruning shows progress for many files
backlog backup --prune --keep 5 --yes
# Shows: Analyzing backups to prune...
#        Pruning 15 backup files...
```

Progress bars appear automatically for operations with 6+ items. For smaller operations, progress messages are shown instead.

#### Viewing Backlog
```bash
# List all open epics
backlog list

# List all tasks (open epics only)
backlog list --only tasks

# Show detailed info for specific items
backlog show 0001 0123

# List only IDs (useful for scripting)
backlog list --ids-only
```

#### Safety & Recovery
```bash
# Create a backup manually
backlog backup

# List available backups
backlog undo --list

# Restore from latest backup
backlog undo

# Restore from specific backup
backlog undo --backup ".backups/backlog.md.20250907_140000.bak"
```

## Configuration File Support

The backlog tool supports configuration files for setting default values. Create a `.backlogrc` file in your current directory or home directory:

```ini
[backlog]
default_file = backlog.md
default_color = true
backup_dir = /path/to/backups
max_backups = 10
```

### Configuration Options

- `default_file`: Default backlog file path (default: `backlog.md`)
- `default_color`: Enable/disable colored output (`true`, `false`, or `auto`)
- `backup_dir`: Directory for backup files (default: same as backlog file)
- `max_backups`: Maximum number of backups to keep (default: 10)

### Configuration File Locations

The tool looks for `.backlogrc` in this order:
1. Current working directory (`./.backlogrc`)
2. Home directory (`~/.backlogrc`)

Current directory settings take precedence over home directory settings.

### Example Usage

```bash
# Create a project-specific config
echo "[backlog]
default_file = project_backlog.md
default_color = true
backup_dir = ./backups
max_backups = 20" > .backlogrc

# Now commands will use these defaults
backlog list  # Uses project_backlog.md
backlog validate  # Uses project_backlog.md
```

## Safety Features

### Automatic Backups
- **Every write operation** creates a timestamped backup in `.backups/`
- Format: `filename.YYYYMMDD_HHMMSS.bak`
- Use `backlog undo` to restore from backups

### Dry-Run Mode
- **Most commands default to dry-run** - they show what would happen without making changes
- Use `--write` flag to actually persist changes
- Commands with `--write` show ⚠️ warning in help text

### Validation
- Use `backlog validate` before making bulk changes
- Catches duplicate IDs, invalid dates, and formatting issues
- Run validation in CI/CD pipelines

## Best Practices

### Workflow Recommendations
1. **Always validate first**: `backlog validate`
2. **Use dry-run for complex changes**: Run command without `--write` first
3. **Check backups**: `backlog undo --list` to see available backups
4. **Use descriptive commit messages** when updating backlog.md

### Common Patterns
```bash
# Safe workflow for bulk updates
backlog validate
backlog edit 0001 0002 0003 --set status=done  # dry-run first
backlog edit 0001 0002 0003 --set status=done --write  # then apply
backlog validate  # verify changes

# Bulk operations workflow
backlog validate
backlog add-task --from-file new_tasks.csv  # preview bulk changes
backlog add-task --from-file new_tasks.csv --write  # apply bulk changes
backlog validate  # verify bulk changes
```

### Status Values
- `open` - Task/epic is ready to work on
- `in progress` - Currently being worked on
- `done` - Completed successfully
- `failed` - Could not be completed
- `cancelled` - No longer needed
- `reverted` - Change was undone

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


## Command Shortcuts

For power users, the CLI supports single-letter shortcuts for common commands:

- `a` → `add-task` - Add a new task
- `e` → `edit` - Edit existing epics/tasks
- `l` → `list` - List epics and tasks
- `s` → `show` - Show details of specific items

Examples:
```bash
# These are equivalent:
backlog add-task --title "New feature" --epic 0001 --write
backlog a --title "New feature" --epic 0001 --write

# List all open epics
backlog list --state open
backlog l --state open

# Show details of epic 0001
backlog show 0001
backlog s 0001
```

### validate

Usage

    backlog validate [--file <path>]

Description
- Performs validation checks on `backlog.md` (IDs uniqueness, date formats, known status tokens).
- Returns non-zero on validation errors and prints details.

### add-task

Usage

    backlog add-task --title <title> [--epic <epic-id>] [--id <id>] [--notes <text>] [--file <path>] [--write]
    backlog add-task --from-file <csv-or-json-file> [--file <path>] [--write]

Description
- Creates a new task under an epic.
- **Single task mode**: Use `--title`, `--epic`, etc. for individual tasks
- **Bulk mode**: Use `--from-file` to add multiple tasks from a CSV or JSON file
- Dry-run by default (prints what would be inserted).
- When `--write` is provided the CLI will insert the task(s) into the backlog and write the updated file (with backup).
- `--epic` must be provided when using `--write` in single mode.
- `--id` forces a specific numeric id (error if the id exists already).
- `--notes` accepts a string; use `\n` to include literal newlines in the CLI (the code converts `\n` into real newlines for modeled fields).

#### File Formats for Bulk Operations

**CSV Format:**
```csv
title,epic,notes,id
"Implement user authentication","0001","Issue reported by user\nNeed to test edge cases","1001"
"Fix login bug","0002","High priority","1002"
```

**JSON Format:**
```json
[
  {
    "title": "Implement user authentication",
    "epic": "0001",
    "notes": "Issue reported by user\nNeed to test edge cases",
    "id": "1001"
  },
  {
    "title": "Fix login bug",
    "epic": "0002",
    "notes": "High priority",
    "id": "1002"
  }
]
```

Examples

    # Single task
    backlog add-task --title "Fix login" --epic 0001 --notes "Investigate\nAdd tests" --write

    # Single task with forced id
    backlog add-task --title "Hotfix" --epic 0001 --id 1234 --write

    # Bulk tasks from CSV
    backlog add-task --from-file tasks.csv --write

    # Bulk tasks from JSON
    backlog add-task --from-file tasks.json --write

    # Preview bulk operations (dry-run)
    backlog add-task --from-file tasks.csv

### add-epic

Usage

    backlog add-epic --title <title> [--id <id>] [--file <path>] [--write]
    backlog add-epic --from-file <csv-or-json-file> [--file <path>] [--write]

Description
- Create a new epic and append it to the `## 1. Epics - open` section.
- **Single epic mode**: Use `--title`, `--id`, etc. for individual epics
- **Bulk mode**: Use `--from-file` to add multiple epics from a CSV or JSON file
- `--id` forces a numeric id (errors on collision); otherwise the tool picks the next unused numeric id.
- Dry-run unless `--write` is specified.

#### File Formats for Bulk Epic Operations

**CSV Format:**
```csv
title,id,notes
"User Authentication Module","1001","Core security feature"
"API Integration","1002","Third-party service integration"
```

**JSON Format:**
```json
[
  {
    "title": "User Authentication Module",
    "id": "1001",
    "notes": "Core security feature"
  },
  {
    "title": "API Integration",
    "id": "1002",
    "notes": "Third-party service integration"
  }
]
```

Examples

    # Single epic
    backlog add-epic --title "New Integration" --write
    backlog add-epic --title "Urgent" --id 2000 --write

    # Bulk epics from CSV
    backlog add-epic --from-file epics.csv --write

    # Bulk epics from JSON
    backlog add-epic --from-file epics.json --write

    # Preview bulk operations (dry-run)
    backlog add-epic --from-file epics.csv

### move-task

Usage

    backlog move-task --task <task-id> --to-epic <epic-id> [--file <path>] [--write]
    backlog move-task --from-file <csv-or-json-file> [--file <path>] [--write]

Description
- Move a task from one epic to another.
- **Single task mode**: Use `--task` and `--to-epic` for individual moves
- **Bulk mode**: Use `--from-file` to move multiple tasks from a CSV or JSON file
- Dry-run by default; with `--write` persists the change.
- If the destination epic already contains a task with the same id, the CLI will generate a new unique id for the moved task.

#### File Formats for Bulk Move Operations

**CSV Format:**
```csv
task,to_epic
"0001","0002"
"0003","0004"
```

**JSON Format:**
```json
[
  {
    "task": "0001",
    "to_epic": "0002"
  },
  {
    "task": "0003",
    "to_epic": "0004"
  }
]
```

Examples

    # Single task move
    backlog move-task --task 0001 --to-epic 0002 --write

    # Bulk moves from CSV
    backlog move-task --from-file moves.csv --write

    # Bulk moves from JSON
    backlog move-task --from-file moves.json --write

    # Preview bulk moves (dry-run)
    backlog move-task --from-file moves.csv

### edit

Usage

    backlog edit <id> [<id> ...] --set key=value [--set key=value ...] [--file <path>] [--write]

Description
- Update one or more epics/tasks in a single command. All ids receive the same set of key=value changes.
- Performs a single write + backup when `--write` is provided (atomic across all ids).

Supported keys for tasks: `title`, `status`, `added`, `closed`, `notes`, `description`.
Supported keys for epics: `title`, `status`, `added`, `closed`, `notes`, `description`.

Notes
- CLI `--set` values may contain literal `\n` sequences which are translated into real newlines for modeled fields like `notes` and `description`.
- For epics, editing modeled fields removes corresponding raw blocks to avoid duplication.
- Missing ids are reported as errors (non-zero exit); successfully updated ids are still applied unless validation failed earlier.

Examples

    # Update single task status and closed date
    backlog edit 0002 --set status=done --set closed=2025-08-29 --write

    # Bulk update multiple tasks to in progress
    backlog edit 0100 0101 0102 --set status="in progress" --write

    # Dry-run a bulk title change (no --write)
    backlog edit 0200 0201 --set title="Refined title"

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

### Bulk Operations Examples

Create a CSV file with multiple tasks:

```csv
title,epic,notes,id
"Implement user authentication","0001","Issue reported by user\nNeed to test edge cases","1001"
"Fix login bug","0002","High priority","1002"
"Add unit tests","0001","Coverage for auth module","1003"
```

Preview bulk task addition:

    backlog add-task --from-file tasks.csv

Apply the bulk changes:

    backlog add-task --from-file tasks.csv --write

Create a JSON file for bulk epic creation:

```json
[
  {
    "title": "User Authentication Module",
    "id": "2001",
    "notes": "Core security feature"
  },
  {
    "title": "API Integration",
    "id": "2002",
    "notes": "Third-party service integration"
  }
]
```

Add multiple epics at once:

    backlog add-epic --from-file epics.json --write

Bulk move tasks between epics:

```csv
task,to_epic
"1001","2001"
"1002","2002"
```

    backlog move-task --from-file moves.csv --write

Safely update ids only (recommended when you don't want formatting changes):

    backlog fix-format --file backlog.md --write --ids-only

List all ids for automation:

    backlog list --state all --only all --ids-only

Show details for a task or epic:

    backlog show 0001

## Troubleshooting

### Common Issues

**"ERROR: backlog file not found"**
```bash
# Create a new backlog file
backlog init

# Or specify a different file
backlog validate --file path/to/my-backlog.md
```

**"ERROR: no id provided"**
```bash
# Use correct syntax for edit command
backlog edit 0001 --set status=done --write
```

**"Duplicate IDs found"**
```bash
# Check for duplicates
backlog check-ids

# Auto-fix duplicate IDs (safe option)
backlog fix-format --ids-only --write
```

**"ERROR: File not found"**
```bash
# Check if the bulk file exists
ls -la tasks.csv

# Use absolute path if needed
backlog add-task --from-file /full/path/to/tasks.csv --write
```

**"ERROR: Invalid CSV file"**
```bash
# Check CSV format - must have headers: title,epic,notes,id
head -1 tasks.csv

# Validate CSV structure
python -c "import csv; print(list(csv.DictReader(open('tasks.csv'))))"
```

**"ERROR: Missing required fields"**
```bash
# For tasks, CSV must have 'title' and 'epic' columns
# For epics, CSV must have 'title' column
# Check your CSV headers match expected format
```

**"ERROR: Epic 'XXXX' not found"**
```bash
# Verify the epic ID exists in your backlog
backlog list --only epics

# Check for typos in epic IDs in your bulk file
```

**Accidentally made changes**
```bash
# See available backups
backlog undo --list

# Restore latest backup
backlog undo
```

### Getting Help
```bash
# Main help
backlog --help

# Command-specific help
backlog add-task --help
backlog edit --help
```
