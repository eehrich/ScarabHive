# SQLite Query

Raw SQL against a configured SQLite file, for debugging. One tool, no
allowlist, no statement classification, no backup: whatever the model writes
is executed.

## What it provides

| Tool | Purpose |
|---|---|
| `sqlite_query_execute_sql` | Execute any valid SQLite statement |

`type = ["mcp-server"]`, `dependencies = ["mcp>=1.14.1"]`.

## Safety model: there is none

This is deliberate — the plugin exists to inspect and repair a database while
debugging, and a read-only variant could not do the second half. The
consequences follow from that, so treat them as the contract:

* `UPDATE`/`DELETE` without a `WHERE` rewrites the table. There is no dry-run.
* Writes are committed immediately (`conn.commit()` on any statement without a
  result set).
* The database is whatever `database:` in `plugins.yaml` points at — one file
  per plugin instance, so the blast radius is that file.

It is therefore **commented out in `config/plugins.yaml`** and switched on for
a specific investigation. Give it to an agent only when you want that agent to
be able to change the data.

## Configuration

```yaml
sqlite_query:
  type: sqlite_query
  enabled: true
  database: "data/writer/books.db"
  query_timeout: 30
```

`query_timeout` is SQLite's lock timeout, not a statement deadline — a slow
`SELECT` is not cancelled by it. Execution runs in a thread
(`asyncio.to_thread`), so a long query does not block the event loop.

## Schema recovery on name misses

`no such table: X` and `no such column: Y` are answered with the follow-up
query the agent would have typed next: the table list (or the columns of the
tables the failed SQL actually names), plus a `did_you_mean` suggestion from
`agent_system.utils.suggest`. Bounded at 25 tables / 30 columns per table — a
re-orientation, never a schema dump.

Other errors (syntax, constraint violations) return the plain message. They
have no mechanical recovery, and inventing one would mislead.

## CLI

```bash
mcp-sqlite-query --database data/writer/books.db \
    --sql "SELECT status, COUNT(*) FROM books GROUP BY status" --json
```

Registered as a console script in `pyproject.toml`. It builds the server with
mocked configs, so it needs no running API — handy for the same queries
`.claude/skills/writer/references/messen.md` documents.

## Tests

`tests/test_plugin_sqlite_query.py`.

## License

Apache-2.0 — see `LICENSE`.
