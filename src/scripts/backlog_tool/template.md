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

## 2. Epics - finished


## 3. Ideas


## 4. EOF

Note: This `backlog.md` must be updated after each interactive turn to reflect progress and new tasks.

---
EOF




