# Backlog

## Documentation and Guidelines

DO NOT MODIFY THIS FILE DIRECTLY. USE backlog TOOL AS INTERFACE. see docs/backlog_tool.md

The reason is to keep the syntax korrekt and check ids and validity.

### Legend

Legend: ✅ = done, ☐ = open, ❌ = failed, ⏳ = in progress/started/partially finished

### Notes

Note:
  - Each task includes `added` and `closed` dates where available. If `closed` is empty (—) the task is still open.
  - Maintain this file after each turn, update status
  - add new tasks and epics by your posposals to Epics - open
  - move finished Epics from 1. Epics - open to 2. Epics - closed, after user appoved (need to ask)
  - completely Proposals and new Idea add to 3. Ideas
  - Policy: Update `backlog.md` only for project-relevant changes (code, tests, docs, configuration). The assistant will not modify this file for routine conversational activity and will add an explicit backlog entry only when it makes or records a project change.
  - Status semantics guidance: use `reverted` or `rejected` when a change was declined or the team decided "do not want this change"; use `cancelled` (or `aborted`) when work was stopped because a new concept or direction superseded it. These statuses are canonical and will be recognized by the backlog tooling.

### Structure/Format

 Epic/Task Structure (must follow):

 if a field is mandatory and nothign to add, add "-"

- ☐ Task/Epic <uniqid 4 digits>: <title>
  - status: <status> (mandatory)
  - description: (optional)
    <multilinetext with detailed description>
  - added: <datetime> (mandatory)
  - closed: <datetime> (mandatory)
  - notes: (optional)
    <multiline text>

Additionally Epics can have tasks.
  - tasks: (mandatory)
    - <same task structure here>

### Sections

- Epics - open: contains all open Epics which are not started or in work
- Epics - finsihed: contains all Epics where all tasks are closed (done or aborted)
- Ideas: list of ideas for new tasks which are not listed in "open" yet.

## 1. Epics - open

- ☐ Epic 9001: Migrate Epic 0001: Configuration / Managed-file model
  - status: open
    - ☐ Task 900100: Scan git history for backlog.md changes (validate original Epic 0001 entries)
      - status: open
      - added: 2025-08-29
    - ☐ Task 900101: Create canonical snippet file for Epic 0001 entries
      - status: open
      - added: 2025-08-29
  - tasks:

- ☐ Epic 9002: Migrate Epic 0002: CLI UX & features
  - status: open
    - ☐ Task 900200: Scan git history for Epic 0002 changes
      - status: open
      - added: 2025-08-29
    - ☐ Task 900201: Verify and reconcile Epic 0002 task contents
      - status: open
      - added: 2025-08-29
    - ☐ Task 900202: Replay/create missing tasks for Epic 0002 using forced IDs
      - status: open
      - added: 2025-08-29
  - tasks:

- ☐ Epic 9003: Migrate Epic 0003: Tests & CI
  - status: open
    - ☐ Task 900300: Scan git history for Epic 0003 changes
      - status: open
      - added: 2025-08-29
    - ☐ Task 900301: Recreate test-related backlog entries with original ids
      - status: open
      - added: 2025-08-29
  - tasks:

- ☐ Epic 9004: Migrate Epic 0004: Plugins discovery & metadata
  - status: open
    - ☐ Task 900400: Scan git history for Epic 0004 changes
      - status: open
      - added: 2025-08-29
    - ☐ Task 900401: Verify plugin migration tasks and replay missing ones
      - status: open
      - added: 2025-08-29
  - tasks:

- ☐ Epic 9000: Migration orchestration
  - status: open
    - ☐ Task 900000: Create full git history export for backlog.md
      - status: open
      - added: 2025-08-29
    - ☐ Task 900001: Create backup of current backlog.md and backlog_migration.md
      - status: open
      - added: 2025-08-29
  - tasks:

- ☐ Epic 9005: Migrate Epic 0005: Packaging / deps
  - status: open
    - ☐ Task 900500: Scan git history for Epic 0005 changes
      - status: open
      - added: 2025-08-29
    - ☐ Task 900501: Verify and reconcile Epic 0005 task contents
      - status: open
      - added: 2025-08-29
    - ☐ Task 900502: Replay/create missing tasks for Epic 0005 using forced IDs
      - status: open
      - added: 2025-08-29
  - tasks:

- ☐ Epic 9006: Migrate Epic 0006: Safety & robustness
  - status: open
    - ☐ Task 900600: Scan git history for Epic 0006 changes
      - status: open
      - added: 2025-08-29
    - ☐ Task 900601: Verify and reconcile Epic 0006 task contents
      - status: open
      - added: 2025-08-29
    - ☐ Task 900602: Replay/create missing tasks for Epic 0006 using forced IDs
      - status: open
      - added: 2025-08-29
  - tasks:

- ☐ Epic 9007: Migrate Epic 0007: Docs / automation
  - status: open
    - ☐ Task 900700: Scan git history for Epic 0007 changes
      - status: open
      - added: 2025-08-29
    - ☐ Task 900701: Verify and reconcile Epic 0007 task contents
      - status: open
      - added: 2025-08-29
    - ☐ Task 900702: Replay/create missing tasks for Epic 0007 using forced IDs
      - status: open
      - added: 2025-08-29
  - tasks:

- ☐ Epic 9008: Migrate Epic 0010: README, examples & onboarding
  - status: open
    - ☐ Task 900800: Scan git history for Epic 0010 changes
      - status: open
      - added: 2025-08-29
    - ☐ Task 900801: Verify and reconcile Epic 0010 task contents
      - status: open
      - added: 2025-08-29
    - ☐ Task 900802: Replay/create missing tasks for Epic 0010 using forced IDs
      - status: open
      - added: 2025-08-29
  - tasks:

- ☐ Epic 9009: Migrate Epic 0011: Concurrency, locking & resilience
  - status: open
    - ☐ Task 900900: Scan git history for Epic 0011 changes
      - status: open
      - added: 2025-08-29
    - ☐ Task 900901: Verify and reconcile Epic 0011 task contents
      - status: open
      - added: 2025-08-29
    - ☐ Task 900902: Replay/create missing tasks for Epic 0011 using forced IDs
      - status: open
      - added: 2025-08-29
  - tasks:

- ☐ Epic 9010: Migrate Epic 0012: Web UI / Dashboard for plugin management
  - status: open
    - ☐ Task 901000: Scan git history for Epic 0012 changes
      - status: open
      - added: 2025-08-29
    - ☐ Task 901001: Verify and reconcile Epic 0012 task contents
      - status: open
      - added: 2025-08-29
    - ☐ Task 901002: Replay/create missing tasks for Epic 0012 using forced IDs
      - status: open
      - added: 2025-08-29
  - tasks:

- ☐ Epic 9011: Migrate Epic 0013: Convert MCP servers to plugins
  - status: open
    - ☐ Task 901100: Scan git history for Epic 0013 changes
      - status: open
      - added: 2025-08-29
    - ☐ Task 901101: Verify and reconcile Epic 0013 task contents
      - status: open
      - added: 2025-08-29
    - ☐ Task 901102: Replay/create missing tasks for Epic 0013 using forced IDs
      - status: open
      - added: 2025-08-29
  - tasks:

- ☐ Epic 9012: Migrate Epic 0009: Tooling: Ruff & Mypy updates
  - status: open
    - ☐ Task 901200: Scan git history for Epic 0009 changes
      - status: open
      - added: 2025-08-29
    - ☐ Task 901201: Verify and reconcile Epic 0009 task contents
      - status: open
      - added: 2025-08-29
    - ☐ Task 901202: Replay/create missing tasks for Epic 0009 using forced IDs
      - status: open
      - added: 2025-08-29
  - tasks:

- ☐ Epic 9013: Migrate Epic 0014: Per-agent configuration and per-agent plugin control
  - status: open
    - ☐ Task 901300: Scan git history for Epic 0014 changes
      - status: open
      - added: 2025-08-29
    - ☐ Task 901301: Verify and reconcile Epic 0014 task contents
      - status: open
      - added: 2025-08-29
    - ☐ Task 901302: Replay/create missing tasks for Epic 0014 using forced IDs
      - status: open
      - added: 2025-08-29
  - tasks:

- ☐ Epic 9014: Migrate Epic 0015: MCP protocol compatibility and adapters
  - status: open
    - ☐ Task 901400: Scan git history for Epic 0015 changes
      - status: open
      - added: 2025-08-29
    - ☐ Task 901401: Verify and reconcile Epic 0015 task contents
      - status: open
      - added: 2025-08-29
    - ☐ Task 901402: Replay/create missing tasks for Epic 0015 using forced IDs
      - status: open
      - added: 2025-08-29
  - tasks:

- ☐ Epic 9015: Migrate Epic 0016: Implement 'Tavily' websearch MCP plugin
  - status: open
    - ☐ Task 901500: Scan git history for Epic 0016 changes
      - status: open
      - added: 2025-08-29
    - ☐ Task 901501: Verify and reconcile Epic 0016 task contents
      - status: open
      - added: 2025-08-29
    - ☐ Task 901502: Replay/create missing tasks for Epic 0016 using forced IDs
      - status: open
      - added: 2025-08-29
  - tasks:

- ☐ Epic 9016: Migrate Epic 0017: MCP server status & progress interface
  - status: open
    - ☐ Task 901600: Scan git history for Epic 0017 changes
      - status: open
      - added: 2025-08-29
    - ☐ Task 901601: Verify and reconcile Epic 0017 task contents
      - status: open
      - added: 2025-08-29
    - ☐ Task 901602: Replay/create missing tasks for Epic 0017 using forced IDs
      - status: open
      - added: 2025-08-29
  - tasks:

- ☐ Epic 9017: Migrate Epic 0018: Backlog maintenance tool
  - status: open
    - ☐ Task 901700: Scan git history for Epic 0018 changes
      - status: open
      - added: 2025-08-29
    - ☐ Task 901701: Verify and reconcile Epic 0018 task contents
      - status: open
      - added: 2025-08-29
    - ☐ Task 901702: Replay/create missing tasks for Epic 0018 using forced IDs
      - status: open
      - added: 2025-08-29
  - tasks:

- ☐ Epic 9018: Migrate Epic 0008: Backlog (finished)
  - status: open
    - ☐ Task 901800: Scan git history for Epic 0008 changes
      - status: open
      - added: 2025-08-29
    - ☐ Task 901801: Verify and reconcile Epic 0008 task contents
      - status: open
      - added: 2025-08-29
    - ☐ Task 901802: Replay/create missing tasks for Epic 0008 using forced IDs
      - status: open
      - added: 2025-08-29
  - tasks:

- ☐ Epic 9019: Migrate Epic 0000: Core fixes
  - status: open
    - ☐ Task 901900: Scan git history for Epic 0000 changes
      - status: open
      - added: 2025-08-29
    - ☐ Task 901901: Verify and reconcile Epic 0000 task contents
      - status: open
      - added: 2025-08-29
  - tasks:
    - ☐ Task 901902: Replay/create missing tasks for Epic 0000 using forced IDs
      - status: open
      - added: 2025-08-29
      - notes:
        - Recreate missing tasks with original ids after verification.

## 2. Epics - finished

## 3. Ideas

## 4. EOF

Note: This `backlog.md` must be updated after each interactive turn to reflect progress and new tasks.

---
EOF