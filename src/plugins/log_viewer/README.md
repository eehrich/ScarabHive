# Log Viewer

Reads the application's log files. The **Logs** panel shows the newest entries of a file and its rotations, with
tracebacks folded under their line, filters by level and search, and follows the file while it grows; agents get
three tools to list, tail and search the files. Only the files named in the configuration are ever opened, by the
panel and by the tools alike.

- **Tools** `log_viewer_list`, `log_viewer_tail` and `log_viewer_search` -- the configured files, the newest lines of
  one, and lines matching a regular expression.
- **Panel** Logs -- entries with time and level, level buttons, search, count, Follow and automatic reload.

Enable it in `config/plugins.yaml` (`log_viewer: {type: log_viewer, enabled: true, log_files: [...]}`) and allow
`+log_viewer/*` in an agent's tool list.

The full manual -- the panel, every parameter and answer, the line formats and the server settings -- is the
plugin's guide, `log_viewer.guide`, in the Help panel.
