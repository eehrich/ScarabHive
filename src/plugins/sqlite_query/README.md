# SQLite query

Gives an agent raw SQL on one SQLite database file -- to inspect it, and to repair it: every statement the model
writes is executed and committed, with no dry run and no backup. The plugin guards the boundary instead: a
statement cannot reach any other file (ATTACH, VACUUM INTO and the directory pragmas are refused), stops after
`query_timeout` or when the run is stopped, and returns at most `max_rows` rows.

- **Tool** `sqlite_query_execute_sql` -- one statement; rows with their columns, or `rows_affected` and
  `last_row_id`. A misspelt table or column answers with the real names and a `did_you_mean`.
- **Command** `tool-sqlite-query --database <file> --sql "<sql>"` runs the same tool without the API.

Off by default. Enable it in `config/plugins.yaml` (`sqlite_query: {type: sqlite_query, enabled: true, database:
<file>}`) and allow `+sqlite_query/*` in an agent's tool list -- only for an agent that may change that data.

The full manual -- the answers and errors, what is refused and why, time and size limits, what the model sees,
and the server settings -- is the plugin's guide, `sqlite_query.guide`, in the Help panel.
