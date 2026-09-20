# Session Archive

The **Session Archive** panel: the conversations the archive sweep put away, and the way back. Web-only plugin, no
MCP tools.

The archive itself belongs to the app — `agent_system/services/session_archive.py` writes it, the app's sweep
schedules it, and `SessionManager` is what it deletes and restores through. This plugin is only its face. The German
documentation of the whole thing is `docs/session_archive.md`.

## What it is for

`data/sessions/` grows without a bound: one book run leaves a root session and several hundred sub-agent sessions
behind, and nothing ever took them away (measured 20.09.2026: 60.196 files, 5,1 GB, 59.960 of them older than a
month). The sweep moves whole conversation trees into one zip each, and this panel is where a tree comes back from.

## Requirements

`session_archive.enabled: true` in `config/config.yaml` runs the periodic sweep. The panel works either way: with the
sweep off, *Archive now* is still there.

Every endpoint answers about the **requesting** user's archive. There is no parameter for whose archive to read.

## The panel

Opened from the panel launcher (category *session*) or at `/plugins/session_archive/`.

- **Stats**: archived conversations, sessions in them, bytes on disk, and after how many days a conversation moves.
- **Table**: title and id, agent, number of sessions, size, when it was last used, when it was archived.
- **Restore** (←): puts the whole tree back into the live store, timestamps untouched. It is listed in the sidebar
  again afterwards and can be continued.
- **Delete** (🗑, asks first): deletes the archive for good. There is no copy after this.
- **Archive now**: runs the sweep for your own sessions, without waiting for the daily one.

## API

| Method | Path | Answers |
|---|---|---|
| `GET` | `/plugins/session_archive/archived` | `{user_id, retention_days, archived: [...]}` |
| `POST` | `/plugins/session_archive/archived/{root_session_id}/restore` | `{session_id, restored, title}` — 409 when a session of the tree is live again |
| `DELETE` | `/plugins/session_archive/archived/{root_session_id}` | `{session_id, sessions}` — 404 when it is not archived |
| `POST` | `/plugins/session_archive/sweep?dry_run=` | the archive report for this user |

503 means the app has no archive service — a half-started app, or a CLI-only process.

## The same thing on the command line

```bash
agent-cli run --session-user admin --list-archived
agent-cli run --session-user admin --archive-sessions --dry-run
agent-cli run --session-user admin --restore-session <root_id>
```

Both surfaces call the same service. What a CLI process cannot see is the API process's running jobs, so its busy
check is the session presence lock — the one guard that works across processes.

## Tests

```bash
pytest src/plugins/session_archive/tests -q     # this plugin's API
pytest tests/session/test_session_archive.py -q # the service underneath
```
